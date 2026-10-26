"""Resumability, change detection, configuration checks and input safety of the specification run."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_fixture as F
import spec_samples as S

NOW = datetime(2026, 10, 25)
LOG = open(os.devnull, 'w')


def run(sources, out, config=None, **kwargs):
    return formatspec.run_spec(sources, out, config=config, workers=2, tools={}, report_date=NOW, log=LOG, **kwargs)


def rows(out):
    db = sqlite3.connect(str(out / 'spec-cache.private.sqlite'))
    try:
        return db.execute('select count(*) from files').fetchone()[0]
    finally:
        db.close()


def tree_state(root):
    state = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            state[str(path)] = (info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            state[str(path)] = ('other',)
    return state


class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.fx = F.Fixture(cls.base / 'fx')
        cls.before = tree_state(cls.fx.data)
        cls.full = run([cls.fx.data], cls.base / 'full', cls.fx.config)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_an_interrupted_run_resumes_to_an_identical_specification(self):
        out = self.base / 'part'
        with self.assertRaises(formatspec.SpecInterrupted):
            run([self.fx.data], out, self.fx.config, stop_after=20)
        finished = rows(out)
        self.assertTrue(20 <= finished < len(self.fx.files))
        self.assertFalse((out / 'spec.json').exists())
        self.assertFalse((out / 'spec-tmp').exists())
        seen = []
        original = formatspec.run_pool

        def spy(tasks, *args, **kwargs):
            seen.append(len(tasks))
            return original(tasks, *args, **kwargs)
        with mock.patch.object(formatspec, 'run_pool', spy):
            again = run([self.fx.data], out, self.fx.config)
        self.assertEqual(again, self.full)
        self.assertEqual(json.loads((out / 'spec.json').read_text()), json.loads((self.base / 'full' / 'spec.json').read_text()))
        self.assertEqual(sum(seen), len(self.fx.files) - finished)
        self.assertEqual(rows(out), len(self.fx.files))

    def test_a_second_run_over_unchanged_files_reads_nothing_again(self):
        seen = []
        original = formatspec.run_pool

        def spy(tasks, *args, **kwargs):
            seen.append(len(tasks))
            return original(tasks, *args, **kwargs)
        with mock.patch.object(formatspec, 'run_pool', spy):
            again = run([self.fx.data], self.base / 'full', self.fx.config)
        self.assertEqual(seen, [])
        self.assertEqual(again, self.full)

    def test_a_changed_file_is_read_again_and_a_vanished_file_is_ignored(self):
        fx = F.Fixture(self.base / 'fx2')
        out = self.base / 'changed'
        first = run([fx.data], out, fx.config)
        target = fx.data / 'Company/Misc/old note.txt'
        target.write_bytes(target.read_bytes() + b' more')
        (fx.data / 'Company/Misc/noext').unlink()
        seen = []
        original = formatspec.run_pool

        def spy(tasks, *args, **kwargs):
            seen.append([t['name'] for t in tasks])
            return original(tasks, *args, **kwargs)
        with mock.patch.object(formatspec, 'run_pool', spy):
            second = run([fx.data], out, fx.config)
        self.assertEqual(seen, [['old note.txt']])
        self.assertEqual(second['totals']['files'], first['totals']['files'] - 1)

    def test_sources_are_never_modified_and_nothing_is_written_inside_them(self):
        self.assertEqual(tree_state(self.fx.data), self.before)


class SettingsChangeTests(unittest.TestCase):
    """The resume cache keeps finished rows only while the settings that decided them are the same."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        (self.data / 'Mail/Box').mkdir(parents=True)
        (self.data / 'note.txt').write_bytes((S.EN * 2).encode())
        self.out = self.base / 'out'

    def spy_run(self, config=None, **kwargs):
        seen = []
        original = formatspec.run_pool

        def spy(tasks, *args, **inner):
            seen.extend(task['name'] for task in tasks)
            return original(tasks, *args, **inner)
        with mock.patch.object(formatspec, 'run_pool', spy):
            spec = formatspec.run_spec([self.data], self.out, config=config, **{'workers': 2, 'tools': {}, 'report_date': NOW, 'log': LOG, **kwargs})
        return spec, sorted(seen)

    def big_zip(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('big.txt', (S.EN * 40).encode())
            archive.writestr('small.txt', (S.RO * 2).encode())
        (self.data / 'big.zip').write_bytes(buffer.getvalue())

    def test_a_larger_limit_reads_an_archive_that_was_cut_by_the_old_limit(self):
        self.big_zip()
        spec, seen = self.spy_run(limits={'max_archive_bytes': 3000})
        self.assertEqual(spec['methodology']['archives']['partly_readable'], 1)
        self.assertIn('big.zip', seen)
        spec, seen = self.spy_run(limits={'max_archive_bytes': 3000})
        self.assertEqual(seen, [])                                       # same limits: nothing is read again
        spec, seen = self.spy_run()
        self.assertEqual(seen, ['big.zip'])                              # the cut archive only; the finished note.txt stays
        self.assertEqual(spec['methodology']['archives']['partly_readable'], 0)
        self.assertEqual(spec['parts']['archive_contents']['files'], 2)

    def test_a_changed_time_limit_reads_files_that_timed_out_again(self):
        self.big_zip()
        original = formatspec.run_pool

        def timed_out(tasks, count, settings, on_result, target=None):
            for task in tasks:
                if task['name'] == 'big.zip':
                    on_result(task, {'status': 'timeout', 'sha': '0' * 64})
            rest = [task for task in tasks if task['name'] != 'big.zip']
            if rest:
                original(rest, count, settings, on_result, target)
        with mock.patch.object(formatspec, 'run_pool', timed_out):
            first = run([self.data], self.out, None, limits={'parse_seconds': 5})
        self.assertEqual(first['methodology']['time_limited_files'], 1)
        _, seen = self.spy_run(limits={'parse_seconds': 5})
        self.assertEqual(seen, [])
        spec, seen = self.spy_run(limits={'parse_seconds': 50})
        self.assertEqual(seen, ['big.zip'])
        self.assertEqual(spec['methodology']['time_limited_files'], 0)

    def test_a_newly_available_extraction_tool_reads_archives_that_could_not_be_opened(self):
        (self.data / 'packed.rar').write_bytes(b'Rar!\x1a\x07\x00 not really a rar archive ' * 4)
        spec, seen = self.spy_run()
        self.assertEqual(spec['methodology']['archives']['not_opened'], {'rar:no extraction tool': 1})
        spec, seen = self.spy_run(tools={'unar': '/nonexistent/unar'})
        self.assertEqual(seen, ['packed.rar'])
        self.assertEqual(spec['methodology']['archives']['not_opened'], {'rar:damaged': 1})
        spec, seen = self.spy_run(tools={'unar': '/nonexistent/unar'})
        self.assertEqual(seen, [])

    def test_finished_rows_survive_a_change_of_settings(self):
        self.spy_run(limits={'max_archive_bytes': 3000})
        spec, seen = self.spy_run(limits={'max_archive_bytes': 9000})
        self.assertEqual(seen, [])
        self.assertEqual(spec['totals']['files'], 1)

    def test_a_change_of_the_mailbox_configuration_reads_the_mbox_files_again(self):
        (self.data / 'Mail/Box/all.mbox').write_bytes(b'')
        import mailbox
        box = mailbox.mbox(str(self.data / 'Mail/Box/all.mbox'))
        box.add(S.eml_bytes('One', 'Ann <ann@synthetic.test>', 'owner@synthetic.test', S.EN * 2, 2022))
        box.flush()
        box.close()
        config = {'mailboxes': [{'root': str(self.data / 'Mail/Box'), 'label': 'Mailbox 1'}]}
        self.spy_run(config)
        spec, seen = self.spy_run(dict(config, mailboxes=[{'root': str(self.data / 'Mail/Box'), 'label': 'Other label'}]))
        self.assertEqual(seen, [])                                       # a label is only shown, it decides nothing about reading
        self.assertEqual(spec['mailboxes'][0]['label'], 'Other label')
        spec, seen = self.spy_run({})
        self.assertEqual(seen, ['all.mbox'])
        spec, seen = self.spy_run(config)
        self.assertEqual(seen, ['all.mbox'])


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()
        (self.data / 'a.txt').write_text('hello world ' * 20)

    def test_roots_and_output_rules_match_the_profile_command(self):
        for out in (self.data, self.data / 'report', self.base):
            with self.subTest(out=out), self.assertRaises(ValueError):
                run([self.data], out)
        alias = self.base / 'alias'
        alias.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(ValueError):
            run([alias], self.base / 'o')
        with self.assertRaises(ValueError):
            run([self.base / 'missing'], self.base / 'o')
        hidden = self.base / '.hidden'
        hidden.mkdir()
        with self.assertRaises(ValueError):
            run([hidden], self.base / 'o')

    def test_unsupported_platform_fails_before_any_file_work(self):
        import profiler
        with mock.patch.object(profiler.os, 'name', 'nt'), mock.patch.object(formatspec, 'walk_roots') as walk:
            with self.assertRaisesRegex(ValueError, 'macOS|POSIX'):
                run([self.data], self.base / 'o')
            walk.assert_not_called()

    def test_configuration_is_validated(self):
        good = {'company': 'X', 'mailboxes': [{'root': str(self.data), 'label': 'Box'}]}
        run([self.data], self.base / 'good', good)
        bad = [{'unknown_key': 1}, {'company': ''}, {'company': 'x\ny'}, {'mailboxes': [{'root': str(self.base / 'elsewhere')}]},
               {'mailboxes': [{'label': 'no root'}]}, {'project_depth': 0}, {'project_depth': True},
               {'stages': {'items': []}}, {'stages': {'items': [{'name': 'a', 'patterns': ['x']}], 'thresholds': [2]}},
               {'drawing_patterns': 'drawing'}, {'calibration': {'sampled': {'candidates': 5, 'non_candidates': 5}}},
               {'calibration': {'sampled': {'candidates': 5, 'non_candidates': 5}, 'true_drawings_among_candidates': 9,
                                'drawings_among_non_candidates': 0, 'schematics': 0, 'views_per_sheet': [1]}},
               {'limits': {'max_archive_bytes': -1}}, ['not', 'an', 'object']]
        for config in bad:
            with self.subTest(config=config), self.assertRaises(formatspec.SpecConfigError):
                run([self.data], self.base / 'bad', config)

    def test_a_declared_mailbox_without_mail_is_an_ordinary_folder(self):
        spec = run([self.data], self.base / 'o2', {'mailboxes': [{'root': str(self.data)}]})
        self.assertNotIn('mailboxes', spec)       # no messages: no mailbox section
        self.assertEqual(spec['parts']['file_archive']['files'], 1)
        self.assertNotIn('mailboxes', spec['parts'])
        self.assertEqual(spec['totals']['files'], 1)


@unittest.skipUnless(shutil.which('7zz') or shutil.which('7z'), 'needs 7zz or 7z')
class VolumeTests(unittest.TestCase):
    def test_split_seven_zip_volumes_count_as_one_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            source = base / 'src'
            source.mkdir()
            payload = os.urandom(20000)
            (source / 'big.bin').write_bytes(payload)
            (source / 'note.txt').write_bytes((S.EN * 2).encode())
            data = base / 'data'
            data.mkdir()
            tool = shutil.which('7zz') or shutil.which('7z')
            subprocess.run([tool, 'a', '-bd', '-v8k', '-mx0', str(data / 'set.7z'), '.'], cwd=source, check=True, capture_output=True)
            volumes = sorted(p.name for p in data.iterdir())
            self.assertGreaterEqual(len(volumes), 3)
            spec = formatspec.run_spec([data], base / 'out', workers=2, tools={'7zz' if shutil.which('7zz') else '7z': tool},
                                       report_date=NOW, log=LOG)
            self.assertEqual(spec['parts']['archive_contents']['archives'], 1)
            self.assertEqual(spec['parts']['archive_contents']['files'], 2)
            archives = next(c for c in spec['categories'] if c['name'].startswith('Compressed'))
            self.assertEqual(archives['files'], len(volumes))
            self.assertEqual(set(archives['formats']), {'001', '002', '003', '004'} & set(archives['formats']))
            self.assertEqual(spec['methodology']['archives']['partly_readable'], 0)


if __name__ == '__main__':
    unittest.main()
