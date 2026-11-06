"""The same results on Windows, macOS and Linux: links of every kind, hidden files, long paths, pinned output folders, archive
member names outside ASCII and the cleanup of temporary data."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import platform_support as P
import portable
import profiler
import spec_archives
import spec_samples as S

NOW = datetime(2026, 11, 5)
LOG = open(os.devnull, 'w')
SEVEN = shutil.which('7zz') or shutil.which('7z') or portable.windows_seven_zip()


def spec(source, out, **kwargs):
    kwargs.setdefault('tools', {})
    return formatspec.run_spec([source], out, workers=2, report_date=NOW, log=LOG, **kwargs)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()


class ArchiveMemberNameTests(Base):
    @unittest.skipUnless(SEVEN, 'needs 7-Zip')
    def test_member_names_with_umlauts_are_read_and_hashed_by_7zip(self):
        source = self.base / 'src'
        (source / 'Übersicht').mkdir(parents=True)
        (source / 'Prüfbericht Ä.txt').write_bytes(S.EN.encode() * 2)
        (source / 'Übersicht' / 'Maße straße.txt').write_bytes(S.EN.encode() * 3)
        subprocess.run([SEVEN, 'a', '-bd', '-y', str(self.data / 'packed.7z'), '.'], cwd=source, check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
        tool = '7zz' if os.path.basename(SEVEN).lower().startswith('7zz') else '7z'
        result = spec(self.data, self.base / 'out', tools={tool: SEVEN})
        archives = result['methodology']['archives']
        self.assertEqual(result['parts']['archive_contents']['files'], 2)
        self.assertEqual(result['parts']['archive_contents']['new_files'], 2)
        self.assertEqual((archives['opened'], archives['partly_readable'], archives['not_opened']), (1, 0, {}))
        self.assertEqual(result['totals']['hashed_files'], 3)
        self.assertFalse((self.base / 'out' / 'spec-tmp').exists())


class TemporaryDataTests(Base):
    def test_read_only_files_and_folders_are_removed(self):
        folder = self.base / 'spec-tmp' / 'task'
        folder.mkdir(parents=True)
        locked = folder / 'member.txt'
        locked.write_text('extracted')
        os.chmod(locked, 0o444)
        portable.remove_tree(self.base / 'spec-tmp')
        self.assertFalse((self.base / 'spec-tmp').exists())

    def test_removing_a_missing_file_or_folder_is_quiet(self):
        portable.remove_tree(self.base / 'missing')
        portable.remove_file(self.base / 'missing.txt')

    def test_tool_environment_keeps_no_inherited_secrets(self):
        with mock.patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'x', 'GITHUB_TOKEN': 'y'}):
            environment = portable.tool_environment(str(self.base))
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', environment)
        self.assertNotIn('GITHUB_TOKEN', environment)
        self.assertEqual(environment['HOME'], str(self.base))
        if P.WINDOWS:
            self.assertEqual((environment['TEMP'], environment['TMP']), (str(self.base), str(self.base)))
            self.assertIn('SYSTEMROOT', environment)

    def test_installed_memory_is_known(self):
        self.assertGreater(portable.physical_memory() or 0, 2 ** 28)


@unittest.skipUnless(P.WINDOWS, 'Windows only')
class WindowsTests(Base):
    def test_a_junction_inside_a_selected_folder_is_never_entered(self):
        (self.data / 'a.txt').write_bytes(S.EN.encode() * 2)
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'secret.txt').write_bytes(b'must never be read')
        P.make_junction(self.data / 'link', elsewhere)
        result = profiler.profile([self.data], self.base / 'profile')
        self.assertEqual(result['inventory']['files'], 1)
        self.assertEqual(result['extraction']['status_counts']['symlink'], 1)
        entries, stats = formatspec.walk_roots([profiler.checked_path(self.data)])
        self.assertEqual([entry.rel for entry in entries], ['a.txt'])
        self.assertEqual(stats['symlinks'], 1)
        self.assertEqual(spec(self.data, self.base / 'out')['totals']['files'], 1)

    def test_a_junction_as_root_parent_or_output_is_refused(self):
        P.make_junction(self.base / 'alias', self.data)
        (self.data / 'inner').mkdir()
        (self.data / 'inner' / 'a.txt').write_text('a')
        for source, output in ((self.base / 'alias', self.base / 'o1'), (self.base / 'alias' / 'inner', self.base / 'o2'),
                               (self.data, self.base / 'alias' / 'report')):
            with self.assertRaises(ValueError):
                profiler.profile([source], output)
            with self.assertRaises(ValueError):
                spec(source, output)
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ['inner'])

    def test_a_file_opened_through_a_junction_parent_is_refused(self):
        (self.data / 'inner').mkdir()
        (self.data / 'inner' / 'a.txt').write_text('a')
        P.make_junction(self.base / 'alias', self.data)
        canonical = str(profiler.checked_path(self.base / 'data' / 'inner' / 'a.txt'))
        os.close(profiler.open_nofollow(canonical))
        detour = canonical.replace(os.sep + 'data' + os.sep, os.sep + 'alias' + os.sep)
        with self.assertRaises(OSError):
            profiler.open_nofollow(detour)

    def test_hidden_and_system_files_are_skipped_like_dot_files(self):
        import ctypes
        (self.data / 'report.txt').write_bytes(S.EN.encode() * 2)
        for name, attributes in (('desktop.ini', 0x2 | 0x4), ('Thumbs.db', 0x2), ('~$report.docx', 0x2)):
            (self.data / name).write_bytes(b'x')
            self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(self.data / name), attributes))
        customized = self.data / 'Customized folder'                   # system only: Windows marks folders with a custom icon so
        customized.mkdir()
        (customized / 'Angebot.txt').write_bytes(S.EN.encode() * 2)
        self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(customized), 0x4))
        result = spec(self.data, self.base / 'out')
        self.assertEqual(result['totals']['files'], 2)
        self.assertEqual(result['methodology']['hidden_entries'], 3)
        self.assertEqual(profiler.profile([self.data], self.base / 'profile')['inventory']['files'], 2)

    def test_files_beyond_260_characters_are_read(self):
        deep = '\\\\?\\' + str(self.data)
        for number in range(12):
            deep += '\\' + f'Projektordner Nummer {number:02d}'
        os.makedirs(deep)
        with open(deep + '\\Bericht.txt', 'wb') as handle:
            handle.write(S.EN.encode() * 2)
        self.assertGreater(len(deep), 300)
        result = spec(self.data, self.base / 'out')
        self.assertEqual((result['totals']['files'], result['totals']['hashed_files']), (1, 1))
        self.assertEqual(profiler.profile([self.data], self.base / 'profile')['inventory']['files'], 1)

    def test_an_open_output_folder_cannot_be_renamed(self):
        out = self.base / 'out'
        descriptor = profiler.output_directory(profiler.checked_path(out))
        try:
            with self.assertRaises(PermissionError):
                out.rename(self.base / 'moved')
        finally:
            os.close(descriptor)
        out.rename(self.base / 'moved')

    def test_roots_take_their_final_long_form(self):
        root = profiler.checked_path(self.data)
        self.assertTrue(str(root).startswith('\\\\?\\'))
        self.assertEqual(os.path.normcase(portable.plain(root)), os.path.normcase(str(self.data)))
        self.assertEqual(profiler.checked_path(str(self.data).upper() + '\\.\\'), root)

    def test_short_forms_are_used_only_when_they_mean_the_same_file(self):
        self.assertEqual(portable.plain('\\\\?\\C:\\Data\\x.txt'), 'C:\\Data\\x.txt')
        self.assertEqual(portable.plain('\\\\?\\UNC\\server\\share\\x'), '\\\\server\\share\\x')
        self.assertEqual(portable.plain('\\\\?\\C:\\Data\\name.'), '\\\\?\\C:\\Data\\name.')
        long = '\\\\?\\C:\\' + 'x' * 300
        self.assertEqual(portable.plain(long), long)

    def test_a_substituted_drive_and_a_network_path_are_read(self):
        (self.data / 'Bericht.txt').write_bytes(S.EN.encode() * 2)
        drive = next((letter + ':' for letter in 'TUVWXYZ' if not os.path.exists(letter + ':\\')), None)
        if drive is None:
            self.skipTest('no free drive letter')
        subprocess.run(['subst', drive, str(self.data)], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ['subst', drive, '/d'], capture_output=True)
        result = spec(drive + '\\', self.base / 'out-drive')
        self.assertEqual((result['totals']['files'], result['totals']['hashed_files']), (1, 1))
        self.assertEqual(profiler.profile([drive + '\\'], self.base / 'profile-drive')['inventory']['files'], 1)
        share = '\\\\localhost\\' + str(self.data)[0] + '$' + str(self.data)[2:]
        if not os.path.isdir(share):
            self.skipTest('no administrative share on this machine')
        result = spec(share, self.base / 'out-share')
        self.assertEqual((result['totals']['files'], result['totals']['hashed_files']), (1, 1))

    def test_7zip_is_found_in_its_install_folder_without_path(self):
        if not portable.windows_seven_zip():
            self.skipTest('7-Zip is not installed')
        with mock.patch.dict(os.environ, {'PATH': ''}):
            self.assertIn('7z', spec_archives.probe_tools())


if __name__ == '__main__':
    unittest.main()
