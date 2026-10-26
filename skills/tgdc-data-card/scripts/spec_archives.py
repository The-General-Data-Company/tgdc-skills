"""Reads compressed archives for the data format specification.

Members are read once: hashed in a stream and, up to a size bound, parsed from
memory. Nothing is executed. Member paths that are absolute or contain '..' are
rejected. A member is a credential when any component of its path is
credential-like by name or extension: it is never parsed, and a tool never
writes it to disk or it is deleted unread right after extraction. Hidden
members are skipped and counted. External extraction tools run with argument
lists (no shell), no input and a timeout, each in its own process group, inside
a private temporary folder that is removed afterwards. The listing of every
archive is read first, so size and ratio limits apply before anything is
extracted, and a watchdog stops the tool when its folder outgrows the allowance.
"""
import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import unicodedata
import zipfile
import zlib
from collections import Counter

import spec_content
from spec_taxonomy import ARCHIVE_EXTENSIONS, CATEGORY_OF_EXTENSION, is_sensitive_path, raw_extension

DEFAULT_LIMITS = {'max_expanded_bytes': 20 * 10 ** 9, 'max_ratio': 200, 'max_depth': 5,
                  'member_parse_bytes': 128 * 1024 * 1024, 'ratio_floor_bytes': 64 * 1024 * 1024}
EXTERNAL_TOOLS = ('7zz', '7z', 'bsdtar', 'unar')
CHAINS = {'7z': ('7zz', '7z', 'bsdtar', 'unar'), 'rar': ('7zz', '7z', 'unar', 'bsdtar'), 'arj': ('7zz', '7z', 'unar'),
          'cab': ('7zz', '7z', 'bsdtar', 'unar'), 'other': ('7zz', '7z', 'unar'), 'zip': ('7zz', '7z', 'bsdtar', 'unar'),
          'split': ('7zz', '7z', 'unar')}
MAGIC = [(b'PK\x03\x04', 'zip'), (b'PK\x05\x06', 'zip'), (b'Rar!\x1a\x07', 'rar'), (b"7z\xbc\xaf'\x1c", '7z'),
         (b'\x1f\x8b', 'gz'), (b'BZh', 'bz2'), (b'\xfd7zXZ\x00', 'xz'), (b'MSCF', 'cab'), (b'\x60\xea', 'arj')]
CHUNK = 1024 * 1024
EXTERNAL_TIMEOUT = 3600
LISTING_TIMEOUT = 600
LISTING_LIMIT = 2 * 10 ** 9           # bytes of listing text before a tool is stopped
MEMBER_MEMORY_BYTES = 16 * 1024 * 1024    # larger members that are still parsed continue in a temporary file
FACTS_CACHE_LIMIT = 20000             # distinct contents whose facts one archive remembers
SAFETY_FREE_BYTES = 256 * 1024 * 1024  # an extraction stops when the disk has less than this left
REASONS = {'password': 'password protected', 'space': 'not enough free disk space', 'timeout': 'timeout'}


class CapReached(Exception):
    pass


class TimeUp(Exception):
    pass


class NoSpace(Exception):
    pass


# ---------------------------------------------------------------------- tool processes
_LIVE = {}      # pid -> Popen of every running tool; each is the leader of its own process group


def spawn(arguments, folder, environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    """Start a tool with no input in a process group of its own, so the whole group can be stopped."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, cwd=folder, env=environment,
                               start_new_session=True)
    _LIVE[process.pid] = process
    return process


def stop_tool(process):
    """Kill the whole process group of a tool that is still running and collect it."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    _LIVE.pop(process.pid, None)


def terminate_tools():
    for process in list(_LIVE.values()):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass


def install_termination_handler():
    """In a worker: on SIGTERM stop every tool process group first, then exit by the signal."""
    def handler(signum, frame):
        terminate_tools()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    signal.signal(signal.SIGTERM, handler)


def lsar_for(unar_path):
    """The lsar listing tool installed next to unar, or None. unar is never run without a listing first."""
    for folder in dict.fromkeys([os.path.dirname(unar_path), os.path.dirname(os.path.realpath(unar_path))]):
        candidate = os.path.join(folder, 'lsar')
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def probe_tools(search_path=None):
    """Available extraction tools: {name: absolute path}. bsdtar is accepted only when it identifies itself, unar only with lsar."""
    found = {}
    for name in EXTERNAL_TOOLS:
        path = shutil.which(name, path=search_path)
        if path and (name != 'unar' or lsar_for(path)):
            found[name] = path
    if 'bsdtar' not in found:
        path = shutil.which('tar', path=search_path)
        if path:
            try:
                banner = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10,
                                        stdin=subprocess.DEVNULL).stdout
            except (OSError, subprocess.SubprocessError):
                banner = ''
            if 'bsdtar' in banner:
                found['bsdtar'] = path
    return found


