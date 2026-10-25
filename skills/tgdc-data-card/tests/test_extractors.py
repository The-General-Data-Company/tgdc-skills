"""Synthetic parser contracts: totals, samples and failures remain distinct."""
import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extractors import extract, DEFAULT_LIMITS
from spec_samples import pdf_with_duplicate_keys


class ExtractorTests(unittest.TestCase):
    def parse(self, extension, data, **limits):
        return extract(extension, data, {**DEFAULT_LIMITS, **limits})

    def test_csv_multiline_records_and_row_cap(self):
        result = self.parse('csv', b'id,note\n1,"first\nsecond"\n2,last\n', max_rows=1)
        self.assertEqual(result['rows_processed'], 1)
        self.assertIsNone(result['rows_total'])
        self.assertEqual(result['status'], 'capped')
        self.assertNotIn('first', str({k: v for k, v in result.items() if k != '_text'}))

    def test_json_object_list_and_jsonl_record_semantics(self):
        for extension, raw, count in [('json', b'{"id": 1}', 1), ('json', b'[{"id":1},{"id":2}]', 2), ('jsonl', b'{"id":1}\n{"id":2}\n', 2)]:
            result = self.parse(extension, raw)
            self.assertEqual(result['rows_processed'], count)
            self.assertEqual(result['rows_total'], count)
        self.assertEqual(self.parse('json', b'[1,2,3]', max_rows=1)['rows_total'], 3)
        self.assertEqual(self.parse('json', b'broken')['status'], 'malformed')

    def test_zero_and_binary_text_are_explicit(self):
        self.assertEqual(self.parse('txt', b'')['status'], 'empty')
        self.assertEqual(self.parse('txt', b'\x00\x01hidden')['status'], 'binary')

    def test_text_character_cap(self):
        result = self.parse('txt', b'hello world', max_characters=5)
        self.assertEqual(result['_text'], 'hello')
        self.assertEqual(result['characters'], 5)
        self.assertEqual(result['status'], 'capped')

    def test_pdf_malformed_encrypted_and_page_total(self):
        from pypdf import PdfWriter
        self.assertEqual(self.parse('pdf', b'invalid')['status'], 'malformed')
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO(); writer.write(stream)
        result = self.parse('pdf', stream.getvalue(), max_pages=1)
        self.assertEqual(result['pages_total'], 3)
        self.assertEqual(result['pages_processed'], 1)
        self.assertEqual(result['no_text_pages'], 1)
        self.assertEqual(result['status'], 'capped')
        self.assertNotIn('scanned', result)
        writer.encrypt('synthetic-password')
        stream = io.BytesIO(); writer.write(stream)
        self.assertEqual(self.parse('pdf', stream.getvalue())['status'], 'encrypted')

    def test_pdf_with_duplicate_dictionary_keys_is_read_not_rejected(self):
        from pypdf import PdfReader
        data = pdf_with_duplicate_keys()
        with self.assertRaises(Exception):
            PdfReader(io.BytesIO(data), strict=True)       # the fixture really is what strict reading refuses
        result = self.parse('pdf', data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['pages_total'], 1)
        self.assertIn('Duplicate key sample', result['_text'])
        self.assertEqual(self.parse('pdf', b'invalid')['status'], 'malformed')

    def test_docx_expansion_limit_and_extraction(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>synthetic words</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(self.parse('docx', stream.getvalue(), max_expanded_bytes=10)['status'], 'decompression_cap')
        result = self.parse('docx', stream.getvalue())
        self.assertEqual(result['words'], 2)
        self.assertIn('synthetic words', result['_text'])

    def test_xlsx_dimensions_are_not_observed_row_totals(self):
        from openpyxl import Workbook
        workbook = Workbook(); sheet = workbook.active
        sheet.append(['id', 'value']); sheet.append([1, 'one']); sheet.append([2, 'two'])
        sheet.cell(100, 1).number_format = '0'
        stream = io.BytesIO(); workbook.save(stream)
        result = self.parse('xlsx', stream.getvalue(), max_rows=1)
        self.assertEqual(result['rows_processed'], 1)
        self.assertIsNone(result['rows_total'])
        self.assertEqual(result['sheet_dimensions'][0]['declared_max_row'], 100)
        self.assertEqual(result['status'], 'capped')

    def test_eml_decodes_body_and_separates_content_date(self):
        raw = b'Date: Tue, 01 Jan 2019 12:00:00 +0000\nFrom: private@example.invalid\nSubject: synthetic confidential\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\nc3ludGhldGljIGJvZHk=\n'
        result = self.parse('eml', raw)
        self.assertEqual(result['_text'], 'synthetic body')
        self.assertEqual(result['content_date'], '2019-01-01T12:00:00+00:00')
        self.assertNotIn('private@example', str(result))

    def test_unsupported_is_explicit(self):
        self.assertEqual(self.parse('dwg', b'bytes')['status'], 'unsupported')


class AdditionalBoundsTests(unittest.TestCase):
    def test_pdf_compressed_stream_expansion_is_capped(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        writer = PdfWriter(); page = writer.add_blank_page(width=100, height=100)
        stream = DecodedStreamObject(); stream.set_data(b' ' * 10000)
        page[NameObject('/Contents')] = writer._add_object(stream.flate_encode())
        buffer = io.BytesIO(); writer.write(buffer)
        result = extract('pdf', buffer.getvalue(), {**DEFAULT_LIMITS, 'max_expanded_bytes': 1000})
        self.assertEqual(result['status'], 'decompression_cap')

    def test_docx_unsafe_xml_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY x "unsafe">]><x>&x;</x>')
        self.assertEqual(extract('docx', buffer.getvalue())['status'], 'malformed')


class SpreadsheetSafetyTests(unittest.TestCase):
    def test_extreme_declared_columns_do_not_expand_empty_grid(self):
        from openpyxl import Workbook
        workbook = Workbook(); workbook.active.append(['id']); workbook.active.append([1])
        stream = io.BytesIO(); workbook.save(stream)
        rewritten = io.BytesIO()
        with zipfile.ZipFile(stream) as original, zipfile.ZipFile(rewritten, 'w') as target:
            for name in original.namelist():
                data = original.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    data = data.replace(b'A1:A2', b'A1:XFD1000000')
                target.writestr(name, data)
        result = extract('xlsx', rewritten.getvalue(), {**DEFAULT_LIMITS, 'max_rows': 2, 'max_cells': 4})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['physical_rows_examined'], 2)
        self.assertEqual(result['cells_examined'], 2)
        self.assertEqual(result['rows_total'], 1)
        self.assertEqual(result['sheet_dimensions'][0], {'declared_max_row': 1000000, 'declared_max_column': 16384})


