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
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import portable
import spec_archives
import spec_content
from profiler import (checked_path, open_nofollow, output_directory, prepare_roots, require_supported_platform,
                      verify_output_directory, write_json)
from spec_taxonomy import ARCHIVE_EXTENSIONS, IMAGE_SHEET_EXTENSIONS, KEYNOTE_SIGNATURE, is_short_label, is_sensitive_path, raw_extension

SCHEMA = 'tgdc-format-spec/1.0'
ENGINE = 7
CACHE_NAME = 'spec-cache.private.sqlite'
LOCK_NAME = 'spec-run.lock'
MEMBER_BATCH = 1000          # archive members sent to the parent at a time
GIB = 2 ** 30
SETTINGS_DEFAULTS = {
    'parse_seconds': 300.0, 'archive_seconds': 7200.0, 'member_parse_bytes': 128 * 1024 * 1024,
    'max_message_bytes': 64 * 1024 * 1024, 'max_archive_bytes': 20 * 10 ** 9, 'max_archive_ratio': 200, 'max_depth': 5,
    'grace_seconds': 60.0, 'hash_floor_seconds': 900.0,
}
CONFIG_STRINGS = ('company', 'sector_line', 'month')
PROGRESS_EVERY = 1000


class SpecInterrupted(Exception):
    """The run stopped before the last file; the cache holds the finished part and a rerun resumes."""


class SpecConfigError(ValueError):
    pass


