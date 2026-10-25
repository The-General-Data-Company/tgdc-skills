"""Builders for synthetic test content. Nothing here resembles real customer data."""
import io
import struct
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from PIL import Image
from reportlab.lib.pagesizes import A1, A3, A4, letter
from reportlab.pdfgen import canvas

EN = ('The contractor shall deliver the completed works to the client within the agreed period and the client will '
      'inspect the works before the final acceptance. All drawings that are attached to this letter have been '
      'prepared by the design team and they describe the water supply network of the village. ')
RO = ('Antreprenorul va livra lucrarile finalizate beneficiarului in termenul convenit, iar beneficiarul va verifica '
      'lucrarile inainte de receptia finala. Toate desenele care sunt atasate la aceasta scrisoare au fost intocmite '
      'de echipa de proiectare si descriu reteaua de alimentare cu apa a satului. ')
DE = ('Der Auftragnehmer liefert die fertigen Arbeiten innerhalb der vereinbarten Frist an den Auftraggeber, und der '
      'Auftraggeber wird die Arbeiten vor der endgultigen Abnahme prufen. Alle Zeichnungen, die diesem Schreiben '
      'beigefugt sind, wurden vom Planungsteam erstellt und beschreiben das Wassernetz des Dorfes. ')
A4P, A3P, A1P, LETTERP = A4, A3, A1, letter


def pdf_bytes(pages, creation_year=None):
    """pages: list of (kind, pagesize, seed) with kind 'text', 'drawing' or 'scan'. The seed makes the page unique."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1)
    for kind, size, seed in pages:
        pdf.setPageSize(size)
        width, height = size
        if kind == 'text':
            text = pdf.beginText(40, height - 60)
            text.setFont('Helvetica', 10)
            body = (seed if isinstance(seed, str) else EN + f' Reference {seed}.') * 3
            for index in range(0, len(body), 90):
                text.textLine(body[index:index + 90])
            pdf.drawText(text)
        elif kind == 'drawing':
            for index in range(400):
                pdf.line(20 + index % 30, 20 + index, width - 20 - index % 17, 30 + index * 1.3)
            pdf.setFont('Helvetica', 6)
            pdf.drawString(30, 30, f'Title block sheet {seed}')
        else:
            image = Image.new('L', (60, 80), 255)
            image.putpixel((seed % 60, seed % 80), 0)
            pdf.drawInlineImage(image, 10, 10, width - 20, height - 20)
        pdf.showPage()
    pdf.save()
    data = buffer.getvalue()
    marker = b"/CreationDate (D:20000101000000+00'00')"
    if creation_year:
        return data.replace(marker, marker.replace(b'2000', str(creation_year).encode()))
    return data.replace(marker, marker.replace(b'D:', b'X:'))


def tiff_bytes(width_mm, height_mm, dpi=200, seed=0):
    width, height = round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi)
    image = Image.new('1', (width, height), 1)
    image.putpixel((seed % width, 0), 0)
    buffer = io.BytesIO()
    image.save(buffer, format='TIFF', compression='group4', dpi=(dpi, dpi))
    return buffer.getvalue()


def png_bytes(seed=0):
    image = Image.new('RGB', (8, 8), (seed % 256, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _core(year, month):
    return ('<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dcterms="http://purl.org/dc/terms/"><dcterms:created>' + f'{year}-{month:02d}-04T10:00:00Z'
            + '</dcterms:created></cp:coreProperties>')


def docx_bytes(text, year=2019):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('word/document.xml', '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                         + text + '</w:t></w:r></w:p></w:body></w:document>')
        archive.writestr('docProps/core.xml', _core(year, 3))
    return buffer.getvalue()


def xlsx_bytes(strings, year=2018):
    buffer = io.BytesIO()
    items = ''.join(f'<si><t>{value}</t></si>' for value in strings)
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('xl/sharedStrings.xml', '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + items + '</sst>')
        archive.writestr('docProps/core.xml', _core(year, 5))
    return buffer.getvalue()


def ole_doc_bytes(text, encoding='cp1252'):
    """A minimal OLE2 compound file whose WordDocument stream holds text (legacy .doc look-alike)."""
    payload = b'\x00' * 0x400 + text.encode(encoding) + b'\x00' * 16
    payload += b'\x00' * (-len(payload) % 512)
    if len(payload) < 4096:
        payload += b'\x00' * (4096 - len(payload))
    data_sectors = len(payload) // 512
    directory_sector = data_sectors
    fat_sector = data_sectors + 1
    end, fat_marker, free = 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
    fat = [i + 1 for i in range(data_sectors - 1)] + [end, end, fat_marker]
    fat += [free] * (128 - len(fat))
    header = bytearray(512)
    header[0:8] = bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<HHHHH', header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into('<IIIIIIIIII', header, 40, 0, 1, directory_sector, 0, 4096, end, 0, end, 0, 0)
    struct.pack_into('<109I', header, 76, *([fat_sector] + [free] * 108))

    def entry(name, kind, child, start, size):
        raw_name = name.encode('utf-16-le')
        item = bytearray(128)
        item[0:len(raw_name)] = raw_name
        struct.pack_into('<HBBIII', item, 64, len(raw_name) + 2 if name else 0, kind, 1, free, free, child)
        struct.pack_into('<I', item, 116, start)
        struct.pack_into('<I', item, 120, size)
        return bytes(item)

    directory = (entry('Root Entry', 5, 1, end, 0) + entry('WordDocument', 2, free, 0, len(payload))
                 + entry('', 0, free, 0, 0) + entry('', 0, free, 0, 0))
    return bytes(header) + payload + directory + struct.pack('<128I', *fat)


def eml_bytes(subject, sender, recipient, body, year=2023, attachments=(), message_id=None, delivered_to=None):
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = sender
    message['To'] = recipient
    if delivered_to:
        message['Delivered-To'] = delivered_to
    message['Message-ID'] = message_id or f'<{subject.replace(" ", ".")}.{year}@synthetic.test>'
    message['Date'] = format_datetime(datetime(year, 6, 15, 12, 0, tzinfo=timezone.utc))
    message.set_content(body)
    for name, content, kind in attachments:
        maintype, subtype = kind.split('/')
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()


def zip_with_encrypted_member(entries, locked_names):
    """A zip whose named members carry the 'encrypted' flag (the data is not really encrypted)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
        offsets = {info.filename: info.header_offset for info in archive.infolist()}
    data = bytearray(buffer.getvalue())
    for name in locked_names:
        offset = offsets[name]
        data[offset + 6] |= 1
        position = 0
        while True:
            position = data.find(b'PK\x01\x02', position)
            if position < 0:
                break
            length = int.from_bytes(data[position + 28:position + 30], 'little')
            if bytes(data[position + 46:position + 46 + length]) == name.encode():
                data[position + 8] |= 1
            position += 4
    return bytes(data)
