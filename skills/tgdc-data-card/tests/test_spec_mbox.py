"""Mailboxes that exist only as .mbox, a single-file mailbox root and a mixed set of roots."""
import mailbox
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

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


def write_mbox(path, messages):
    box = mailbox.mbox(str(path))
    for data in messages:
        box.add(data)
    box.flush()
    box.close()


def note(number, ids=True, body=None):
    data = S.eml_bytes(f'Note {number}', 'Ann <ann@synthetic.test>', 'owner@synthetic.test', body or (S.EN * 2 + f' Number {number}.'), 2022,
                       delivered_to='owner@synthetic.test', message_id=f'<note{number}@synthetic.test>')
    return data if ids else re.sub(rb'(?m)^Message-ID:.*\n', b'', data)


class MailboxIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.box = self.base / 'data/Mail/Box'
        self.box.mkdir(parents=True)
        self.config = {'mailboxes': [{'root': str(self.box), 'label': 'Mailbox 1 (owner)'}]}

    def run_spec(self, out='out', **kwargs):
        return formatspec.run_spec([self.base / 'data'], self.base / out, config=self.config, workers=2, tools={}, report_date=NOW, log=LOG, **kwargs)

    def pdf_text(self, spec, name='x.pdf'):
        import json
        path = self.base / 'spec.json'
        path.write_text(json.dumps(spec))
        report.render(ROOT / 'assets/examples/profile.synthetic.json', ROOT / 'assets/examples/answers.synthetic.json', self.base / name, path)
        from pypdf import PdfReader
        return ' '.join(' '.join(page.extract_text() for page in PdfReader(self.base / name).pages).split())

    def test_hostile_markup_in_one_message_does_not_lose_the_mailbox(self):
        messages = [note(number) for number in range(99)]
        messages.append(note(99, body='<html><body>' + '<script>' * 400000 + 'text'))
        write_mbox(self.box / 'all.mbox', messages)
        started = time.monotonic()
        spec = self.run_spec(limits={'archive_seconds': 120})
        self.assertLess(time.monotonic() - started, 100)
        row = spec['mailboxes'][0]
        self.assertEqual(row['messages'], 100)
        self.assertNotIn('incomplete', row)
        self.assertEqual(spec['methodology']['time_limited_files'], 0)

    def test_a_deadline_keeps_the_message_total_and_says_the_mailbox_is_incomplete(self):
        write_mbox(self.box / 'all.mbox', [note(number) for number in range(300)])
        spec = self.run_spec(limits={'archive_seconds': 0.0001})
        row = spec['mailboxes'][0]
        self.assertEqual(row['messages'], 300)
        self.assertGreater(row['incomplete']['messages'], 0)
        self.assertEqual(spec['methodology']['time_limited_files'], 1)
        text = self.pdf_text(spec)
        self.assertRegex(text, r'Mailbox 1 \(owner\): \d+ messages? (was|were) counted but not analyzed in full')
        self.assertIn('300', text)

    def test_a_complete_mailbox_is_not_flagged(self):
        write_mbox(self.box / 'all.mbox', [note(number) for number in range(5)])
        spec = self.run_spec()
        self.assertNotIn('incomplete', spec['mailboxes'][0])
        self.assertNotIn('not analyzed in full', self.pdf_text(spec))

    def test_known_message_ids_are_built_once_per_mailbox_and_travel_with_the_workers(self):
        for number in range(3):
            (self.box / f'm{number}.eml').write_bytes(note(number))
        for name in ('a.mbox', 'b.mbox', 'c.mbox'):
            write_mbox(self.box / name, [note(number) for number in range(3)] + [note(10 + len(name))])
        seen = []
        original = formatspec.run_pool

        def spy(tasks, count, settings, *args, **kwargs):
            seen.append((list(tasks), settings))
            return original(tasks, count, settings, *args, **kwargs)
        with mock.patch.object(formatspec, 'run_pool', spy):
            spec = self.run_spec()
        mbox_tasks, settings = seen[-1]
        self.assertEqual(sorted(task['name'] for task in mbox_tasks), ['a.mbox', 'b.mbox', 'c.mbox'])
        self.assertTrue(all('known' not in task for task in mbox_tasks))
        self.assertEqual(len(settings['known'][0]), 3)
        self.assertEqual(spec['mailboxes'][0]['messages'], 3 + 1)

    def test_messages_without_an_id_are_not_counted_twice_across_eml_and_mbox(self):
        for number in range(3):
            (self.box / f'm{number}.eml').write_bytes(note(number, ids=False))
        write_mbox(self.box / 'all.mbox', [note(number, ids=False) for number in range(3)])
        spec = self.run_spec()
        row = spec['mailboxes'][0]
        self.assertEqual(row['messages'], 3)
        self.assertEqual([item['kind'] for item in row['alternatives']], ['mbox'])
        self.assertEqual(spec['languages']['total']['messages'], 3)

    def test_overlapping_mbox_exports_count_each_message_once(self):
        write_mbox(self.box / 'inbox.mbox', [note(number) for number in range(0, 5)])
        write_mbox(self.box / 'older.mbox', [note(number) for number in range(3, 8)])
        spec = self.run_spec()
        self.assertEqual(spec['mailboxes'][0]['messages'], 8)
        self.assertEqual(spec['parts']['mailboxes']['messages'], 8)
        self.assertEqual(spec['languages']['total']['messages'], 8)
        self.assertEqual(sum(row['messages'] for row in spec['years']['rows']), 8)

    def test_overlapping_exports_without_ids_count_each_message_once(self):
        write_mbox(self.box / 'inbox.mbox', [note(number, ids=False) for number in range(0, 5)])
        write_mbox(self.box / 'older.mbox', [note(number, ids=False) for number in range(3, 8)])
        self.assertEqual(self.run_spec()['mailboxes'][0]['messages'], 8)

    def test_a_copy_of_an_mbox_file_is_an_alternative_export(self):
        write_mbox(self.box / 'inbox.mbox', [note(number) for number in range(4)])
        (self.box / 'copy of inbox.mbox').write_bytes((self.box / 'inbox.mbox').read_bytes())
        spec = self.run_spec()
        row = spec['mailboxes'][0]
        self.assertEqual(row['messages'], 4)
        self.assertEqual([(item['kind'], item['files']) for item in row['alternatives']], [('mbox', 1)])
        self.assertEqual(spec['languages']['total']['messages'], 4)

    def test_an_mbox_that_could_not_be_read_is_reported_and_the_mailbox_stays(self):
        write_mbox(self.box / 'all.mbox', [note(number) for number in range(3)])
        original = formatspec.run_pool

        def killed(tasks, count, settings, on_result, target=None):
            for task in tasks:
                on_result(task, {'status': 'timeout', 'sha': '0' * 64})       # the worker was stopped by the pool
        with mock.patch.object(formatspec, 'run_pool', killed):
            spec = self.run_spec()
        row = spec['mailboxes'][0]
        self.assertEqual(row['messages'], 0)
        self.assertEqual(row['incomplete'], {'messages': 0, 'files': 1})
        self.assertEqual(spec['methodology']['time_limited_files'], 1)
        self.assertRegex(self.pdf_text(spec), r'1 mailbox file could not be read')


if __name__ == '__main__':
    unittest.main()
