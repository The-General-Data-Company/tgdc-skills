"""Bounded local parsers. Returned _text is transient and must never be serialized.

A processed row is a data record: CSV/XLSX exclude the first non-empty header
row; JSON objects count as one record, arrays as one per element, JSONL as one
per nonblank line. Worksheet dimensions are metadata, never observed totals.
"""
import csv
from contextlib import contextmanager
import hashlib
import io
import json
import re
import zipfile
from datetime import timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

DEFAULT_LIMITS = {
    'max_files': 100000, 'max_sample_files': 1000,
    'max_file_bytes': 50 * 1024 * 1024, 'max_pages': 50,
    'max_rows': 10000, 'max_characters': 200000,
    'parser_seconds': 15.0, 'max_expanded_bytes': 200 * 1024 * 1024,
    'max_cells': 200000, 'max_json_nodes': 200000,
}
SUPPORTED = frozenset({'pdf', 'docx', 'xlsx', 'csv', 'tsv', 'json', 'jsonl', 'eml', 'txt', 'md'})


class ParseLimit(Exception):
    pass


class TextSample:
    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.length = 0
        self.capped = False
        self.json_nodes = 0

    def add(self, text):
        if text is None or text == '':
            return
        text = str(text)
        available = self.limit - self.length
        if self.parts and available > 0:
            self.parts.append('\n'); self.length += 1; available -= 1
        self.parts.append(text[:max(0, available)])
        self.length += min(len(text), max(0, available))
        self.capped |= len(text) > available

    def finish(self, result):
        text = ''.join(self.parts)
        result.update(_text=text, characters=len(text), words=len(re.findall(r'\b\w+\b', text)),
                      has_extracted_text=bool(text.strip()))
        if self.capped and result['status'] == 'ok':
            result['status'] = 'capped'
        result['text_complete'] = result['status'] == 'ok'
        return result


def empty_result(status='ok'):
    return {'status': status, 'characters': 0, 'words': 0, 'rows_processed': 0,
            'rows_total': None, 'pages_processed': 0, 'pages_total': None,
            'no_text_pages': 0, 'content_date': None, 'text_complete': False}


