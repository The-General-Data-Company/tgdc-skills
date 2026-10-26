"""The two-part PDF: card pages, then the data format specification built from spec.json only."""
import copy
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import report
import spec_fixture as F
import spec_taxonomy as T

LOG = open(os.devnull, 'w')
TITLES = ['Summary', 'Content by category', 'File formats (top 35 by volume)', 'Mailboxes', 'Languages',
          'Inside the compressed archives', 'Technical drawings and schematics', 'By year', 'Notes']


def spec_report_notes(spec):
    import spec_report
    return spec_report.notes(spec)


def pdf_text(path):
    reader = PdfReader(path)
    return reader, [page.extract_text() for page in reader.pages]


class SpecReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.fx = F.Fixture(cls.base / 'fx')
        cls.spec = formatspec.run_spec([cls.fx.data], cls.base / 'out', config=cls.fx.config, workers=2, tools={},
                                       report_date=datetime(2026, 10, 25), log=LOG)
        cls.profile = ROOT / 'assets/examples/profile.synthetic.json'
        cls.answers = ROOT / 'assets/examples/answers.synthetic.json'

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, spec, name='card.pdf'):
        path = self.base / (name + '.spec.json')
        path.write_text(json.dumps(spec))
        out = self.base / name
        report.render(self.profile, self.answers, out, path)
        return pdf_text(out)

    def card_only(self):
        out = self.base / 'only.pdf'
        report.render(self.profile, self.answers, out)
        return pdf_text(out)

    def test_every_section_title_appears_in_order_after_the_unchanged_card_pages(self):
        reader, pages = self.render(self.spec)
        _, card_pages = self.card_only()
        self.assertEqual(pages[:len(card_pages)], card_pages)
        spec_text = '\n'.join(pages[len(card_pages):])
        position = -1
        for title in TITLES:
            found = spec_text.find(title, position + 1)
            self.assertGreater(found, position, title)
            position = found
        self.assertIn('Data format specification of the original files', spec_text)
        self.assertIn('Synthetic Works SRL', spec_text)
        self.assertIn('Construction contractor (water, sewerage)', spec_text)
        self.assertIn('October 2026', ' '.join(spec_text.split()))
        for page in pages:
            self.assertIn('TGDC', page)

    def test_metadata_and_characters(self):
        reader, pages = self.render(self.spec)
        self.assertEqual(reader.metadata.producer, 'The General Data Company')
        self.assertEqual(report.PRODUCER, 'The General Data Company')
        self.assertEqual(reader.metadata.author, 'TGDC')
        _, card_pages = self.card_only()
        spec_text = '\n'.join(pages[len(card_pages):]).lower()
        everything = '\n'.join(pages) + json.dumps({k: str(v) for k, v in reader.metadata.items()})
        for char in ('\u2013', '\u2014'):
            self.assertNotIn(char, everything)
        for word in ('cla' + 'ude', 'co' + 'dex', 'report' + 'l' + 'ab', 'generated' + ' by'):
            self.assertNotIn(word, everything.lower(), word)
        for word in ('not disclosed', 'n/a', 'on request', 'unknown', 'none', 'not available', 'tbd'):
            self.assertNotIn(word, spec_text, word)
        self.assertNotIn(' - \n', spec_text)

    def test_numbers_formats_and_reference_labels(self):
        _, pages = self.render(self.spec)
        text = '\n'.join(pages)
        flat = ' '.join(text.split())
        totals = self.spec['totals']
        self.assertIn(f"{totals['bytes'] / 1e3:,.1f} KB ({T.binary_text(totals['bytes'])}), {totals['files']:,} files", flat)
        for label in ['Company file archive', 'Business mailboxes', 'Mailbox export log and script files', 'Unique file contents (by SHA-256)',
                      'Document period (file archive, 99% of dated documents)', 'E-mail period', 'Native CAD files (DWG / DXF)',
                      'Technical drawings and schematics (sheets)', 'Dedicated drawing files vs documents with drawings', 'Sheet sizes',
                      'Where the sheets are', 'Drawings within complete project files', 'Drawings with bills of quantities',
                      'Drawing revisions', 'Languages (main language per file, files)',
                      'Volume by content category (KB)'.upper(), 'Volume by year: archive (bars) and e-mails (line)'.upper(),
                      'Mailbox 1 (owner, Gmail) - .mbox export', 'Mailbox 1 (owner, Gmail) - extracted attachments',
                      'Total text-format files examined', 'Other languages (isolated detections)', 'date not reliable']:
            self.assertIn(label, flat, label)
        self.assertIn('2012-2022', flat)                       # ASCII hyphen in year ranges
        self.assertIn('2021-2025', flat)
        self.assertIn('4 received messages held; volume and attachment figures increase once the sent messages are added', flat)
        self.assertIn('Files with substantial text in both Romanian and English', flat)
        self.assertNotIn('Estimated individual views', flat)   # no visual calibration: the row is left out
        self.assertIn('(same messages)', flat)
        self.assertIn('.eml (RFC 822, attachments embedded)', flat)
        self.assertIn('project milestones', flat)
        self.assertIn("next to the project's other documents", flat)
        self.assertIn('2021-2024', flat)                                  # the period column of the mailbox table is wide enough
        self.assertIn(f"Unique file contents (by SHA-256) {totals['unique_contents']} of {totals['files']} files", flat)
        self.assertIn('Mailbox 3 (company, Gmail): 5 messages in the account, volume and attachment figures cover the 3 received messages '
                      'and increase once the sent messages are added', flat)
        self.assertIn('Mailbox 2 (co-owner, Yahoo): 4 received messages held; volume and attachment figures increase once the sent messages are added', flat)
        self.assertIn('Synthetic Works SRL', flat)
        self.assertIn('Data format specification of the original files \u00b7 Construction contractor (water, sewerage) \u00b7 October 2026', flat)
        self.assertIn("the document's own date where known, otherwise the file date", flat)
        _, card_pages = self.card_only()
        specification = ' '.join(' '.join(pages[len(card_pages):]).split())
        self.assertNotIn('ARCHIVE S', specification.upper())              # header words are not broken

    def scaled(self, factor):
        """The fixture specification with every volume multiplied, so that one structure covers all display units."""
        def walk(value, key=''):
            if isinstance(value, dict):
                return {name: walk(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [walk(item, key) for item in value]
            if isinstance(value, int) and not isinstance(value, bool) and key.endswith('bytes'):
                return value * factor
            return value
        return walk(copy.deepcopy(self.spec))

    def specification_text(self, spec, name):
        _, pages = self.render(spec, name)
        _, card_pages = self.card_only()
        return ' '.join(' '.join(pages[len(card_pages):]).split())

    def test_the_display_unit_follows_the_total_volume(self):
        import spec_report
        for total, name in [(0, 'KB'), (9999, 'KB'), (9999999, 'KB'), (10 ** 7, 'MB'), (98946673, 'MB'), (9999999999, 'MB'),
                            (10 ** 10, 'GB'), (290330000000, 'GB')]:
            self.assertEqual(spec_report.display_unit(total).name, name, total)

    def test_a_small_selection_shows_megabytes_in_every_volume_column_and_the_charts(self):
        spec = self.scaled(500)
        total = spec['totals']['bytes']
        self.assertTrue(10 ** 7 <= total < 10 ** 10)
        flat = self.specification_text(spec, 'megabytes.pdf')
        self.assertIn(f"{total / 1e6:,.1f} MB ({T.binary_text(total)}), {spec['totals']['files']:,} files", flat)
        self.assertIn('Volume by content category (MB)'.upper(), flat)
        for header in ('OF WHICH INSIDE ARCHIVES: MB', 'ARCHIVE MB', 'MB (ALL UNPACKED)', 'MB (NEW CONTENT)'):
            self.assertIn(header, flat.upper(), header)
        self.assertIn('archive MB', flat)                                    # axis of the year chart
        self.assertNotRegex(flat, r'\bGB\b')
        self.assertIn('volumes in decimal MB (1 MB = 10^6 bytes)', flat)
        self.assertIn('MB values are rounded to three decimals; 0.000 means less than 0.5 KB', flat)
        categories = sorted(spec['categories'], key=lambda c: -c['bytes'])
        self.assertIn(f"Total {spec['totals']['files']:,} {total / 1e6:,.2f}", flat)       # category table: total row
        for category in categories:
            self.assertIn(f"{category['bytes'] / 1e6:,.2f}", flat)
        self.assertIn(f"{categories[0]['bytes'] / 1e6:,.1f}", flat)                       # label of the longest bar
        top = spec['formats'][0]
        self.assertIn(f"{top['bytes'] / 1e6:,.3f}", flat)                                 # formats table: three decimals
        language = spec['languages']['rows'][0]
        self.assertIn(f"{language['bytes'] / 1e6:,.2f}", flat)

    def test_a_tiny_selection_shows_kilobytes(self):
        flat = self.specification_text(self.spec, 'kilobytes.pdf')
        total = self.spec['totals']['bytes']
        self.assertLess(total, 10 ** 7)
        self.assertIn('Volume by content category (KB)'.upper(), flat)
        self.assertIn('archive KB', flat)
        self.assertNotRegex(flat, r'\b(GB|MB)\b')
        self.assertIn('volumes in decimal KB (1 KB = 10^3 bytes)', flat)
        self.assertIn('KB values are rounded to three decimals; 0.000 means less than 0.5 byte', flat)
        self.assertIn(f"Total {self.spec['totals']['files']:,} {total / 1e3:,.2f}", flat)
        self.assertIn(f"{self.spec['formats'][0]['bytes'] / 1e3:,.3f}", flat)

    def test_a_large_selection_keeps_gigabytes_as_in_the_reference(self):
        spec = self.scaled(100000)
        total = spec['totals']['bytes']
        self.assertGreaterEqual(total, 10 ** 10)
        flat = self.specification_text(spec, 'gigabytes.pdf')
        self.assertIn(f"{total / 1e9:,.1f} GB ({T.binary_text(total)}), {spec['totals']['files']:,} files", flat)
        self.assertIn('Volume by content category (GB)'.upper(), flat)
        self.assertIn('archive GB', flat)
        self.assertIn('ARCHIVE GB', flat.upper())
        self.assertIn(f"Total {spec['totals']['files']:,} {total / 1e9:,.2f}", flat)
        self.assertIn(f"{spec['formats'][0]['bytes'] / 1e9:,.3f}", flat)
        self.assertIn('volumes in decimal GB (1 GB = 10^9 bytes)', flat)
        self.assertIn('GB values are rounded to three decimals; 0.000 means less than 0.5 MB', flat)
        for header in ('OF WHICH INSIDE ARCHIVES: GB', 'GB (ALL UNPACKED)', 'GB (NEW CONTENT)'):
            self.assertIn(header, flat.upper(), header)
        self.assertNotIn('MB (ALL UNPACKED)', flat.upper())

    def test_summary_rows_never_use_a_larger_unit_than_the_document(self):
        for factor, forbidden in ((500, r'\bGB\b'), (1, r'\b(GB|MB)\b')):
            flat = self.specification_text(self.scaled(factor), f'summary-{factor}.pdf')
            self.assertNotRegex(flat, forbidden, factor)
        # a part below 0.1 of the unit falls to the smaller unit, as in the reference, where small parts read in MB beside a GB headline
        import spec_report
        self.assertEqual(spec_report.display_unit(290e9).short(24.4e6), '24.4 MB')
        self.assertEqual(spec_report.display_unit(290e9).short(0.7e9), '0.7 GB')
        self.assertEqual(spec_report.display_unit(98.9e6).short(98.9e6), '98.9 MB')
        self.assertEqual(spec_report.display_unit(98.9e6).short(5.4e9), '5,400.0 MB')
        self.assertEqual(spec_report.display_unit(98.9e6).short(24e3), '24.0 KB')
        self.assertEqual(spec_report.display_unit(5e6).short(5e6), '5,000.0 KB')
        self.assertEqual(spec_report.display_unit(5e6).short(640), '640 bytes')

    def last_page_lines(self, pages):
        lines = [line for line in pages[-1].splitlines()
                 if line.strip() and 'DATA CARD / FOR REVIEW' not in line and not line.startswith('TGDC /') and '@' not in line]
        return lines

    def test_the_notes_never_leave_a_few_lines_on_a_last_page_of_their_own(self):
        short = []
        for count in range(20, 42):
            spec = copy.deepcopy(self.spec)
            spec['years']['rows'] = [{'year': 1990 + i, 'files': 1, 'bytes': 1000 * (i + 1), 'messages': i} for i in range(count)]
            _, pages = self.render(spec, 'notes-sweep.pdf')
            lines = self.last_page_lines(pages)
            if 'Notes' not in pages[-1] and len(lines) < 8:
                short.append((count, len(lines)))
        self.assertEqual(short, [])

    def test_the_notes_keep_a_heading_with_its_first_notes_and_all_notes_stay_in_order(self):
        for count in (3, 14, 28, 33):
            spec = copy.deepcopy(self.spec)
            spec['years']['rows'] = [{'year': 1990 + i, 'files': 1, 'bytes': 1000 * (i + 1), 'messages': i} for i in range(count)]
            _, pages = self.render(spec, 'notes-order.pdf')
            for number, page in enumerate(pages):
                lines = [line for line in page.splitlines() if line.strip()]
                if 'Notes' in lines:
                    after = lines[lines.index('Notes') + 1:]
                    self.assertGreaterEqual(len([line for line in after if line.startswith('\u2022')]), 2, count)
            text = ' '.join(' '.join(pages).split())
            position = -1
            for item in spec_report_notes(spec):
                found = text.find(item[:40], position + 1)
                self.assertGreater(found, position, item[:40])
                position = found

    def test_the_reason_for_unconfirmed_languages_follows_the_counts(self):
        import spec_report
        reason = spec_report.no_language_reason
        self.assertEqual(reason({'files': 10, 'scans': 8}), 'mainly scans without a text layer; also very short or garbled text')
        self.assertEqual(reason({'files': 10, 'scans': 2}), 'mainly very short or garbled text; also scans without a text layer')
        self.assertEqual(reason({'files': 10, 'scans': 5}), 'mainly very short or garbled text; also scans without a text layer')
        self.assertEqual(reason({'files': 10, 'scans': 10}), 'scans without a text layer')
        self.assertEqual(reason({'files': 10, 'scans': 0}), 'very short or garbled text')
        self.assertEqual(reason({'files': 10}), 'scans without a text layer, very short or garbled text')

    def test_the_unconfirmed_language_rows_name_the_main_reason_found_in_the_data(self):
        flat = ' '.join('\n'.join(self.render(self.spec)[1]).split())
        self.assertIn('Files of text formats where no language could be confirmed (mainly very short or garbled text; also scans without a text layer)', flat)
        self.assertIn('No language confirmed (mainly very short or garbled text; also scans without a text layer)', flat)
        self.assertNotIn('mainly scans without a text layer', flat)
        spec = copy.deepcopy(self.spec)
        spec['languages']['none']['scans'] = spec['languages']['none']['files']
        flat = ' '.join('\n'.join(self.render(spec, 'scans.pdf')[1]).split())
        self.assertIn('No language confirmed (scans without a text layer)', flat)
        self.assertNotIn('garbled', flat)
        self.assertIn('no language could be confirmed (scans without a text layer)', flat)

    def test_sections_without_data_are_left_out(self):
        spec = copy.deepcopy(self.spec)
        for key in ('mailboxes', 'languages', 'archives', 'drawings', 'cad', 'years'):
            spec.pop(key, None)
        for key in ('mailboxes', 'alternative_exports', 'export_logs', 'archive_contents', 'held_without_archives'):
            spec['parts'].pop(key, None)
        spec['periods'].pop('email', None)
        _, pages = self.render(spec, 'bare.pdf')
        flat = ' '.join(' '.join(pages).split())
        for title in ('Mailboxes', 'Languages', 'Inside the compressed archives', 'Technical drawings and schematics', 'By year'):
            self.assertNotIn(title, flat.replace('Business mailboxes', ''), title)
        for label in ('Business mailboxes', 'Alternative exports', 'Mailbox export log', 'Contents of the compressed archives', 'Native CAD',
                      'E-mail period', 'Languages (main language', 'Drawing revisions', 'Sheet sizes'):
            self.assertNotIn(label, flat, label)
        for title in ('Summary', 'Content by category', 'File formats (top 35 by volume)', 'Notes'):
            self.assertIn(title, flat)
        self.assertNotIn('Of which inside archives', flat)

    def test_large_numbers_are_rounded_like_the_reference_and_views_need_calibration(self):
        spec = copy.deepcopy(self.spec)
        drawings = spec['drawings']
        drawings.update({'sheets': 15790, 'engineering': 13370, 'schematics': 2420, 'files': 6300, 'calibrated': True, 'sampled_pages': 60,
                         'views': {'total': 27040, 'per_sheet': 1.7, 'low': 1.5, 'high': 1.9}})
        drawings['dedicated'] = {'files': 4090, 'sheets': 6890}
        drawings['documents'] = {'files': 2210, 'sheets': 8900}
        drawings['sizes'] = {'A4': 7190, 'A3': 4410, 'A2': 840, 'A1': 510, 'A0': 330, 'US Letter / Legal / Tabloid': 660, 'image file': 270, 'other / custom size': 1580}
        drawings['locations'] = {'file': 12350, 'mail': 2530, 'archive': 910}
        spec['totals'].update({'bytes': 290330000000, 'files': 269785})
        drawings['projects']['sheets'], drawings['projects']['total_sheets'] = 11720, 15790
        _, pages = self.render(spec, 'big.pdf')
        flat = ' '.join(' '.join(pages).split())
        self.assertIn('290.3 GB (270.4 GiB), 269,785 files', flat)
        self.assertIn('about 15,800 sheets: about 13,400 engineering drawings + about 2,400 schematics / diagrams, in about 6,300 files', flat)
        self.assertIn('A4 7,200, A3 4,400, A2 800, A1 500, A0 300, US Letter / Legal / Tabloid 700, image file 300, other / custom size 1,600', flat)
        self.assertIn('about 27,000 (about 1.7 per sheet, range 1.5-1.9)', flat)
        self.assertIn('company file archive: 12,400 sheets; only in the e-mail (attachments): 2,500 sheets; only inside compressed archives: 900 sheets', flat)
        self.assertNotIn('archive: about', flat)
        self.assertIn('Estimated individual views', flat)
        self.assertRegex(flat, r'15,790 .*sheets|sheets 15,790|15,790')      # detail table to the nearest 10, not 100

    def test_tables_repeat_their_header_row_across_page_breaks(self):
        spec = copy.deepcopy(self.spec)
        spec['years']['rows'] = [{'year': 1900 + i, 'files': i, 'bytes': i * 1000, 'messages': i} for i in range(120)]
        _, pages = self.render(spec, 'long.pdf')
        with_header = [page for page in pages if 'ARCHIVE FILES' in page.upper() and re.search(r'\b19\d\d\b', page)]
        self.assertGreaterEqual(len(with_header), 2)

    def test_pdf_has_no_file_names_paths_addresses_or_text(self):
        _, pages = self.render(self.spec)
        flat = ' '.join(' '.join(pages).split()).lower()
        for token in F.PRIVATE_TOKENS:
            self.assertNotIn(token.lower(), flat, token)
        self.assertNotIn(str(self.base).lower(), flat)
        self.assertNotIn('@synthetic', flat)

    def test_notes_use_singular_and_plural_correctly_and_name_unchecked_mailbox_pdfs(self):
        spec = copy.deepcopy(self.spec)
        archives = spec['methodology']['archives']
        archives.update({'members_not_extracted': 1, 'partly_readable': 1, 'not_opened': {'zip:damaged': 1}})
        spec['methodology']['unchecked_pdfs'] = {'files': 996, 'bytes': 700000000, 'mailbox_attachments': {'files': 996, 'bytes': 700000000}}
        spec['totals']['bytes'] = 290330000000
        spec['methodology']['hidden_entries'], spec['methodology']['credential_folders'], spec['methodology']['symlinks'] = 1, 1, 1
        _, pages = self.render(spec, 'single.pdf')
        flat = ' '.join(' '.join(pages).split())
        for sentence in ['1 file could not be extracted (password-protected or damaged) and is not counted', '1 archive was only partly readable',
                         '1 archive could not be opened at all', '996 PDFs (0.7 GB) among the extracted mailbox attachments were not checked for a text layer',
                         '1 hidden file or folder (names that start with a dot) was not entered', '1 folder with a credential-like name was not entered',
                         '1 symbolic link was not followed']:
            self.assertIn(sentence, flat, sentence)
        _, card_pages = self.card_only()
        specification = ' '.join(' '.join(pages[len(card_pages):]).split())
        for wrong in ('1 files', '1 archives', '1 PDFs', '1 projects', '1 sheets'):
            self.assertNotRegex(specification, r'(?<![\d,])' + wrong, wrong)

    def test_a_page_break_never_leaves_fewer_than_four_body_rows_on_either_side(self):
        import spec_report
        from reportlab.platypus import Paragraph
        rows = [[Paragraph(f'row {i}', spec_report.card.ParagraphStyle('x', fontSize=8)), '1'] for i in range(40)]
        header = [Paragraph('head', spec_report.card.ParagraphStyle('x', fontSize=8)), 'v']
        table = spec_report.BalancedTable([header] + rows, colWidths=[100, 40], repeatRows=1)
        for height in range(60, 520, 7):
            parts = table.split(300, height)
            if not parts:
                continue
            self.assertEqual(len(parts), 2, height)
            self.assertGreaterEqual(len(parts[0]._cellvalues) - 1, 4, height)
            self.assertGreaterEqual(len(parts[1]._cellvalues) - 1, 4, height)

    def test_an_invalid_specification_is_rejected_and_output_is_preserved(self):
        out = self.base / 'keep.pdf'
        out.write_bytes(b'previous')
        broken = self.base / 'broken.json'
        broken.write_text(json.dumps({'schema': 'other'}))
        with self.assertRaises(ValueError):
            report.render(self.profile, self.answers, out, broken)
        self.assertEqual(out.read_bytes(), b'previous')
        negative = copy.deepcopy(self.spec)
        negative['totals']['files'] = -1
        broken.write_text(json.dumps(negative))
        with self.assertRaises(ValueError):
            report.render(self.profile, self.answers, out, broken)
        self.assertEqual(out.read_bytes(), b'previous')


if __name__ == '__main__':
    unittest.main()
