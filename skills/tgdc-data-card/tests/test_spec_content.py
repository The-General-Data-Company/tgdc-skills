"""Per-content analyzers on synthetic bytes."""
import io
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec_content as C
import spec_samples as S


class PdfTests(unittest.TestCase):
    def test_text_layer_pages_sizes_and_vector_pages(self):
        data = S.pdf_bytes([('text', S.A4P, 1), ('drawing', S.A4P, 2), ('text', S.A3P, 3), ('drawing', S.A1P, 4), ('scan', S.LETTERP, 5)], creation_year=2017)
        facts = C.analyze_pdf(io.BytesIO(data), text_pages=1)
        self.assertEqual(facts['pages'], 5)
        self.assertFalse(facts['scanned'])
        self.assertEqual(facts['year'], 2017)
        self.assertEqual(facts['classes']['A4'], [2, 1])        # two A4 pages, one vector-heavy
        self.assertEqual(facts['classes']['A3'], [1, 0])        # larger than A4: counted by size, no vector flag needed
        self.assertEqual(facts['classes']['A1'], [1, 0])
        self.assertEqual(facts['classes']['letter'], [1, 0])    # an image-only page is not vector-heavy
        self.assertNotIn('_text', facts)

    def test_image_only_pdf_is_scanned_and_first_three_pages_are_checked_for_members(self):
        scan = S.pdf_bytes([('scan', S.A4P, 1), ('scan', S.A4P, 2)])
        self.assertTrue(C.analyze_pdf(io.BytesIO(scan), text_pages=1)['scanned'])
        mixed = S.pdf_bytes([('scan', S.A4P, 1), ('text', S.A4P, 2)])
        self.assertTrue(C.analyze_pdf(io.BytesIO(mixed), text_pages=1)['scanned'])
        self.assertFalse(C.analyze_pdf(io.BytesIO(mixed), text_pages=3)['scanned'])

    def test_text_is_returned_only_on_request(self):
        data = S.pdf_bytes([('text', S.A4P, S.RO)])
        facts = C.analyze_pdf(io.BytesIO(data), want_text=True)
        self.assertIn('Antreprenorul', facts['_text'])

    def test_broken_pdf_is_unchecked(self):
        self.assertEqual(C.analyze_pdf(io.BytesIO(b'not a pdf'))['unchecked'], True)
        self.assertEqual(C.analyze_pdf(io.BytesIO(b''))['unchecked'], True)

    def test_candidate_pages_for_calibration(self):
        data = S.pdf_bytes([('text', S.A4P, 1), ('drawing', S.A4P, 2), ('text', S.A3P, 3)])
        rows = C.page_candidates(io.BytesIO(data))
        self.assertEqual([(i, name, flag) for i, name, flag, _ in rows], [(0, 'A4', False), (1, 'A4', True), (2, 'A3', False)])


class ImageTests(unittest.TestCase):
    def test_tiff_size_comes_from_dpi(self):
        facts = C.analyze_tiff(io.BytesIO(S.tiff_bytes(297, 420, 200)))
        self.assertEqual(facts['classes'], {'A3': [1, 0]})
        self.assertEqual(C.analyze_tiff(io.BytesIO(b'junk'))['unchecked'], True)

    def test_plain_images_are_one_image_file_sheet(self):
        self.assertEqual(C.image_sheet_facts('png'), {'pages': 1, 'classes': {'image': [1, 0]}})
        self.assertIsNone(C.image_sheet_facts('doc'))