def decode_text(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16')
    else:
        text = data.decode('utf-8-sig')
    controls = sum(ord(character) < 32 and character not in '\n\r\t\f' for character in text)
    if '\x00' in text or controls > max(1, len(text) // 100):
        raise ValueError('binary content')
    return text


def checked_zip(data, limits):
    archive = zipfile.ZipFile(io.BytesIO(data))
    entries = archive.infolist()
    if any(entry.flag_bits & 1 for entry in entries):
        archive.close(); raise PermissionError('encrypted archive')
    if sum(entry.file_size for entry in entries) > limits['max_expanded_bytes']:
        archive.close(); raise ParseLimit('expanded archive cap')
    return archive


def json_text(value, sample, limits):
    # Iterative traversal bounds recursion and number of visited values.
    stack = [value]
    while stack and not sample.capped:
        item = stack.pop(); sample.json_nodes += 1
        if sample.json_nodes > limits['max_json_nodes']:
            sample.capped = True; break
        if isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif item is not None:
            sample.add(item)


class HTMLText(HTMLParser):
    def __init__(self, sample):
        super().__init__(convert_charrefs=True)
        self.sample = sample
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.ignore += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.ignore:
            self.ignore -= 1

    def handle_data(self, data):
        if not self.ignore:
            self.sample.add(data.strip())


@contextmanager
def pdf_expansion_bounds(maximum):
    """Use pinned pypdf decoder limits and a cumulative compressed-stream budget."""
    from pypdf import filters
    from pypdf.errors import LimitReachedError
    from pypdf.generic import EncodedStreamObject
    names = ('ZLIB_MAX_OUTPUT_LENGTH', 'LZW_MAX_OUTPUT_LENGTH',
             'RUN_LENGTH_MAX_OUTPUT_LENGTH', 'JBIG2_MAX_OUTPUT_LENGTH',
             'MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH', 'MAX_DECLARED_STREAM_LENGTH')
    previous = {name: getattr(filters, name) for name in names}
    original = EncodedStreamObject.get_data
    decoded_ids = set(); expanded = 0

    def bounded_data(stream):
        nonlocal expanded
        try:
            data = original(stream)
        except LimitReachedError as error:
            raise ParseLimit('PDF expansion cap') from error
        if id(stream) not in decoded_ids:
            expanded += len(data); decoded_ids.add(id(stream))
        if expanded > maximum:
            raise ParseLimit('PDF expansion cap')
        return data

    try:
        for name, old in previous.items():
            setattr(filters, name, min(old, maximum) if old else maximum)
        EncodedStreamObject.get_data = bounded_data
        yield
    finally:
        EncodedStreamObject.get_data = original
        for name, old in previous.items():
            setattr(filters, name, old)


def extract(extension, data, limits=None):
    """Parse bytes only. Errors are fixed status codes, never source diagnostics."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    extension = extension.lower().lstrip('.')
    result = empty_result()
    sample = TextSample(limits['max_characters'])
    if extension not in SUPPORTED:
        return empty_result('unsupported')
    if not data:
        return empty_result('empty')
    if len(data) > limits['max_file_bytes']:
        return empty_result('size_cap')
    try:
        if extension in ('txt', 'md', 'csv', 'tsv', 'json', 'jsonl'):
            try:
                text = decode_text(data)
            except (UnicodeError, ValueError):
                return empty_result('binary')
            if extension in ('txt', 'md'):
                sample.add(text)
            elif extension in ('csv', 'tsv'):
                csv.field_size_limit(limits['max_file_bytes'])
                reader = csv.reader(io.StringIO(text, newline=''), delimiter='\t' if extension == 'tsv' else ',', strict=True)
                header = next((row for row in reader if row), None)
                cells = 0
                for row in reader:
                    if not row:
                        continue
                    if result['rows_processed'] >= limits['max_rows'] or cells + len(row) > limits['max_cells']:
                        result['status'] = 'capped'; break
                    result['rows_processed'] += 1; cells += len(row)
                    for cell in row:
                        sample.add(cell)
                if result['status'] == 'ok':
                    result['rows_total'] = result['rows_processed']
                result['header_present'] = header is not None
            elif extension == 'json':
                value = json.loads(text)
                rows = value if isinstance(value, list) else [value]
                result['rows_total'] = len(rows)
                result['rows_processed'] = min(len(rows), limits['max_rows'])
                if len(rows) > limits['max_rows']:
                    result['status'] = 'capped'
                for row in rows[:limits['max_rows']]:
                    json_text(row, sample, limits)
            else:
                for line in io.StringIO(text):
                    if not line.strip():
                        continue
                    if result['rows_processed'] >= limits['max_rows']:
                        result['status'] = 'capped'; break
                    value = json.loads(line)
                    result['rows_processed'] += 1
                    json_text(value, sample, limits)
                if result['status'] == 'ok':
                    result['rows_total'] = result['rows_processed']
        elif extension == 'pdf':
            from pypdf import PdfReader
            with pdf_expansion_bounds(limits['max_expanded_bytes']):
                reader = PdfReader(io.BytesIO(data), strict=False)     # real files repeat dictionary keys; bounds and deadlines still apply
                if reader.is_encrypted:
                    return empty_result('encrypted')
                result['pages_total'] = len(reader.pages)
                result['pages_processed'] = min(result['pages_total'], limits['max_pages'])
                if result['pages_total'] > limits['max_pages']:
                    result['status'] = 'capped'
                for index in range(result['pages_processed']):
                    text = reader.pages[index].extract_text() or ''
                    result['no_text_pages'] += not bool(text.strip())
                    sample.add(text)
        elif extension == 'docx':
            with checked_zip(data, limits) as archive:
                raw = archive.read('word/document.xml')
                if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
                    raise ValueError('unsafe XML')
                root = ElementTree.fromstring(raw)
                for node in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
                    sample.add(node.text or '')
                    if sample.capped:
                        break
            result['parts_processed'] = ['main_document']
        elif extension == 'xlsx':
            with checked_zip(data, limits):
                pass
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
            result['sheet_dimensions'] = []
            result['sheet_count'] = len(workbook.worksheets)
            scanned = cells = 0
            from openpyxl.worksheet._reader import WorkSheetParser
            row_tag = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row'
            cell_tag = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'
            try:
                for sheet in workbook.worksheets:
                    result['sheet_dimensions'].append({'declared_max_row': sheet.max_row, 'declared_max_column': sheet.max_column})
                    header = False
                    row_nonempty = False
                    # Reuse pinned openpyxl value decoding but stream actual XML cells,
                    # never a grid generated or truncated from worksheet dimensions.
                    with sheet._get_source() as source:
                        decoder = WorkSheetParser(source, sheet._shared_strings, data_only=True,
                            epoch=workbook.epoch, date_formats=workbook._date_formats,
                            timedelta_formats=workbook._timedelta_formats)
                        for event, node in ElementTree.iterparse(source, events=('start', 'end')):
                            if event == 'start' and node.tag == row_tag:
                                if scanned >= limits['max_rows'] + len(workbook.worksheets) or (header and result['rows_processed'] >= limits['max_rows']):
                                    result['status'] = 'capped'; break
                                scanned += 1
                                decoder.row_counter = int(node.get('r', decoder.row_counter + 1))
                                decoder.col_counter = 0
                                row_nonempty = False
                            elif event == 'end' and node.tag == cell_tag:
                                if cells >= limits['max_cells']:
                                    result['status'] = 'capped'; break
                                cells += 1
                                value = decoder.parse_cell(node)['value']
                                row_nonempty |= value is not None
                                if header and value is not None:
                                    sample.add(value)
                                node.clear()
                            elif event == 'end' and node.tag == row_tag:
                                if row_nonempty:
                                    if not header:
                                        header = True
                                    else:
                                        result['rows_processed'] += 1
                                node.clear()
                    if result['status'] == 'capped':
                        break
                if result['status'] == 'ok':
                    result['rows_total'] = result['rows_processed']
            finally:
                workbook.close()
            result['physical_rows_examined'] = scanned
            result['cells_examined'] = cells
        elif extension == 'eml':
            message = BytesParser(policy=policy.default).parsebytes(data)
            if message.defects:
                raise ValueError('malformed email')
            if message.get('Date'):
                try:
                    date = parsedate_to_datetime(message['Date'])
                    if date.tzinfo is not None:
                        result['content_date'] = date.astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError):
                    pass
            result['email_reference_hashes'] = sorted({hashlib.sha256(identifier.encode('utf-8')).hexdigest()
                for field in ('Message-ID', 'In-Reply-To', 'References')
                for identifier in re.findall(r'<[^<>]+>', str(message.get(field, '')))})
            # Select one body representation, avoid alternative/attachment duplication.
            body = message.get_body(preferencelist=('plain', 'html'))
            if body is not None:
                if body.defects:
                    raise ValueError('malformed email body')
                text = body.get_content()
                if body.defects:
                    raise ValueError('malformed decoded email body')
                if body.get_content_type() == 'text/html':
                    parser = HTMLText(sample); parser.feed(text); parser.close()
                else:
                    sample.add(text.strip())
            result['body_parts_processed'] = int(body is not None)
        return sample.finish(result)
    except ImportError:
        return empty_result('dependency_missing')
    except ParseLimit:
        return empty_result('decompression_cap')
    except PermissionError:
        return empty_result('encrypted')
    except Exception:
        return empty_result('malformed')
