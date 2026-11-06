"""The datacard.py spec, spec-sample and render commands run from the skill folder."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / 'scripts' / 'datacard.py'
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import spec_fixture as F
from pypdf import PdfReader


def cli(*arguments, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, arguments)], cwd=cwd, capture_output=True, text=True, timeout=300)


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.fx = F.Fixture(cls.base / 'fx')
        cls.config = cls.base / 'spec-config.json'
        cls.config.write_text(json.dumps(cls.fx.config))
        cls.out = cls.base / 'report'
        cls.spec_run = cli('spec', '--source', cls.fx.data, '--output', cls.out, '--config', cls.config, '--workers', 2, cwd=cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_spec_command_writes_the_specification_and_reports_progress_without_names(self):
        run = self.spec_run
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Specification written to', run.stdout)
        spec = json.loads((self.out / 'spec.json').read_text())
        self.assertEqual(spec['schema'], 'tgdc-format-spec/1.0')
        self.assertRegex(run.stderr, r'\[spec\] \d+ of \d+ files, [\d.]+ of [\d.]+ GB read, elapsed \d+:\d\d:\d\d')
        for token in F.PRIVATE_TOKENS:
            self.assertNotIn(token.lower(), (run.stdout + run.stderr).lower(), token)

    def test_render_with_the_specification_writes_one_two_part_pdf(self):
        profile = self.base / 'profile-out'
        done = cli('profile', '--source', self.fx.data, '--output', profile, cwd=self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        answers = self.base / 'answers.json'
        answers.write_text(json.dumps({'provider_context': 'Synthetic test organization'}))
        pdf = self.base / 'TGDC-Data-Card.pdf'
        done = cli('render', '--profile', profile / 'profile.json', '--answers', answers, '--spec', self.out / 'spec.json', '--output', pdf, cwd=self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        reader = PdfReader(pdf)
        text = '\n'.join(page.extract_text() for page in reader.pages)
        self.assertIn('Synthetic test organization', text)
        self.assertIn('Content by category', text)
        self.assertEqual(reader.metadata.producer, 'The General Data Company')
        broken = self.base / 'broken-spec.json'
        broken.write_text('{"schema": "x"}')
        done = cli('render', '--profile', profile / 'profile.json', '--answers', answers, '--spec', broken, '--output', pdf, cwd=self.base)
        self.assertEqual(done.returncode, 2)

    def test_interrupted_command_exits_with_three_and_the_same_command_resumes(self):
        out = self.base / 'resume'
        first = cli('spec', '--source', self.fx.data, '--output', out, '--config', self.config, '--workers', 2, '--stop-after', 15, cwd=self.base)
        self.assertEqual(first.returncode, 3, first.stderr)
        self.assertIn('run the same command again to resume', first.stderr)
        second = cli('spec', '--source', self.fx.data, '--output', out, '--config', self.config, '--workers', 2, cwd=self.base)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads((out / 'spec.json').read_text()), json.loads((self.out / 'spec.json').read_text()))

    def test_bad_configuration_and_unsafe_output_fail_with_exit_two(self):
        bad = self.base / 'bad.json'
        bad.write_text('{"nonsense": 1}')
        done = cli('spec', '--source', self.fx.data, '--output', self.base / 'o1', '--config', bad, cwd=self.base)
        self.assertEqual(done.returncode, 2)
        self.assertIn('Unknown configuration key', done.stderr)
        done = cli('spec', '--source', self.fx.data, '--output', self.fx.data / 'inside', cwd=self.base)
        self.assertEqual(done.returncode, 2)

    def test_page_sample_writes_private_images_with_candidate_flags_only(self):
        done = cli('spec-sample', '--spec-dir', self.out, '--n', 20, cwd=self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        folder = self.out / 'calibration.private'
        if os.name != 'nt':                                   # Windows has no permission bits; the folder's access list applies
            self.assertEqual(oct(folder.stat().st_mode & 0o777), '0o700')
        index = json.loads((folder / 'index.private.json').read_text())['images']
        images = sorted(p.name for p in folder.glob('*.png'))
        self.assertEqual(images, sorted(item['file'] for item in index))
        self.assertGreaterEqual(len(images), 4)
        self.assertLessEqual(len(images), 20)
        flags = {item['candidate'] for item in index}
        self.assertEqual(flags, {True, False})
        from PIL import Image
        for name in images:
            with Image.open(folder / name) as image:
                self.assertLessEqual(max(image.size), 1000)
        text = (folder / 'index.private.json').read_text().lower()
        for token in F.PRIVATE_TOKENS:
            self.assertNotIn(token.lower(), text)
        again = cli('spec-sample', '--spec-dir', self.out, '--n', 20, cwd=self.base)
        self.assertEqual(again.returncode, 0)
        missing = cli('spec-sample', '--spec-dir', self.base / 'nowhere', cwd=self.base)
        self.assertEqual(missing.returncode, 2)


class CliLimitTests(unittest.TestCase):
    def test_negative_and_non_finite_limits_are_rejected_before_any_work(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            (base / 'data').mkdir()
            (base / 'data/a.txt').write_text('hello world ' * 20)
            for option, value in [('--workers', -1), ('--parse-seconds', -5), ('--archive-seconds', -0.5), ('--max-archive-bytes', -1),
                                  ('--max-archive-ratio', -200), ('--parse-seconds', 'nan'), ('--archive-seconds', 'inf')]:
                with self.subTest(option=option, value=value):
                    done = cli('spec', '--source', base / 'data', '--output', base / 'out', option, value, cwd=base)
                    self.assertEqual(done.returncode, 2)
                    self.assertIn(option, done.stderr)
                    self.assertFalse((base / 'out').exists())
            done = cli('spec-sample', '--spec-dir', base / 'out', '--n', -3, cwd=base)
            self.assertEqual(done.returncode, 2)
            ok = cli('spec', '--source', base / 'data', '--output', base / 'out', '--parse-seconds', 30, '--max-archive-ratio', 100, cwd=base)
            self.assertEqual(ok.returncode, 0, ok.stderr)


if __name__ == '__main__':
    unittest.main()
