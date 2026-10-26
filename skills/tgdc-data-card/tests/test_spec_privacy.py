"""Nothing taken from file names or folder names reaches spec.json or the PDF except fixed vocabulary."""
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import report
import spec_report
import spec_samples as S
import spec_taxonomy as T
from pypdf import PdfReader

LOG = open(os.devnull, 'w')
NOW = datetime(2026, 10, 25)
# Every fragment below sits in a file name, a folder name or an archive name of the adversarial tree.
SECRETS = ['smith', 'acmecorp', 'corp', 'zebracli', 'bob miller', 'miller', 'acme', 'john', 'jane', 'doe', 'roe', 'ceo', 'quarterly',
           'annual', 'backup 2020', 'export of']


def words_in(text, secret):
    return re.search(r'(?<![a-z0-9])' + re.escape(secret) + r'(?![a-z0-9])', text.lower()) is not None


def build_tree(base):
    data = Path(base) / 'data'
    (data / 'Company/ACME Corp').mkdir(parents=True)
    (data / 'Mail/Box1').mkdir(parents=True)
    names = ['J.Smith', 'Anna.Smith', 'Report ACME.acmecorp', 'Plan ACME.acmecorp', 'john.doe@acme.corp', 'jane.roe@acme.corp',
             'Quarterly.zebracli', 'Annual.zebracli']
    for number, name in enumerate(names):
        (data / 'Company/ACME Corp' / name).write_bytes(f'distinct content {number} '.encode() * 20)
    (data / 'Company/ACME Corp/plan.pdf').write_bytes(S.pdf_bytes([('text', S.A4P, 1)]))
    messages = [S.eml_bytes(f'Request {number}', 'Ann <ann@synthetic.test>', 'owner@synthetic.test', S.EN * 2 + f' {number}.', 2021 + number,
                            delivered_to='owner@synthetic.test') for number in range(1, 4)]
    for number, message in enumerate(messages, 1):
        (data / f'Mail/Box1/m{number}.eml').write_bytes(message)
    export = io.BytesIO()
    with zipfile.ZipFile(export, 'w', zipfile.ZIP_DEFLATED) as archive:
        for number, message in enumerate(messages, 1):
            archive.writestr(f'm{number}.eml', message)
    (data / 'Mail/Box1/Export of CEO Bob Miller.Bob Miller backup 2020').write_bytes(export.getvalue())
    config = {'mailboxes': [{'root': str(data / 'Mail/Box1'), 'label': 'Mailbox 1 (owner)'}]}
    return data, config


class AdversarialNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.data, cls.config = build_tree(cls.base)
        cls.out = cls.base / 'out'
        cls.spec = formatspec.run_spec([cls.data], cls.out, config=cls.config, workers=2, tools={}, report_date=NOW, log=LOG)
        cls.path = cls.out / 'spec.json'
        pdf = cls.base / 'card.pdf'
        report.render(ROOT / 'assets/examples/profile.synthetic.json', ROOT / 'assets/examples/answers.synthetic.json', pdf, cls.path)
        cls.pdf_text = ' '.join(' '.join(page.extract_text() for page in PdfReader(pdf).pages).split())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_tree_really_holds_an_alternative_export_archive(self):
        boxes = self.spec['mailboxes']
        self.assertTrue(any(item['kind'] == 'archive' for item in boxes[0]['alternatives']), boxes)

    def test_no_name_fragment_reaches_spec_json(self):
        text = self.path.read_text().lower()
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertFalse(words_in(text, secret), secret)

    def test_no_name_fragment_reaches_the_pdf(self):
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertFalse(words_in(self.pdf_text, secret), secret)

    def test_unknown_extensions_on_several_files_are_grouped(self):
        rows = {(row['ext'], row['category']): row['files'] for row in self.spec['formats']}
        self.assertEqual(rows[(T.IRREGULAR_EXTENSION, T.OTHER)], 8)

    def test_every_extension_in_the_specification_is_a_known_format_or_a_sentinel(self):
        labels = set()

        def collect(value, key=None):
            if isinstance(value, dict):
                for name, item in value.items():
                    if name in ('ext', 'formats', 'extensions', 'metadata_extensions'):
                        labels.update(x for x in ([item] if isinstance(item, str) else item) if isinstance(x, str))
                    collect(item, name)
            elif isinstance(value, list):
                for item in value:
                    collect(item, key)
        collect(self.spec)
        self.assertTrue(labels)
        for label in labels:
            with self.subTest(label=label):
                self.assertTrue(T.is_extension_label(label), label)

    def test_the_alternative_export_archive_reports_a_sentinel_not_its_extension(self):
        archive = next(item for item in self.spec['mailboxes'][0]['alternatives'] if item['kind'] == 'archive')
        self.assertEqual(archive['extensions'], [T.IRREGULAR_EXTENSION])
        self.assertIn(T.IRREGULAR_EXTENSION, self.pdf_text)
        self.assertNotIn('.(other', self.pdf_text)