def acquire_lock(descriptor):
    """Exclusive lock on the output folder for the length of a run; a second run there is refused."""
    try:
        lock = portable.open_at(descriptor, LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except PermissionError as error:
        if not portable.WINDOWS:
            raise
        # Windows refuses to open a lock file that a running run holds without sharing, or that is still being deleted.
        raise SpecConfigError('Another specification run is using this output folder. Wait for it to finish.') from error
    try:
        portable.lock(lock)
    except OSError as error:
        os.close(lock)
        raise SpecConfigError('Another specification run is using this output folder. Wait for it to finish.') from error
    return lock


def _terminate(signum, frame):
    raise KeyboardInterrupt()


def release_lock(descriptor, lock):
    try:
        portable.unlink_at(descriptor, LOCK_NAME)
    except OSError:
        pass
    os.close(lock)


# --------------------------------------------------------------------------- configuration
def _text(value, key, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise SpecConfigError(f'Invalid value for {key}.')
    return value.strip()


def _label(value, key):
    """A label that appears in the PDF: short, generic, neither an address nor a link."""
    text = _text(value, key, 200)
    if not is_short_label(text):
        raise SpecConfigError(f'{key} must be a short generic label (at most 60 characters, no address, no link, no person or client name).')
    return text


def _patterns(value, key):
    if not isinstance(value, list) or len(value) > 200:
        raise SpecConfigError(f'Invalid value for {key}.')
    return [_text(item, key, 100) for item in value]


def _inside(path, roots, key):
    resolved = portable.comparable(checked_path(path))
    for index, root in enumerate(roots):
        root = portable.comparable(root)
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
        config['mailboxes'].append({'root': index, 'prefix': relative, 'label': _label(item['label'], 'mailboxes.label') if item.get('label') else None})
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
            parsed.append({'name': _label(item.get('name'), 'stages.name'), 'patterns': _patterns(item.get('patterns', []), 'stages.patterns')})
        thresholds = stages.get('thresholds')
        if thresholds is not None:
            if (not isinstance(thresholds, list) or not thresholds
                    or any(isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= len(parsed) for t in thresholds)):
                raise SpecConfigError('stages.thresholds must be integers between 1 and the number of stages.')
            thresholds = sorted(set(thresholds))
        config['stages'] = {'title': _label(stages.get('title', 'lifecycle stages'), 'stages.title'), 'items': parsed, 'thresholds': thresholds}
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
    if not isinstance(views, list) or not views or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 200 for v in views):
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
    __slots__ = ('root', 'rel', 'path', 'name', 'size', 'mtime_ns', 'dev', 'ino', 'credential', 'keynote', 'role', 'parts', 'style', 'leader', 'id')

    def __init__(self, root, rel, path, size, mtime_ns, dev, ino, credential, keynote=False):
        self.root, self.rel, self.path, self.name = root, rel, path, rel.rsplit('/', 1)[-1]
        self.size, self.mtime_ns, self.dev, self.ino, self.credential, self.keynote = size, mtime_ns, dev, ino, credential, keynote
        self.role = self.parts = self.style = self.leader = None
        self.id = os.fsencode(f'{root}:{rel}')


def classify_file(path, name):
    """(credential, keynote) of a file. Only the name is looked at, except for a .key file whose name has nothing else credential-like:
    its first four bytes tell a Keynote presentation (a zip signature) from a key file. Nothing else is opened."""
    if not is_sensitive_path((name,)):
        return False, False
    if (raw_extension(name) == 'key' and not name.startswith('.') and not is_sensitive_path((name[:-len('.key')],))
            and head_of(path, len(KEYNOTE_SIGNATURE)) == KEYNOTE_SIGNATURE):
        return False, True
    return True, False


def walk_roots(roots):
    """Every regular, non-hidden file below the roots. Links are never followed; hidden entries and folders with credential-like names are not entered.

    The ancestors of a name were checked on the way down, so checking the name itself covers every path component."""
    entries = []
    stats = Counter()
    for index, root in enumerate(roots):
        if root.is_file():
            details = root.stat(follow_symlinks=False)
            entries.append(Entry(index, root.name, str(root), details.st_size, details.st_mtime_ns, *portable.identity(details)[:2],
                                 *classify_file(str(root), root.name)))
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
                with os.scandir(portable.scandir_target(descriptor, directory)) as iterator:
                    for item in iterator:
                        try:
                            if portable.is_link_entry(item):
                                stats['symlinks'] += 1
                                continue
                            if portable.hidden_entry(item):           # Windows hidden entries and system files: desktop.ini, Thumbs.db, ~$ lock files
                                stats['hidden_entries'] += 1
                                continue
                            found.append((item.name, portable.entry_stat(item)))
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
                    if is_sensitive_path((name,)):
                        stats['credential_folders'] += 1
                    else:
                        stack.append((directory / name, child))
                elif stat.S_ISREG(details.st_mode):
                    entries.append(Entry(index, child, str(directory / name), details.st_size, details.st_mtime_ns, *portable.identity(details)[:2],
                                         *classify_file(str(directory / name), name)))
                else:
                    stats['special_files'] += 1
    entries.sort(key=lambda entry: (entry.root, entry.rel))
    stats['orphan_volumes'] += assign_parts(entries)
    return entries, stats


def head_of(path, count=600):
    """First bytes of a file, opened without following links; empty when it cannot be read."""
    try:
        descriptor = open_nofollow(path)
    except OSError:
        return b''
    try:
        return os.read(descriptor, count)
    except OSError:
        return b''
    finally:
        os.close(descriptor)


def assign_parts(entries):
    """Link the volumes of multi-part archives: the first volume leads, the rest follow.

    A numbered series (x.001, x.002) counts only when its first part has an archive signature. Volumes whose first part is
    missing are marked 'orphan'. Returns the number of orphan volumes."""
    folders = defaultdict(dict)
    for entry in entries:
        folder = entry.rel.rsplit('/', 1)[0] if '/' in entry.rel else ''
        folders[(entry.root, folder)][entry.name.lower()] = entry
    orphans = 0
    for names in folders.values():
        groups = defaultdict(list)
        for lowered, entry in names.items():
            match = re.match(r'^(.+)\.part0*(\d+)\.rar$', lowered)
            if match:
                groups[('rarpart', match.group(1))].append((int(match.group(2)), entry))
                continue
            match = re.match(r'^(.+)\.r(\d{2,3})$', lowered)
            if match:
                groups[('rar', match.group(1))].append((int(match.group(2)) + 1, entry))
                continue
            match = re.match(r'^(.+)\.z(\d{2,3})$', lowered)
            if match:
                groups[('zip', match.group(1))].append((int(match.group(2)), entry))
                continue
            match = re.match(r'^(.+)\.(\d{3,})$', lowered)
            if match:
                groups[('num', match.group(1))].append((int(match.group(2)), entry))
        for (style, base), members in groups.items():
            has_first = True
            if style == 'rar':
                has_first = base + '.rar' in names
                if has_first:
                    members.append((0, names[base + '.rar']))
            elif style == 'zip':
                has_first = base + '.zip' in names
                if has_first:
                    members.append((10 ** 6, names[base + '.zip']))
            members.sort(key=lambda pair: pair[0])
            first = members[0][0]
            if style == 'rarpart':
                has_first = first == 1
            elif style == 'num':
                has_first = first in (0, 1)
            if not has_first:
                if style != 'num' or raw_extension(base) in ARCHIVE_EXTENSIONS:
                    for _, entry in members:
                        entry.role, entry.style = 'orphan', style
                        orphans += 1
                continue
            if len(members) < 2:
                continue
            if style == 'num' and not spec_archives.magic_kind(head_of(members[0][1].path)):
                continue
            ordered = [entry for _, entry in members]
            leader = ordered[-1] if style == 'zip' else ordered[0]
            leader.role, leader.style = 'leader', style
            leader.parts = [leader.path] + [part.path for part in ordered if part is not leader]
            for part in ordered:
                if part is not leader:
                    part.role, part.style, part.leader = 'follower', style, leader.id
    return orphans


# --------------------------------------------------------------------------- cache
FINGERPRINT_LIMITS = tuple(SETTINGS_DEFAULTS)


def unfinished(result):
    """True for a row that was cut short or failed, so that other limits or tools could give another result."""
    if result.get('status', 'ok') != 'ok' or result.get('unreadable'):
        return True
    archive = result.get('arch')
    if archive and (archive.get('partly') or archive.get('failed') or archive.get('timeout') or (archive.get('kind') and not archive.get('ok'))):
        return True
    mailbox = result.get('mbox')
    return bool(mailbox and (mailbox.get('cut') or mailbox.get('partial')))


def fingerprint(settings, cfg):
    """What decides how a file is read: the limits, the names of the extraction tools and the mailbox roots (labels only decide how they are shown)."""
    return {'limits': {key: settings[key] for key in FINGERPRINT_LIMITS}, 'tools': sorted(settings['tools']),
            'mailboxes': sorted([mailbox['root'], mailbox['prefix']] for mailbox in cfg['mailboxes'])}


def stored_ns(nanoseconds):
    """A file date as the cache holds it: SQLite integers have 64 bits, and some file systems report dates beyond that."""
    return max(-2 ** 63, min(nanoseconds, 2 ** 63 - 1))


class Cache:
    def __init__(self, directory_descriptor, path):
        descriptor = portable.open_at(directory_descriptor, os.path.basename(path), os.O_RDWR | os.O_CREAT, 0o600)
        portable.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self.path = str(path)
        self.db = sqlite3.connect(portable.plain(self.path), isolation_level=None)
        # The write-ahead log needs shared memory, which a network share does not provide; there the cache keeps a rollback journal.
        self.db.execute('PRAGMA journal_mode=' + ('DELETE' if portable.is_network_path(self.path) else 'WAL'))
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS files(id BLOB PRIMARY KEY, root INTEGER, path BLOB, size INTEGER, mtime_ns INTEGER, status TEXT, result TEXT)')
        # One row per archive member: the hash and size in fixed-size columns, the rest of the member's facts as short JSON.
        self.db.execute('CREATE TABLE IF NOT EXISTS members(file_id BLOB, seq INTEGER, sha BLOB, size INTEGER, body TEXT, PRIMARY KEY(file_id, seq)) WITHOUT ROWID')
        stored = self.db.execute("SELECT value FROM meta WHERE key='engine'").fetchone()
        if stored and stored[0] != str(ENGINE):
            self.db.execute('DELETE FROM files')
            self.db.execute('DELETE FROM members')
        self.db.execute('DELETE FROM members WHERE file_id NOT IN (SELECT id FROM files)')       # left by a run that was killed mid-archive
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('engine', ?)", (str(ENGINE),))
        self.pending = 0
        self.db.execute('BEGIN')

    def index(self):
        return {row[0]: (row[1], row[2]) for row in self.db.execute('SELECT id, size, mtime_ns FROM files')}

    def put(self, entry, result):
        self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)',
                        (entry.id, entry.root, os.fsencode(entry.rel), entry.size, stored_ns(entry.mtime_ns), result.get('status', 'ok'),
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

    def add_members(self, identifier, start, batch):
        rows = []
        for offset, member in enumerate(batch):
            body = {key: value for key, value in member.items() if key not in ('s', 'h')}
            rows.append((identifier, start + offset, bytes.fromhex(member['h']) if member.get('h') else None, member['s'],
                         json.dumps(body, ensure_ascii=False, separators=(',', ':'))))
        self.db.executemany('INSERT OR REPLACE INTO members VALUES(?,?,?,?,?)', rows)
        self.pending += 1
        if self.pending >= 200:
            self.commit()

    def drop_members(self, identifier, keep=0):
        """Remove the members of a file from position keep on; everything when keep is 0."""
        self.db.execute('DELETE FROM members WHERE file_id=? AND seq>=?', (identifier, keep))

    def members(self, identifier):
        """The members of one archive in order, one at a time."""
        for size, sha, body in self.db.execute('SELECT size, sha, body FROM members WHERE file_id=? ORDER BY seq', (identifier,)):
            member = json.loads(body)
            member['s'] = size
            if sha is not None:
                member['h'] = sha.hex()
            yield member

    def member_owners(self):
        return {row[0] for row in self.db.execute('SELECT DISTINCT file_id FROM members')}

    def member_hashes(self, identifier):
        for (sha,) in self.db.execute('SELECT sha FROM members WHERE file_id=? AND sha IS NOT NULL ORDER BY seq', (identifier,)):
            yield sha.hex()

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)', (key, json.dumps(value)))

    def get_meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def reconcile(self, current):
        """Remember the settings of this run; drop the finished rows that a change of settings can change. Returns the number dropped.

        A change of the read-relevant limits or of the extraction tools drops the rows that were cut short (timeout, crashed, io_error,
        partly or failed); a change of the mailbox roots drops the .mbox rows, whose known messages depend on them. The rows that
        read completely stay."""
        stored = self.get_meta('fingerprint')
        self.set_meta('fingerprint', current)
        if not stored:
            return 0
        drop = set()
        if stored.get('limits') != current['limits'] or stored.get('tools') != current['tools']:
            drop |= self.stale(unfinished)
        if stored.get('mailboxes') != current['mailboxes']:
            drop |= self.stale(lambda result: 'mbox' in result)
        for identifier in drop:
            self.db.execute('DELETE FROM files WHERE id=?', (identifier,))
            self.db.execute('DELETE FROM members WHERE file_id=?', (identifier,))
        return len(drop)

    def stale(self, test):
        """Identifiers of the rows whose result passes test. Only rows that can match are parsed."""
        found = set()
        query = ("SELECT id, result FROM files WHERE status != 'ok' OR instr(result, '\"arch\":') > 0 OR instr(result, '\"mbox\":') > 0 "
                 "OR instr(result, '\"unreadable\":') > 0")
        for identifier, result in self.db.execute(query).fetchall():
            if test(json.loads(result)):
                found.add(identifier)
        return found

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
        if identity is not None and portable.identity(info) != tuple(identity):
            os.close(descriptor)
            return None, 'changed'
    except OSError:
        os.close(descriptor)
        return None, 'io_error'
    return descriptor, None