def magic_kind(head):
    """Archive kind from magic bytes alone, or None."""
    for signature, kind in MAGIC:
        if head.startswith(signature):
            return kind
    if head[257:262] == b'ustar':
        return 'tar'
    return None


def archive_kind(name, head=b''):
    """Archive kind from the extension, or from magic bytes for files whose extension says nothing."""
    lowered = name.lower()
    extension = raw_extension(lowered)
    if extension in ARCHIVE_EXTENSIONS:
        if extension in ('zip', 'zipx'):
            return 'zip'
        if extension == 'tar':
            return 'tar'
        if extension == 'tgz' or (extension == 'gz' and lowered.endswith('.tar.gz')):
            return 'tar.gz'
        if extension == 'tbz2' or (extension == 'bz2' and lowered.endswith('.tar.bz2')):
            return 'tar.bz2'
        if extension == 'txz' or (extension == 'xz' and lowered.endswith('.tar.xz')):
            return 'tar.xz'
        if extension in ('gz', 'bz2', 'xz', '7z', 'rar', 'arj', 'cab'):
            return extension
        return 'other'
    if extension in CATEGORY_OF_EXTENSION:
        return None
    return magic_kind(head)


def may_hold_archive(name, role=None):
    """False for a file that cannot be an archive, so no temporary folder is needed for it."""
    extension = raw_extension(name)
    return role == 'leader' or extension in ARCHIVE_EXTENSIONS or extension not in CATEGORY_OF_EXTENSION


def unsafe_name(name):
    normalized = name.replace('\\', '/')
    return (not normalized or normalized.startswith('/') or re.match(r'^[A-Za-z]:', normalized) is not None
            or '..' in normalized.split('/') or '\x00' in normalized)


def classify_member(name):
    """'cred', 'hidden' or None for a member path. Every component counts, and a credential name wins over a hidden folder."""
    normalized = name.replace('\\', '/')
    parts = [part for part in normalized.split('/') if part not in ('', '.')]
    if is_sensitive_path([part for part in parts if not part.startswith('.')]):
        return 'cred'
    if normalized.startswith('__MACOSX/') or any(part.startswith('.') for part in parts):
        return 'hidden'
    return None


def clean(text):
    return str(text).encode('utf-8', 'replace').decode('utf-8')


def glob_escape(name):
    return re.sub(r'([*?\[\]\\])', r'\\\1', name)


class Listing:
    """What a tool says an archive holds: regular files as (path, size, encrypted), links, and whether the sizes can be trusted."""
    __slots__ = ('files', 'links', 'complete', 'code')

    def __init__(self, files, links, complete=True):
        self.files, self.links, self.complete, self.code = files, links, complete, 0


