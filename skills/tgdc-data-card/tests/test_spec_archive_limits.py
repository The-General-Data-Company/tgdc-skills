"""Archive safety: bounds before extraction, encrypted and truncated archives, depth, credential members, tool process groups."""
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import spec_archives as A
import spec_samples as S
import spec_taxonomy as T
from spec_pool_targets import hold_a_tool

SEVEN = shutil.which('7zz') or shutil.which('7z')
BSDTAR = shutil.which('bsdtar') or ('/usr/bin/bsdtar' if Path('/usr/bin/bsdtar').exists() else None)
CREDENTIAL_MEMBERS = ['home/.ssh/id_rsa', 'home/.ssh/id_ed25519', 'keys/service-account.json', 'keys/oauth_client.json', 'token.txt', 'tokens.json',
                      'api_key.txt', 'My Passwords/list.txt', 'ACME Credentials/x.txt']


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def script(path, text):
    Path(path).write_text('#!/bin/sh\n' + text)
    os.chmod(path, 0o755)
    return str(path)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pids, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not any(alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.work = self.base / 'work'
        self.work.mkdir()

    def reader(self, tools, **limits):
        return A.Reader(str(self.work), tools=tools, limits=limits or None)

    def open(self, reader, kind, name, data):
        return reader.open_archive(kind, name, io.BytesIO(data), 1, '', compressed=len(data))

    def seven(self, name, files, *flags):
        source = self.base / ('src-' + name)
        for rel, data in files:
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        target = self.base / name
        subprocess.run([SEVEN, 'a', '-bd', '-y', *flags, str(target), '.'], cwd=source, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        return target.read_bytes()


class PythonReaderTests(Base):
    def test_every_path_component_is_checked_for_credentials(self):
        files = [(name, b'secret ' * 40) for name in CREDENTIAL_MEMBERS] + [('docs/ok.txt', S.EN.encode() * 2)]
        reader = self.reader({})
        self.assertTrue(self.open(reader, 'zip', 'x.zip', zip_bytes(files)))
        by_name = {m['n']: m for m in reader.members}
        self.assertEqual(sorted(by_name), sorted(CREDENTIAL_MEMBERS + ['docs/ok.txt']))
        for name in CREDENTIAL_MEMBERS:
            with self.subTest(name=name):
                self.assertTrue(by_name[name]['cred'])
                self.assertNotIn('lang', by_name[name])          # hashed in memory at most, never parsed
        self.assertNotIn('cred', by_name['docs/ok.txt'])
        self.assertEqual(by_name['docs/ok.txt']['lang']['lang'], 'en')

    def test_hidden_components_are_skipped_and_counted_but_a_dot_prefix_is_not_hidden(self):
        files = [('.git/config', b'x'), ('.hidden.txt', b'x'), ('a/.cache/y.txt', b'x'), ('./plain/ok.txt', S.EN.encode() * 2)]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        reader = self.reader({})
        self.assertTrue(self.open(reader, 'tar', 'x.tar', buffer.getvalue()))
        self.assertEqual([m['n'].removeprefix('./') for m in reader.members], ['plain/ok.txt'])
        self.assertEqual(reader.hidden, 3)

    def test_an_archive_below_the_depth_limit_is_recorded_as_too_deep_and_partly_read(self):
        data = b'leaf text ' * 20
        for level in range(9):
            data = zip_bytes([('level.zip' if level else 'leaf.txt', data)])
        reader = self.reader({})
        self.assertTrue(self.open(reader, 'zip', 'top.zip', data))
        self.assertEqual(reader.opened, 5)
        self.assertEqual(dict(reader.failed), {'zip:too deep': 1})
        self.assertEqual(reader.partly, 1)
        self.assertEqual(len(reader.members), 5)
        self.assertEqual(sum(1 for m in reader.members if m.get('cont')), 4)
        unopened = [m for m in reader.members if m.get('arch') and not m.get('cont')]
        self.assertEqual(len(unopened), 1)                  # its contents are not counted, so the file itself is content

    def test_a_deep_archive_is_found_by_its_signature_too(self):
        inner = zip_bytes([('x.txt', b'text ' * 30)])
        data = inner
        for _ in range(5):
            data = zip_bytes([('blob', data)])
        reader = self.reader({}, max_depth=2)
        self.open(reader, 'zip', 'top.zip', data)
        self.assertEqual(dict(reader.failed), {'zip:too deep': 1})
        self.assertEqual(reader.partly, 1)

    def test_zip_symbolic_links_are_skipped_and_counted(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            link = zipfile.ZipInfo('link.txt')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '/etc/passwd')
            archive.writestr('real.txt', S.EN.encode() * 2)
        reader = self.reader({})
        self.assertTrue(self.open(reader, 'zip', 'x.zip', buffer.getvalue()))
        self.assertEqual([m['n'] for m in reader.members], ['real.txt'])
        self.assertEqual(reader.links, 1)
        self.assertEqual(reader.partly, 0)

    def test_members_go_to_a_sink_in_batches_and_are_not_kept(self):
        files = [(f'd/f{number}.txt', f'member number {number} '.encode() * 3) for number in range(25)]
        batches = []
        reader = A.Reader(str(self.work), tools={}, sink=lambda batch: batches.append(list(batch)), flush=10)
        self.assertTrue(reader.open_archive('zip', 'x.zip', io.BytesIO(zip_bytes(files)), 1, '', compressed=1000))
        reader.flush_members()
        self.assertEqual([len(batch) for batch in batches], [10, 10, 5])
        self.assertEqual(reader.members, [])
        self.assertEqual(reader.member_count, 25)
        self.assertEqual(sorted(m['n'] for batch in batches for m in batch), sorted(name for name, _ in files))

    def test_the_result_cache_is_bounded(self):
        files = [(f'f{number}.txt', f'member {number} '.encode() * 3) for number in range(40)]
        reader = self.reader({})
        with mock.patch.object(A, 'FACTS_CACHE_LIMIT', 8):
            self.open(reader, 'zip', 'x.zip', zip_bytes(files))
        self.assertLessEqual(len(reader.cache), 8)
        self.assertEqual(len(reader.members), 40)


@unittest.skipUnless(SEVEN, 'needs 7zz or 7z')
class SevenZipTests(Base):
    tools = property(lambda self: {'7zz' if shutil.which('7zz') else '7z': SEVEN})

    def forbid_extraction(self):
        patcher = mock.patch.object(A.Reader, 'run_guarded', side_effect=AssertionError('the archive must not be extracted'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_ratio_applies_to_the_listing_before_anything_is_extracted(self):
        data = self.seven('zeros.7z', [('zeros.bin', b'\x00' * 3_000_000)])
        self.forbid_extraction()
        reader = self.reader(self.tools, max_ratio=20, ratio_floor_bytes=1000)
        self.assertTrue(self.open(reader, '7z', 'zeros.7z', data))
        self.assertEqual((reader.partly, reader.members), (1, []))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_the_declared_size_over_the_cap_stops_before_extraction_with_bsdtar_too(self):
        if not BSDTAR:
            self.skipTest('needs bsdtar')
        data = self.seven('zeros.7z', [('zeros.bin', b'\x00' * 3_000_000)])
        self.forbid_extraction()
        reader = self.reader({'bsdtar': BSDTAR}, max_expanded_bytes=1_000_000)
        self.assertTrue(self.open(reader, '7z', 'zeros.7z', data))
        self.assertEqual((reader.partly, reader.members), (1, []))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_header_encrypted_archive_is_password_protected_and_not_opened(self):
        data = self.seven('locked.7z', [('a.txt', b'text ' * 30)], '-psecret', '-mhe=on')
        reader = self.reader(self.tools)
        self.assertFalse(self.open(reader, '7z', 'locked.7z', data))
        self.assertEqual(dict(reader.failed), {'7z:password protected': 1})
        self.assertEqual((reader.opened, reader.members), (0, []))

    def test_a_header_encrypted_archive_through_bsdtar_alone(self):
        if not BSDTAR:
            self.skipTest('needs bsdtar')
        data = self.seven('locked.7z', [('a.txt', b'text ' * 30)], '-psecret', '-mhe=on')
        reader = self.reader({'bsdtar': BSDTAR})
        self.assertFalse(self.open(reader, '7z', 'locked.7z', data))
        self.assertEqual(dict(reader.failed), {'7z:password protected': 1})

    def test_content_encrypted_members_are_lost_and_the_archive_is_partly_read(self):
        data = self.seven('locked.7z', [('a.txt', b'text ' * 30), ('b.bin', os.urandom(2000))], '-psecret')
        reader = self.reader(self.tools)
        self.assertTrue(self.open(reader, '7z', 'locked.7z', data))
        self.assertEqual((reader.lost, reader.partly, reader.members), (2, 1, []))

    def test_a_listing_that_cannot_be_read_is_damaged_not_opened(self):
        data = self.seven('ok.7z', [('b.bin', os.urandom(5000))])
        reader = self.reader(self.tools)
        self.assertFalse(self.open(reader, '7z', 'cut.7z', data[:len(data) // 2]))
        self.assertEqual(dict(reader.failed), {'7z:damaged': 1})
        self.assertEqual(reader.opened, 0)

    def test_a_truncated_member_is_short_and_the_archive_partly_read(self):
        payload = os.urandom(300_000)
        full = zip_bytes([('a.txt', S.EN.encode() * 2), ('b.bin', payload)])
        reader = self.reader(self.tools)
        self.assertTrue(self.open(reader, 'zip', 'cut.zip', full[:150_000]))
        by_name = {m['n']: m for m in reader.members}
        self.assertEqual(reader.partly, 1)
        self.assertNotIn('short', by_name['a.txt'])
        self.assertTrue(by_name['b.bin']['short'])
        self.assertLess(by_name['b.bin']['s'], len(payload))

    def test_a_tool_failure_with_complete_members_still_marks_the_archive_partly_read(self):
        wrapper = script(self.base / 'seven-fails', f'"{SEVEN}" "$@"\nstatus=$?\n[ "$1" = x ] && exit 2\nexit $status\n')
        data = self.seven('ok.7z', [('a.txt', S.EN.encode() * 2)])
        reader = self.reader({'7zz': wrapper})
        self.assertTrue(self.open(reader, '7z', 'ok.7z', data))
        self.assertEqual((len(reader.members), reader.partly), (1, 1))

    def test_credential_members_are_never_extracted_and_are_counted_by_name(self):
        files = [(name, b'secret ' * 40) for name in CREDENTIAL_MEMBERS] + [('docs/ok.txt', S.EN.encode() * 2), ('.git/config', b'x')]
        data = self.seven('creds.7z', files)
        log = self.base / 'wanted.log'
        wrapper = script(self.base / 'seven-log', f'for a in "$@"; do case "$a" in @*) cat "${{a#@}}" >> "{log}";; esac; done\nexec "{SEVEN}" "$@"\n')
        reader = self.reader({'7zz': wrapper})
        self.assertTrue(self.open(reader, '7z', 'creds.7z', data))
        by_name = {unicodedata.normalize('NFC', m['n']): m for m in reader.members}
        self.assertEqual(sorted(by_name), sorted(CREDENTIAL_MEMBERS + ['docs/ok.txt']))
        for name in CREDENTIAL_MEMBERS:
            with self.subTest(name=name):
                self.assertTrue(by_name[name]['cred'])
                self.assertEqual(by_name[name]['s'], len(b'secret ' * 40))
                self.assertNotIn('h', by_name[name])
                self.assertNotIn('lang', by_name[name])
        self.assertEqual(log.read_text().split('\n')[:-1], ['docs/ok.txt'])        # only the plain member was ever asked for
        self.assertEqual(reader.hidden, 1)
        self.assertEqual((reader.lost, reader.partly), (0, 0))

    def test_bsdtar_credential_members_are_deleted_unread_before_anything_else(self):
        if not BSDTAR:
            self.skipTest('needs bsdtar')
        files = [(name, b'secret ' * 40) for name in CREDENTIAL_MEMBERS] + [('docs/ok.txt', S.EN.encode() * 2), ('docs/two.txt', S.RO.encode() * 2)]
        data = self.seven('creds.7z', files)
        seen = []
        original = A.Reader.add_member

        def spy(reader, name, *args, **kwargs):
            leftover = [str(p) for p in self.work.rglob('*') if p.is_file() and T.is_sensitive_path(p.relative_to(self.work).parts[2:])]
            seen.append((name, leftover))
            return original(reader, name, *args, **kwargs)
        reader = self.reader({'bsdtar': BSDTAR})
        with mock.patch.object(A.Reader, 'add_member', spy):
            self.assertTrue(self.open(reader, '7z', 'creds.7z', data))
        self.assertEqual(sorted(name for name, _ in seen), ['docs/ok.txt', 'docs/two.txt'])
        self.assertEqual([left for _, left in seen if left], [])
        credential = [m for m in reader.members if m.get('cred')]
        self.assertEqual(len(credential), len(CREDENTIAL_MEMBERS))
        self.assertTrue(all('h' not in m and m['s'] == len(b'secret ' * 40) for m in credential))


class UnconfirmedMemberTests(Base):
    """A tool that exits non-zero may leave a cut-off or damaged member at full size: such a member gets no hash."""

    def assert_unconfirmed(self, member):
        self.assertTrue(member['short'])
        self.assertNotIn('h', member)
        self.assertNotIn('lang', member)

    def cut_zip(self):
        return zip_bytes([('a.txt', S.EN.encode() * 2), ('b.bin', os.urandom(300_000))])[:150_000]

    def damaged_seven(self):
        payload = os.urandom(300_000)
        data = bytearray(self.seven('s.7z', [('a.txt', S.EN.encode() * 2), ('b.bin', payload)], '-mx0'))
        data[bytes(data).index(payload) + 500] ^= 0xFF
        return bytes(data)

    @unittest.skipUnless(BSDTAR, 'needs bsdtar')
    def test_bsdtar_pads_a_truncated_zip_member_and_it_gets_no_hash(self):
        reader = self.reader({'bsdtar': BSDTAR})
        self.assertTrue(self.open(reader, 'zip', 'cut.zip', self.cut_zip()))
        by_name = {m['n']: m for m in reader.members}
        self.assertEqual(by_name['b.bin']['s'], 300_000)             # the file on disk is padded to the declared size
        self.assert_unconfirmed(by_name['b.bin'])
        self.assertIn('h', by_name['a.txt'])
        self.assertNotIn('short', by_name['a.txt'])
        self.assertEqual(by_name['a.txt']['lang']['lang'], 'en')
        self.assertEqual(reader.partly, 1)

    @unittest.skipUnless(BSDTAR and SEVEN, 'needs bsdtar and 7zz')
    def test_bsdtar_names_a_member_with_a_bad_checksum_and_only_that_member_loses_its_hash(self):
        reader = self.reader({'bsdtar': BSDTAR})
        self.assertTrue(self.open(reader, '7z', 'bad.7z', self.damaged_seven()))
        by_name = {m['n']: m for m in reader.members}
        self.assert_unconfirmed(by_name['b.bin'])
        self.assertIn('h', by_name['a.txt'])
        self.assertEqual(reader.partly, 1)

    @unittest.skipUnless(SEVEN, 'needs 7zz or 7z')
    def test_a_member_7zz_reports_with_a_bad_checksum_loses_its_hash(self):
        reader = self.reader({'7zz': SEVEN})
        self.assertTrue(self.open(reader, '7z', 'bad.7z', self.damaged_seven()))
        by_name = {m['n']: m for m in reader.members}
        self.assert_unconfirmed(by_name['b.bin'])
        self.assertIn('h', by_name['a.txt'])
        self.assertEqual(reader.partly, 1)

    def fake_bsdtar(self, listing, extraction):
        return script(self.base / 'fake-bsdtar', f'''if [ "$1" = "-tvf" ]; then
{listing}
  exit 0
fi
out=""
while [ $# -gt 0 ]; do case "$1" in -C) out="$2"; shift;; esac; shift; done
{extraction}
''')

    LINE = "  printf -- '-rw-r--r--  0 0  0  10 Oct 25 16:18 a.txt\\n'"
    BODY = 'printf 0123456789 > "$out/a.txt"\nexit 1'

    def test_a_failed_extraction_that_names_nothing_keeps_the_hashes_the_listing_confirms(self):
        reader = self.reader({'bsdtar': self.fake_bsdtar(self.LINE, self.BODY)})
        self.assertTrue(self.open(reader, '7z', 'x.7z', b'7z\xbc\xaf\x27\x1c' + b'\x00' * 100))
        member, = reader.members
        self.assertIn('h', member)
        self.assertNotIn('short', member)
        self.assertEqual(reader.partly, 1)

    def test_a_failed_extraction_that_names_nothing_leaves_unchecked_members_without_a_hash(self):
        listing = self.LINE + "\n  printf 'unreadable line\\n'"           # the sizes of this listing cannot be trusted
        reader = self.reader({'bsdtar': self.fake_bsdtar(listing, self.BODY)})
        self.assertTrue(self.open(reader, '7z', 'x.7z', b'7z\xbc\xaf\x27\x1c' + b'\x00' * 100))
        member, = reader.members
        self.assert_unconfirmed(member)
        self.assertEqual(reader.partly, 1)

    def test_a_successful_extraction_is_never_second_guessed_by_its_messages(self):
        body = 'printf "a.txt: a warning" >&2\nprintf 0123456789 > "$out/a.txt"\nexit 0'
        reader = self.reader({'bsdtar': self.fake_bsdtar(self.LINE, body)})
        self.assertTrue(self.open(reader, '7z', 'x.7z', b'7z\xbc\xaf\x27\x1c' + b'\x00' * 100))
        member, = reader.members
        self.assertIn('h', member)
        self.assertEqual((reader.partly, reader.shorts), (0, 0))


class FakeToolTests(Base):
    """Tools that misbehave on purpose: lying headers, output floods, hangs and an unlisted tool."""

    def fake_seven(self, listing_size, body):
        return script(self.base / 'fake7zz', f'''if [ "$1" = "l" ]; then
  printf 'Path = a.txt\\nSize = {listing_size}\\nFolder = -\\nAttributes = A\\nEncrypted = -\\n\\n'
  exit 0
fi
out=""
for a in "$@"; do case "$a" in -o*) out="${{a#-o}}";; esac; done
{body}
''')

    def run_fake(self, tool, **limits):
        reader = self.reader({'7zz': tool}, **limits)
        started = time.monotonic()
        opened = self.open(reader, '7z', 'x.7z', b'7z\xbc\xaf\x27\x1c' + b'\x00' * 100)
        return reader, opened, time.monotonic() - started

    def test_the_watchdog_kills_the_whole_process_group_when_the_folder_outgrows_the_allowance(self):
        pids = self.base / 'pids.txt'
        tool = self.fake_seven(10, f'sleep 120 &\necho $! >> "{pids}"\necho $$ >> "{pids}"\ndd if=/dev/zero of="$out/a.txt" bs=1048576 count=5 2>/dev/null\nwait')
        reader, opened, elapsed = self.run_fake(tool, max_expanded_bytes=1_000_000)
        self.assertTrue(opened)
        self.assertEqual(reader.partly, 1)
        self.assertLess(elapsed, 20)
        self.assertTrue(gone_within([int(p) for p in pids.read_text().split()]), 'the tool and its child must both be stopped')
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_tool_runs_in_its_own_process_group(self):
        groups = self.base / 'groups.txt'
        tool = self.fake_seven(10, f'ps -o pid=,pgid= -p $$ >> "{groups}"\nprintf x > "$out/a.txt"')
        reader, opened, _ = self.run_fake(tool)
        self.assertTrue(opened)
        pid, group = groups.read_text().split()
        self.assertEqual(pid, group)
        self.assertNotEqual(int(group), os.getpgrp())

    def test_terminating_the_worker_stops_its_tool_and_children(self):
        import multiprocessing
        pids = self.base / 'pids.txt'
        tool = self.fake_seven(10, f'sleep 120 &\necho $! >> "{pids}"\necho $$ >> "{pids}"\nwait')
        context = multiprocessing.get_context('spawn')
        process = context.Process(target=hold_a_tool, args=(str(self.work), tool))
        process.start()
        end = time.monotonic() + 15
        while time.monotonic() < end and not (pids.exists() and len(pids.read_text().split()) == 2):
            time.sleep(0.05)
        started = [int(p) for p in pids.read_text().split()]
        self.assertEqual(len(started), 2)
        process.terminate()
        process.join(10)
        self.assertTrue(gone_within(started), 'SIGTERM to the worker must stop the tool group')

    def test_a_worker_that_lost_its_parent_stops_the_tool_and_leaves_its_folder_clean(self):
        pids = self.base / 'pids.txt'
        tool = self.fake_seven(10, f'sleep 120 &\necho $! >> "{pids}"\necho $$ >> "{pids}"\nwait')
        reader = self.reader({'7zz': tool})
        parent = [os.getppid()]

        def lose_the_parent():                                      # once the tool runs, the parent this worker started under is gone
            while not (pids.exists() and len(pids.read_text().split()) == 2):
                time.sleep(0.01)
            parent[0] = 0
        threading.Thread(target=lose_the_parent, daemon=True).start()
        with mock.patch.object(A, '_PARENT', parent):
            with self.assertRaises(A.ParentGone):
                self.open(reader, '7z', 'x.7z', b'7z\xbc\xaf\x27\x1c' + b'\x00' * 100)
        self.assertTrue(gone_within([int(p) for p in pids.read_text().split()]), 'the tool and its child must both be stopped')
        self.assertEqual(list(self.work.iterdir()), [])

    def test_every_reading_loop_of_a_worker_that_lost_its_parent_ends(self):
        reader = self.reader({})
        with mock.patch.object(A, '_PARENT', [0]):
            with self.assertRaises(A.ParentGone):
                reader.check_time()
            with self.assertRaises(A.ParentGone):
                reader.hash_only(io.BytesIO(b'x' * 10))
        reader.check_time()                                          # outside a worker nothing is watched

    def test_a_size_listing_over_the_allowance_stops_unar_before_it_runs(self):
        marker = self.base / 'unar-ran'
        unar = script(self.base / 'unar', f'echo ran >> "{marker}"\nexit 0\n')
        lsar = script(self.base / 'lsar', 'printf \'{"lsarFormatVersion":2,"lsarContents":[{"XADFileName":"a.txt","XADFileSize":9000000000,"XADIsDirectory":0}]}\'\n')
        reader = self.reader({'unar': unar, 'lsar': lsar}, max_expanded_bytes=1_000_000)
        self.assertTrue(self.open(reader, 'rar', 'x.rar', b'Rar!\x1a\x07\x00' + b'\x00' * 64))
        self.assertFalse(marker.exists())
        self.assertEqual((reader.partly, reader.members), (1, []))

    def test_unar_without_a_listing_tool_is_not_used(self):
        marker = self.base / 'unar-ran'
        unar = script(self.base / 'unar', f'echo ran >> "{marker}"\nexit 0\n')
        reader = self.reader({'unar': unar})
        self.assertFalse(self.open(reader, 'rar', 'x.rar', b'Rar!\x1a\x07\x00' + b'\x00' * 64))
        self.assertFalse(marker.exists())
        self.assertEqual(dict(reader.failed), {'rar:no extraction tool': 1})

    def test_unar_extracts_what_the_listing_allows_and_credentials_are_deleted_unread(self):
        lsar = script(self.base / 'lsar', 'printf \'{"lsarContents":[{"XADFileName":"a.txt","XADFileSize":60},{"XADFileName":"keys/token.txt","XADFileSize":6}]}\'\n')
        unar = script(self.base / 'unar', 'out=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
                      'mkdir -p "$out/keys"; printf "%060d" 0 > "$out/a.txt"; printf secret > "$out/keys/token.txt"\n')
        reader = self.reader({'unar': unar, 'lsar': lsar})
        self.assertTrue(self.open(reader, 'rar', 'x.rar', b'Rar!\x1a\x07\x00' + b'\x00' * 64))
        by_name = {m['n']: m for m in reader.members}
        self.assertEqual(sorted(by_name), ['a.txt', 'keys/token.txt'])
        self.assertTrue(by_name['keys/token.txt']['cred'])
        self.assertNotIn('h', by_name['keys/token.txt'])
        self.assertEqual(by_name['a.txt']['s'], 60)

    def test_not_enough_free_space_is_checked_for_every_tool_before_extraction(self):
        marker = self.base / 'unar-ran'
        unar = script(self.base / 'unar', f'echo ran >> "{marker}"\nexit 0\n')
        lsar = script(self.base / 'lsar', 'printf \'{"lsarContents":[{"XADFileName":"a.txt","XADFileSize":500000}]}\'\n')
        with mock.patch.object(shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10 ** 9, 10 ** 9 - 1_300_000, 1_300_000)):
            reader = self.reader({'unar': unar, 'lsar': lsar})
            self.open(reader, 'rar', 'x.rar', b'Rar!\x1a\x07\x00' + b'\x00' * 64)
        self.assertFalse(marker.exists())
        self.assertEqual(dict(reader.failed), {'rar:not enough free disk space': 1})


if __name__ == '__main__':
    unittest.main()