def _open_regular(path, identity):
    descriptor, problem = open_verified(path, identity)
    return (os.fdopen(descriptor, 'rb'), None) if descriptor is not None else (None, problem)


def _facts_for_stream(extension, stream, text, size, settings, deadline):
    """Facts of a loose document. Text formats come from their first bytes, other formats from the open file; no copy is held."""
    if extension in spec_content.TEXT_HEAD_EXTENSIONS:
        return spec_content.content_facts(extension, text, text_pages=1, deadline=deadline)
    if extension in ('pdf', 'tif', 'tiff'):
        stream.seek(0)
        return spec_content.content_facts(extension, stream, text_pages=1, deadline=deadline)
    if size > settings['member_parse_bytes']:
        return {}
    if extension in spec_content.STREAM_EXTENSIONS:
        stream.seek(0)
        return spec_content.content_facts(extension, stream, text_pages=1, deadline=deadline)
    if extension in IMAGE_SHEET_EXTENSIONS:
        return spec_content.content_facts(extension, b'', text_pages=1, deadline=deadline)       # the facts do not depend on the bytes
    return {}


def _unchanged(descriptor, task):
    """False when the open file changed in size or modification time since the inventory."""
    try:
        info = os.fstat(descriptor)
    except OSError:
        return False
    return (info.st_size, info.st_mtime_ns) == (task['identity'][2], task['identity'][3])


