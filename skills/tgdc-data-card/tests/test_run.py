"""The one command: a card can only be written from a walk of every selected file.

Before 1.5.0 a first step listed at most 100,000 files in alphabetical folder order and the first page of the card showed
that part as the selected files. These tests hold the properties that make that impossible: no file limit, one walk for
both parts of the card, a content sample spread over all folders, and a renderer that refuses files of different runs."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / 'scripts' / 'datacard.py'
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import profiler
import platform_support as P
import spec_fixture as F
import spec_samples as S
from pypdf import PdfReader

LOG = open(os.devnull, 'w')


def cli(*arguments, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)], cwd=cwd, capture_output=True, text=True, timeout=600)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def first_page(pdf):
    return ' '.join(PdfReader(pdf).pages[0].extract_text().split())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()
        self.out = self.base / 'report'


class OneWalkTests(Base):
    def drawings_sort_last(self):
        """PDF folders sort first, drawing folders last: 60 PDF and 40 DWG, the layout that hid the drawings behind a file limit."""
        for folder in ('A jobs', 'B jobs'):
            (self.data / folder).mkdir()
            for number in range(30):
                (self.data / folder / f'{number:02d}.pdf').write_bytes(S.pdf_bytes([('text', S.A4P, number)]))
        for folder in ('Y cad', 'Z cad'):
            (self.data / folder).mkdir()
            for number in range(20):
                (self.data / folder / f'{number:02d}.dwg').write_bytes(b'AC1032' + bytes([number]) * 40)

    def test_the_first_page_counts_every_file_and_not_the_folders_that_sort_first(self):
        self.drawings_sort_last()
        done = cli('run', '--source', self.data, '--output', self.out, '--workers', 2)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        self.assertEqual({kind: item['files'] for kind, item in profile['inventory']['by_extension'].items()}, {'dwg': 40, 'pdf': 60})
        self.assertTrue(profile['scope']['enumeration_complete'])
        self.assertEqual((profile['inventory']['files'], profile['inventory']['bytes']), (spec['totals']['held_files'], spec['totals']['held_bytes']))
        formats = {row['ext']: row['files'] for row in spec['all_formats']}
        self.assertEqual((formats['pdf'], formats['dwg']), (60, 40))
        page = first_page(self.out / 'TGDC-Data-Card.pdf')
        self.assertIn('100 Selected files', page)
        self.assertIn('Measured inventory of every selected file', page)
        self.assertNotIn('INCOMPLETE', page)
        self.assertIn('DWG, PDF', page)
        self.assertIn('Files on disk: 100;', done.stdout)

    def test_no_option_and_no_limit_can_cap_the_inventory(self):
        self.drawings_sort_last()
        for option in ('--max-files', '--max-sample-files', '--max-hash-bytes'):
            with self.subTest(option=option):
                done = cli('run', '--source', self.data, '--output', self.out, option, 50)
                self.assertEqual(done.returncode, 2)
                self.assertFalse(self.out.exists())
        self.assertNotIn('max_files', profiler.DEFAULT_LIMITS)
        with self.assertRaises(ValueError):
            profiler.profile([self.data], self.out, limits={'max_files': 50})
        result = profiler.profile([self.data], self.out, limits={'max_sample_files': 1})       # the content sample is bounded, the inventory is not
        self.assertEqual((result['inventory']['files'], result['extraction']['attempted']), (100, 1))
        self.assertEqual(result['extraction']['status_counts']['sample_cap'], 59)

    def test_the_commands_of_older_releases_are_refused_and_name_the_one_command(self):
        (self.data / 'a.txt').write_text('a')
        for command in ('profile', 'spec'):
            with self.subTest(command=command):
                done = cli(command, '--source', self.data, '--output', self.out)
                self.assertEqual(done.returncode, 2)
                self.assertIn('datacard.py run', done.stderr)
                self.assertFalse(self.out.exists())

    def test_the_content_sample_is_spread_over_all_folders(self):
        for folder in ('01 first', '02 middle', '03 last'):
            (self.data / folder / 'deep').mkdir(parents=True)
            for number in range(40):
                (self.data / folder / 'deep' / f'{number:02d}.txt').write_text(S.EN + f' {folder} {number}')
        result = profiler.profile([self.data], self.out, limits={'max_sample_files': 12})
        self.assertEqual(result['inventory']['files'], 120)
        sampled = [Path(item['path']).parts[-3] for item in load(self.out / 'manifest.private.json')['files']]
        self.assertEqual({folder: sampled.count(folder) for folder in sorted(set(sampled))}, {'01 first': 4, '02 middle': 4, '03 last': 4})

    def test_the_profile_and_the_full_pass_count_the_same_files_on_a_mixed_archive(self):
        fixture = F.Fixture(self.base / 'fx')                          # mailboxes, nested archives, credential-like names, hidden entries
        (self.base / 'spec-config.json').write_text(json.dumps(fixture.config))
        done = cli('run', '--source', fixture.data, '--output', self.out, '--config', self.base / 'spec-config.json', '--workers', 2)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        entries, _ = formatspec.walk_roots([profiler.checked_path(fixture.data)])
        self.assertEqual(profile['inventory']['files'], len(entries))
        self.assertEqual((profile['inventory']['files'], profile['inventory']['bytes']), (spec['totals']['held_files'], spec['totals']['held_bytes']))
        self.assertGreater(spec['totals']['files'], spec['totals']['held_files'])              # archive members are counted apart
        self.assertEqual(sum(item['files'] for item in profile['inventory']['by_extension'].values()), len(entries))
        duplicates = profile['metrics']['duplicate_files']
        self.assertEqual((duplicates['value'], duplicates['denominator']),
                         (spec['totals']['hashed_files'] - spec['totals']['unique_contents'], spec['totals']['hashed_files']))
        self.assertIn(f"{len(entries):,} Selected files", first_page(self.out / 'TGDC-Data-Card.pdf'))
        text = (self.out / 'profile.json').read_text() + done.stdout + done.stderr
        for token in F.PRIVATE_TOKENS:
            self.assertNotIn(token.lower(), text.lower(), token)

    def test_files_with_credential_like_names_are_counted_and_never_opened_for_content(self):
        (self.data / 'report.txt').write_text(S.EN)
        (self.data / 'password list.txt').write_text('PRIVATE_SYNTHETIC_SENTINEL ' + S.EN)
        (self.data / 'server.pem').write_text('PRIVATE_SYNTHETIC_SENTINEL')
        (self.data / 'secrets').mkdir()
        (self.data / 'secrets' / 'inside.txt').write_text('PRIVATE_SYNTHETIC_SENTINEL')
        done = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        self.assertEqual((profile['inventory']['files'], spec['totals']['held_files']), (3, 3))
        self.assertEqual(profile['extraction']['attempted'], 1)
        self.assertEqual(profile['extraction']['status_counts']['excluded'], 3)      # two files and the folder that was not entered
        manifest = (self.out / 'manifest.private.json').read_text()
        self.assertNotIn('password list', manifest)
        for text in (manifest, json.dumps(profile), json.dumps(spec)):
            self.assertNotIn('PRIVATE_SYNTHETIC_SENTINEL', text)

    def test_archive_members_do_not_inflate_the_files_on_disk(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for number in range(5):
                archive.writestr(f'inner/{number}.txt', S.EN + str(number))
        (self.data / 'bundle.zip').write_bytes(buffer.getvalue())
        (self.data / 'loose.txt').write_text(S.EN)
        done = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        self.assertEqual((profile['inventory']['files'], spec['totals']['held_files'], spec['totals']['files']), (2, 2, 7))
        self.assertIn('2 Selected files', first_page(self.out / 'TGDC-Data-Card.pdf'))

    @unittest.skipIf(P.WINDOWS or (hasattr(os, 'geteuid') and os.geteuid() == 0), 'needs permission bits and a user they apply to')
    def test_a_folder_that_cannot_be_read_marks_the_first_page_incomplete(self):
        (self.data / 'open').mkdir()
        (self.data / 'open' / 'a.txt').write_text(S.EN)
        locked = self.data / 'locked'
        locked.mkdir()
        (locked / 'b.txt').write_text(S.EN)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        done = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        self.assertFalse(profile['scope']['enumeration_complete'])
        self.assertEqual((profile['inventory']['files'], spec['totals']['held_files'], spec['methodology']['unreadable_folders']), (1, 1, 1))
        self.assertIn('INCOMPLETE INVENTORY / some folders or files could not be read', first_page(self.out / 'TGDC-Data-Card.pdf'))
        self.assertIn('could not be read', done.stdout)

    def test_two_disjoint_roots_and_a_single_file_root(self):
        second = self.base / 'second'
        second.mkdir()
        (self.data / 'a.txt').write_text(S.EN)
        (second / 'b.txt').write_text(S.DE)
        (self.base / 'single.csv').write_text('id,state\n1,open\n')
        other = self.base / 'elsewhere'
        other.mkdir()
        (other / 'single.csv').write_text('id,state\n1,open\n2,closed\n')
        done = cli('run', '--source', self.data, '--source', second, '--source', other / 'single.csv', '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        self.assertEqual((profile['inventory']['files'], spec['totals']['held_files'], profile['scope']['selected_root_count']), (3, 3, 3))
        self.assertEqual(profile['metrics']['rows_processed']['value'], 2)


class RunFlowTests(Base):
    def setUp(self):
        super().setUp()
        (self.data / 'Projects').mkdir()
        (self.data / 'Projects' / 'report.txt').write_text(S.EN * 3)
        (self.data / 'Projects' / 'table.csv').write_text('id,state\n1,open\n2,closed\n')

    def test_the_same_command_again_finds_the_answers_in_the_report_folder(self):
        first = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn('Synthetic operations', '\n'.join(page.extract_text() for page in PdfReader(self.out / 'TGDC-Data-Card.pdf').pages))
        (self.out / 'answers.json').write_text(json.dumps({'provider_context': 'Synthetic operations'}))
        (self.out / 'spec-config.json').write_text(json.dumps({'company': 'Muster Bau GmbH', 'sector_line': 'Construction', 'month': 'November 2026',
                                                                'scope': 'full archive'}))
        second = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(second.returncode, 0, second.stderr)
        text = '\n'.join(page.extract_text() for page in PdfReader(self.out / 'TGDC-Data-Card.pdf').pages)
        self.assertIn('Synthetic operations', text)
        self.assertIn('Muster Bau GmbH', text)
        self.assertEqual(load(self.out / 'spec.json')['scope'], 'full archive')
        elsewhere = self.base / 'other-answers.json'
        elsewhere.write_text(json.dumps({'provider_context': 'Named explicitly'}))
        third = cli('run', '--source', self.data, '--output', self.out, '--answers', elsewhere)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn('Named explicitly', '\n'.join(page.extract_text() for page in PdfReader(self.out / 'TGDC-Data-Card.pdf').pages))

    def test_answers_that_are_not_plain_strings_are_refused_before_any_file_is_read(self):
        self.out.mkdir()
        for content in ('{"notes": 5}', '{"notes": ["a"]}', '[1]', 'not json'):
            with self.subTest(content=content):
                (self.out / 'answers.json').write_text(content)
                done = cli('run', '--source', self.data, '--output', self.out)
                self.assertEqual(done.returncode, 2)
                self.assertNotIn('Traceback', done.stderr)
                self.assertEqual(sorted(path.name for path in self.out.iterdir()), ['answers.json'])

    def test_a_card_is_never_rendered_from_files_of_different_selections(self):
        done = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        other = self.base / 'other'
        other.mkdir()
        for number in range(7):
            (other / f'{number}.txt').write_text(S.EN + str(number))
        second = self.base / 'other-report'
        self.assertEqual(cli('run', '--source', other, '--output', second).returncode, 0)
        answers = self.base / 'answers.json'
        answers.write_text('{}')
        pdf = self.out / 'TGDC-Data-Card.pdf'
        before = pdf.read_bytes()
        mixed = cli('render', '--profile', self.out / 'profile.json', '--answers', answers, '--spec', second / 'spec.json', '--output', pdf)
        self.assertEqual(mixed.returncode, 2)
        self.assertIn('do not count the same files', mixed.stderr)
        self.assertEqual(pdf.read_bytes(), before)
        # A profile of a release with the file limit: 2 of the files listed, the specification counts all of them.
        capped = load(self.out / 'profile.json')
        capped['scope']['enumeration_complete'] = False
        capped['inventory']['files'] = capped['metrics']['selected_files']['value'] = 1
        capped['inventory']['by_extension'].pop('csv')
        capped['inventory']['bytes'] = capped['metrics']['selected_bytes']['value'] = capped['inventory']['by_extension']['txt']['bytes']
        capped['extraction'].update(attempted=1, succeeded=1)
        old = self.base / 'old-profile.json'
        old.write_text(json.dumps(capped))
        stale = cli('render', '--profile', old, '--answers', answers, '--spec', self.out / 'spec.json', '--output', pdf)
        self.assertEqual(stale.returncode, 2, stale.stderr)
        self.assertEqual(pdf.read_bytes(), before)
        same = cli('render', '--profile', self.out / 'profile.json', '--answers', answers, '--spec', self.out / 'spec.json', '--output', pdf)
        self.assertEqual(same.returncode, 0, same.stderr)

    def test_an_interrupted_run_writes_no_card_and_the_same_command_finishes_it(self):
        for number in range(12):
            (self.data / f'{number:02d}.txt').write_text(S.EN + str(number))
        first = cli('run', '--source', self.data, '--output', self.out, '--workers', 2, '--stop-after', 5)
        self.assertEqual(first.returncode, 3, first.stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), ['spec-cache.private.sqlite'])
        second = cli('run', '--source', self.data, '--output', self.out, '--workers', 2)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(load(self.out / 'profile.json')['inventory']['files'], 14)
        self.assertEqual(load(self.out / 'spec.json')['totals']['held_files'], 14)
        self.assertTrue((self.out / 'TGDC-Data-Card.pdf').read_bytes().startswith(b'%PDF'))

    def test_a_file_added_between_two_runs_appears_in_both_parts(self):
        self.assertEqual(cli('run', '--source', self.data, '--output', self.out).returncode, 0)
        (self.data / 'Projects' / 'new.dwg').write_bytes(b'AC1032 synthetic')
        (self.data / 'Projects' / 'table.csv').unlink()
        done = cli('run', '--source', self.data, '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        profile, spec = load(self.out / 'profile.json'), load(self.out / 'spec.json')
        self.assertEqual(sorted(profile['inventory']['by_extension']), ['dwg', 'txt'])
        self.assertEqual((profile['inventory']['files'], spec['totals']['held_files']), (2, 2))

    def test_an_empty_selection_still_gives_a_consistent_card(self):
        empty = self.base / 'empty'
        empty.mkdir()
        done = cli('run', '--source', empty, '--output', self.out)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((load(self.out / 'profile.json')['inventory']['files'], load(self.out / 'spec.json')['totals']['held_files']), (0, 0))
        self.assertIn('No files were inventoried', '\n'.join(page.extract_text() for page in PdfReader(self.out / 'TGDC-Data-Card.pdf').pages))

    def test_a_second_run_on_the_same_report_folder_is_refused_while_the_first_holds_it(self):
        descriptor = profiler.output_directory(profiler.checked_path(self.out))
        lock = formatspec.acquire_lock(descriptor)
        try:
            done = cli('run', '--source', self.data, '--output', self.out)
            self.assertEqual(done.returncode, 2)
            self.assertIn('Another', done.stderr)
            self.assertFalse((self.out / 'profile.json').exists())
        finally:
            formatspec.release_lock(descriptor, lock)
            os.close(descriptor)


if __name__ == '__main__':
    unittest.main()