class JsonBudgetTests(unittest.TestCase):
    def test_node_budget_is_per_document_across_records(self):
        result = extract('json', b'[[1,2],[3,4]]', {**DEFAULT_LIMITS, 'max_json_nodes': 3})
        self.assertEqual(result['status'], 'capped')
        self.assertEqual(result['_text'], '1\n2')


class ScalarEvidenceTests(unittest.TestCase):
    def test_numeric_zero_is_not_missing_text(self):
        result = extract('json', b'[0]')
        self.assertEqual(result['characters'], 1)
        self.assertEqual(result['_text'], '0')


class WorksheetDimensionRegressionTests(unittest.TestCase):
    def source(self):
        from openpyxl import Workbook
        workbook = Workbook(); sheet = workbook.active
        sheet.append(['id', 'value']); sheet.append([1, 'one']); sheet.append([2, 'two'])
        stream = io.BytesIO(); workbook.save(stream)
        rewritten = io.BytesIO()
        with zipfile.ZipFile(stream) as archive, zipfile.ZipFile(rewritten, 'w') as target:
            for name in archive.namelist():
                data = archive.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    data = data.replace(b'A1:B3', b'A1:A1')
                target.writestr(name, data)
        return rewritten.getvalue()

    def test_underdeclared_dimensions_do_not_hide_real_rows_and_cells(self):
        result = extract('xlsx', self.source())
        self.assertEqual(result['rows_processed'], 2)
        self.assertEqual(result['rows_total'], 2)
        self.assertEqual(result['characters'], 11)
        self.assertEqual(result['sheet_dimensions'][0], {'declared_max_row': 1, 'declared_max_column': 1})
        self.assertTrue(result['text_complete'])

    def test_underdeclared_dimension_stream_still_obeys_row_cap(self):
        result = extract('xlsx', self.source(), {**DEFAULT_LIMITS, 'max_rows': 1})
        self.assertEqual(result['rows_processed'], 1)
        self.assertEqual(result['status'], 'capped')
        self.assertIsNone(result['rows_total'])
        self.assertFalse(result['text_complete'])


if __name__ == '__main__':
    unittest.main()