def watched_lines(stream):
    """The lines of a stream; a worker whose parent died leaves within a few thousand lines."""
    for number, line in enumerate(stream):
        if number % 4096 == 0:
            spec_archives.check_parent()
        yield line


def process_task(task, settings, stage, descriptors, emit=None):
    """Hash one file and extract its facts. The file arrives as an open descriptor, never as a path.

    Archive members go to emit(start, batch) when it is given and into the result otherwise.
    Returns a JSON-ready dictionary and never raises."""
    name = task['name']
    extension = raw_extension(name)
    streams = [os.fdopen(descriptor, 'rb') for descriptor in descriptors]
    stream = streams[0]
    watch = os.dup(stream.fileno())            # a parser may close the stream; this one still answers for the final check
    try:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return {'status': 'io_error'}
        if portable.identity(info) != tuple(task['identity']):
            return {'status': 'changed'}
        size = task['identity'][2]
        text_head = extension in spec_content.TEXT_HEAD_EXTENSIONS and not task.get('hash_only')
        digest = hashlib.sha256()
        text = bytearray()
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
            spec_archives.check_parent()
            if text_head and len(text) < spec_content.TEXT_HEAD_BYTES:
                text.extend(block[:spec_content.TEXT_HEAD_BYTES - len(text)])
        if total != size:
            return {'status': 'changed'}
        sha = digest.hexdigest()
        stage(sha)
        result = {'status': 'ok', 'sha': sha}
        if task.get('hash_only'):
            return result if _unchanged(watch, task) else {'status': 'changed'}          # credential and key files: only the checksum
        now = time.monotonic()
        deadline = now + settings['parse_seconds']
        try:
            role = task.get('role')
            kind = spec_archives.archive_kind(name, head)
            if extension == 'eml' and size <= settings['max_message_bytes']:
                stream.seek(0)
                result['mail'] = spec_content.analyze_message(stream.read(), deadline=deadline)
            elif extension == 'eml':
                result['unreadable'] = True
            elif extension == 'mbox':
                stream.seek(0)
                result['mbox'] = spec_content.analyze_mbox(watched_lines(stream), settings.get('known', {}).get(task.get('mailbox'), ()), settings['max_message_bytes'],
                                                           deadline=now + settings['archive_seconds'])
            elif role == 'leader' and task.get('style') == 'num':
                result['arch'] = _read_archive(task, streams, size, name, None, settings, emit)
            elif role == 'orphan':
                pass                               # a volume whose first part is missing cannot be opened; it is hashed and counted
            elif role != 'follower' and kind:
                result['arch'] = _read_archive(task, streams, size, name, kind, settings, emit)
            else:
                text_bytes = bytes(text)
                if size > spec_content.TEXT_HEAD_BYTES:
                    text_bytes = spec_content.trim_partial_character(text_bytes)
                result.update(_facts_for_stream(extension, stream, text_bytes, size, settings, deadline))
        except Exception:
            result['unreadable'] = True
        return result if _unchanged(watch, task) else {'status': 'changed'}
    finally:
        os.close(watch)
        for item in streams:
            item.close()


