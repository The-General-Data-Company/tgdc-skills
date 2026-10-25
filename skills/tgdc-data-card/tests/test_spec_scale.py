"""Scale smoke test: 5,000 small files give consistent totals. Set TGDC_SKIP_SLOW=1 to skip it."""
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_samples as S
import spec_taxonomy as T

LOG = open(os.devnull, 'w')
COUNTS = {'txt': 2400, 'docx': 1200, 'pdf': 600, 'eml': 300, 'xlsx': 300, 'zip': 100, 'png': 100}


@unittest.skipIf(os.environ.get('TGDC_SKIP_SLOW'), 'slow test skipped')
class ScaleTests(unittest.TestCase):
    def test_five_thousand_files_finish_with_consistent_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            data = base / 'data'
            texts = [S.EN, S.RO]
            pdfs = [S.pdf_bytes([('text', S.A4P, 1)]), S.pdf_bytes([('drawing', S.A3P, 2)]), S.pdf_bytes([('text', S.A4P, 3), ('scan', S.A4P, 4)])]
            size = 0
            members = 0
            member_bytes = 0
            for kind, count in COUNTS.items():
                for number in range(count):
                    directory = data / kind / f'd{number // 50:03d}'
                    directory.mkdir(parents=True, exist_ok=True)
                    text = texts[number % 2] * 2 + f' Item {kind} {number}.'
                    if kind == 'txt':
                        payload = text.encode()
                    elif kind == 'docx':
                        payload = S.docx_bytes(text, 2015 + number % 10)
                    elif kind == 'pdf':
                        payload = pdfs[number % 3] + b'\n%unique ' + str(number).encode() + b'\n'
                    elif kind == 'eml':
                        payload = S.eml_bytes(f'Subject {number}', 'a@x.test', 'b@x.test', text, year=2020 + number % 5)
                    elif kind == 'xlsx':
                        payload = S.xlsx_bytes([text, 'Row ' + str(number)], 2016 + number % 8)
                    elif kind == 'png':
                        payload = S.png_bytes(number)
                    else:
                        buffer = io.BytesIO()
                        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                            for member in range(3):
                                inner = (texts[member % 2] * 2 + f' Member {number} {member}.').encode()
                                archive.writestr(f'm{member}.txt', inner)
                                members += 1
                                member_bytes += len(inner)
                        payload = buffer.getvalue()
                    (directory / f'{kind}{number:04d}.{kind}').write_bytes(payload)
                    size += len(payload)
            started = time.monotonic()
            spec = formatspec.run_spec([data], base / 'out', workers=4, tools={}, report_date=datetime(2026, 10, 25), log=LOG)
            elapsed = time.monotonic() - started
            print(f'\nscale smoke: {sum(COUNTS.values()):,} files read in {elapsed:.1f} s', file=sys.stderr)
        total = sum(COUNTS.values()) + members
        self.assertEqual(spec['totals']['files'], total)
        self.assertEqual(spec['totals']['bytes'], size + member_bytes)
        self.assertEqual(spec['totals']['hashed_files'], total)
        self.assertEqual(spec['totals']['unique_contents'], total)
        self.assertEqual(sum(c['files'] for c in spec['categories']), total)
        self.assertEqual(sum(c['bytes'] for c in spec['categories']), size + member_bytes)
        by_name = {c['name']: c['files'] for c in spec['categories']}
        self.assertEqual(by_name, {T.DOCUMENTS: 2400 + 1200 + members + 600, T.SPREADSHEETS: 300, T.EMAIL: 300, T.PHOTOS: 100, T.ARCHIVES: 100})
        self.assertEqual(spec['parts']['archive_contents'], {'files': members, 'bytes': member_bytes, 'archives': 100,
                                                            'new_files': members, 'new_bytes': member_bytes})
        languages = spec['languages']
        rows = {r['code']: r for r in languages['rows']}
        examined = 2400 + 1200 + 600 + 300 + 300 + members
        self.assertEqual(languages['total']['files'], examined)
        self.assertEqual(sum(r['files'] for r in languages['rows']) + languages.get('none', {}).get('files', 0), examined)
        self.assertEqual(languages['total']['messages'], 300)
        self.assertGreater(rows['en']['files'], 2000)
        self.assertGreater(rows['ro']['files'], 2000)
        self.assertEqual(sum(r['files'] for r in spec['years']['rows']), 5000)
        self.assertEqual(spec['drawings']['sheets'], 200)                  # one A3 sheet in each of the 200 drawing PDFs


if __name__ == '__main__':
    unittest.main()
