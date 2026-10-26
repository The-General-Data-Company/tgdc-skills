"""A .key file is a credential file unless it starts with a zip signature: then it is a Keynote presentation."""
import io
import os
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
import spec_archives as A
import spec_taxonomy as T

NOW = datetime(2026, 10, 25)
PEM = b'-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n'


def keynote_bytes(slides=3):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('Index/Document.iwa', b'\x00' * 200)
        for number in range(slides):
            archive.writestr(f'Index/Slide-{number}.iwa', os.urandom(50))
    return buffer.getvalue()


def zip_of(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()
        self.deck = keynote_bytes()
        files = {'Deck.key': self.deck, 'server.key': PEM, 'empty.key': b'', 'password list.key': keynote_bytes(), 'Q3 plan.KEY': keynote_bytes(),
                 'plain.txt': b'text', 'cert.pem': PEM}
        for name, data in files.items():
            (self.data / name).write_bytes(data)

    def entries(self):
        entries, _ = formatspec.walk_roots([self.data])
        return {entry.name: entry for entry in entries}

    def test_a_key_file_with_a_zip_signature_is_a_keynote_and_anything_else_stays_a_credential(self):
        entries = self.entries()
        flags = {name: (entry.credential, entry.keynote) for name, entry in entries.items()}
        self.assertEqual(flags, {'Deck.key': (False, True), 'Q3 plan.KEY': (False, True), 'server.key': (True, False), 'empty.key': (True, False),
                                 'password list.key': (True, False), 'plain.txt': (False, False), 'cert.pem': (True, False)})

    def test_the_walk_reads_four_bytes_of_a_key_file_and_nothing_of_other_files(self):
        original = formatspec.head_of
        calls = []

        def spy(path, count=600):
            calls.append((os.path.basename(path), count))
            return original(path, count)
        with mock.patch.object(formatspec, 'head_of', spy):
            self.entries()
        self.assertEqual({name for name, _ in calls}, {'Deck.key', 'server.key', 'empty.key', 'Q3 plan.KEY'})   # credential names and non-key files are not opened
        self.assertEqual({count for _, count in calls}, {4})

    def test_the_run_counts_a_keynote_as_a_presentation_and_a_key_as_a_credential_file(self):
        spec = formatspec.run_spec([self.data], self.base / 'out', workers=1, tools={}, report_date=NOW, log=open(os.devnull, 'w'))
        rows = {row['name']: row for row in spec['categories']}
        self.assertEqual((rows[T.PRESENTATIONS]['files'], rows[T.PRESENTATIONS]['formats']), (2, ['key']))
        self.assertEqual(rows[T.CREDENTIALS]['files'], 4)                   # server.key, empty.key, password list.key, cert.pem
        self.assertEqual(spec['totals']['hashed_files'], 7)                 # every one of them is hashed
        self.assertEqual(spec['totals']['unique_contents'], 6)              # the two PEM files are one content

    def test_the_category_follows_the_flag(self):
        self.assertEqual(T.category_of('Deck.key'), T.CREDENTIALS)
        self.assertEqual(T.category_of('Deck.key', keynote=True), T.PRESENTATIONS)
        self.assertEqual(T.category_of('~$Deck.key', keynote=True), T.SYSTEM)


class ArchiveMemberTests(unittest.TestCase):
    def read(self, entries):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        reader = A.Reader(self.tmp.name, tools={})
        data = zip_of(entries)
        self.assertTrue(reader.open_archive('zip', 'x.zip', io.BytesIO(data), 1, '', compressed=len(data)))
        return {member['n']: member for member in reader.members}

    def test_a_key_member_with_a_zip_signature_is_a_keynote_and_anything_else_stays_a_credential(self):
        deck = keynote_bytes()
        by_name = self.read([('Decks/Deck.key', deck), ('Decks/server.key', PEM), ('Decks/password list.key', deck), ('passwords/Deck.key', deck),
                             ('empty.key', b'')])
        self.assertNotIn('cred', by_name['Decks/Deck.key'])
        self.assertTrue(by_name['Decks/Deck.key']['keynote'])
        self.assertEqual(by_name['Decks/Deck.key']['s'], len(deck))
        import hashlib
        self.assertEqual(by_name['Decks/Deck.key']['h'], hashlib.sha256(deck).hexdigest())       # the four bytes that were looked at are part of the checksum
        self.assertNotIn('lang', by_name['Decks/Deck.key'])                                       # a presentation of this kind is hashed, not parsed
        for name in ('Decks/server.key', 'Decks/password list.key', 'passwords/Deck.key', 'empty.key'):
            with self.subTest(name=name):
                self.assertTrue(by_name[name]['cred'])
                self.assertNotIn('keynote', by_name[name])
                self.assertNotIn('lang', by_name[name])


if __name__ == '__main__':
    unittest.main()