class Reader:
    def __init__(self, temp_dir, tools=None, limits=None, deadline=None, want_text=True, sink=None, flush=1000):
        self.temp_dir = temp_dir
        self.tools = dict(probe_tools() if tools is None else tools)
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.deadline = deadline
        self.members = []            # kept only when no sink takes the members
        self.member_count = 0
        self.sink = sink
        self.flush = flush
        self.batch = []
        self.opened = 0
        self.partly = 0
        self.lost = 0
        self.failed = Counter()      # '<kind>:<reason>' -> archives that could not be opened
        self.used = Counter()        # '<kind>:<tool>' -> archives opened with that tool
        self.expanded = 0
        self.compressed = 1
        self.cache = {}
        self.timed_out = False
        self.hidden = 0
        self.links = 0
        self.shorts = 0

    # ------------------------------------------------------------------ budgets
    def check_time(self):
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.timed_out = True
            raise TimeUp()

    def charge(self, amount):
        self.expanded += amount
        if self.expanded > self.limits['max_expanded_bytes']:
            raise CapReached('expanded size cap')
        if (self.expanded > self.limits['ratio_floor_bytes']
                and self.expanded / max(1, self.compressed) > self.limits['max_ratio']):
            raise CapReached('expansion ratio cap')

    def allowance(self):
        """Bytes an extraction may still add before the size cap or the expansion ratio cap is reached."""
        ceiling = min(self.limits['max_expanded_bytes'], max(self.limits['ratio_floor_bytes'], self.limits['max_ratio'] * self.compressed))
        return max(0, ceiling - self.expanded)

    # ------------------------------------------------------------------ members
    def emit(self, record):
        self.member_count += 1
        if self.sink is None:
            self.members.append(record)
            return
        self.batch.append(record)
        if len(self.batch) >= self.flush:
            self.flush_members()

    def flush_members(self):
        if self.batch:
            batch, self.batch = self.batch, []
            self.sink(batch)

    def spill(self, prefix_data):
        handle = tempfile.NamedTemporaryFile(dir=self.temp_dir, delete=False)
        handle.write(prefix_data)
        return handle

    def hash_only(self, stream):
        """SHA-256 and size of a stream; the bytes are neither parsed nor kept."""
        digest = hashlib.sha256()
        total = 0
        while True:
            self.check_time()
            block = stream.read(CHUNK)
            if not block:
                return digest.hexdigest(), total
            digest.update(block)
            total += len(block)
            self.charge(len(block))

    def read_member(self, stream, need_file, head_only=False, spill_ok=False):
        """Stream a member once. Returns (sha256, size, bytes or None, spool path or None).

        Text formats keep only their first bytes. Other members stay in memory up to MEMBER_MEMORY_BYTES; a larger member that is
        still parsed or opened continues in a temporary file; one above the parse bound that is not opened is only hashed."""
        digest = hashlib.sha256()
        buffer = bytearray()
        total = 0
        spool = None
        dropped = False
        cap = self.limits['member_parse_bytes']
        memory = min(cap, MEMBER_MEMORY_BYTES)
        while True:
            self.check_time()
            block = stream.read(CHUNK)
            if not block:
                break
            digest.update(block)
            total += len(block)
            self.charge(len(block))
            if spool is not None:
                spool.write(block)
            elif head_only:
                if len(buffer) < spec_content.TEXT_HEAD_BYTES:
                    buffer.extend(block[:spec_content.TEXT_HEAD_BYTES - len(buffer)])
            elif dropped:
                continue
            elif total <= memory:
                buffer.extend(block)
            elif need_file or spill_ok:
                spool = self.spill(bytes(buffer))
                spool.write(block)
                buffer = bytearray()
            else:
                buffer = bytearray()
                dropped = True
        if spool is not None:
            spool.close()
            if total > cap and not need_file:
                os.unlink(spool.name)
                return digest.hexdigest(), total, None, None
            return digest.hexdigest(), total, None, spool.name
        if dropped:
            return digest.hexdigest(), total, None, None
        return digest.hexdigest(), total, bytes(buffer), None

    def add_member(self, name, declared_size, opener, depth, prefix, expect=None):
        self.check_time()
        label = clean(prefix + name)
        extension = raw_extension(name)
        flag = classify_member(name)
        if flag == 'hidden':
            self.hidden += 1
            return
        if flag == 'cred':
            with opener() as stream:
                sha, size = self.hash_only(stream)
            self.emit({'n': label, 'x': extension, 's': size, 'h': sha, 'cred': True})
            return
        likely_nested = extension in ARCHIVE_EXTENSIONS and depth < self.limits['max_depth']
        need_file = likely_nested or extension in ('pdf', 'tif', 'tiff') or extension not in CATEGORY_OF_EXTENSION
        head_only = extension in spec_content.TEXT_HEAD_EXTENSIONS
        spill_ok = extension in spec_content.STREAM_EXTENSIONS or extension == 'eml'
        with opener() as stream:
            sha, size, data, spool = self.read_member(stream, need_file, head_only, spill_ok)
        record = {'n': label, 'x': extension, 's': size, 'h': sha}
        short = expect is not None and size != expect
        if short:
            del record['h']                 # a cut-off member has no content hash: it is never unique and never new
            record['short'] = True
            self.shorts += 1
        try:
            head = data[:600] if data is not None else b''
            if spool and not head:
                with open(spool, 'rb') as handle:
                    head = handle.read(600)
            kind = None if short else archive_kind(name, head)
            if kind:
                record['arch'] = True
                if depth >= self.limits['max_depth']:
                    self.failed[f'{kind}:too deep'] += 1
                    self.partly += 1
                elif data is not None or spool:
                    before = self.opened
                    opened = self.open_nested(kind, name, data, spool, depth + 1, label + '!/')
                    if opened and self.opened > before:
                        record['cont'] = True
            elif (data is not None or spool) and not short:
                cached = self.cache.get(sha)
                if cached is None:
                    source = data if data is not None else open(spool, 'rb')
                    try:
                        cached = spec_content.content_facts(extension, source, text_pages=3, deadline=self.deadline,
                                                            mail_attachments=False)
                    finally:
                        if data is None:
                            source.close()
                    if len(self.cache) < FACTS_CACHE_LIMIT:
                        self.cache[sha] = cached
                record.update(cached)
        except (CapReached, TimeUp):
            raise
        except Exception:
            record['unreadable'] = True
        finally:
            if spool:
                try:
                    os.unlink(spool)
                except OSError:
                    pass
        self.emit(record)

    def lose(self, count=1):
        self.lost += count

    # ------------------------------------------------------------------ dispatch
    def open_nested(self, kind, name, data, spool, depth, prefix):
        if spool:
            source = open(spool, 'rb')
        else:
            source = io.BytesIO(data)
        try:
            return self.open_archive(kind, name, source, depth, prefix, compressed=None)
        finally:
            source.close()

    def open_archive(self, kind, name, stream, depth, prefix='', compressed=None, parts=None):
        """Read one archive from a seekable binary stream. Returns True when its listing could be read."""
        if compressed is not None:
            self.compressed = max(1, compressed)
        if depth > self.limits['max_depth']:
            self.failed[f'{kind}:too deep'] += 1
            return False
        try:
            if kind == 'zip' and not parts:
                result = self.read_zip(stream, depth, prefix)
                if result is not None:
                    return result
                return self.read_external(kind, name, stream, depth, prefix, parts)
            if kind in ('tar', 'tar.gz', 'tar.bz2', 'tar.xz'):
                return self.read_tar(kind, stream, depth, prefix)
            if kind in ('gz', 'bz2', 'xz'):
                return self.read_single(kind, name, stream, depth, prefix)
            return self.read_external(kind, name, stream, depth, prefix, parts)
        except TimeUp:
            self.partly += 1
            return True
        except CapReached:
            self.partly += 1
            return True

    # ------------------------------------------------------------------ zip
    def read_zip(self, stream, depth, prefix):
        try:
            archive = zipfile.ZipFile(stream)
            infos = archive.infolist()
        except zipfile.BadZipFile:
            return None
        except Exception:
            return None
        self.opened += 1
        self.used['zip:zipfile'] += 1
        damaged = False
        try:
            for info in infos:
                if info.is_dir():
                    continue
                name = info.filename
                if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
                    self.links += 1
                    continue
                if unsafe_name(name) or info.flag_bits & 1:
                    self.lose(); damaged = True
                    continue
                try:
                    self.add_member(name, info.file_size, lambda info=info: archive.open(info), depth, prefix)
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError, zlib.error):
                    self.lose(); damaged = True
        finally:
            archive.close()
        if damaged:
            self.partly += 1
        return True

    # ------------------------------------------------------------------ tar and single-file compression
    def read_tar(self, kind, stream, depth, prefix):
        mode = {'tar': 'r|', 'tar.gz': 'r|gz', 'tar.bz2': 'r|bz2', 'tar.xz': 'r|xz'}[kind]
        try:
            archive = tarfile.open(fileobj=stream, mode=mode)
            first = archive.next()
        except Exception:
            self.failed[f'{kind}:damaged'] += 1
            return False
        self.opened += 1
        self.used[f'{kind}:tarfile'] += 1
        damaged = False
        try:
            member = first
            while member is not None:
                if member.isreg():
                    if unsafe_name(member.name):
                        self.lose(); damaged = True
                    else:
                        try:
                            self.add_member(member.name, member.size, lambda member=member: archive.extractfile(member), depth, prefix)
                        except (CapReached, TimeUp):
                            raise
                        except (tarfile.TarError, OSError, EOFError):
                            self.lose(); damaged = True
                elif member.issym() or member.islnk():
                    self.links += 1
                try:
                    member = archive.next()
                except (tarfile.TarError, OSError, EOFError):
                    damaged = True
                    break
        finally:
            archive.close()
        if damaged:
            self.partly += 1
        return True

    def read_single(self, kind, name, stream, depth, prefix):
        opener = {'gz': gzip.GzipFile, 'bz2': bz2.BZ2File, 'xz': lzma.LZMAFile}[kind]
        inner = re.sub(r'\.(gz|bz2|xz)$', '', name, flags=re.I) or 'content'
        try:
            probe = opener(fileobj=stream) if kind == 'gz' else opener(stream)
            probe.read(1)
            stream.seek(0)
            handle = opener(fileobj=stream) if kind == 'gz' else opener(stream)
        except Exception:
            self.failed[f'{kind}:damaged'] += 1
            return False
        self.opened += 1
        self.used[f'{kind}:python'] += 1
        try:
            self.add_member(inner, 0, lambda: handle, depth, prefix)
        except (CapReached, TimeUp):
            raise
        except Exception:
            self.lose(); self.partly += 1
        return True

    # ------------------------------------------------------------------ external tools
    def materialize(self, stream, name, folder):
        stream.seek(0, os.SEEK_END)
        if shutil.disk_usage(folder).free < stream.tell() * 2.2 + 1024 * 1024:
            raise NoSpace()
        extension = raw_extension(name)
        target = os.path.join(folder, 'archive' + ('.' + extension if extension.isalnum() and len(extension) <= 8 else ''))
        stream.seek(0)
        with open(target, 'wb') as handle:
            shutil.copyfileobj(stream, handle, CHUNK)
        return target

    def usable(self, tool):
        return tool in self.tools and (tool != 'unar' or lsar_for(self.tools['unar']) is not None)

    def read_external(self, kind, name, stream, depth, prefix, parts):
        chain_key = 'split' if parts else (kind if kind in CHAINS else 'other')
        chain = [tool for tool in CHAINS[chain_key] if self.usable(tool)]
        if not chain:
            self.failed[f'{kind}:no extraction tool'] += 1
            return False
        folder = tempfile.mkdtemp(dir=self.temp_dir)
        try:
            if parts:
                target = None
                for part_name, part_stream in parts:
                    path = os.path.join(folder, os.path.basename(part_name) or 'part')
                    with open(path, 'wb') as handle:
                        shutil.copyfileobj(part_stream, handle, CHUNK)
                    target = target or path
            else:
                target = self.materialize(stream, name, folder)
            for tool in chain:
                outcome = self.run_tool(tool, kind, target, folder, depth, prefix)
                if outcome is True:
                    return True
                if outcome in REASONS:
                    self.failed[f'{kind}:{REASONS[outcome]}'] += 1
                    return False
            self.failed[f'{kind}:damaged'] += 1
            return False
        except NoSpace:
            self.failed[f'{kind}:not enough free disk space'] += 1
            return False
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def wait_for(self, process, arguments, timeout, watch=None, allowance=0, size_file=None):
        """Wait for a tool, checking the deadline, the folder it fills and the disk. Returns its exit code."""
        started = time.monotonic()
        interval = 0.1
        while True:
            try:
                return process.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(arguments, timeout)
            self.check_time()
            scanned = time.monotonic()
            if watch is not None:
                if folder_size(watch) > allowance:
                    raise CapReached('expanded size cap')
                if shutil.disk_usage(watch).free < SAFETY_FREE_BYTES:
                    raise NoSpace()
            if size_file is not None and os.path.getsize(size_file) > LISTING_LIMIT:
                raise CapReached('listing size cap')
            interval = min(1.0, max(0.1, 4 * (time.monotonic() - scanned)))

    def run_guarded(self, arguments, folder, environment, watch, allowance, timeout=EXTERNAL_TIMEOUT):
        """Run an extraction tool with no input, stopping its whole process group when the unpacked folder outgrows the allowance,
        the disk runs low or the time is up. Returns the exit code."""
        process = spawn(arguments, folder, environment)
        try:
            return self.wait_for(process, arguments, timeout, watch=watch, allowance=allowance)
        finally:
            stop_tool(process)

    def run_to_file(self, arguments, folder, environment, path, timeout=LISTING_TIMEOUT):
        """Run a listing command with its output going to a file. Returns (exit code, first bytes of output and errors)."""
        with open(path, 'wb') as out, open(path + '.err', 'wb') as err:
            process = spawn(arguments, folder, environment, stdout=out, stderr=err)
            try:
                code = self.wait_for(process, arguments, timeout, size_file=path)
            finally:
                stop_tool(process)
        text = b''
        for part in (path + '.err', path):
            with open(part, 'rb') as handle:
                text += handle.read(65536)
        return code, text

    def list_archive(self, tool, executable, target, folder, environment):
        """Read what the archive holds before anything is extracted. A Listing, 'password', or False for damaged or unreadable."""
        listing_path = os.path.join(folder, 'listing.txt')
        if tool in ('7zz', '7z'):
            arguments = [executable, 'l', '-slt', '-ba', '-spd', '-bd', target]
        elif tool == 'bsdtar':
            arguments = [executable, '-tvf', target]
        else:
            lsar = lsar_for(executable)
            if lsar is None:
                return False
            arguments = [lsar, '-j', target]
        code, text = self.run_to_file(arguments, folder, environment, listing_path)
        try:
            if tool in ('7zz', '7z'):
                listing = read_seven_listing(listing_path)
            elif tool == 'bsdtar':
                listing = read_tar_listing(listing_path)
            else:
                listing = read_lsar_listing(listing_path)
        finally:
            os.unlink(listing_path)
        # A tool that fails and lists nothing, or lists nothing at all, could not open the archive: header-encrypted or damaged.
        if listing is None or not listing.files:
            lowered = text.lower()
            return 'password' if (b'password' in lowered or b'passphrase' in lowered or b'encrypted' in lowered) else False
        listing.code = code
        return listing

    def plan(self, listing, prefix, record_flagged):
        """Sort the listed files. Returns (wanted {path: size}, unsafe, encrypted, flagged count). Flagged files are credential-named
        or hidden; when the tool never writes them they are recorded here, by name and listed size."""
        wanted, unsafe, encrypted, flagged = {}, 0, 0, 0
        for path, size, is_encrypted in listing.files:
            if unsafe_name(path):
                unsafe += 1
            elif is_encrypted:
                encrypted += 1
            else:
                flag = classify_member(path)
                if flag:
                    flagged += 1
                    if record_flagged:
                        self.record_flagged(flag, path, size, prefix)
                else:
                    wanted[path] = size
        return wanted, unsafe, encrypted, flagged

    def record_flagged(self, flag, relative, size, prefix):
        if flag == 'hidden':
            self.hidden += 1
        else:
            self.emit({'n': clean(prefix + relative), 'x': raw_extension(relative), 's': size, 'cred': True})

    def run_tool(self, tool, kind, target, folder, depth, prefix):
        executable = self.tools[tool]
        destination = os.path.join(folder, 'out')
        os.makedirs(destination, exist_ok=True)
        environment = {'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8', 'HOME': folder}
        problems = 0
        try:
            listing = self.list_archive(tool, executable, target, folder, environment)
            if listing is False or isinstance(listing, str):
                return listing
            if listing.code != 0:
                problems += 1
            self.links += listing.links
            declared = sum(size for _, size, _ in listing.files)
            if listing.complete:
                if declared > self.allowance():
                    raise CapReached('expanded size cap')
                if shutil.disk_usage(folder).free < declared * 1.1 + 1024 * 1024:
                    return 'space'
            seven = tool in ('7zz', '7z')
            wanted, unsafe, encrypted, flagged = self.plan(listing, prefix, record_flagged=seven)
            sizes = {unicodedata.normalize('NFC', path): size for path, size in wanted.items()}
            if seven:
                if wanted:
                    list_file = os.path.join(folder, 'wanted.txt')
                    with open(list_file, 'w', encoding='utf-8') as handle:
                        handle.write('\n'.join(wanted) + '\n')
                    code = self.run_guarded([executable, 'x', '-y', '-bd', '-spd', '-scsUTF-8', '-aoa', '-o' + destination, target, '@' + list_file],
                                            folder, environment, destination, self.allowance())
                    problems += 1 if code != 0 else 0
                expected = len(wanted)
            else:
                if tool == 'bsdtar':
                    arguments = [executable, '-xf', target, '-C', destination, '--no-same-owner', '--no-same-permissions']
                else:
                    arguments = [executable, '-q', '-D', '-o', destination, target]
                code = self.run_guarded(arguments, folder, environment, destination, self.allowance())
                if code != 0 and not any(os.scandir(destination)):
                    return False
                problems += 1 if code != 0 else 0
                expected = len(wanted) + flagged if listing.complete else None
                sizes = sizes if listing.complete else {}
            self.lose(unsafe + encrypted)
            problems += unsafe + encrypted
        except subprocess.TimeoutExpired:
            return 'timeout'
        except NoSpace:
            return 'space'
        except OSError:
            return False
        self.opened += 1
        self.used[f'{kind}:{tool}'] += 1
        found, shorts = self.walk_extracted(destination, depth, prefix, sizes, record_flagged=not seven)
        if expected is not None:
            missing = max(0, expected - found)
            self.lose(missing)
            problems += missing
        problems += shorts
        if problems:
            self.partly += 1
        return True

    def walk_extracted(self, destination, depth, prefix, sizes=None, record_flagged=True):
        """Process every regular file below a private extraction folder. Links are never followed.

        Credential-named and hidden files are deleted unread in a first pass, before any other file is opened. Returns
        (regular files found, members shorter than the listing says)."""
        found = 0
        for path, relative, info in self.regular_files(destination):
            found += 1
            flag = classify_member(relative)
            if flag:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                if record_flagged:
                    self.record_flagged(flag, relative, info.st_size, prefix)
        shorts_before = self.shorts
        for path, relative, info in self.regular_files(destination):
            self.check_time()
            expect = (sizes or {}).get(unicodedata.normalize('NFC', relative))
            self.add_member(relative, info.st_size, lambda path=path: open(path, 'rb'), depth, prefix, expect=expect)
            try:
                os.unlink(path)
            except OSError:
                pass
        return found, self.shorts - shorts_before

    def regular_files(self, destination):
        stack = [destination]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in entries:
                self.check_time()
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    yield entry.path, os.path.relpath(entry.path, destination).replace(os.sep, '/'), info


