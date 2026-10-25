"""No real customer content is used by these safety and evidence tests."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from profiler import profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve(); self.root = self.base / 'input'; self.root.mkdir()
        self.out = self.base / 'output'

    def tearDown(self):
        self.tmp.cleanup()

    def test_contract_duplicates_privacy_and_source_immutability(self):
        source = self.root / 'orders.csv'; source.write_text('order_id,status\n1,closed\n2,open\n')
        (self.root / 'copy.csv').write_bytes(source.read_bytes())
        (self.root / 'private.txt').symlink_to(self.base / 'elsewhere.txt')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = profile([self.root], self.out)
        self.assertEqual(result['inventory']['files'], 2)
        self.assertEqual(result['metrics']['duplicate_files']['value'], 1)
        self.assertEqual(result['metrics']['duplicate_files']['denominator'], 2)
        self.assertNotIn('order_id', json.dumps(result)); self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn('closed', json.dumps(result)); self.assertNotIn('workflow_completion', result['metrics'])
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        private = self.out / 'manifest.private.json'
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertIn(str(source), private.read_text())
        self.assertNotIn('order_id', private.read_text())
        self.assertEqual(json.loads((self.out / 'profile.json').read_text()), result)

    def test_hidden_credentials_unsupported_empty_and_errors_counted(self):
        (self.root / '.hidden').write_text('PRIVATE_SYNTHETIC_SENTINEL')
        (self.root / 'credentials.json').write_text('PRIVATE_SYNTHETIC_SENTINEL')
        (self.root / 'service-account.json').write_text('PRIVATE_SYNTHETIC_SENTINEL')
        (self.root / 'client_secret_123.json').write_text('PRIVATE_SYNTHETIC_SENTINEL')
        (self.root / 'empty.txt').touch(); (self.root / 'bad.pdf').write_text('secret-error-payload')
        (self.root / 'drawing.dwg').write_bytes(b'synthetic')
        result = profile([self.root], self.out)
        self.assertEqual(result['inventory']['files'], 3)
        self.assertEqual(result['extraction']['errors'], 1)
        self.assertEqual(result['extraction']['status_counts']['malformed'], 1)
        self.assertEqual(result['extraction']['status_counts']['unsupported'], 1)
        self.assertGreaterEqual(result['extraction']['skipped'], 3)
        self.assertNotIn('PRIVATE_SYNTHETIC_SENTINEL', json.dumps(result))
        self.assertNotIn('secret-error-payload', json.dumps(result))

    def test_missing_overlapping_and_symlink_roots_rejected(self):
        child = self.root / 'child'; child.mkdir()
        alias = self.base / 'alias'; alias.symlink_to(self.root, target_is_directory=True)
        for roots in [[self.base / 'missing'], [self.root, child], [alias], [alias / 'child'], [self.root, self.root]]:
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                profile(roots, self.out)

    def test_output_overlaps_and_symlink_output_rejected(self):
        output_alias = self.base / 'output-alias'; output_alias.symlink_to(self.root, target_is_directory=True)
        for output in [self.root, self.root / 'report', self.base, output_alias / 'report']:
            with self.subTest(output=output), self.assertRaises(ValueError):
                profile([self.root], output)
        (self.root / 'one.txt').write_text('one')
        with self.assertRaises(ValueError):
            profile([self.root / 'one.txt'], self.root)

    def test_inventory_sample_and_size_caps_have_honest_denominators(self):
        for i in range(4):
            (self.root / f'{i}.txt').write_text('sample words')
        result = profile([self.root], self.out, limits={'max_files': 3, 'max_sample_files': 1, 'max_file_bytes': 4})
        self.assertFalse(result['scope']['enumeration_complete'])
        self.assertEqual(result['inventory']['files'], 3)
        self.assertEqual(result['extraction']['status_counts']['size_cap'], 1)
        self.assertIsNone(result['metrics']['duplicate_files']['value'])
        self.assertEqual(result['metrics']['duplicate_files']['denominator'], 0)
        self.assertEqual(result['languages']['denominator'], 1)
        self.assertEqual(result['languages']['document_counts'], {'unknown': 1})
        self.assertIn('sample_cap', result['extraction']['status_counts'])

    def test_question_ids_and_evidence_contract(self):
        (self.root / 'one.csv').write_text('id,status\n1,complete\n')
        result = profile([self.root], self.out)
        ids = {question['id'] for question in result['questions']}
        self.assertTrue({'rights', 'archive_scope', 'history', 'systems', 'case_mapping', 'provider_context'} <= ids)
        for metric in result['metrics'].values():
            self.assertEqual(set(metric), {'value', 'unit', 'basis', 'denominator', 'method', 'coverage'})
        self.assertEqual(result['schema_version'], '1.0')
        self.assertIn('filesystem_mtime_bounds', result['metrics'])
        self.assertIn('eml_content_date_bounds', result['metrics'])
        self.assertIsNone(result['metrics']['eml_content_date_bounds']['value'])
        self.assertNotIn('tokens', result['metrics'])

    def test_sample_stratified_by_extension(self):
        for i in range(4):
            (self.root / f'{i}.txt').write_text('text')
        (self.root / 'table.csv').write_text('id\n1\n')
        result = profile([self.root], self.out, limits={'max_sample_files': 2})
        manifest = json.loads((self.out / 'manifest.private.json').read_text())
        selected = {Path(item['path']).suffix for item in manifest['files'] if item['status'] == 'ok'}
        self.assertEqual(selected, {'.txt', '.csv'})

    def test_real_language_detection_content_only_and_english_sample_share(self):
        (self.root / 'German-name.txt').write_text('This synthetic report contains English sentences describing customer service workflows and archived documents. The team reviews every request and records its outcome for future analysis.')
        (self.root / 'English-name.txt').write_text('Dieser synthetische Bericht enthält deutsche Sätze über die Bearbeitung von Kundenanfragen und archivierte Unterlagen. Das Team prüft jede Anfrage und dokumentiert das Ergebnis für spätere Analysen.')
        (self.root / 'tiny.txt').write_text('abc 123')
        result = profile([self.root], self.out)
        self.assertEqual(result['languages']['document_counts'], {'de': 1, 'en': 1, 'unknown': 1})
        self.assertEqual(result['metrics']['english_document_share']['value'], 1 / 3)
        self.assertEqual(result['metrics']['english_document_share']['denominator'], 3)
        self.assertEqual(result['metrics']['english_document_share']['basis'], 'inferred')
        self.assertEqual(sum(result['languages']['unknown_reasons'].values()), 1)
        self.assertNotIn('model_inference', result['languages']['unknown_reasons'])

    def test_missing_detector_is_unknown_and_does_not_claim_zero_english(self):
        (self.root / 'one.txt').write_text('This synthetic document contains sufficient English text to classify a document when the local language dependency is installed and available.')
        run = subprocess.run([sys.executable, '-S', str(SCRIPTS / 'datacard.py'), 'profile', '--source', str(self.root), '--output', str(self.out)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads((self.out / 'profile.json').read_text())
        self.assertEqual(result['languages']['document_counts'], {'unknown': 1})
        self.assertIsNone(result['metrics']['english_document_share']['value'])

    def test_hash_budget_and_symlink_subdirectory_do_not_read_outside(self):
        (self.root / 'one.txt').write_text('sample')
        elsewhere = self.base / 'elsewhere'; elsewhere.mkdir()
        (elsewhere / 'outside.txt').write_text('outside source')
        (self.root / 'link').symlink_to(elsewhere, target_is_directory=True)
        result = profile([self.root], self.out, limits={'max_hash_bytes': 1})
        self.assertEqual(result['inventory']['files'], 1)
        self.assertIsNone(result['metrics']['duplicate_files']['value'])
        self.assertEqual(result['extraction']['status_counts']['symlink'], 1)

    def test_unknown_extension_cannot_leak_identifier_in_shareable_inventory(self):
        (self.root / 'record.customer123').write_bytes(b'synthetic')
        result = profile([self.root], self.out)
        self.assertEqual(result['inventory']['by_extension'], {'other': {'files': 1, 'bytes': 9}})
        self.assertNotIn('customer123', json.dumps(result))
        manifest = json.loads((self.out / 'manifest.private.json').read_text())
        self.assertEqual(manifest['files'][0]['source_extension'], 'customer123')

    def test_nonfinite_deadlines_are_rejected_before_output_creation(self):
        for index, deadline in enumerate([float('nan'), float('inf')]):
            output = self.base / f'bad-output-{index}'
            with self.assertRaises(ValueError):
                profile([self.root], output, limits={'parser_seconds': deadline})
            self.assertFalse(output.exists())

    def test_language_fraction_method_does_not_claim_population_lower_bound(self):
        (self.root / 'sample.txt').write_text('This synthetic English report contains sufficient text to classify the document while explicitly acknowledging that the model can make classification errors.')
        result = profile([self.root], self.out)
        method = result['metrics']['english_document_share']['method']
        self.assertNotIn('lower bound', method.lower())
        self.assertIn('classified-English fraction of attempted sample', method)

    def test_output_directory_swap_after_validation_cannot_write_into_source(self):
        from unittest.mock import patch
        import profiler
        (self.root / 'one.txt').write_text('source bytes')
        original = profiler.write_json
        swapped = False

        def swap_before_write(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                self.out.rename(self.base / 'original-output')
                self.out.symlink_to(self.root, target_is_directory=True)
                swapped = True
            return original(*args, **kwargs)

        with patch('profiler.write_json', side_effect=swap_before_write):
            try:
                profile([self.root], self.out)
            except (ValueError, OSError):
                pass
        self.assertTrue(swapped)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['one.txt'])
        self.assertEqual((self.root / 'one.txt').read_text(), 'source bytes')

    def test_output_swap_at_last_path_check_is_rejected_before_source_writes(self):
        from unittest.mock import patch
        import profiler
        (self.root / 'one.txt').write_text('source bytes')
        self.out.mkdir()
        original = profiler.checked_path
        checks = 0

        def swap_after_check(path):
            nonlocal checks
            checked = original(path)
            if checked == self.out:
                checks += 1
                if checks == 2:
                    self.out.rename(self.base / 'original-output')
                    self.out.symlink_to(self.root, target_is_directory=True)
            return checked

        with patch('profiler.checked_path', side_effect=swap_after_check):
            try:
                profile([self.root], self.out)
            except (ValueError, OSError):
                pass
        self.assertEqual(checks, 2)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['one.txt'])

    def test_missing_output_parent_swapped_during_creation_cannot_create_in_source(self):
        from unittest.mock import patch
        import profiler
        (self.root / 'one.txt').write_text('source bytes')
        output = self.base / 'new-parent' / 'report'
        original = profiler.os.mkdir
        swapped = False

        def swap_after_parent_creation(path, *args, **kwargs):
            nonlocal swapped
            result = original(path, *args, **kwargs)
            if Path(path).name == 'new-parent' and not swapped:
                (self.base / 'new-parent').rename(self.base / 'original-parent')
                (self.base / 'new-parent').symlink_to(self.root, target_is_directory=True)
                swapped = True
            return result

        with patch('profiler.os.mkdir', side_effect=swap_after_parent_creation):
            try:
                profile([self.root], output)
            except (ValueError, OSError):
                pass
        self.assertTrue(swapped)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['one.txt'])

    def test_regular_source_replaced_by_fifo_cannot_block_before_parser(self):
        script = r"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import profiler
root, output = Path(sys.argv[2]), Path(sys.argv[3])
source = root / 'race.txt'; source.write_text('original regular file')
original = profiler.inventory
def replace_after_inventory(*args, **kwargs):
    result = original(*args, **kwargs)
    source.unlink(); os.mkfifo(source)
    return result
with patch('profiler.inventory', side_effect=replace_after_inventory):
    result = profiler.profile([root], output)
assert result['extraction']['status_counts']['io_error'] == 1
print('FIFO replacement rejected without blocking')
"""
        try:
            run = subprocess.run([sys.executable, '-c', script, str(SCRIPTS), str(self.root), str(self.out)], capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            self.fail('FIFO replacement blocked before the parser deadline')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('FIFO replacement rejected', run.stdout)

    def test_successfully_parsed_files_are_distinct_from_files_with_extracted_text(self):
        import io
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        pdf = io.BytesIO(); writer.write(pdf)
        (self.root / 'no-text.pdf').write_bytes(pdf.getvalue())
        (self.root / 'empty.json').write_text('{}')
        (self.root / 'text.txt').write_text('PRIVATE_TEXT_METRIC_SENTINEL')
        result = profile([self.root], self.out)
        self.assertEqual(result['metrics']['documents_processed']['value'], 3)
        metric = result['metrics']['files_with_extracted_text']
        self.assertEqual(metric['value'], 1)
        self.assertEqual(metric['unit'], 'files')
        self.assertEqual(metric['denominator'], 3)
        self.assertEqual(metric['basis'], 'measured')
        self.assertEqual(metric['coverage'], result['metrics']['extracted_characters']['coverage'])
        self.assertNotIn('PRIVATE_TEXT_METRIC_SENTINEL', json.dumps(result))
        manifest = (self.out / 'manifest.private.json').read_text()
        self.assertNotIn('PRIVATE_TEXT_METRIC_SENTINEL', manifest)

    def test_whitespace_only_extraction_has_observed_zero_text_files(self):
        (self.root / 'blank.txt').write_text('   \n   ')
        result = profile([self.root], self.out)
        self.assertEqual(result['metrics']['documents_processed']['value'], 1)
        self.assertEqual(result['metrics']['files_with_extracted_text']['value'], 0)
        self.assertEqual(result['metrics']['files_with_extracted_text']['denominator'], 1)
        self.assertEqual(result['metrics']['extracted_characters']['value'], 7)

    def test_deadline_terminates_worker(self):
        (self.root / 'one.txt').write_text('sample')
        result = profile([self.root], self.out, limits={'parser_seconds': 0.000001})
        self.assertEqual(result['extraction']['status_counts']['timeout'], 1)

    def test_cli_errors_do_not_expose_paths_or_source(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'datacard.py'), 'profile', '--source', str(self.base / 'secret-name'), '--output', str(self.out)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('secret-name', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
