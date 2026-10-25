"""Skill folder checks: catalog shape and the profile and render commands run from the folder."""
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_public_catalog_is_target_not_150_automatic_features(self):
        guide = (ROOT / 'references/field-guide.md').read_text()
        catalog = json.loads((ROOT / 'references/field-catalog.json').read_text())
        self.assertEqual(len(catalog['fields']), 150)
        self.assertEqual(len({field['id'] for field in catalog['fields']}), 150)
        self.assertEqual(len(catalog['automatic_metrics']), 16)
        self.assertIn('first non-empty row', guide)
        self.assertIn('target catalog', guide)
        self.assertIn('not implemented', guide)

    def test_skill_metadata_matches_folder_name(self):
        text = (ROOT / 'SKILL.md').read_text()
        self.assertTrue(text.startswith('---\nname: tgdc-data-card\n'))
        self.assertEqual(ROOT.name, 'tgdc-data-card')
        self.assertTrue((ROOT / 'LICENSE.txt').is_file())

    def test_actual_profile_and_render_from_skill_folder(self):
        selected = self.base / 'selected'; selected.mkdir()
        text = selected / 'synthetic.txt'
        text.write_text('This synthetic document describes a business process for testing. ' * 12)
        (selected / 'synthetic.csv').write_text('id,state\n1,open\n2,closed\n')
        source_hash = hashlib.sha256(text.read_bytes()).digest()
        output = self.base / 'report'
        script = SCRIPTS / 'datacard.py'
        result = subprocess.run([sys.executable, str(script), 'profile', '--source', str(selected), '--output', str(output)], cwd=self.base, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = json.loads((output / 'profile.json').read_text())
        self.assertEqual(profile['inventory']['files'], 2)
        self.assertEqual(profile['languages']['denominator'], 2)
        self.assertEqual(profile['metrics']['rows_processed']['value'], 2)
        self.assertNotIn(str(selected), json.dumps(profile))
        self.assertEqual(source_hash, hashlib.sha256(text.read_bytes()).digest())
        self.assertEqual(stat.S_IMODE((output / 'manifest.private.json').stat().st_mode), 0o600)
        answers = output / 'answers.json'; answers.write_text(json.dumps({'provider_context': 'Synthetic test organization', 'dataset_departments': 'Synthetic operations'}))
        pdf = output / 'TGDC-Data-Card.pdf'
        result = subprocess.run([sys.executable, str(script), 'render', '--profile', str(output / 'profile.json'), '--answers', str(answers), '--output', str(pdf)], cwd=self.base, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        from pypdf import PdfReader
        pages = PdfReader(pdf).pages
        self.assertGreaterEqual(len(pages), 2)
        contents = '\n'.join(page.extract_text() for page in pages)
        self.assertIn('Synthetic operations', contents)
        self.assertIn('Review', result.stdout)

    def test_unsupported_platform_fails_before_filesystem_work(self):
        import profiler
        with patch.object(profiler.os, 'name', 'nt'), patch.object(profiler, 'checked_path') as checked:
            with self.assertRaisesRegex(ValueError, 'macOS|POSIX'):
                profiler.profile(['/not-selected'], self.base / 'out')
            checked.assert_not_called()
        with patch.object(profiler.os, 'supports_dir_fd', set()), patch.object(profiler, 'checked_path') as checked:
            with self.assertRaisesRegex(ValueError, 'macOS|POSIX'):
                profiler.profile(['/not-selected'], self.base / 'out')
            checked.assert_not_called()


if __name__ == '__main__':
    unittest.main()