def _read_archive(task, streams, size, name, kind, settings, emit=None):
    """Read an archive from its open volumes (the first is the leader); temporary files live in the folder the parent made for the task."""
    folder = tempfile.mkdtemp(dir=task.get('temp') or settings['temp_dir'])
    opened = []
    sent = [0]

    def sink(batch):
        emit(sent[0], batch)
        sent[0] += len(batch)
    try:
        limits = {'max_expanded_bytes': settings['max_archive_bytes'], 'max_ratio': settings['max_archive_ratio'],
                  'max_depth': settings['max_depth'], 'member_parse_bytes': settings['member_parse_bytes']}
        reader = spec_archives.Reader(folder, tools=settings['tools'], limits=limits, deadline=time.monotonic() + settings['archive_seconds'],
                                      sink=sink if emit else None, flush=MEMBER_BATCH)
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
        reader.flush_members()
        result = {'kind': kind, 'ok': bool(ok), 'opened': reader.opened, 'partly': reader.partly, 'lost': reader.lost,
                  'failed': dict(reader.failed), 'used': dict(reader.used), 'hidden': reader.hidden, 'links': reader.links,
                  'timeout': reader.timed_out, 'n': reader.member_count}
        if not emit:
            result['members'] = reader.members
        return result
    finally:
        for item in opened:
            item.close()
        portable.remove_tree(folder)