class ValidateRejectsFreeText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        data, config = build_tree(cls.base)
        config['stages'] = {'title': 'project milestones', 'items': [{'name': 'tender', 'patterns': ['tender']}]}
        config['project_roots'] = [str(data / 'Company')]
        cls.spec = formatspec.run_spec([data], cls.base / 'out', config=config, workers=2, tools={}, report_date=NOW, log=LOG)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_specification_as_written_is_valid(self):
        spec_report.validate(copy.deepcopy(self.spec))

    def reject(self, change):
        spec = copy.deepcopy(self.spec)
        change(spec)
        with self.assertRaises(ValueError):
            spec_report.validate(spec)

    def test_an_extension_outside_the_known_formats_is_rejected_in_every_place(self):
        archive = next(item for item in self.spec['mailboxes'][0]['alternatives'] if item['kind'] == 'archive')
        self.assertTrue(archive)
        changes = {
            'formats ext': lambda s: s['formats'][0].__setitem__('ext', 'smith'),
            'formats category': lambda s: s['formats'][0].__setitem__('category', 'Anything'),
            'category formats': lambda s: s['categories'][0]['formats'].append('acmecorp'),
            'alternative extensions': lambda s: next(i for i in s['mailboxes'][0]['alternatives'] if i['kind'] == 'archive')['extensions'].append('bob miller'),
            'summary alternative extensions': lambda s: next(i for i in s['parts']['alternative_exports']['items'] if i['kind'] == 'archive')['extensions'].append('corp'),
            'sentinel with a prefix': lambda s: s['formats'][0].__setitem__('ext', '.pdf'),
            'mixed case': lambda s: s['formats'][0].__setitem__('ext', 'PDF'),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.reject(change)

    def test_the_two_sentinel_labels_and_known_formats_are_accepted(self):
        for label in (T.NO_EXTENSION, T.IRREGULAR_EXTENSION, 'pdf', 'dwg'):
            with self.subTest(label=label):
                spec = copy.deepcopy(self.spec)
                spec['formats'][0]['ext'] = label
                spec_report.validate(spec)

    def test_labels_and_stage_names_are_short(self):
        self.reject(lambda s: s['mailboxes'][0].__setitem__('label', 'x' * 61))
        self.reject(lambda s: s['mailboxes'][0].__setitem__('label', 'owner@example.test'))
        if 'drawings' in self.spec and 'projects' in self.spec['drawings'] and 'stages' in self.spec['drawings']['projects']:
            self.reject(lambda s: s['drawings']['projects']['stages']['names'].append('y' * 61))

    def test_archive_refusal_reasons_are_fixed_vocabulary(self):
        self.reject(lambda s: s['methodology']['archives']['not_opened'].update({'zip:Bob Miller backup 2020': 1}))
        self.reject(lambda s: s['methodology']['archives']['not_opened'].update({'bob miller:damaged': 1}))


class ConfigLabels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'data').mkdir()
        self.roots = [self.base / 'data']

    def config(self, **changes):
        return formatspec.validate_config(changes, self.roots)

    def test_long_labels_and_addresses_are_rejected(self):
        root = str(self.base / 'data')
        for label in ('x' * 61, 'owner@example.test', 'see https://example.test/x'):
            with self.subTest(label=label), self.assertRaises(formatspec.SpecConfigError):
                self.config(mailboxes=[{'root': root, 'label': label}])
        self.config(mailboxes=[{'root': root, 'label': 'Mailbox 1 (owner, Gmail)'}, {'root': root, 'label': 'x' * 60}])

    def test_long_stage_names_are_rejected(self):
        for name in ('y' * 61, 'office@example.test'):
            with self.subTest(name=name), self.assertRaises(formatspec.SpecConfigError):
                self.config(stages={'items': [{'name': name, 'patterns': ['x']}]})
        self.config(stages={'items': [{'name': 'z' * 60, 'patterns': ['x']}]})


if __name__ == '__main__':
    unittest.main()