class TextTests(unittest.TestCase):
    def test_office_and_plain_text_and_dates(self):
        text, year = C.extract_text('docx', S.docx_bytes('Hello contractor works', 2015))
        self.assertEqual((text, year), ('Hello contractor works', 2015))
        text, year = C.extract_text('xlsx', S.xlsx_bytes(['Bill', 'of quantities'], 2012))
        self.assertEqual((text, year), ('Bill of quantities', 2012))
        self.assertEqual(C.extract_text('txt', 'plain words'.encode())[0], 'plain words')
        self.assertEqual(C.extract_text('html', b'<html><script>x()</script><p>Visible &amp; text</p></html>')[0].split(), ['Visible', '&', 'text'])
        self.assertEqual(C.extract_text('png', b'')[0], '')

    def test_unsafe_xml_and_encrypted_office_are_rejected(self):
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY a "b">]><w:document xmlns:w="urn:x"><w:t>&a;</w:t></w:document>')
        with self.assertRaises(ValueError):
            C.extract_text('docx', buffer.getvalue())

    def test_legacy_doc_best_effort_in_both_encodings(self):
        for encoding in ('cp1252', 'utf-16-le'):
            with self.subTest(encoding=encoding):
                text = C.legacy_doc_text(S.ole_doc_bytes(S.EN * 2, encoding))
                self.assertIn('contractor shall deliver', text)

    def test_documents_are_read_from_a_stream_without_loading_them_as_bytes(self):
        facts = C.content_facts('docx', io.BytesIO(S.docx_bytes(S.EN * 2, 2015)))
        self.assertEqual((facts['lang']['lang'], facts['year']), ('en', 2015))
        facts = C.content_facts('xlsx', io.BytesIO(S.xlsx_bytes([S.RO * 2], 2012)))
        self.assertEqual((facts['lang']['lang'], facts['year']), ('ro', 2012))
        facts = C.content_facts('doc', io.BytesIO(S.ole_doc_bytes(S.EN * 2, 'cp1252')))
        self.assertEqual(facts['lang']['lang'], 'en')
        data, _ = MailTests().build()
        self.assertEqual(C.content_facts('eml', io.BytesIO(data))['lang']['lang'], 'ro')
        self.assertEqual(C.content_facts('eml', data)['lang']['lang'], 'ro')

    def test_a_cut_off_text_loses_only_an_incomplete_character(self):
        text = ('ăîșț' * 5).encode('utf-8')
        for cut in range(1, 9):
            with self.subTest(cut=cut):
                self.assertEqual(C.trim_partial_character(text[:cut]).decode('utf-8'), text[:cut].decode('utf-8', 'ignore'))
        self.assertEqual(C.trim_partial_character(b'plain'), b'plain')
        self.assertEqual(C.trim_partial_character(b''), b'')

    def test_text_formats_are_the_ones_read_from_their_first_bytes_only(self):
        self.assertEqual(C.TEXT_HEAD_BYTES, 2 * 1024 * 1024)
        self.assertEqual(sorted(C.TEXT_HEAD_EXTENSIONS), ['csv', 'htm', 'html', 'md', 'rtf', 'tsv', 'txt', 'xhtml'])

    def test_romanian_cp1250_text_decodes(self):
        data = 'Lucrările şi reţelele'.encode('cp1250')
        self.assertIn('Lucr', C.decode_plain(data))


