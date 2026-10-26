"""Reads compressed archives for the data format specification.

Members are read once: hashed in a stream and, up to a size bound, parsed from
memory. Nothing is executed. Member paths that are absolute or contain '..' are
rejected. Credential-named members are listed by name and size only and never
read. Seven-Zip style tools run with argument lists (no shell), no input and a
timeout, inside a private temporary folder that is removed afterwards.
"""
import bz2
import gzip
import hashlib
import io
import lzma
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
import zlib
from collections import Counter

import spec_content
from spec_taxonomy import ARCHIVE_EXTENSIONS, CATEGORY_OF_EXTENSION, is_credential_name, raw_extension

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
REASONS = {'password': 'password protected', 'space': 'not enough free disk space', 'timeout': 'timeout'}


class CapReached(Exception):
    pass


class TimeUp(Exception):
    pass


class NoSpace(Exception):
    pass


def probe_tools(search_path=None):
    """Available extraction tools: {name: absolute path}. bsdtar is accepted only when it identifies itself."""
    found = {}
    for name in EXTERNAL_TOOLS:
        path = shutil.which(name, path=search_path)
        if path:
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
    for signature, kind in MAGIC:
        if head.startswith(signature):
            return kind
    if head[257:262] == b'ustar':
        return 'tar'
    return None


def unsafe_name(name):
    normalized = name.replace('\\', '/')
    return (not normalized or normalized.startswith('/') or re.match(r'^[A-Za-z]:', normalized) is not None
            or '..' in normalized.split('/') or '\x00' in normalized)


def clean(text):
    return str(text).encode('utf-8', 'replace').decode('utf-8')