def worker_main(connection, settings):
    spec_archives.install_termination_handler(settings.get('parent_pid'))
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
            descriptors = [portable.receive_descriptor(connection) for _ in range(task['handles'])]
            try:
                result = process_task(task, settings, lambda sha: connection.send(('hash', sha)), descriptors,
                                      emit=lambda start, batch: connection.send(('members', (start, batch))))
            except Exception:
                result = {'status': 'error'}
            connection.send(('done', result))
    finally:
        connection.close()


# --------------------------------------------------------------------------- worker pool
class Worker:
    def __init__(self, context, settings, target=None, job=None):
        parent, child = context.Pipe()
        self.target = target or worker_main
        self.process = context.Process(target=self.target, args=(child, settings), daemon=True)
        self.process.start()
        if portable.WINDOWS:
            portable.add_to_job(job, self.process.sentinel)      # the worker and its tools die with this process, even when it is killed
        child.close()
        self.connection = parent
        self.task = None
        self.deadline = None
        self.sha = None
        self.members = []

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


def release(task):
    """Remove the private folder the parent made for a task, whatever became of the task."""
    temp = task.pop('temp', None) if isinstance(task, dict) else None
    if temp:
        portable.remove_tree(temp)


def run_pool(tasks, count, settings, on_result, target=None, on_members=None):
    """Run tasks on worker processes. A worker that exceeds its deadline or dies is replaced; the file is recorded as such.

    Each task that may hold an archive gets a private temporary folder, made here and removed here once the task is over.
    Archive members arrive in batches and go to on_members(task, start, batch), or into the result when it is not given."""
    context = multiprocessing.get_context('spawn')
    settings = {**settings, 'parent_pid': os.getpid()}          # a worker leaves when this process is gone
    pending = deque(tasks)
    job = portable.kill_on_close_job() if portable.WINDOWS else None
    workers = []

    def replace(worker):
        worker.kill()
        worker.__init__(context, settings, worker.target, job)
    try:
        for _ in range(max(1, min(count, len(pending) or 1))):
            workers.append(Worker(context, settings, target or worker_main, job))
        while pending or any(worker.task for worker in workers):
            for worker in workers:
                while worker.task is None and pending:
                    task = pending.popleft()
                    descriptors, problem = open_volumes(task)
                    if problem:
                        on_result(task, {'status': problem})
                        continue
                    if settings.get('temp_dir') and spec_archives.may_hold_archive(task['name'], task.get('role')):
                        task['temp'] = tempfile.mkdtemp(dir=settings['temp_dir'])
                    worker.task, worker.sha, worker.members = task, None, []
                    worker.deadline = time.monotonic() + max(settings['hash_floor_seconds'], task['identity'][2] / 1e6 * 2)
                    failed_to_send = False
                    try:
                        message = {key: value for key, value in task.items() if key not in ('path', 'parts', '_attempt')}
                        message['handles'] = len(descriptors)
                        worker.connection.send(message)
                        for descriptor in descriptors:
                            portable.send_descriptor(worker.connection, descriptor, worker.process.pid)
                    except (OSError, ValueError, RuntimeError):
                        failed_to_send = True          # the worker died at start; RuntimeError is the missing hand-over acknowledgement
                    finally:
                        for descriptor in descriptors:
                            os.close(descriptor)
                    if failed_to_send:
                        worker.task = None
                        replace(worker)
                        release(task)
                        if task.get('_attempt', 0) < 1:
                            task['_attempt'] = 1
                            pending.appendleft(task)
                        else:
                            on_result(task, {'status': 'crashed'})
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
                        if message == 'members':
                            if on_members:
                                on_members(worker.task, payload[0], payload[1])
                            else:
                                worker.members.extend(payload[1])
                            continue
                        finished = payload
                elif time.monotonic() > worker.deadline:
                    finished = {'status': 'timeout'}
                if finished is None:
                    continue
                task = worker.task
                if finished.get('status') == 'crashed' and not worker.sha and task.get('_attempt', 0) < 1:
                    # The worker died before it read the file, for example at start: the file gets one more try on a new worker.
                    # macOS notices this when the file is handed over; Windows and Linux only when the worker's pipe closes.
                    replace(worker)
                    worker.task = None
                    release(task)
                    task['_attempt'] = 1
                    pending.appendleft(task)
                    continue
                if finished.get('status') in ('crashed', 'timeout'):
                    if worker.sha:
                        finished['sha'] = worker.sha
                    replace(worker)
                elif worker.members and isinstance(finished.get('arch'), dict):
                    finished['arch']['members'] = worker.members
                worker.task = None
                release(task)
                on_result(task, finished)
    finally:
        for worker in workers:
            try:
                worker.connection.send(None)
            except (OSError, ValueError):
                pass
        for worker in workers:
            worker.kill()
        for worker in workers:
            if worker.task is not None:
                release(worker.task)
        portable.close_job(job)


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