class MailTests(unittest.TestCase):
    def build(self):
        pdf = S.pdf_bytes([('text', S.A3P, 7)])
        return S.eml_bytes('Water plan', 'Ann <ann@example.test>', 'Owner <owner@example.test>', S.RO * 3, year=2022,
                           attachments=[('plan.pdf', pdf, 'application/pdf'), ('layout.dxf', b'0\nSECTION\n', 'application/octet-stream')],
                           delivered_to='owner@example.test'), pdf

    def test_message_facts_have_no_addresses_or_text(self):
        data, pdf = self.build()
        facts = C.analyze_message(data)
        self.assertEqual(facts['year'], 2022)
        self.assertEqual(len(facts['mid']), 16)
        self.assertEqual(len(facts['from']), 16)
        self.assertEqual(len(facts['to']), 1)
        self.assertEqual(facts['lang']['lang'], 'ro')
        self.assertEqual(sorted(a['ext'] for a in facts['atts']), ['dxf', 'pdf'])
        attached = next(a for a in facts['atts'] if a['ext'] == 'pdf')
        import hashlib
        self.assertEqual(attached['sha'], hashlib.sha256(pdf).hexdigest())
        self.assertEqual(attached['pdf']['classes'], {'A3': [1, 0]})
        flat = repr(facts)
        for private in ('ann@example', 'owner@example', 'Water plan', 'Antreprenorul'):
            self.assertNotIn(private, flat)

    def test_mbox_messages_are_counted_streamed_and_known_ones_skipped(self):
        import mailbox
        import tempfile
        one, _ = self.build()
        two = S.eml_bytes('Second note', 'Bob <bob@example.test>', 'Owner <owner@example.test>', S.EN * 3, year=2023)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'x.mbox'
            box = mailbox.mbox(str(path))
            box.add(one); box.add(two); box.flush(); box.close()
            known = {C.analyze_message(one)['mid']}
            with open(path, 'rb') as stream:
                result = C.analyze_mbox(stream, known, 1 << 20)
        self.assertEqual(result['count'], 2)
        self.assertTrue(result['messages'][0]['known'])
        self.assertEqual(result['messages'][1]['lang']['lang'], 'en')
        self.assertEqual(result['messages'][1]['year'], 2023)


class MarkupTests(unittest.TestCase):
    def timed(self, text):
        started = time.monotonic()
        result = C.strip_markup(text)
        return result, time.monotonic() - started

    def test_tags_and_script_blocks_are_removed(self):
        text, _ = self.timed('<p>Hello <b>world</b></p><script>var secret = 1;</script><style>p { color: red }</style><SCRIPT type="x">more()</SCRIPT> end &amp; done')
        self.assertEqual(' '.join(text.split()), 'Hello world end & done')

    def test_unclosed_script_and_tag_floods_take_linear_time(self):
        for text in ('<script>' * 60000, '<' * 400000, '<a ' * 150000, '<style>x' * 60000 + '</script>', '<script' * 80000):
            with self.subTest(sample=text[:12]):
                result, seconds = self.timed(text)
                self.assertLess(seconds, 2.0)
                self.assertIsInstance(result, str)

    def test_input_is_capped_at_about_one_megabyte(self):
        result, seconds = self.timed('word ' * 3000000)
        self.assertLessEqual(len(result), C.MARKUP_LIMIT)
        self.assertLess(seconds, 2.0)

    def test_a_plain_less_than_sign_stays_text(self):
        text, _ = self.timed('if a < b then c')
        self.assertEqual(text, 'if a < b then c')


