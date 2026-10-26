"""Archive reading: nesting, caps, unsafe names, credentials, damaged and unsupported archives."""
import bz2
import gzip
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_archives as A
import spec_samples as S


def zip_bytes(entries, nested_flag=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.temp = self.tmp.name

    def read(self, kind, name, data, tools=None, limits=None, depth=1):
        reader = A.Reader(self.temp, tools={} if tools is None else tools, limits=limits)
        opened = reader.open_archive(kind, name, io.BytesIO(data), depth, '', compressed=len(data))
        return reader, opened

    def test_zip_members_are_hashed_and_nested_archives_recursed(self):
        report = S.pdf_bytes([('text', S.A4P, 1), ('drawing', S.A3P, 2)])
        inner = zip_bytes([('deep/new.txt', b'inner text ' * 20), ('copy.pdf', report)])
        outer = zip_bytes([('a/report.pdf', report), ('inner.zip', inner), ('notes.txt', S.EN.encode() * 2), ('empty/', b'')])
        reader, opened = self.read('zip', 'outer.zip', outer)
        self.assertTrue(opened)
        self.assertEqual(reader.opened, 2)
        names = sorted(m['n'] for m in reader.members)
        self.assertEqual(names, ['a/report.pdf', 'inner.zip', 'inner.zip!/copy.pdf', 'inner.zip!/deep/new.txt', 'notes.txt'])
        by_name = {m['n']: m for m in reader.members}
        self.assertEqual(by_name['a/report.pdf']['h'], sha(report))
        self.assertEqual(by_name['inner.zip!/copy.pdf']['h'], sha(report))
        self.assertTrue(by_name['inner.zip']['cont'])
        self.assertEqual(by_name['inner.zip']['h'], sha(inner))
        self.assertEqual(by_name['a/report.pdf']['pdf']['classes']['A3'], [1, 0])
        self.assertEqual(by_name['notes.txt']['lang']['lang'], 'en')
        self.assertEqual(reader.lost, 0)
        self.assertEqual(reader.partly, 0)
        self.assertEqual(reader.used['zip:zipfile'], 2)

    def test_unsafe_encrypted_credential_and_hidden_members(self):
        data = S.zip_with_encrypted_member([('ok.txt', b'fine text ' * 20), ('../outside.txt', b'x'), ('/absolute.txt', b'x'),
                                            ('Passwords.txt', b'top secret'), ('__MACOSX/._ok.txt', b'junk'), ('locked.txt', b'cipher')], ['locked.txt'])
        reader, opened = self.read('zip', 'x.zip', data)
        self.assertTrue(opened)
        by_name = {m['n']: m for m in reader.members}
        self.assertEqual(sorted(by_name), ['Passwords.txt', 'ok.txt'])
        self.assertTrue(by_name['Passwords.txt']['cred'])
        self.assertEqual(by_name['Passwords.txt']['h'], sha(b'top secret'))      # only a checksum; the bytes are never parsed
        self.assertEqual(by_name['Passwords.txt']['s'], len(b'top secret'))
        self.assertNotIn('lang', by_name['Passwords.txt'])
        self.assertEqual(reader.lost, 3)          # two unsafe names and one encrypted member
        self.assertEqual(reader.partly, 1)
        self.assertEqual(reader.hidden, 1)

    def test_expanded_size_and_ratio_caps_stop_the_archive(self):
        data = zip_bytes([('a.bin', b'\x00' * 400000), ('b.bin', b'\x01' * 400000)])
        reader, opened = self.read('zip', 'x.zip', data, limits={'max_expanded_bytes': 500000})
        self.assertTrue(opened)
        self.assertEqual(reader.partly, 1)
        self.assertLessEqual(len(reader.members), 1)
        reader, _ = self.read('zip', 'x.zip', data, limits={'max_ratio': 20, 'ratio_floor_bytes': 1000})
        self.assertEqual(reader.partly, 1)

    def test_depth_limit_records_a_deeper_archive_as_too_deep_and_the_outer_archive_as_partly_read(self):
        data = b'leaf text ' * 20
        for level in range(7):
            data = zip_bytes([('level.zip' if level else 'leaf.txt', data)])
        reader, _ = self.read('zip', 'top.zip', data)
        self.assertEqual(reader.opened, 5)
        self.assertEqual(len(reader.members), 5)
        self.assertEqual(sum(1 for m in reader.members if m.get('cont')), 4)
        self.assertEqual(dict(reader.failed), {'zip:too deep': 1})
        self.assertEqual(reader.partly, 1)

    def test_damaged_archive_without_a_tool_is_not_unpacked(self):
        reader, opened = self.read('zip', 'bad.zip', b'PK\x03\x04 this is not really a zip' * 5)
        self.assertFalse(opened)
        self.assertEqual(reader.failed['zip:no extraction tool'], 1)
        reader, opened = self.read('rar', 'x.rar', b'Rar!\x1a\x07\x00 data' * 10)
        self.assertFalse(opened)
        self.assertEqual(reader.failed['rar:no extraction tool'], 1)

    def test_tar_variants_and_single_compression(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for name, data in [('docs/a.txt', S.RO.encode() * 2), ('../escape.txt', b'x')]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        reader, opened = self.read('tar.gz', 'x.tar.gz', buffer.getvalue())
        self.assertTrue(opened)
        self.assertEqual([m['n'] for m in reader.members], ['docs/a.txt'])
        self.assertEqual(reader.members[0]['lang']['lang'], 'ro')
        self.assertEqual(reader.lost, 1)
        for kind, compress in [('gz', gzip.compress), ('bz2', bz2.compress)]:
            reader, opened = self.read(kind, f'note.txt.{kind}', compress(S.EN.encode() * 2))
            self.assertTrue(opened)
            self.assertEqual([m['n'] for m in reader.members], ['note.txt'])

    def test_kind_detection(self):
        self.assertEqual(A.archive_kind('a.ZIP'), 'zip')
        self.assertEqual(A.archive_kind('a.tar.gz'), 'tar.gz')
        self.assertEqual(A.archive_kind('a.tgz'), 'tar.gz')
        self.assertEqual(A.archive_kind('a.gz'), 'gz')
        self.assertEqual(A.archive_kind('a.rar'), 'rar')
        self.assertEqual(A.archive_kind('a.docx', b'PK\x03\x04'), None)
        self.assertEqual(A.archive_kind('a.cdr', b'PK\x03\x04'), 'zip')
        self.assertEqual(A.archive_kind('noext', b'Rar!\x1a\x07\x00'), 'rar')
        self.assertIsNone(A.archive_kind('plain.txt', b'hello'))
        self.assertTrue(A.unsafe_name('../x'))
        self.assertTrue(A.unsafe_name('a/../x'))
        self.assertTrue(A.unsafe_name('/x'))
        self.assertTrue(A.unsafe_name('C:\\x'))
        self.assertFalse(A.unsafe_name('a/b..c/x'))


@unittest.skipUnless(shutil.which('7zz') or shutil.which('7z'), 'needs 7zz or 7z')
class ExternalToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'work').mkdir()
        self.tool = shutil.which('7zz') or shutil.which('7z')
        source = self.base / 'src'
        (source / 'sub').mkdir(parents=True)
        (source / 'a b.txt').write_bytes(S.EN.encode() * 2)
        (source / 'sub' / 'Lucrări*?.txt').write_bytes(S.RO.encode() * 2)
        (source / 'password list.txt').write_bytes(b'top secret')
        self.report = S.pdf_bytes([('text', S.A4P, 1)])
        (source / 'report.pdf').write_bytes(self.report)
        subprocess.run([self.tool, 'a', '-bd', str(self.base / 'x.7z'), '.'], cwd=source, check=True, capture_output=True)
        self.data = (self.base / 'x.7z').read_bytes()

    def read(self, tools):
        reader = A.Reader(str(self.base / 'work'), tools=tools)
        opened = reader.open_archive('7z', 'x.7z', io.BytesIO(self.data), 1, '', compressed=len(self.data))
        return reader, opened

    def test_7z_through_the_tool_with_credentials_never_extracted(self):
        reader, opened = self.read({'7zz' if shutil.which('7zz') else '7z': self.tool})
        self.assertTrue(opened)
        by_name = {unicodedata.normalize('NFC', m['n']): m for m in reader.members}
        self.assertEqual(sorted(by_name), ['a b.txt', 'password list.txt', 'report.pdf', 'sub/Lucrări*?.txt'])
        self.assertTrue(by_name['password list.txt']['cred'])
        self.assertEqual(by_name['password list.txt']['s'], len(b'top secret'))       # listed by name and size; never extracted or hashed
        self.assertNotIn('h', by_name['password list.txt'])
        self.assertEqual(by_name['report.pdf']['h'], sha(self.report))
        self.assertEqual(by_name['sub/Lucrări*?.txt']['lang']['lang'], 'ro')
        self.assertEqual((reader.lost, reader.partly), (0, 0))
        self.assertEqual(list((self.base / 'work').iterdir()), [])     # private temporary folders are removed

    def test_a_tool_archive_over_the_size_cap_or_without_disk_space_is_not_unpacked(self):
        name = 'x.7z'
        tools = {'7zz' if shutil.which('7zz') else '7z': self.tool}
        capped = A.Reader(str(self.base / 'work'), tools=tools, limits={'max_expanded_bytes': 10})
        self.assertTrue(capped.open_archive('7z', name, io.BytesIO(self.data), 1, '', compressed=len(self.data)))
        self.assertEqual((capped.partly, capped.members), (1, []))
        from unittest import mock
        with mock.patch.object(shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 99, 1)):
            reader = A.Reader(str(self.base / 'work'), tools=tools)
            reader.open_archive('7z', name, io.BytesIO(self.data), 1, '', compressed=len(self.data))
        self.assertEqual(dict(reader.failed), {'7z:not enough free disk space': 1})
        self.assertEqual(list((self.base / 'work').iterdir()), [])

    @unittest.skipUnless(shutil.which('bsdtar') or Path('/usr/bin/bsdtar').exists(), 'needs bsdtar')
    def test_bsdtar_reads_7z_when_it_is_the_only_tool(self):
        reader, opened = self.read({'bsdtar': shutil.which('bsdtar') or '/usr/bin/bsdtar'})
        self.assertTrue(opened)
        names = sorted(unicodedata.normalize('NFC', m['n']) for m in reader.members if not m.get('cred'))
        self.assertEqual(names, ['a b.txt', 'report.pdf', 'sub/Lucrări*?.txt'])
        self.assertTrue(any(m.get('cred') for m in reader.members))


if __name__ == '__main__':
    unittest.main()