class Reader:
    def __init__(self, temp_dir, tools=None, limits=None, deadline=None, want_text=True):
        self.temp_dir = temp_dir
        self.tools = dict(probe_tools() if tools is None else tools)
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.deadline = deadline
        self.members = []
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
        """Bytes an extraction may still add before the archive cap is reached."""
        return max(0, self.limits['max_expanded_bytes'] - self.expanded)

    # ------------------------------------------------------------------ members
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

    def read_member(self, stream, need_file):
        """Stream a member once. Returns (sha256, size, bytes or None, spool path or None)."""
        digest = hashlib.sha256()
        buffer = bytearray()
        total = 0
        spool = None
        cap = self.limits['member_parse_bytes']
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
            elif total <= cap:
                buffer.extend(block)
            elif need_file:
                spool = self.spill(bytes(buffer))
                spool.write(block)
                buffer = bytearray()
            else:
                buffer = bytearray()
                cap = -1
        if spool is not None:
            spool.close()
            return digest.hexdigest(), total, None, spool.name
        return digest.hexdigest(), total, (bytes(buffer) if total <= self.limits['member_parse_bytes'] else None), None

    def add_member(self, name, declared_size, opener, depth, prefix):
        self.check_time()
        label = clean(prefix + name)
        extension = raw_extension(name)
        normalized = name.replace('\\', '/')
        if normalized.rsplit('/', 1)[-1].startswith('.') or normalized.startswith('__MACOSX/'):
            self.hidden += 1
            return
        if is_credential_name(name):
            with opener() as stream:
                sha, size = self.hash_only(stream)
            self.members.append({'n': label, 'x': extension, 's': size, 'h': sha, 'cred': True})
            return
        likely_nested = extension in ARCHIVE_EXTENSIONS and depth < self.limits['max_depth']
        need_file = likely_nested or extension in ('pdf', 'tif', 'tiff') or extension not in CATEGORY_OF_EXTENSION
        with opener() as stream:
            sha, size, data, spool = self.read_member(stream, need_file)
        record = {'n': label, 'x': extension, 's': size, 'h': sha}
        try:
            head = data[:600] if data is not None else b''
            if spool and not head:
                with open(spool, 'rb') as handle:
                    head = handle.read(600)
            kind = archive_kind(name, head) if depth < self.limits['max_depth'] else None
            if kind and (data is not None or spool):
                before = self.opened
                opened = self.open_nested(kind, name, data, spool, depth + 1, label + '!/')
                record['arch'] = True
                if opened and self.opened > before:
                    record['cont'] = True
            elif data is not None or spool:
                cached = self.cache.get(sha)
                if cached is None:
                    source = data if data is not None else open(spool, 'rb')
                    try:
                        cached = spec_content.content_facts(extension, source, text_pages=3, deadline=self.deadline,
                                                            mail_attachments=False)
                    finally:
                        if data is None:
                            source.close()
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
        self.members.append(record)

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

    def read_external(self, kind, name, stream, depth, prefix, parts):
        chain_key = 'split' if parts else (kind if kind in CHAINS else 'other')
        chain = [tool for tool in CHAINS[chain_key] if tool in self.tools]
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

    def run_guarded(self, arguments, folder, environment, watch, allowance, timeout=EXTERNAL_TIMEOUT):
        """Run an extraction tool with no input, stopping it when the unpacked folder outgrows the allowance or the time is up."""
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=folder,
                                   env=environment)
        started = time.monotonic()
        try:
            while True:
                try:
                    return process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                self.check_time()
                if folder_size(watch) > allowance:
                    raise CapReached('expanded size cap')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def run_tool(self, tool, kind, target, folder, depth, prefix):
        executable = self.tools[tool]
        destination = os.path.join(folder, 'out')
        os.makedirs(destination, exist_ok=True)
        environment = {'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8', 'HOME': folder}
        listed = None
        try:
            if tool in ('7zz', '7z'):
                listing = subprocess.run([executable, 'l', '-slt', '-ba', '-spd', '-bd', target], capture_output=True,
                                         stdin=subprocess.DEVNULL, timeout=600, cwd=folder, env=environment)
                if listing.returncode != 0 and not listing.stdout.strip():
                    return 'password' if b'password' in (listing.stderr + listing.stdout).lower() or b'encrypted' in listing.stderr.lower() else False
                listed = parse_listing(listing.stdout.decode('utf-8', 'replace'))
                files = [entry for entry in listed if not entry['dir'] and not entry['link']]
                declared = sum(entry['size'] for entry in files)
                if declared > self.allowance():
                    raise CapReached('expanded size cap')
                if shutil.disk_usage(folder).free < declared * 1.1 + 1024 * 1024:
                    return 'space'
                wanted, unsafe, encrypted = [], 0, 0
                for entry in files:
                    if unsafe_name(entry['path']):
                        unsafe += 1
                    elif entry['encrypted']:
                        encrypted += 1
                    else:
                        wanted.append(entry)
                if wanted:
                    list_file = os.path.join(folder, 'wanted.txt')
                    with open(list_file, 'w', encoding='utf-8') as handle:
                        handle.write('\n'.join(entry['path'] for entry in wanted) + '\n')
                    self.run_guarded([executable, 'x', '-y', '-bd', '-spd', '-scsUTF-8', '-aoa', '-o' + destination, target, '@' + list_file],
                                     folder, environment, destination, self.allowance())
                self.lose(unsafe + encrypted)
                problems = unsafe + encrypted
            elif tool == 'bsdtar':
                code = self.run_guarded([executable, '-xf', target, '-C', destination, '--no-same-owner', '--no-same-permissions'],
                                        folder, environment, destination, self.allowance())
                if code != 0 and not any(os.scandir(destination)):
                    return False
                problems = 1 if code != 0 else 0
            else:
                code = self.run_guarded([executable, '-q', '-D', '-o', destination, target], folder, environment, destination, self.allowance())
                if code != 0 and not any(os.scandir(destination)):
                    return False
                problems = 1 if code != 0 else 0
        except subprocess.TimeoutExpired:
            return 'timeout'
        except OSError:
            return False
        self.opened += 1
        self.used[f'{kind}:{tool}'] += 1
        found = self.walk_extracted(destination, depth, prefix)
        if listed is not None:
            expected = len([entry for entry in listed if not entry['dir'] and not entry['link'] and not unsafe_name(entry['path'])
                            and not entry['encrypted']])
            missing = max(0, expected - found)
            self.lose(missing)
            problems += missing
        if problems:
            self.partly += 1
        return True

    def walk_extracted(self, destination, depth, prefix):
        """Process every regular file below a private extraction folder. Links are never followed."""
        found = 0
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
                relative = os.path.relpath(entry.path, destination).replace(os.sep, '/')
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    found += 1
                    if is_credential_name(entry.name):
                        with open(entry.path, 'rb') as handle:
                            sha, size = self.hash_only(handle)
                        self.members.append({'n': clean(prefix + relative), 'x': raw_extension(entry.name), 's': size, 'h': sha, 'cred': True})
                        os.unlink(entry.path)
                        continue
                    self.add_member(relative, info.st_size, lambda path=entry.path: open(path, 'rb'), depth, prefix)
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
                else:
                    continue
        return found


def _is_link(attributes):
    parts = attributes.split()
    mode = parts[-1] if parts else ''
    return mode.startswith('l')


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


def parse_listing(text):
    entries = []
    for block in re.split(r'\n\s*\n', text.strip('\n')):
        fields = {}
        for line in block.splitlines():
            if ' = ' in line:
                key, value = line.split(' = ', 1)
                fields[key.strip()] = value
            elif line.endswith(' ='):
                fields[line[:-2].strip()] = ''
        if 'Path' not in fields:
            continue
        attributes = fields.get('Attributes', '')
        entries.append({'path': fields['Path'], 'size': int(fields.get('Size') or 0),
                        'dir': fields.get('Folder', '-') == '+' or attributes.startswith('D'),
                        'link': _is_link(attributes),
                        'encrypted': fields.get('Encrypted', '-') == '+'})
    return entries
