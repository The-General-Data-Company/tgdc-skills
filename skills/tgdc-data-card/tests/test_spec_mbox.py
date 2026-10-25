"""Mailboxes that exist only as .mbox, a single-file mailbox root and a mixed set of roots."""
import mailbox
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import report
import spec_samples as S

LOG = open(os.devnull, 'w')
NOW = datetime(2026, 10, 25)


def build_mbox(path):
    plan = S.pdf_bytes([('drawing', S.A3P, 61)])
    messages = [
        S.eml_bytes('One', 'Ann <ann@synthetic.test>', 'owner@synthetic.test', S.RO * 2, 2022, [('plan.pdf', plan, 'application/pdf'), ('x.dxf', b'0\nEOF\n', 'application/octet-stream')],
                    delivered_to='owner@synthetic.test'),
        S.eml_bytes('Two', 'Bob <bob@synthetic.test>', 'owner@synthetic.test', S.EN * 2, 2023, delivered_to='owner@synthetic.test'),
        S.eml_bytes('Three', 'owner@synthetic.test', 'Ann <ann@synthetic.test>', S.EN * 2 + ' Reply.', 2024),
    ]
    box = mailbox.mbox(str(path))
    for data in messages:
        box.add(data)
    box.flush()
    box.close()


class MboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'data/Mail/Box').mkdir(parents=True)
        build_mbox(self.base / 'data/Mail/Box/primary.mbox')

    def run_spec(self, config, out='out', sources=None):
        return formatspec.run_spec(sources or [self.base / 'data'], self.base / out, config=config, workers=2, tools={}, report_date=NOW, log=LOG)

    def test_a_mailbox_that_exists_only_as_mbox_counts_its_messages(self):
        spec = self.run_spec({'mailboxes': [{'root': str(self.base / 'data/Mail/Box'), 'label': 'Mailbox 1 (owner, Gmail)'}]})
        row = spec['mailboxes'][0]
        size = (self.base / 'data/Mail/Box/primary.mbox').stat().st_size
        self.assertEqual((row['messages'], row['bytes'], row['files'], row['format'], row['period']), (3, size, 1, 'mbox', [2022, 2024]))
        self.assertEqual((row['sent'], row['received'], row['sent_missing']), (1, 2, False))
        self.assertNotIn('alternatives', row)
        self.assertEqual(spec['parts']['mailboxes']['formats'], ['mbox'])
        self.assertEqual(spec['parts']['mailboxes']['messages'], 3)
        self.assertEqual(spec['languages']['other'], {'files': 3, 'bytes': spec['languages']['total']['bytes'], 'messages': 3})
        self.assertEqual(spec['languages']['rows'], [])
        self.assertEqual(spec['drawings']['sheets'], 1)
        self.assertEqual(spec['drawings']['locations'], {'mail': 1})
        self.assertEqual(spec['cad']['mail'], 1)
        self.assertEqual([r['messages'] for r in spec['years']['rows']], [1, 1, 1])

    def test_a_mailbox_root_may_be_one_file_and_the_pdf_still_renders(self):
        spec = self.run_spec({'mailboxes': [{'root': str(self.base / 'data/Mail/Box/primary.mbox')}]}, 'single')
        self.assertEqual(spec['mailboxes'][0]['label'], 'Mailbox 1')
        self.assertEqual(spec['mailboxes'][0]['messages'], 3)
        import json
        path = self.base / 'spec.json'
        path.write_text(json.dumps(spec))
        report.render(ROOT / 'assets/examples/profile.synthetic.json', ROOT / 'assets/examples/answers.synthetic.json', self.base / 'x.pdf', path)
        from pypdf import PdfReader
        text = ' '.join(' '.join(page.extract_text() for page in PdfReader(self.base / 'x.pdf').pages).split())
        self.assertIn('3 e-mail messages (.mbox)', text)
        self.assertIn('Mailbox 1', text)

    def test_mail_outside_a_declared_mailbox_is_an_ordinary_file(self):
        spec = self.run_spec({}, 'plain')
        self.assertNotIn('mailboxes', spec)
        self.assertNotIn('mailboxes', spec['parts'])
        self.assertEqual(spec['parts']['file_archive']['files'], 1)
        self.assertEqual(spec['languages']['total']['messages'], 3)         # the messages are still examined
        self.assertFalse(spec['years']['has_mail'])

    def test_two_selected_roots_are_read_together(self):
        (self.base / 'second').mkdir()
        (self.base / 'second/note.txt').write_text(S.EN * 2)
        spec = self.run_spec({}, 'two', sources=[self.base / 'data', self.base / 'second'])
        self.assertEqual(spec['totals']['files'], 2)


if __name__ == '__main__':
    unittest.main()
