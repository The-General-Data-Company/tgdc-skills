"""Whole-run behavior of the archive and worker safety measures: lock, stale folders, member storage, truncated and orphan archives."""
import fcntl
import io
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_samples as S

NOW = datetime(2026, 10, 25)
LOG = open(os.devnull, 'w')
SEVEN = shutil.which('7zz') or shutil.which('7z')
BSDTAR = shutil.which('bsdtar') or ('/usr/bin/bsdtar' if Path('/usr/bin/bsdtar').exists() else None)


def run(source, out, workers=2, tools=None, **kwargs):
    return formatspec.run_spec([source], out, workers=workers, tools={} if tools is None else tools, report_date=NOW, log=LOG, **kwargs)


def cache_rows(out, query):
    db = sqlite3.connect(str(Path(out) / 'spec-cache.private.sqlite'))
    try:
        return db.execute(query).fetchall()
    finally:
        db.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()
        self.out = self.base / 'out'

    def zip_of(self, count):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for number in range(count):
                archive.writestr(f'd{number % 3}/f{number}.txt', f'member number {number} of the archive '.encode() * 3)
        return buffer.getvalue()


class LockAndFolderTests(Base):
    def test_a_second_run_in_the_same_output_folder_is_refused_and_the_lock_is_released_after_a_run(self):
        (self.data / 'a.txt').write_bytes(S.EN.encode() * 2)
        self.out.mkdir()
        descriptor = os.open(self.out / formatspec.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(formatspec.SpecConfigError) as caught:
                run(self.data, self.out)
            self.assertIn('another', str(caught.exception).lower())
            self.assertFalse((self.out / 'spec.json').exists())
        finally:
            os.close(descriptor)
        run(self.data, self.out)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ['spec-cache.private.sqlite', 'spec.json'])

    def test_a_refused_second_run_leaves_the_temporary_data_of_the_running_run_alone(self):
        (self.data / 'a.txt').write_bytes(S.EN.encode() * 2)
        self.out.mkdir()
        held = self.out / 'spec-tmp' / 'task1'
        held.mkdir(parents=True)
        (held / 'member.bin').write_bytes(b'extracted by the run that holds the lock')
        descriptor = os.open(self.out / formatspec.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(formatspec.SpecConfigError):
                run(self.data, self.out)
            self.assertTrue((held / 'member.bin').is_file())
        finally:
            os.close(descriptor)

    def test_the_lock_is_released_when_a_run_fails(self):
        (self.data / 'a.txt').write_bytes(S.EN.encode() * 2)
        with mock.patch.object(formatspec, 'walk_roots', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                run(self.data, self.out)
        run(self.data, self.out)

    def test_stale_temporary_data_of_an_earlier_killed_run_is_removed_at_the_start(self):
        (self.data / 'a.txt').write_bytes(S.EN.encode() * 2)
        stale = self.out / 'spec-tmp' / 'tmp123' / 'out'
        stale.mkdir(parents=True)
        (stale / 'password list.txt').write_bytes(b'left behind by a killed run')
        seen = []
        original = formatspec.run_pool

        def spy(*args, **kwargs):
            seen.append(sorted(p.name for p in (self.out / 'spec-tmp').iterdir()))
            return original(*args, **kwargs)
        formatspec.run_pool = spy
        try:
            run(self.data, self.out)
        finally:
            formatspec.run_pool = original
        self.assertEqual(seen, [[]])
        self.assertFalse((self.out / 'spec-tmp').exists())


class MemberStorageTests(Base):
    def test_archive_members_live_in_a_side_table_and_the_archive_row_holds_counters_only(self):
        (self.data / 'many.zip').write_bytes(self.zip_of(30))
        (self.data / 'plain.txt').write_bytes(S.EN.encode() * 2)
        spec = run(self.data, self.out)
        row = json.loads(cache_rows(self.out, "select result from files where path = cast('many.zip' as blob)")[0][0])
        self.assertNotIn('members', row['arch'])
        self.assertEqual((row['arch']['n'], row['arch']['opened'], row['arch']['kind']), (30, 1, 'zip'))
        members = cache_rows(self.out, 'select seq, length(sha), size, body from members order by seq')
        self.assertEqual([m[0] for m in members], list(range(30)))
        self.assertEqual({m[1] for m in members}, {32})                          # fixed-size hash column
        self.assertEqual(spec['parts']['archive_contents']['files'], 30)
        self.assertEqual(spec['totals']['files'], 32)

    def test_a_changed_archive_replaces_its_members_and_a_vanished_one_leaves_none(self):
        archive = self.data / 'many.zip'
        archive.write_bytes(self.zip_of(30))
        run(self.data, self.out)
        archive.write_bytes(self.zip_of(7))
        spec = run(self.data, self.out)
        self.assertEqual(cache_rows(self.out, 'select count(*) from members')[0][0], 7)
        self.assertEqual(spec['parts']['archive_contents']['files'], 7)
        archive.write_bytes(b'now just a text file ' * 10)
        os.utime(archive, ns=(1, 1))
        run(self.data, self.out)
        self.assertEqual(cache_rows(self.out, 'select count(*) from members')[0][0], 0)

    def test_members_of_files_the_cache_no_longer_knows_are_dropped_on_open(self):
        (self.data / 'a.txt').write_bytes(S.EN.encode() * 2)
        run(self.data, self.out)
        db = sqlite3.connect(str(self.out / 'spec-cache.private.sqlite'))
        db.execute("insert into members values(x'00', 0, null, 1, '{}')")
        db.commit()
        db.close()
        run(self.data, self.out)
        self.assertEqual(cache_rows(self.out, 'select count(*) from members')[0][0], 0)


@unittest.skipUnless(BSDTAR, 'needs bsdtar')
class BsdtarDamagedArchiveRunTests(Base):
    def test_a_truncated_member_that_bsdtar_pads_is_neither_unique_nor_new_content(self):
        full = io.BytesIO()
        with zipfile.ZipFile(full, 'w') as archive:
            archive.writestr('a.txt', S.EN.encode() * 2)
            archive.writestr('b.bin', os.urandom(300_000))
        (self.data / 'cut.zip').write_bytes(full.getvalue()[:150_000])
        spec = run(self.data, self.out, tools={'bsdtar': BSDTAR})
        contents = spec['parts']['archive_contents']
        self.assertEqual(contents['files'], 2)
        self.assertEqual(contents['new_files'], 1)                               # only a.txt
        self.assertEqual(spec['totals']['unique_contents'], 2)                   # the zip file and a.txt
        self.assertEqual(spec['totals']['hashed_files'], 2)
        self.assertEqual(spec['methodology']['archives']['partly_readable'], 1)


@unittest.skipUnless(SEVEN, 'needs 7zz or 7z')
class DamagedArchiveRunTests(Base):
    tools = property(lambda self: {'7zz' if shutil.which('7zz') else '7z': SEVEN})

    def test_a_truncated_member_is_neither_unique_nor_new_content(self):
        payload = os.urandom(300_000)
        full = io.BytesIO()
        with zipfile.ZipFile(full, 'w') as archive:
            archive.writestr('a.txt', S.EN.encode() * 2)
            archive.writestr('b.bin', payload)
        (self.data / 'cut.zip').write_bytes(full.getvalue()[:150_000])
        spec = run(self.data, self.out, tools=self.tools)
        contents = spec['parts']['archive_contents']
        self.assertEqual(contents['files'], 2)
        self.assertEqual(contents['new_files'], 1)                               # only a.txt
        self.assertEqual(spec['totals']['unique_contents'], 2)                   # the zip file and a.txt
        self.assertEqual(spec['totals']['hashed_files'], 2)
        self.assertEqual(spec['methodology']['archives']['partly_readable'], 1)

    def test_a_header_encrypted_archive_is_reported_as_not_opened(self):
        source = self.base / 'src'
        source.mkdir()
        (source / 'a.txt').write_bytes(S.EN.encode() * 2)
        subprocess.run([SEVEN, 'a', '-bd', '-y', '-psecret', '-mhe=on', str(self.data / 'locked.7z'), '.'], cwd=source, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        spec = run(self.data, self.out, tools=self.tools)
        archives = spec['methodology']['archives']
        self.assertEqual((archives['opened'], archives['not_opened']), (0, {'7z:password protected': 1}))

    def test_a_tool_stopped_by_the_parent_leaves_no_process_or_folder_behind(self):
        pids = self.base / 'pids.txt'
        tool = self.base / 'fake7zz'
        tool.write_text(f'''#!/bin/sh
if [ "$1" = "l" ]; then
  printf 'Path = a.txt\\nSize = 10\\nFolder = -\\nAttributes = A\\nEncrypted = -\\n\\n'
  exit 0
fi
sleep 120 &
echo $! >> "{pids}"
echo $$ >> "{pids}"
wait
''')
        os.chmod(tool, 0o755)
        (self.data / 'x.7z').write_bytes(b"7z\xbc\xaf'\x1c" + b'\x00' * 200)
        # The parent gives up on the worker (grace below zero) before the reader's own deadline would stop the tool.
        started = time.monotonic()
        spec = run(self.data, self.out, workers=1, tools={'7zz': str(tool)}, limits={'archive_seconds': 4, 'grace_seconds': -2})
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(spec['methodology']['time_limited_files'], 1)
        started_pids = [int(p) for p in pids.read_text().split()]
        end = time.monotonic() + 5
        while time.monotonic() < end and any(_alive(pid) for pid in started_pids):
            time.sleep(0.05)
        self.assertEqual([pid for pid in started_pids if _alive(pid)], [])
        self.assertFalse((self.out / 'spec-tmp').exists())


    def test_a_terminated_run_stops_workers_and_tools_and_removes_its_folder(self):
        pids = self.base / 'pids.txt'
        tool = self.base / 'fake7zz'
        tool.write_text(f'''#!/bin/sh
if [ "$1" = "l" ]; then
  printf 'Path = a.txt\\nSize = 10\\nFolder = -\\nAttributes = A\\nEncrypted = -\\n\\n'
  exit 0
fi
sleep 120 &
echo $! >> "{pids}"
echo $$ >> "{pids}"
wait
''')
        os.chmod(tool, 0o755)
        (self.data / 'x.7z').write_bytes(b"7z\xbc\xaf'\x1c" + b'\x00' * 200)
        driver = self.base / 'driver.py'
        driver.write_text(f'''
import sys
sys.path.insert(0, {str(HERE.parent / 'scripts')!r})
import formatspec

if __name__ == '__main__':
    try:
        formatspec.run_spec([{str(self.data)!r}], {str(self.out)!r}, workers=1, tools={{'7zz': {str(tool)!r}}}, log=open('/dev/null', 'w'))
    except formatspec.SpecInterrupted:
        sys.exit(3)
''')
        process = subprocess.Popen([sys.executable, str(driver)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.monotonic() + 60
            while time.monotonic() < end and not (pids.exists() and len(pids.read_text().split()) == 2):
                time.sleep(0.05)
            started = [int(p) for p in pids.read_text().split()]
            self.assertEqual(len(started), 2, 'the tool must be running before the run is terminated')
            process.terminate()
            self.assertEqual(process.wait(timeout=60), 3)
        finally:
            if process.poll() is None:
                process.kill()
        end = time.monotonic() + 10
        while time.monotonic() < end and any(_alive(pid) for pid in started):
            time.sleep(0.05)
        self.assertEqual([pid for pid in started if _alive(pid)], [])
        self.assertFalse((self.out / 'spec-tmp').exists())
        self.assertFalse((self.out / formatspec.LOCK_NAME).exists())


class OrphanedWorkerTests(Base):
    def test_a_worker_whose_parent_was_killed_stops_its_tool_and_exits(self):
        pids = self.base / 'pids.txt'
        tool = self.base / 'fake7zz'
        tool.write_text(f'''#!/bin/sh
if [ "$1" = "l" ]; then
  printf 'Path = a.txt\\nSize = 10\\nFolder = -\\nAttributes = A\\nEncrypted = -\\n\\n'
  exit 0
fi
sleep 120 &
echo $! >> "{pids}"
echo $$ >> "{pids}"
echo $PPID >> "{pids}"
wait
''')
        os.chmod(tool, 0o755)
        (self.data / 'x.7z').write_bytes(b"7z\xbc\xaf'\x1c" + b'\x00' * 200)
        driver = self.base / 'driver.py'
        driver.write_text(f'''
import sys
sys.path.insert(0, {str(HERE.parent / 'scripts')!r})
import formatspec

if __name__ == '__main__':
    formatspec.run_spec([{str(self.data)!r}], {str(self.out)!r}, workers=1, tools={{'7zz': {str(tool)!r}}}, log=open('/dev/null', 'w'))
''')
        process = subprocess.Popen([sys.executable, str(driver)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = []

        def clean_up():
            for pid in started:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            if process.poll() is None:
                process.kill()
        self.addCleanup(clean_up)
        end = time.monotonic() + 60
        while time.monotonic() < end and not (pids.exists() and len(pids.read_text().split()) == 3):
            time.sleep(0.05)
        started.extend(int(p) for p in pids.read_text().split())
        self.assertEqual(len(started), 3, 'the tool must be running before its parent is killed')
        process.kill()
        process.wait(timeout=60)
        end = time.monotonic() + 15
        while time.monotonic() < end and any(_alive(pid) for pid in started):
            time.sleep(0.05)
        self.assertEqual([pid for pid in started if _alive(pid)], [], 'the tool, its child and the worker must all be gone')


class VolumeAndLinkTests(Base):
    def test_orphan_volumes_and_archive_links_are_reported(self):
        for name in ['p.part2.rar', 'p.part3.rar', 'q.r00']:
            (self.data / name).write_bytes(b'volume without a first part')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            link = zipfile.ZipInfo('link.txt')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '/etc/passwd')
            archive.writestr('real.txt', S.EN.encode() * 2)
        (self.data / 'l.zip').write_bytes(buffer.getvalue())
        spec = run(self.data, self.out)
        archives = spec['methodology']['archives']
        self.assertEqual((archives['orphan_volumes'], archives['links_skipped']), (3, 1))
        self.assertEqual(archives['not_opened'], {})
        self.assertEqual(spec['parts']['archive_contents']['files'], 1)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == '__main__':
    unittest.main()
