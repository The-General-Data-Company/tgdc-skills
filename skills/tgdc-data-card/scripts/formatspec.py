"""Data format specification: a full, resumable pass over every selected file.

The pass reads files only through no-follow descriptors, hashes every file and
every archive member, and keeps per-file facts in a private SQLite cache that
stays on this machine. spec.json receives aggregates only: counts, sizes,
years and partner-declared vocabulary. Nothing here opens a network
connection, executes file content or modifies a source.
"""
import hashlib
import json
import multiprocessing
import multiprocessing.connection
import multiprocessing.reduction as reduction
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import spec_archives
import spec_content
from profiler import (checked_path, excluded, open_nofollow, output_directory, prepare_roots, require_supported_platform,
                      verify_output_directory, write_json)
from spec_taxonomy import is_credential_name, raw_extension

SCHEMA = 'tgdc-format-spec/1.0'
ENGINE = 3
CACHE_NAME = 'spec-cache.private.sqlite'
SETTINGS_DEFAULTS = {
    'parse_seconds': 300.0, 'archive_seconds': 7200.0, 'member_parse_bytes': 256 * 1024 * 1024,
    'max_message_bytes': 64 * 1024 * 1024, 'max_archive_bytes': 20 * 10 ** 9, 'max_archive_ratio': 200, 'max_depth': 5,
    'grace_seconds': 60.0, 'hash_floor_seconds': 900.0,
}
CONFIG_STRINGS = ('company', 'sector_line', 'month')
PROGRESS_EVERY = 1000


class SpecInterrupted(Exception):
    """The run stopped before the last file; the cache holds the finished part and a rerun resumes."""


class SpecConfigError(ValueError):
    pass


