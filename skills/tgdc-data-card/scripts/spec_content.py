"""Per-content analyzers for the data format specification.

Each function reads bytes or a seekable binary file object, never a path, and
returns plain numbers and short hashes. No text is returned except inside the
calling worker, which turns it into language facts and drops it.
"""
import hashlib
import io
import math
import re
import time
import zipfile
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree

import spec_language
from spec_taxonomy import A4_LIKE, IMAGE_SHEET_EXTENSIONS, TEXT_EXTRACTABLE, raw_extension, size_class

TEXT_LIMIT = 40000
MAX_XML_BYTES = 64 * 1024 * 1024
VECTOR_PATH_MINIMUM = 100          # path objects on a page before it can count as vector-heavy
VECTOR_TEXT_FACTOR = 2             # ... and fewer than this many characters per path object
TEXT_LAYER_MINIMUM = 10            # letters and digits on a page that make it a text page
MAX_VECTOR_PAGES = 2000            # A4-like pages inspected per PDF; beyond that every k-th page is sampled
MAX_TEXT_PAGES = 40
MAX_FRAMES = 5000
ATTACHMENT_KEYS = ('pdf', 'img')
MARKUP_LIMIT = 1000000             # characters of an HTML body that are stripped; the language check needs far less
MESSAGE_SECONDS = 30.0             # time budget of one message of an mbox file, attachments included


def sha256_stream(stream, chunk=1024 * 1024):
    digest = hashlib.sha256()
    total = 0
    while True:
        block = stream.read(chunk)
        if not block:
            break
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def short_hash(value):
    return hashlib.sha256(value.strip().lower().encode('utf-8', 'replace')).hexdigest()[:16]


# ----------------------------------------------------------------------------- PDF
def _count_paths(raw, container, limit, depth=0, form=None):
    """Count path objects on a page (or inside a form object), stopping at limit."""
    if form is None:
        total = raw.FPDFPage_CountObjects(container)
        getter = raw.FPDFPage_GetObject
    else:
        total = raw.FPDFFormObj_CountObjects(form)
        getter = lambda _page, index: raw.FPDFFormObj_GetObject(form, index)
    found = 0
    for index in range(total):
        obj = getter(container, index)
        kind = raw.FPDFPageObj_GetType(obj)
        if kind == raw.FPDF_PAGEOBJ_PATH:
            found += 1
        elif kind == raw.FPDF_PAGEOBJ_FORM and depth < 4:
            found += _count_paths(raw, container, limit - found, depth + 1, obj)
        if found >= limit:
            return found
    return found


def _alnum(text):
    return sum(char.isalnum() for char in text)


def page_key(width_pt, height_pt):
    """Internal page class: a standard size, 'oversize' (larger than A4, no standard size) or 'other'."""
    name, _, larger = size_class(width_pt, height_pt)
    if name == 'other':
        return ('oversize' if larger else 'other'), larger
    return name, larger


def pdf_year(value):
    match = re.match(r'D:(\d{4})', value or '')
    return int(match.group(1)) if match else None