def folder_size(path):
    """Bytes below a folder, counting regular files only and never following links."""
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------- listings
def _seven_blocks(lines):
    fields = {}
    for line in lines:
        line = line.rstrip('\n')
        if not line.strip():
            if fields:
                yield fields
            fields = {}
        elif ' = ' in line:
            key, value = line.split(' = ', 1)
            fields[key.strip()] = value
        elif line.endswith(' ='):
            fields[line[:-2].strip()] = ''
    if fields:
        yield fields


def read_seven_listing(path):
    """Files of a 7-Zip style technical listing (7zz l -slt -ba)."""
    files, links = [], 0
    with open(path, encoding='utf-8', errors='replace') as handle:
        for fields in _seven_blocks(handle):
            if 'Path' not in fields:
                continue
            attributes = fields.get('Attributes', '')
            parts = attributes.split()
            if fields.get('Folder', '-') == '+' or attributes.startswith('D'):
                continue
            if (parts[-1] if parts else '').startswith('l'):
                links += 1
                continue
            try:
                size = int(fields.get('Size') or 0)
            except ValueError:
                size = 0
            files.append((fields['Path'], size, fields.get('Encrypted', '-') == '+'))
    return Listing(files, links)


_TAR_LINE = re.compile(r'^([-dlhcbps])[rwxsStTl-]{9}[@+.]?\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\w{3}\s+\d{1,2}\s+(?:\d{2}:\d{2}|\d{4})\s(.*)$')


def read_tar_listing(path):
    """Files of a bsdtar verbose listing (bsdtar -tvf). A line that cannot be read makes the sizes untrusted."""
    files, links, complete = [], 0, True
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            match = _TAR_LINE.match(line)
            if not match:
                complete = False
                continue
            kind, size, name = match.groups()
            if kind == '-':
                files.append((name, int(size), False))
            elif kind in ('l', 'h'):
                links += 1
    return Listing(files, links, complete)


def read_lsar_listing(path):
    """Files of an lsar JSON listing (lsar -j). None when the output is not the expected JSON."""
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            entries = json.load(handle)['lsarContents']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    files, links = [], 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('XADFileName'), str):
            return None
        if entry.get('XADIsDirectory'):
            continue
        if entry.get('XADIsLink'):
            links += 1
            continue
        try:
            size = int(entry.get('XADFileSize') or 0)
        except (TypeError, ValueError):
            size = 0
        files.append((entry['XADFileName'], size, bool(entry.get('XADIsEncrypted'))))
    return Listing(files, links)
