"""Skill folder checks: catalog shape and the profile and render commands run from the folder."""
import hashlib
import json
import re
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

    def test_public_catalog_is_target_not_123_automatic_features(self):
        guide = (ROOT / 'references/field-guide.md').read_text()
        catalog = json.loads((ROOT / 'references/field-catalog.json').read_text())
        self.assertEqual(len(catalog['fields']), 123)
        self.assertEqual(len({field['id'] for field in catalog['fields']}), 123)
        self.assertFalse([field['id'] for field in catalog['fields'] if field['id'].startswith('task_') or field['level'] == 'task'])
        self.assertNotIn('150', guide)
        self.assertIn('123', guide)
        self.assertEqual(len(catalog['automatic_metrics']), 16)
        self.assertIn('first non-empty row', guide)
        self.assertIn('target catalog', guide)
        self.assertIn('not implemented', guide)

    def partner_facing_files(self):
        files = [ROOT / 'SKILL.md', ROOT / 'agents/openai.yaml', ROOT / 'references/field-guide.md', ROOT / 'references/format-spec.md',
                 ROOT / 'references/privacy.md', ROOT / 'references/field-catalog.json', ROOT / 'assets/README.md']
        files += sorted(SCRIPTS.glob('*.py')) + sorted((ROOT / 'assets/examples').glob('*.json'))
        return files

    def test_partner_facing_wording_rules(self):
        words = ['la' + 'bs?', 'buy' + 'er', 'resa' + 'le', 'rese' + 'll', 'li' + 'cens', 'market' + 'place', 'moneti' + 'z', 'train' + 'ing', 'reinforce' + 'ment',
                 'r' + 'l', 'roll' + 'out', 'pass ?' + 'rate']
        forbidden = re.compile(r'\b(?:' + '|'.join(words[:2]) + r')\b|' + '|'.join(words[2:9]) + r'|\b' + words[9] + r'\b|' + '|'.join(words[10:]), re.I)
        for sample in ('the ' + 'buy' + 'er', 'a ' + 'la' + 'b', 'AI ' + 'la' + 'bs', 'mark' + 'etplace', 'the ' + 'r' + 'l loop', 'pass' + ' rate', 'train' + 'ing data'):
            self.assertIsNotNone(forbidden.search(sample), sample)
        self.assertIsNone(forbidden.search('mailbox labels and the lab' + 'el column'))
        contraction = re.compile(r"\b(?:don't|doesn't|can't|won't|isn't|aren't|it's|that's|there's|we're|you're|they're|didn't|wasn't|couldn't|shouldn't|wouldn't|hasn't|haven't|let's)\b", re.I)
        traces = re.compile('|'.join(['cla' + 'ude', 'co' + 'dex', 'anthro' + 'pic', 'generated' + ' by', 'chat' + 'gpt']), re.I)
        texts = {path: path.read_text(encoding='utf-8') for path in self.partner_facing_files()}
        readme = ROOT.parents[1] / 'README.md'
        if readme.is_file():
            text = readme.read_text(encoding='utf-8')
            texts[readme] = text[text.index('## tgdc-data-card'):text.index('## Security')]
        for path, text in texts.items():
            for number, line in enumerate(text.splitlines(), 1):
                if line.startswith('license:') or 'Apache License' in line or 'Open Font License' in line or 'Official license:' in line:
                    continue
                rules = (('wording', forbidden), ('contraction', contraction)) + (() if path == readme else (('trace', traces),))
                for rule, pattern in rules:      # the README names the agents it installs into; the skill folder never does
                    self.assertIsNone(pattern.search(line), f'{rule} in {path.name}:{number}')
            self.assertNotIn('\u2013', text, path.name)
            self.assertNotIn('\u2014', text, path.name)
        for path in (ROOT / 'tests').glob('*.py'):
            body = path.read_text(encoding='utf-8')
            self.assertNotIn('\u2013', body, path.name)
            self.assertNotIn('\u2014', body, path.name)

    def test_skill_metadata_matches_folder_name(self):
        text = (ROOT / 'SKILL.md').read_text()
        self.assertTrue(text.startswith('---\nname: tgdc-data-card\n'))
        self.assertEqual(ROOT.name, 'tgdc-data-card')
        self.assertTrue((ROOT / 'LICENSE.txt').is_file())
        self.assertIn('  version: "1.1.1"', text)
        self.assertEqual(len([line for line in text.splitlines() if re.match(r'^\d\. \*\*', line)]), 8)
        for name in ('references/format-spec.md', 'references/privacy.md', 'references/field-guide.md'):
            self.assertIn(name, text)

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

    def test_isolated_mode_runs_profile_and_spec(self):
        # Agent hosts may start downloaded scripts with `python -I`, which drops the script folder from sys.path.
        selected = self.base / 'isolated-input'; selected.mkdir()
        (selected / 'note.txt').write_text('This synthetic document describes a business process for testing. ' * 12)
        output = self.base / 'isolated-report'
        script = SCRIPTS / 'datacard.py'
        for command in (['profile'], ['spec']):
            result = subprocess.run([sys.executable, '-I', str(script), *command, '--source', str(selected), '--output', str(output)],
                                    cwd=self.base, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / 'spec.json').is_file())

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