def analyze_pdf(source, text_pages=1, want_text=False, deadline=None):
    """Page sizes, text-layer check, vector-heavy A4 pages and (optionally) text of a PDF.

    Returns {'unchecked': True} when the PDF cannot be opened or parsed."""
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
    except ImportError:
        return {'unchecked': True, 'pages': 0}
    try:
        document = pdfium.PdfDocument(source)
    except Exception:
        return {'unchecked': True, 'pages': 0}
    text = []
    try:
        pages = len(document)
        classes = {}
        checked = {}
        vector = {}
        has_text_layer = False
        gathered = 0
        # Text layer of the first pages, and text for the language check.
        wanted = pages if want_text else min(text_pages, pages)
        for index in range(min(wanted, MAX_TEXT_PAGES if want_text else text_pages)):
            if want_text and gathered >= TEXT_LIMIT and index >= text_pages:
                break
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    content = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            if index < text_pages and _alnum(content) >= TEXT_LAYER_MINIMUM:
                has_text_layer = True
            if want_text and gathered < TEXT_LIMIT:
                text.append(content)
                gathered += len(content)
        sizes = [document.get_page_size(index) for index in range(pages)]
        ordinal = {}
        for width, height in sizes:
            name, _ = page_key(width, height)
            if name in A4_LIKE or name == 'other':
                ordinal[name] = ordinal.get(name, 0) + 1
        total_like = sum(ordinal.values())
        step = max(1, math.ceil(total_like / MAX_VECTOR_PAGES))
        like_index = 0
        truncated = False
        for index, (width, height) in enumerate(sizes):
            name, _ = page_key(width, height)
            entry = classes.setdefault(name, [0, 0])
            entry[0] += 1
            if not (name in A4_LIKE or name == 'other'):
                continue
            like_index += 1
            if (like_index - 1) % step or truncated:
                continue
            if deadline is not None and time.monotonic() > deadline:
                truncated = True
                continue
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    characters = textpage.count_chars()
                finally:
                    textpage.close()
                needed = max(VECTOR_PATH_MINIMUM, characters // VECTOR_TEXT_FACTOR + 1)
                paths = _count_paths(raw, page.raw, needed)
            finally:
                page.close()
            checked[name] = checked.get(name, 0) + 1
            if paths >= needed:
                vector[name] = vector.get(name, 0) + 1
        for name, entry in classes.items():
            if name in checked and checked[name] < entry[0]:
                entry[1] = int(round(vector.get(name, 0) * entry[0] / checked[name]))
            else:
                entry[1] = vector.get(name, 0)
        result = {'pages': pages, 'scanned': not has_text_layer, 'classes': classes,
                  'year': pdf_year(document.get_metadata_value('CreationDate')), 'sampled': step > 1 or truncated}
        if want_text:
            result['_text'] = '\n'.join(text)
        return result
    except Exception:
        return {'unchecked': True, 'pages': 0}
    finally:
        document.close()


def page_candidates(source):
    """Per-page (index, internal class, vector flag, larger than A4) for the calibration sample."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    document = pdfium.PdfDocument(source)
    rows = []
    try:
        for index in range(len(document)):
            width, height = document.get_page_size(index)
            name, larger = page_key(width, height)
            flag = False
            if name in A4_LIKE or name == 'other':
                page = document[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        characters = textpage.count_chars()
                    finally:
                        textpage.close()
                    needed = max(VECTOR_PATH_MINIMUM, characters // VECTOR_TEXT_FACTOR + 1)
                    flag = _count_paths(raw, page.raw, needed) >= needed
                finally:
                    page.close()
            rows.append((index, name, flag, larger))
    finally:
        document.close()
    return rows


# ----------------------------------------------------------------------------- TIFF and images
def analyze_tiff(source):
    """Frame count and physical size class per frame, from the file header only."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        image = Image.open(source)
    except Exception:
        return {'unchecked': True, 'pages': 0}
    classes = {}
    try:
        frames = min(getattr(image, 'n_frames', 1), MAX_FRAMES)
        for index in range(frames):
            image.seek(index)
            width, height = image.size
            dpi = image.info.get('dpi')
            if dpi and dpi[0] and dpi[1] and dpi[0] >= 50 and dpi[1] >= 50:
                key, _ = page_key(width / dpi[0] * 72.0, height / dpi[1] * 72.0)
            else:
                key = 'image'
            classes.setdefault(key, [0, 0])[0] += 1
        return {'pages': frames, 'classes': classes}
    except Exception:
        return {'unchecked': True, 'pages': 0}
    finally:
        image.close()


def image_sheet_facts(extension):
    return {'pages': 1, 'classes': {'image': [1, 0]}} if extension in IMAGE_SHEET_EXTENSIONS else None


# ----------------------------------------------------------------------------- Text extraction
def decode_plain(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', 'replace')
    head = data[:4096]
    if head.count(b'\x00') > len(head) // 20:
        return ''
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1250', 'replace')


_BLOCK_OPEN = re.compile(r'<(script|style)\b', re.I)
_BLOCK_CLOSE = {'script': re.compile(r'</script\s*>', re.I), 'style': re.compile(r'</style\s*>', re.I)}
_TAG = re.compile(r'<[^<>]*>')


def strip_markup(text):
    """Text of an HTML body without tags, scripts and style sheets.

    The input is cut at MARKUP_LIMIT characters and every step moves forward only: a closing tag that is missing once is missing
    for the rest of the text, and a tag cannot contain another '<'. The time therefore grows linearly with the length, however
    hostile the markup is."""
    text = text[:MARKUP_LIMIT]
    pieces, position, search_from, missing = [], 0, 0, set()
    while True:
        match = _BLOCK_OPEN.search(text, search_from)
        if match is None:
            break
        name = match.group(1).lower()
        close = None if name in missing else _BLOCK_CLOSE[name].search(text, match.end())
        if close is None:
            missing.add(name)
            search_from = match.end()
            continue
        pieces.append(text[position:match.start()])
        pieces.append(' ')
        position = search_from = close.end()
    pieces.append(text[position:])
    return unescape(_TAG.sub(' ', ''.join(pieces)))


def strip_rtf(text):
    text = re.sub(r"\\'[0-9a-fA-F]{2}", ' ', text)
    text = re.sub(r'\\[a-zA-Z]+-?\d* ?', ' ', text)
    return re.sub(r'[{}\\]', ' ', text)


def _safe_xml(raw):
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise ValueError('unsafe XML')
    return ElementTree.fromstring(raw)


def _xml_texts(archive, name, tag_suffixes):
    with archive.open(name) as stream:
        raw = stream.read(MAX_XML_BYTES)
    root = _safe_xml(raw)
    parts = []
    size = 0
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] in tag_suffixes and node.text:
            parts.append(node.text)
            size += len(node.text)
            if size > TEXT_LIMIT:
                break
    return parts


def office_year(archive):
    try:
        with archive.open('docProps/core.xml') as stream:
            root = _safe_xml(stream.read(1024 * 1024))
        for node in root.iter():
            if node.tag.endswith('}created') and node.text:
                match = re.match(r'(\d{4})-', node.text.strip())
                if match:
                    return int(match.group(1))
    except Exception:
        pass
    return None


def office_text(extension, source):
    """(text, year of creation) for zip-based Office and OpenDocument files."""
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if any(info.flag_bits & 1 for info in archive.infolist()):
            raise PermissionError('encrypted')
        year = office_year(archive)
        if extension in ('docx', 'docm'):
            parts = _xml_texts(archive, 'word/document.xml', {'t'}) if 'word/document.xml' in names else []
        elif extension in ('xlsx', 'xlsm'):
            parts = _xml_texts(archive, 'xl/sharedStrings.xml', {'t'}) if 'xl/sharedStrings.xml' in names else []
        elif extension in ('pptx', 'pptm', 'ppsx'):
            parts = []
            for name in sorted(n for n in names if re.match(r'ppt/slides/slide\d+\.xml$', n))[:60]:
                parts.extend(_xml_texts(archive, name, {'t'}))
        else:
            parts = _xml_texts(archive, 'content.xml', {'p', 'h', 'span'}) if 'content.xml' in names else []
    return ' '.join(parts)[:TEXT_LIMIT], year


def legacy_doc_text(data):
    """Best-effort text of a Word 97-2003 file: printable UTF-16LE and cp1252 runs of the WordDocument stream."""
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        if not ole.exists('WordDocument'):
            return ''
        stream = ole.openstream('WordDocument').read(8 * 1024 * 1024)
    finally:
        ole.close()
    wide = stream.decode('utf-16-le', 'ignore')
    narrow = stream.decode('cp1252', 'ignore')
    runs_wide = re.findall(r'[^\W\d_ ][^\x00-\x08\x0e-\x1f]{3,}', wide)
    runs_narrow = re.findall(r'[A-Za-z\u00c0-\u024f][ -~\u00a0-\u024f]{7,}', narrow)
    wide_text = ' '.join(runs_wide)
    narrow_text = ' '.join(runs_narrow)
    best = wide_text if sum(map(str.isalpha, wide_text)) >= sum(map(str.isalpha, narrow_text)) else narrow_text
    return best[:TEXT_LIMIT]


def extract_text(extension, data):
    """Text and own-date year of a document held in memory. Raises on unreadable input."""
    if extension in ('txt', 'md', 'csv', 'tsv'):
        return decode_plain(data)[:TEXT_LIMIT], None
    if extension in ('html', 'htm', 'xhtml'):
        return strip_markup(decode_plain(data))[:TEXT_LIMIT], None
    if extension == 'rtf':
        return strip_rtf(decode_plain(data))[:TEXT_LIMIT], None
    if extension in ('docx', 'docm', 'xlsx', 'xlsm', 'pptx', 'pptm', 'ppsx', 'odt', 'ods', 'odp'):
        return office_text(extension, io.BytesIO(data))
    if extension == 'doc':
        return legacy_doc_text(data), None
    return '', None


# ----------------------------------------------------------------------------- E-mail
def _addresses(message, names):
    found = []
    for name in names:
        for header in message.get_all(name, []):
            try:
                found.extend(address for _, address in getaddresses([str(header)]) if address)
            except Exception:
                continue
    return found


def _header_year(message):
    try:
        value = message.get('Date')
        return parsedate_to_datetime(str(value)).year if value else None
    except Exception:
        return None


def body_key(message, normalized):
    """Short key of a message that has no Message-ID: its Date, From and Subject headers and the text of its bodies.

    Two exports of the same message carry the same headers and text, whatever the file format around them."""
    material = [' '.join(str(message.get(name, '')).split()) for name in ('Date', 'From', 'Subject')] + sorted(set(normalized))
    if not any(material):
        return None
    return 'b:' + hashlib.sha256('\n'.join(material).encode('utf-8', 'replace')).hexdigest()[:16]


def message_key(facts):
    """The identity of a message in the facts: the short hash of its Message-ID, else its body key, else None."""
    return facts.get('mid') or facts.get('bid')


def analyze_message(data, attachments=True, deadline=None):
    """Facts of one RFC 822 message: ids as short hashes, no addresses, no text.

    When the deadline passes, the remaining attachments are skipped and the message is marked partial."""
    message = BytesParser(policy=policy.default).parsebytes(data)
    facts = {'size': len(data)}
    message_id = str(message.get('Message-ID', '')).strip()
    facts['mid'] = short_hash(message_id) if message_id else None
    facts['year'] = _header_year(message)
    senders = _addresses(message, ['From'])
    facts['from'] = short_hash(senders[0]) if senders else None
    recipients = _addresses(message, ['Delivered-To']) + _addresses(message, ['To'])
    facts['to'] = sorted({short_hash(address) for address in recipients})[:40]
    bodies, plain, html, found, normalized = [], [], [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            continue
        filename = part.get_filename()
        kind = part.get_content_type()
        if filename is None and kind in ('text/plain', 'text/html'):
            digest = hashlib.sha256(payload).hexdigest()
            try:
                content = part.get_content()
            except Exception:
                content = payload.decode('utf-8', 'replace')
            bodies.append(digest)
            bodies.append(hashlib.sha256(content.encode('utf-8', 'replace')).hexdigest())
            content = content[:MARKUP_LIMIT]
            normalized.append(hashlib.sha256(' '.join(content.split()).encode('utf-8', 'replace')).hexdigest())
            (plain if kind == 'text/plain' else html).append(content)
            continue
        if not attachments:
            continue
        if deadline is not None and time.monotonic() > deadline:
            facts['partial'] = True
            continue
        extension = raw_extension(filename or '')
        record = {'sha': hashlib.sha256(payload).hexdigest(), 'ext': extension, 'size': len(payload),
                  'name': (filename or '').encode('utf-8', 'replace').decode('utf-8')}
        if extension == 'pdf' or kind == 'application/pdf':
            record['pdf'] = analyze_pdf(payload, text_pages=3, deadline=deadline)
        elif extension in ('tif', 'tiff'):
            record['img'] = analyze_tiff(io.BytesIO(payload))
        elif extension in IMAGE_SHEET_EXTENSIONS:
            record['img'] = image_sheet_facts(extension)
        found.append(record)
    facts['bodies'] = sorted(set(bodies))
    facts['atts'] = found
    if not message_id:
        key = body_key(message, normalized)
        if key:
            facts['bid'] = key
    body = '\n'.join(plain) if plain else strip_markup('\n'.join(html))
    facts['lang'] = spec_language.analyze_text(body)
    return facts


def iter_mbox(stream, max_message_bytes, skip=None):
    """Yield (message bytes, truncated) for each message of an mbox file, streaming.

    When skip() is true at the start of a message, the message is counted but not collected: (None, False)."""
    current = []
    size = 0
    truncated = False
    skipping = False
    previous_blank = True
    started = False
    for line in stream:
        if line.startswith(b'From ') and previous_blank:
            if started:
                yield (None if skipping else b''.join(current)), truncated
            current, size, truncated, started = [], 0, False, True
            skipping = bool(skip and skip())
            previous_blank = False
            continue
        previous_blank = line in (b'\n', b'\r\n')
        if not started or skipping:
            continue
        if size < max_message_bytes:
            current.append(line)
            size += len(line)
        else:
            truncated = True
    if started:
        yield (None if skipping else b''.join(current)), truncated


def analyze_mbox(stream, known_ids, max_message_bytes, deadline=None, message_seconds=None):
    """Facts of an mbox file. Messages already known from loose .eml files are recorded by id only.

    count is the number of messages in the file. Messages that start after the deadline are counted and not analyzed (cut);
    messages whose attachments were skipped, whose size was cut or that could not be read are listed as partial."""
    messages = []
    total = cut = partial = 0
    budget = MESSAGE_SECONDS if message_seconds is None else message_seconds
    for data, truncated in iter_mbox(stream, max_message_bytes, lambda: deadline is not None and time.monotonic() > deadline):
        total += 1
        if data is None:
            cut += 1
            continue
        limit = time.monotonic() + budget
        if deadline is not None:
            limit = min(limit, deadline)
        try:
            message = BytesParser(policy=policy.default).parsebytes(data, headersonly=True)
            message_id = str(message.get('Message-ID', '')).strip()
            mid = short_hash(message_id) if message_id else None
        except Exception:
            mid = None
        if mid and mid in known_ids:
            messages.append({'mid': mid, 'known': True, 'size': len(data)})
            continue
        try:
            facts = analyze_message(data, attachments=not truncated, deadline=limit)
        except Exception:
            facts = {'mid': mid, 'size': len(data), 'unreadable': True}
        if not mid and facts.get('bid') in known_ids:
            messages.append({'mid': None, 'bid': facts['bid'], 'known': True, 'size': len(data)})
            continue
        if truncated or facts.get('partial') or facts.get('unreadable'):
            partial += 1
        messages.append(facts)
    return {'count': total, 'messages': messages, 'cut': cut, 'partial': partial}


# ----------------------------------------------------------------------------- one document, any context
NO_LANGUAGE = {'lang': None, 'chunks': {}}


def content_facts(extension, data, text_pages=1, deadline=None, mail_attachments=True):
    """Sheet, language and own-date facts of one document held in memory (bytes) or in a seekable file.

    The returned dictionary never contains text."""
    facts = {}
    if extension == 'pdf':
        pdf = analyze_pdf(data, text_pages=text_pages, want_text=True, deadline=deadline)
        text = pdf.pop('_text', '')
        facts['pdf'] = pdf
        if pdf.get('year'):
            facts['year'] = pdf['year']
        facts['lang'] = spec_language.analyze_text(text) if text and not pdf.get('scanned') else dict(NO_LANGUAGE)
    elif extension in ('tif', 'tiff'):
        facts['img'] = analyze_tiff(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
        facts['lang'] = dict(NO_LANGUAGE)
    elif extension in IMAGE_SHEET_EXTENSIONS:
        facts['img'] = image_sheet_facts(extension)
    elif extension == 'eml':
        if isinstance(data, (bytes, bytearray)):
            message = analyze_message(bytes(data), attachments=mail_attachments, deadline=deadline)
            facts['lang'] = message['lang']
            if message.get('year'):
                facts['year'] = message['year']
    elif extension in TEXT_EXTRACTABLE and isinstance(data, (bytes, bytearray)):
        try:
            text, year = extract_text(extension, bytes(data))
            facts['lang'] = spec_language.analyze_text(text)
            if year:
                facts['year'] = year
        except Exception:
            facts['lang'] = dict(NO_LANGUAGE)
            facts['unreadable'] = True
    return facts