# --------------------------------------------------------------------------- configuration
def _text(value, key, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise SpecConfigError(f'Invalid value for {key}.')
    return value.strip()


def _patterns(value, key):
    if not isinstance(value, list) or len(value) > 200:
        raise SpecConfigError(f'Invalid value for {key}.')
    return [_text(item, key, 100) for item in value]


def _inside(path, roots, key):
    resolved = checked_path(path)
    for index, root in enumerate(roots):
        if resolved == root or root in resolved.parents:
            return index, resolved.relative_to(root).as_posix() if resolved != root else ''
    raise SpecConfigError(f'{key} must be inside a selected root.')


def validate_config(raw, roots):
    """Validate the partner-written configuration. All keys are optional; unknown keys are rejected."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise SpecConfigError('The specification configuration must be a JSON object.')
    allowed = {'company', 'sector_line', 'month', 'mailboxes', 'project_roots', 'project_depth', 'stages',
               'bill_of_quantities_patterns', 'drawing_patterns', 'schematic_patterns', 'calibration', 'revision_tokens', 'limits'}
    unknown = set(raw) - allowed
    if unknown:
        raise SpecConfigError('Unknown configuration key: ' + ', '.join(sorted(unknown)))
    config = {}
    for key in CONFIG_STRINGS:
        if key in raw:
            config[key] = _text(raw[key], key)
    config['mailboxes'] = []
    for item in raw.get('mailboxes', []):
        if not isinstance(item, dict) or set(item) - {'root', 'label'} or 'root' not in item:
            raise SpecConfigError('Each mailbox needs a root and may have a label.')
        index, relative = _inside(_text(item['root'], 'mailboxes.root', 4096), roots, 'A mailbox root')
        config['mailboxes'].append({'root': index, 'prefix': relative, 'label': _text(item['label'], 'mailboxes.label') if item.get('label') else None})
    for number, mailbox in enumerate(config['mailboxes'], 1):
        mailbox['label'] = mailbox['label'] or f'Mailbox {number}'
    config['project_roots'] = []
    for item in raw.get('project_roots', []):
        index, relative = _inside(_text(item, 'project_roots', 4096), roots, 'A project root')
        config['project_roots'].append({'root': index, 'prefix': relative})
    depth = raw.get('project_depth', 1)
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 8:
        raise SpecConfigError('project_depth must be an integer from 1 to 8.')
    config['project_depth'] = depth
    stages = raw.get('stages')
    if stages is not None:
        if not isinstance(stages, dict) or set(stages) - {'title', 'items', 'thresholds'}:
            raise SpecConfigError('Invalid stages.')
        items = stages.get('items', [])
        if not isinstance(items, list) or not items or len(items) > 40:
            raise SpecConfigError('stages.items must list the partner stages.')
        parsed = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {'name', 'patterns'}:
                raise SpecConfigError('Invalid stage item.')
            parsed.append({'name': _text(item.get('name'), 'stages.name', 80), 'patterns': _patterns(item.get('patterns', []), 'stages.patterns')})
        thresholds = stages.get('thresholds')
        if thresholds is not None:
            if (not isinstance(thresholds, list) or not thresholds
                    or any(isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= len(parsed) for t in thresholds)):
                raise SpecConfigError('stages.thresholds must be integers between 1 and the number of stages.')
            thresholds = sorted(set(thresholds))
        config['stages'] = {'title': _text(stages.get('title', 'lifecycle stages'), 'stages.title', 60), 'items': parsed, 'thresholds': thresholds}
    for key in ('bill_of_quantities_patterns', 'drawing_patterns', 'schematic_patterns', 'revision_tokens'):
        config[key] = _patterns(raw.get(key, []), key)
    calibration = raw.get('calibration')
    if calibration is not None:
        config['calibration'] = validate_calibration(calibration)
    limits = raw.get('limits', {})
    if not isinstance(limits, dict) or set(limits) - {'max_archive_bytes', 'max_archive_ratio', 'max_depth', 'parse_seconds', 'archive_seconds'}:
        raise SpecConfigError('Invalid limits.')
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise SpecConfigError('Limits must be positive numbers.')
    config['limits'] = dict(limits)
    return config


def validate_calibration(value):
    required = {'sampled', 'true_drawings_among_candidates', 'drawings_among_non_candidates', 'schematics', 'views_per_sheet'}
    if not isinstance(value, dict) or set(value) != required:
        raise SpecConfigError('calibration needs exactly: ' + ', '.join(sorted(required)))
    sampled = value['sampled']
    if (not isinstance(sampled, dict) or set(sampled) != {'candidates', 'non_candidates'}
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in sampled.values()) or not sampled['candidates']):
        raise SpecConfigError('calibration.sampled needs candidates and non_candidates counts.')
    for key in ('true_drawings_among_candidates', 'drawings_among_non_candidates', 'schematics'):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0:
            raise SpecConfigError(f'calibration.{key} must be a count.')
    if value['true_drawings_among_candidates'] > sampled['candidates'] or value['drawings_among_non_candidates'] > sampled['non_candidates']:
        raise SpecConfigError('calibration counts exceed the sampled pages.')
    if value['schematics'] > value['true_drawings_among_candidates'] + value['drawings_among_non_candidates']:
        raise SpecConfigError('calibration.schematics exceeds the true drawings.')
    views = value['views_per_sheet']
    if not isinstance(views, list) or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 200 for v in views):
        raise SpecConfigError('calibration.views_per_sheet must list counts of views per sheet.')
    return {'sampled': dict(sampled), 'true_drawings_among_candidates': value['true_drawings_among_candidates'],
            'drawings_among_non_candidates': value['drawings_among_non_candidates'], 'schematics': value['schematics'],
            'views_per_sheet': list(views)}


def load_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise SpecConfigError('The specification configuration could not be read as JSON.') from error
    return data


# --------------------------------------------------------------------------- inventory
class Entry:
    __slots__ = ('root', 'rel', 'path', 'name', 'size', 'mtime_ns', 'dev', 'ino', 'credential', 'role', 'parts', 'style', 'leader', 'id')

    def __init__(self, root, rel, path, size, mtime_ns, dev, ino, credential):
        self.root, self.rel, self.path, self.name = root, rel, path, rel.rsplit('/', 1)[-1]
        self.size, self.mtime_ns, self.dev, self.ino, self.credential = size, mtime_ns, dev, ino, credential
        self.role = self.parts = self.style = self.leader = None
        self.id = os.fsencode(f'{root}:{rel}')


def walk_roots(roots):
    """Every regular, non-hidden file below the roots. Links are never followed; hidden entries are not entered."""
    entries = []
    stats = Counter()
    for index, root in enumerate(roots):
        if root.is_file():
            details = root.stat(follow_symlinks=False)
            entries.append(Entry(index, root.name, str(root), details.st_size, details.st_mtime_ns, details.st_dev, details.st_ino,
                                 excluded(root) or is_credential_name(root.name)))
            continue
        stack = [(root, '')]
        while stack:
            directory, relative = stack.pop()
            try:
                descriptor = open_nofollow(directory, directory=True)
            except OSError:
                stats['unreadable_folders'] += 1
                continue
            found = []
            try:
                with os.scandir(descriptor) as iterator:
                    for item in iterator:
                        try:
                            if item.is_symlink():
                                stats['symlinks'] += 1
                                continue
                            found.append((item.name, item.stat(follow_symlinks=False)))
                        except OSError:
                            stats['unreadable_entries'] += 1
            finally:
                os.close(descriptor)
            for name, details in sorted(found, key=lambda pair: pair[0]):
                if name.startswith('.'):
                    stats['hidden_entries'] += 1
                    continue
                child = relative + '/' + name if relative else name
                if stat.S_ISDIR(details.st_mode):
                    if excluded(Path(name)):
                        stats['credential_folders'] += 1
                    else:
                        stack.append((directory / name, child))
                elif stat.S_ISREG(details.st_mode):
                    entries.append(Entry(index, child, str(directory / name), details.st_size, details.st_mtime_ns, details.st_dev,
                                         details.st_ino, excluded(Path(name)) or is_credential_name(name)))
                else:
                    stats['special_files'] += 1
    entries.sort(key=lambda entry: (entry.root, entry.rel))
    assign_parts(entries)
    return entries, stats


def assign_parts(entries):
    """Link the volumes of multi-part archives: the first volume leads, the rest follow."""
    folders = defaultdict(dict)
    for entry in entries:
        folder = entry.rel.rsplit('/', 1)[0] if '/' in entry.rel else ''
        folders[(entry.root, folder)][entry.name.lower()] = entry
    for names in folders.values():
        groups = defaultdict(list)
        for lowered, entry in names.items():
            match = re.match(r'^(.+)\.part0*(\d+)\.rar$', lowered)
            if match:
                groups[('rarpart', match.group(1))].append((int(match.group(2)), entry))
                continue
            match = re.match(r'^(.+)\.r(\d{2,3})$', lowered)
            if match and match.group(1) + '.rar' in names:
                groups[('rar', match.group(1))].append((int(match.group(2)) + 1, entry))
                continue
            match = re.match(r'^(.+)\.z(\d{2,3})$', lowered)
            if match and match.group(1) + '.zip' in names:
                groups[('zip', match.group(1))].append((int(match.group(2)), entry))
                continue
            match = re.match(r'^(.+)\.(\d{3,})$', lowered)
            if match:
                groups[('num', match.group(1))].append((int(match.group(2)), entry))
        for (style, base), members in groups.items():
            if style == 'rar':
                members.append((0, names[base + '.rar']))
            elif style == 'zip':
                members.append((10 ** 6, names[base + '.zip']))
            members.sort(key=lambda pair: pair[0])
            if len(members) < 2:
                continue
            if style == 'num' and members[0][0] not in (0, 1):
                continue
            ordered = [entry for _, entry in members]
            leader = ordered[-1] if style == 'zip' else ordered[0]
            leader.role, leader.style = 'leader', style
            leader.parts = [leader.path] + [part.path for part in ordered if part is not leader]
            for part in ordered:
                if part is not leader:
                    part.role, part.style, part.leader = 'follower', style, leader.id


# --------------------------------------------------------------------------- cache
class Cache:
    def __init__(self, directory_descriptor, path):
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        descriptor = os.open(os.path.basename(path), flags, 0o600, dir_fd=directory_descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS files(id BLOB PRIMARY KEY, root INTEGER, path BLOB, size INTEGER, mtime_ns INTEGER, status TEXT, result TEXT)')
        stored = self.db.execute("SELECT value FROM meta WHERE key='engine'").fetchone()
        if stored and stored[0] != str(ENGINE):
            self.db.execute('DELETE FROM files')
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('engine', ?)", (str(ENGINE),))
        self.pending = 0
        self.db.execute('BEGIN')

    def index(self):
        return {row[0]: (row[1], row[2]) for row in self.db.execute('SELECT id, size, mtime_ns FROM files')}

    def put(self, entry, result):
        self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)',
                        (entry.id, entry.root, os.fsencode(entry.rel), entry.size, entry.mtime_ns, result.get('status', 'ok'),
                         json.dumps(result, ensure_ascii=False, separators=(',', ':'))))
        self.pending += 1
        if self.pending >= 200:
            self.commit()

    def results(self, wanted=None, suffix=None):
        query = 'SELECT id, result FROM files'
        arguments = ()
        if suffix:
            query += " WHERE lower(cast(path as text)) LIKE ? ESCAPE '\\'"
            arguments = ('%' + suffix.replace('_', '\\_').replace('%', '\\%'),)
        for identifier, result in self.db.execute(query + ' ORDER BY id', arguments):
            if wanted is None or identifier in wanted:
                yield identifier, json.loads(result)

    def get(self, identifier):
        row = self.db.execute('SELECT result FROM files WHERE id=?', (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)', (key, json.dumps(value)))

    def get_meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self):
        self.db.execute('COMMIT')
        self.db.execute('BEGIN')
        self.pending = 0

    def close(self):
        try:
            self.db.execute('COMMIT')
        except sqlite3.OperationalError:
            pass
        try:
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            self.db.close()


# --------------------------------------------------------------------------- worker
def open_verified(path, identity):
    """Open a file without following links; (descriptor, None) or (None, reason)."""
    try:
        descriptor = open_nofollow(path)
    except OSError:
        return None, 'io_error'
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError('not a regular file')
        if identity is not None and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != tuple(identity):
            os.close(descriptor)
            return None, 'changed'
    except OSError:
        os.close(descriptor)
        return None, 'io_error'
    return descriptor, None


def _open_regular(path, identity):
    descriptor, problem = open_verified(path, identity)
    return (os.fdopen(descriptor, 'rb'), None) if descriptor is not None else (None, problem)


def _facts_for_stream(extension, stream, data, settings, deadline):
    if data is not None:
        return spec_content.content_facts(extension, data, text_pages=1, deadline=deadline)
    if extension in ('pdf', 'tif', 'tiff'):
        stream.seek(0)
        return spec_content.content_facts(extension, stream, text_pages=1, deadline=deadline)
    return {}


def process_task(task, settings, stage, descriptors):
    """Hash one file and extract its facts. The file arrives as an open descriptor, never as a path.

    Returns a JSON-ready dictionary and never raises."""
    name = task['name']
    extension = raw_extension(name)
    streams = [os.fdopen(descriptor, 'rb') for descriptor in descriptors]
    stream = streams[0]
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return {'status': 'io_error'}
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != tuple(task['identity']):
            return {'status': 'changed'}
        size = task['identity'][2]
        keep = size <= settings['member_parse_bytes']
        digest = hashlib.sha256()
        buffer = bytearray() if keep else None
        head = b''
        total = 0
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            if not head:
                head = block[:600]
            digest.update(block)
            total += len(block)
            if keep:
                buffer.extend(block)
        if total != size:
            return {'status': 'changed'}
        sha = digest.hexdigest()
        stage(sha)
        data = bytes(buffer) if keep else None
        result = {'status': 'ok', 'sha': sha}
        now = time.monotonic()
        deadline = now + settings['parse_seconds']
        try:
            role = task.get('role')
            if extension == 'eml' and data is not None and size <= settings['max_message_bytes']:
                result['mail'] = spec_content.analyze_message(data, deadline=deadline)
            elif extension == 'eml':
                result['unreadable'] = True
            elif extension == 'mbox':
                stream.seek(0)
                result['mbox'] = spec_content.analyze_mbox(stream, set(task.get('known', ())), settings['max_message_bytes'],
                                                           deadline=now + settings['archive_seconds'])
            elif role == 'leader' and task.get('style') == 'num':
                result['arch'] = _read_archive(task, streams, size, name, None, settings)
            elif role != 'follower' and spec_archives.archive_kind(name, head):
                result['arch'] = _read_archive(task, streams, size, name, spec_archives.archive_kind(name, head), settings)
            else:
                result.update(_facts_for_stream(extension, stream, data, settings, deadline))
        except Exception:
            result['unreadable'] = True
        return result
    finally:
        for item in streams:
            item.close()


def _read_archive(task, streams, size, name, kind, settings):
    """Read an archive from its open volumes (the first is the leader); temporary files live in a private folder."""
    folder = tempfile.mkdtemp(dir=settings['temp_dir'])
    opened = []
    try:
        limits = {'max_expanded_bytes': settings['max_archive_bytes'], 'max_ratio': settings['max_archive_ratio'],
                  'max_depth': settings['max_depth'], 'member_parse_bytes': settings['member_parse_bytes']}
        reader = spec_archives.Reader(folder, tools=settings['tools'], limits=limits, deadline=time.monotonic() + settings['archive_seconds'])
        parts = None
        compressed = size
        source, source_name = streams[0], name
        if task.get('role') == 'leader' and len(streams) > 1:
            names = task['part_names']
            if task.get('style') == 'num':
                joined = os.path.join(folder, 'joined')
                with open(joined, 'wb') as target:
                    for part in streams:
                        part.seek(0)
                        shutil.copyfileobj(part, target, 1024 * 1024)
                source = open(joined, 'rb')
                opened.append(source)
                source_name = re.sub(r'\.\d{3,}$', '', name)
                kind = spec_archives.archive_kind(source_name, source.read(600))
                compressed = os.path.getsize(joined)
            else:
                parts = list(zip(names, streams))
                compressed = sum(os.fstat(part.fileno()).st_size for part in streams)
        if not kind:
            return {'kind': None, 'ok': False, 'opened': 0}
        source.seek(0)
        ok = reader.open_archive(kind, source_name, source, 1, '', compressed=compressed, parts=parts)
        return {'kind': kind, 'ok': bool(ok), 'opened': reader.opened, 'partly': reader.partly, 'lost': reader.lost,
                'failed': dict(reader.failed), 'used': dict(reader.used), 'hidden': reader.hidden, 'timeout': reader.timed_out,
                'members': reader.members}
    finally:
        for item in opened:
            item.close()
        shutil.rmtree(folder, ignore_errors=True)


def worker_main(connection, settings):
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        while True:
            try:
                task = connection.recv()
            except EOFError:
                break
            if task is None:
                break
            descriptors = [reduction.recv_handle(connection) for _ in range(task['handles'])]
            try:
                result = process_task(task, settings, lambda sha: connection.send(('hash', sha)), descriptors)
            except Exception:
                result = {'status': 'error'}
            connection.send(('done', result))
    finally:
        connection.close()


# --------------------------------------------------------------------------- worker pool
class Worker:
    def __init__(self, context, settings, target=None):
        parent, child = context.Pipe()
        self.target = target or worker_main
        self.process = context.Process(target=self.target, args=(child, settings), daemon=True)
        self.process.start()
        child.close()
        self.connection = parent
        self.task = None
        self.deadline = None
        self.sha = None

    def kill(self):
        try:
            self.connection.close()
        except OSError:
            pass
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)


MAX_VOLUMES = 200


def open_volumes(task):
    """Open the file and, for the first volume of a multi-part archive, its other volumes, all without following links."""
    descriptor, problem = open_verified(task['path'], task['identity'])
    if problem:
        return [], problem
    descriptors = [descriptor]
    names = [task['name']]
    for path in (task.get('parts') or [])[1:MAX_VOLUMES]:
        extra, _ = open_verified(path, None)
        if extra is not None:
            descriptors.append(extra)
            names.append(os.path.basename(path))
    task['part_names'] = names
    return descriptors, None


def run_pool(tasks, count, settings, on_result, target=None):
    """Run tasks on worker processes. A worker that exceeds its deadline or dies is replaced; the file is recorded as such."""
    context = multiprocessing.get_context('spawn')
    pending = deque(tasks)
    workers = [Worker(context, settings, target or worker_main) for _ in range(max(1, min(count, len(pending) or 1)))]
    try:
        while pending or any(worker.task for worker in workers):
            for worker in workers:
                while worker.task is None and pending:
                    task = pending.popleft()
                    descriptors, problem = open_volumes(task)
                    if problem:
                        on_result(task, {'status': problem})
                        continue
                    worker.task, worker.sha = task, None
                    worker.deadline = time.monotonic() + max(settings['hash_floor_seconds'], task['identity'][2] / 1e6 * 2)
                    try:
                        message = {key: value for key, value in task.items() if key not in ('path', 'parts')}
                        message['handles'] = len(descriptors)
                        worker.connection.send(message)
                        for descriptor in descriptors:
                            reduction.send_handle(worker.connection, descriptor, worker.process.pid)
                    except (OSError, ValueError):
                        worker.deadline = 0
                    finally:
                        for descriptor in descriptors:
                            os.close(descriptor)
            busy = [worker for worker in workers if worker.task is not None]
            ready = multiprocessing.connection.wait([worker.connection for worker in busy], timeout=1.0)
            for worker in busy:
                finished = None
                if worker.connection in ready:
                    try:
                        message, payload = worker.connection.recv()
                    except (EOFError, OSError):
                        finished = {'status': 'crashed'}
                    else:
                        if message == 'hash':
                            worker.sha = payload
                            long_running = worker.task.get('long', False)
                            budget = (settings['archive_seconds'] if long_running else settings['parse_seconds']) + settings['grace_seconds']
                            worker.deadline = time.monotonic() + budget
                            continue
                        finished = payload
                elif time.monotonic() > worker.deadline:
                    finished = {'status': 'timeout'}
                if finished is None:
                    continue
                if finished.get('status') in ('crashed', 'timeout'):
                    if worker.sha:
                        finished['sha'] = worker.sha
                    worker.kill()
                    task = worker.task
                    worker.__init__(context, settings, worker.target)
                    worker.task = None
                    on_result(task, finished)
                else:
                    task = worker.task
                    worker.task = None
                    on_result(task, finished)
    finally:
        for worker in workers:
            try:
                worker.connection.send(None)
            except (OSError, ValueError):
                pass
        for worker in workers:
            worker.kill()


# --------------------------------------------------------------------------- run
def format_duration(seconds):
    seconds = int(max(0, seconds))
    return f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


class Progress:
    def __init__(self, total_files, total_bytes, already_files, already_bytes, stream):
        self.total_files, self.total_bytes = total_files, total_bytes
        self.files, self.bytes = already_files, already_bytes
        self.base_bytes = already_bytes
        self.started = time.monotonic()
        self.stream = stream
        self.next = (already_files // PROGRESS_EVERY + 1) * PROGRESS_EVERY

    def add(self, size):
        self.files += 1
        self.bytes += size
        if self.files >= self.next or self.files == self.total_files:
            self.next = (self.files // PROGRESS_EVERY + 1) * PROGRESS_EVERY
            elapsed = time.monotonic() - self.started
            rate = (self.bytes - self.base_bytes) / elapsed if elapsed > 0 else 0
            remaining = (self.total_bytes - self.bytes) / rate if rate > 0 else None
            eta = f', about {format_duration(remaining)} left' if remaining is not None and self.files < self.total_files else ''
            print(f'[spec] {self.files:,} of {self.total_files:,} files, {self.bytes / 1e9:,.1f} of {self.total_bytes / 1e9:,.1f} GB read, '
                  f'elapsed {format_duration(elapsed)}{eta}', file=self.stream, flush=True)


def check_dependencies():
    """Fail early and clearly when a pinned library is missing, instead of reporting every PDF as unchecked."""
    for name in ('pypdfium2', 'PIL', 'olefile', 'py3langid'):
        __import__(name)


def default_workers():
    return max(1, min(8, (os.cpu_count() or 2) - 1 or 1))


def run_spec(sources, output, config=None, workers=None, limits=None, tools=None, stop_after=None, log=None, report_date=None):
    """Read every selected file and write spec.json and the private cache into the output folder."""
    from spec_aggregate import aggregate
    require_supported_platform()
    check_dependencies()
    stream = log if log is not None else sys.stderr
    roots, output = prepare_roots(sources, output)
    cfg = validate_config(config, roots)
    settings = {**SETTINGS_DEFAULTS}
    for key, value in {**cfg['limits'], **(limits or {})}.items():
        if key in settings and value is not None:
            settings[key] = type(SETTINGS_DEFAULTS[key])(value)
    settings['tools'] = spec_archives.probe_tools() if tools is None else dict(tools)
    descriptor = output_directory(output)
    cache = None
    temp_dir = output / 'spec-tmp'
    try:
        verify_output_directory(output, descriptor)
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        os.mkdir(temp_dir, 0o700)
        settings['temp_dir'] = str(temp_dir)
        entries, walk_stats = walk_roots(roots)
        cache = Cache(descriptor, output / CACHE_NAME)
        cache.set_meta('roots', [str(root) for root in roots])
        cache.set_meta('config', {key: value for key, value in cfg.items()})
        known = cache.index()
        by_id = {entry.id: entry for entry in entries}
        todo, done_files, done_bytes = [], 0, 0
        for entry in entries:
            if known.get(entry.id) == (entry.size, entry.mtime_ns):
                done_files += 1
                done_bytes += entry.size
            elif entry.credential:
                cache.put(entry, {'status': 'credential'})
                done_files += 1
                done_bytes += entry.size
            else:
                todo.append(entry)
        total_bytes = sum(entry.size for entry in entries)
        progress = Progress(len(entries), total_bytes, done_files, done_bytes, stream)
        count = workers or default_workers()

        def task_for(entry, extra=None):
            ext = raw_extension(entry.name)
            task = {'id': entry.id, 'path': entry.path, 'name': entry.name, 'identity': (entry.dev, entry.ino, entry.size, entry.mtime_ns),
                    'role': entry.role, 'style': entry.style, 'parts': entry.parts, 'long': ext in ('mbox',) or entry.role == 'leader' or ext in spec_archives.ARCHIVE_EXTENSIONS,
                    'size': entry.size}
            if extra:
                task.update(extra)
            return task

        processed = [0]

        def handle(task, result):
            entry = by_id[task['id']]
            cache.put(entry, result)
            progress.add(entry.size)
            processed[0] += 1
            if stop_after is not None and processed[0] >= stop_after:
                raise SpecInterrupted()

        first = [task_for(entry) for entry in todo if raw_extension(entry.name) != 'mbox']
        second = [entry for entry in todo if raw_extension(entry.name) == 'mbox']
        try:
            if first:
                run_pool(first, count, settings, handle)
            if second:
                known_ids = mailbox_message_ids(cache, entries, cfg)
                tasks = [task_for(entry, {'known': sorted(known_ids.get(mailbox_of(entry, cfg), ()))}) for entry in second]
                run_pool(tasks, count, settings, handle)
        except (SpecInterrupted, KeyboardInterrupt):
            cache.commit()
            raise SpecInterrupted()
        cache.commit()
        current = datetime.now(timezone.utc)
        context = {'current_year': (report_date or current).year, 'walk': dict(walk_stats), 'tools': settings['tools'],
                   'settings': settings, 'workers': count}
        spec = aggregate(entries, cache, cfg, context)
        verify_output_directory(output, descriptor)
        write_json(descriptor, 'spec.json', spec)
        verify_output_directory(output, descriptor)
        return spec
    finally:
        if cache is not None:
            cache.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.close(descriptor)


def mailbox_of(entry, cfg):
    """Index of the deepest configured mailbox containing the entry, or None."""
    best = None
    for index, mailbox in enumerate(cfg['mailboxes']):
        if mailbox['root'] != entry.root:
            continue
        prefix = mailbox['prefix']
        if not prefix or entry.rel == prefix or entry.rel.startswith(prefix + '/'):
            if best is None or len(prefix) > len(cfg['mailboxes'][best]['prefix']):
                best = index
    return best


def mailbox_message_ids(cache, entries, cfg):
    """Message ids of the loose .eml files of each mailbox, for skipping duplicate messages inside .mbox files."""
    by_id = {entry.id: entry for entry in entries}
    found = defaultdict(set)
    for identifier, result in cache.results(wanted=by_id, suffix='.eml'):
        mail = result.get('mail')
        entry = by_id[identifier]
        if mail and mail.get('mid') and raw_extension(entry.name) == 'eml':
            found[mailbox_of(entry, cfg)].add(mail['mid'])
    return found