def installed_ram():
    """Bytes of installed memory, or None when the system does not say."""
    return portable.physical_memory()


_UNKNOWN = object()


def default_workers(cpus=None, ram_bytes=_UNKNOWN):
    """One worker per spare processor, up to 8, and at most one per 2 GiB of memory."""
    count = max(1, min(8, (cpus or os.cpu_count() or 2) - 1 or 1))
    ram = installed_ram() if ram_bytes is _UNKNOWN else ram_bytes
    if ram:
        count = min(count, max(1, int(ram // (2 * GIB))))
    return count


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
    lock = None
    previous_handler = None
    temp_dir = output / 'spec-tmp'
    try:
        if threading.current_thread() is threading.main_thread():
            previous_handler = signal.signal(signal.SIGTERM, _terminate)      # a terminated run stops its workers and tools and cleans up
        lock = acquire_lock(descriptor)
        verify_output_directory(output, descriptor)
        if temp_dir.exists():                       # left by a run that was killed; extracted data must not outlive it
            portable.remove_tree(temp_dir)
        os.mkdir(temp_dir, 0o700)
        settings['temp_dir'] = str(temp_dir)
        entries, walk_stats = walk_roots(roots)
        cache = Cache(descriptor, output / CACHE_NAME)
        cache.set_meta('roots', [str(root) for root in roots])
        cache.set_meta('config', {key: value for key, value in cfg.items()})
        cache.reconcile(fingerprint(settings, cfg))
        known = cache.index()
        by_id = {entry.id: entry for entry in entries}
        todo, done_files, done_bytes = [], 0, 0
        for entry in entries:
            if known.get(entry.id) == (entry.size, stored_ns(entry.mtime_ns)):
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
                    'size': entry.size, 'hash_only': entry.credential}
            if extra:
                task.update(extra)
            return task

        processed = [0]

        holders = cache.member_owners()        # files that have member rows now; a new result must replace or remove them

        def keep_members(task, start, batch):
            holders.add(task['id'])
            cache.add_members(task['id'], start, batch)

        def handle(task, result):
            entry = by_id[task['id']]
            arch = result.get('arch')
            if entry.id in holders:
                kept = arch.get('n', 0) if isinstance(arch, dict) and 'members' not in arch else 0
                cache.drop_members(entry.id, kept)          # nothing of a failed or older read stays; a finished read keeps its own rows
                if not kept:
                    holders.discard(entry.id)
            cache.put(entry, result)
            progress.add(entry.size)
            processed[0] += 1
            if stop_after is not None and processed[0] >= stop_after:
                raise SpecInterrupted()

        first = [task_for(entry) for entry in todo if raw_extension(entry.name) != 'mbox']
        second = [entry for entry in todo if raw_extension(entry.name) == 'mbox']
        try:
            if first:
                run_pool(first, count, settings, handle, on_members=keep_members)
            if second:
                # The message ids of each mailbox are built once and reach every worker once, with its settings, not with every task.
                known_ids = mailbox_message_ids(cache, entries, cfg)
                tasks = [task_for(entry, {'mailbox': mailbox_of(entry, cfg)}) for entry in second]
                run_pool(tasks, count, {**settings, 'known': {index: frozenset(ids) for index, ids in known_ids.items() if index is not None}}, handle,
                         on_members=keep_members)
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
        if lock is not None:                    # a refused run must not touch the temporary data of the run that holds the lock
            portable.remove_tree(temp_dir)
            release_lock(descriptor, lock)
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
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
    """Message keys (Message-ID hash, else body key) of the loose .eml files of each mailbox, for skipping duplicate messages inside .mbox files."""
    by_id = {entry.id: entry for entry in entries}
    found = defaultdict(set)
    for identifier, result in cache.results(wanted=by_id, suffix='.eml'):
        mail = result.get('mail')
        entry = by_id[identifier]
        if mail and spec_content.message_key(mail) and raw_extension(entry.name) == 'eml':
            found[mailbox_of(entry, cfg)].add(spec_content.message_key(mail))
    return found