class MboxBudgetTests(unittest.TestCase):
    def mbox(self, messages):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'x.mbox'
        import mailbox
        box = mailbox.mbox(str(path))
        for data in messages:
            box.add(data)
        box.flush()
        box.close()
        return path

    def messages(self, count, ids=True):
        data = [S.eml_bytes(f'Note {n}', 'Ann <ann@example.test>', 'Owner <owner@example.test>', S.EN * 3 + f' number {n}.', year=2022,
                            message_id=f'<n{n}@example.test>') for n in range(count)]
        return data if ids else [re.sub(rb'(?m)^Message-ID:.*\n', b'', item) for item in data]

    def test_messages_past_the_deadline_are_still_counted_and_reported_as_cut(self):
        path = self.mbox(self.messages(30))
        with open(path, 'rb') as stream:
            result = C.analyze_mbox(stream, set(), 1 << 20, deadline=time.monotonic() - 1)
        self.assertEqual(result['count'], 30)
        self.assertEqual(result['cut'], 30)
        self.assertEqual(result['messages'], [])

    def test_a_deadline_in_the_middle_keeps_the_total(self):
        path = self.mbox(self.messages(40))
        original = C.analyze_message
        calls = []

        def slow(*args, **kwargs):
            calls.append(1)
            time.sleep(0.02)
            return original(*args, **kwargs)
        with open(path, 'rb') as stream, mock.patch.object(C, 'analyze_message', slow):
            result = C.analyze_mbox(stream, set(), 1 << 20, deadline=time.monotonic() + 0.1)
        self.assertEqual(result['count'], 40)
        self.assertEqual(len(result['messages']) + result['cut'], 40)
        self.assertGreater(result['cut'], 0)
        self.assertGreater(len(result['messages']), 0)

    def test_a_message_over_its_budget_skips_the_rest_of_its_attachments_and_is_reported(self):
        pdfs = [('a.pdf', S.pdf_bytes([('text', S.A4P, 1)]), 'application/pdf'), ('b.pdf', S.pdf_bytes([('text', S.A4P, 2)]), 'application/pdf')]
        data = S.eml_bytes('With files', 'Ann <ann@example.test>', 'Owner <owner@example.test>', S.EN * 3, attachments=pdfs)
        facts = C.analyze_message(data, deadline=time.monotonic() - 1)
        self.assertTrue(facts['partial'])
        self.assertEqual(facts['atts'], [])
        whole = C.analyze_message(data)
        self.assertNotIn('partial', whole)
        self.assertEqual(len(whole['atts']), 2)
        path = self.mbox([data])
        with open(path, 'rb') as stream, mock.patch.object(C, 'MESSAGE_SECONDS', -1):
            result = C.analyze_mbox(stream, set(), 1 << 20)
        self.assertEqual((result['count'], result['cut'], result['partial']), (1, 0, 1))

    def test_a_truncated_message_is_counted_as_partial(self):
        data = S.eml_bytes('Large', 'Ann <ann@example.test>', 'Owner <owner@example.test>', 'x ' * 50000)
        path = self.mbox([data])
        with open(path, 'rb') as stream:
            result = C.analyze_mbox(stream, set(), 1000)
        self.assertEqual((result['count'], result['partial']), (1, 1))

    def test_messages_without_an_id_get_a_body_key_that_matches_across_formats(self):
        one, two = self.messages(2, ids=False)
        first = C.analyze_message(one)
        again = C.analyze_message(one.replace(b'\n', b'\r\n') if b'\r\n' not in one else one)
        self.assertIsNone(first['mid'])
        self.assertTrue(first['bid'].startswith('b:'))
        self.assertEqual(first['bid'], again['bid'])
        self.assertNotEqual(first['bid'], C.analyze_message(two)['bid'])
        self.assertEqual(C.message_key(first), first['bid'])
        with_id = C.analyze_message(self.messages(1)[0])
        self.assertNotIn('bid', with_id)
        self.assertEqual(C.message_key(with_id), with_id['mid'])

    def test_known_messages_without_an_id_are_recognized_by_their_body_key(self):
        one, two = self.messages(2, ids=False)
        path = self.mbox([one, two])
        with open(path, 'rb') as stream:
            result = C.analyze_mbox(stream, {C.analyze_message(one)['bid']}, 1 << 20)
        self.assertTrue(result['messages'][0]['known'])
        self.assertNotIn('known', result['messages'][1])


class XmlGuardTests(unittest.TestCase):
    ENTITY = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY x "boom">]><d>&x;</d>'

    def test_a_doctype_is_refused_in_every_encoding(self):
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32'):
            with self.subTest(encoding=encoding):
                raw = self.ENTITY.encode(encoding)
                with self.assertRaises(ValueError):
                    C._safe_xml(raw)
        with self.assertRaises(ValueError):
            C._safe_xml(b'<?xml version="1.0"?><!doctype d><d/>')
        self.assertEqual(C._safe_xml('<?xml version="1.0" encoding="UTF-16"?><d>fine</d>'.encode('utf-16')).text, 'fine')

    def test_an_office_file_with_a_utf_16_doctype_is_unreadable_not_parsed(self):
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', self.ENTITY.replace('<d>&x;</d>', '<w:document xmlns:w="urn:w"><w:t>&x;</w:t></w:document>').encode('utf-16'))
        with self.assertRaises(ValueError):
            C.extract_text('docx', buffer.getvalue())


if __name__ == '__main__':
    unittest.main()
