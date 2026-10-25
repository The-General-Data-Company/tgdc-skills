"""A synthetic company archive with a registry of what every file is.

The registry is filled while the files are created, so expected counts come
from the construction and not from the code under test.
"""
import email
import hashlib
import io
import mailbox
import os
import zipfile
from email import policy
from datetime import datetime
from pathlib import Path

import spec_samples as S
import spec_taxonomy as T

PRIVATE_TOKENS = ['Northfield', 'Southbank', '01 Tender documents', '02 Drawings', '03 Contract', '04 Acceptance',
                  'tender notice', 'overview rev', 'wiring schematic', 'site layout', 'pipe layout', 'parola cont', 'Parole.xlsx', 'keystore',
                  'old note', 'oldformat', 'secretproject', 'Box1', 'Box2', 'ann@synthetic.test', 'bob@synthetic.test',
                  'owner@synthetic.test', 'co@synthetic.test', 'Water network plan', 'Antreprenorul', 'contractor shall',
                  'Auftragnehmer', 'Misc', 'Paper archive', 'run_export', 'contacts', 'split.zip']
OWNER, COOWNER = 'owner@synthetic.test', 'co@synthetic.test'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp(path, year):
    moment = datetime(year, 6, 1, 12).timestamp()
    os.utime(path, (moment, moment))


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.data = self.base / 'data'
        self.data.mkdir(parents=True)
        self.files = []       # loose files
        self.members = []     # archive members
        self.archives_opened = 0
        self.lost_members = 0
        self.partly = 0
        self.not_opened = {}
        self.messages = {}    # mailbox label -> list of dicts
        self.sheets = {}      # sha -> dict(sheets by size label, dedicated flag, location, schematic)
        self.build()

    # ------------------------------------------------------------------ registry helpers
    def put(self, rel, data, category, part='file', year=2022, label=None, lang='-', message=False, cad=False, doc=False,
            sheets=None, schematic=False, loc=None, cred=False, pages=None):
        path = self.data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        stamp(path, year)
        name = path.name
        if label is None:
            suffix = Path(name).suffix[1:].lower()
            label = suffix or '(no extension)'
        item = dict(rel=rel, size=len(data), sha=None if cred else sha(data), category=category, part=part, year=year, label=label,
                    lang=lang, message=message, cad=cad, doc=doc, cred=cred)
        self.files.append(item)
        if sheets:
            self.sheet(data, sheets, schematic, loc or ('file' if part == 'file' else 'mail'), pages)
        return path

    def sheet(self, data, sizes, schematic=False, loc='file', pages=None):
        key = sha(data)
        order = {'file': 0, 'mail': 1, 'archive': 2}
        current = self.sheets.get(key)
        if current is None:
            self.sheets[key] = {'sizes': dict(sizes), 'loc': loc, 'schematic': schematic, 'pages': pages or sum(sizes.values())}
        elif order[loc] < order[current['loc']]:
            current['loc'] = loc

    def member(self, archive, name, data, category, new, label=None, lang='-', container=False, cad=False, cred=False, sheets=None, loc='archive'):
        suffix = Path(name).suffix[1:].lower()
        self.members.append(dict(archive=archive, name=name, size=len(data), sha=None if cred else sha(data), category=category,
                                 label=label or suffix or '(no extension)', new=new, lang=lang, container=container, cad=cad, cred=cred))
        if sheets:
            self.sheet(data, sheets, False, loc)

    # ------------------------------------------------------------------ the tree
    def build(self):
        D, SC, SP, EM, ST, AR, OT = T.DOCUMENTS, T.SCANNED, T.SPREADSHEETS, T.EMAIL, T.STRUCTURED, T.ARCHIVES, T.OTHER
        # ---- company file archive: projects
        tender = S.pdf_bytes([('text', S.A4P, 1), ('text', S.A4P, 2)], creation_year=2016)
        self.tender = tender
        self.put('Company/Projects/Northfield/01 Tender documents/tender notice.pdf', tender, D, year=2024, lang='en', doc=True)
        self.put('Company/Projects/Southbank/01 Tender documents/tender copy.pdf', tender, D, year=2025, lang='en', doc=True)
        self.tender_years = 2016
        self.files[-1]['year'] = self.files[-2]['year'] = 2016
        self.put('Company/Projects/Northfield/01 Tender documents/Bill of Quantities.xlsx',
                 S.xlsx_bytes([S.EN, S.EN.replace('contractor', 'supplier')], 2018), SP, year=2024, lang='en', doc=True)
        self.files[-1]['year'] = 2018
        site = S.pdf_bytes([('text', S.A3P, 11), ('text', S.A3P, 12)])
        self.site = site
        self.put('Company/Projects/Northfield/02 Drawings/site layout.pdf', site, D, year=2020, lang='en', doc=True, sheets={'A3': 2})
        self.put('Company/Projects/Southbank/02 Drawings/site layout.pdf', site, D, year=2020, lang='en', doc=True, sheets={'A3': 2})
        self.put('Company/Projects/Northfield/02 Drawings/overview rev1.pdf', S.pdf_bytes([('drawing', S.A1P, 21)]), D, year=2021, lang=None, doc=True, sheets={'A1': 1})
        self.put('Company/Projects/Northfield/02 Drawings/overview rev2.pdf', S.pdf_bytes([('drawing', S.A1P, 22)]), D, year=2021, lang=None, doc=True, sheets={'A1': 1})
        self.put('Company/Projects/Northfield/02 Drawings/wiring schematic.pdf', S.pdf_bytes([('drawing', S.A4P, 23)]), D, year=2021, lang=None, doc=True,
                 sheets={'A4': 1}, schematic=True)
        self.put('Company/Projects/Northfield/03 Contract/contract.docx', S.docx_bytes(S.RO * 3 + ' Contract 1.', 2019), D, year=2024, lang='ro', doc=True)
        self.files[-1]['year'] = 2019
        self.put('Company/Projects/Southbank/03 Contract/contract beta.docx', S.docx_bytes(S.RO * 3 + ' Contract 2.', 2019), D, year=2024, lang='ro', doc=True)
        self.files[-1]['year'] = 2019
        mixed = S.RO * 8 + S.EN * 3
        self.put('Company/Projects/Southbank/03 Contract/bilingual contract.docx', S.docx_bytes(mixed, 2020), D, year=2024, lang='ro', doc=True)
        self.files[-1]['year'] = 2020
        self.bilingual = 1
        acceptance = S.pdf_bytes([('text', S.A4P, 31), ('drawing', S.A4P, 32), ('text', S.A4P, 33)])
        self.put('Company/Projects/Northfield/04 Acceptance/acceptance protocol.pdf', acceptance, D, year=2022, lang='en', doc=True, sheets={'A4': 1}, pages=3)
        dxf = b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n' * 3
        self.dxf = dxf
        self.put('Company/Projects/Southbank/02 Drawings/pipe layout.dxf', dxf, T.CAD, year=2020, cad=True)
        # ---- scans
        self.put('Company/Paper archive/scan001.pdf', S.pdf_bytes([('scan', S.A4P, 1), ('scan', S.A4P, 2)]), SC, year=2022, lang=None, doc=True)
        self.put('Company/Paper archive/scan002.tif', S.tiff_bytes(297, 420, 200, 3), SC, year=2022, lang=None, doc=True, sheets={'A3': 1})
        # ---- miscellaneous
        misc = 'Company/Misc/'
        self.put(misc + 'old note.txt', (S.EN * 2).encode(), D, year=1985, lang='en')
        self.unreliable_files = [self.files[-1]]
        self.put(misc + 'future.txt', (S.EN * 2 + ' Future.').encode(), D, year=2999, lang='en')
        self.unreliable_files.append(self.files[-1])
        self.put(misc + 'oldformat.doc', S.ole_doc_bytes(S.EN * 2 + ' Legacy.'), D, year=2012, lang='en', doc=True)
        self.put(misc + 'parola cont.txt', b'top secret value', T.CREDENTIALS, year=2023, cred=True, label='txt')
        self.put(misc + 'keystore.p12', b'\x30\x82binary', T.CREDENTIALS, year=2023, cred=True)
        self.put(misc + 'photo1.jpg', b'\xff\xd8\xff\xe0' + b'jpeg bytes one' * 5, T.PHOTOS, year=2017)
        self.put(misc + 'photo2.png', S.png_bytes(2), T.PHOTOS, year=2017)
        self.put(misc + 'drawings scan.png', S.png_bytes(9), T.PHOTOS, year=2023, sheets={'image file': 1})
        self.put(misc + 'tool.exe', b'MZ' + b'\x00' * 40, T.PROGRAMS, year=2023)
        self.put(misc + 'style.css', b'body { color: black; }\n', T.SOURCE, year=2023)
        self.put(misc + 'data.json', b'{"a": 1}\n', ST, year=2023)
        self.put(misc + 'backup.bak', b'backup data bytes', ST, year=2023)
        self.put(misc + 'noext', b'no extension content', OT, year=2023, label='(no extension)')
        self.put(misc + 'secretproject.weird file name', b'irregular suffix bytes', OT, year=2023, label='(other / irregular extension)')
        self.put(misc + 'plan.dwg', b'AC1018' + b'\x00' * 30, T.CAD, year=2020, cad=True)
        self.put(misc + 'damaged.zip', b'PK\x03\x04 not really a zip archive ' * 4, AR, year=2022)
        self.put(misc + 'packed.rar', b'Rar!\x1a\x07\x00 not really a rar archive ' * 4, AR, year=2022)
        self.not_opened = {'zip:no extraction tool': 1, 'rar:no extraction tool': 1}
        # hidden and credential-like entries and a link: never entered, never counted
        (self.data / 'Company/Misc/.hidden').mkdir()
        (self.data / 'Company/Misc/.hidden/inside.txt').write_text('hidden content')
        (self.data / 'Company/Misc/.DS_Store').write_bytes(b'x')
        (self.data / 'Company/Misc/Credentials').mkdir()
        (self.data / 'Company/Misc/Credentials/list.txt').write_text('secret')
        (self.data / 'Company/Misc/link.txt').symlink_to(self.base / 'elsewhere.txt')
        self.hidden_entries = 2
        self.credential_folders = 1
        self.symlinks = 1
        self.build_archives()
        self.build_mail()

    def build_archives(self):
        D, SC, AR = T.DOCUMENTS, T.SCANNED, T.ARCHIVES
        inner_text = (S.EN * 2 + ' New inner.').encode()
        inner_drawing = S.pdf_bytes([('drawing', S.A3P, 41)])
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('new.txt', inner_text)
            archive.writestr('drawing.pdf', inner_drawing)
        report = S.docx_bytes(S.RO * 3 + ' Report 1.', 2021)
        readme = (S.EN * 2 + ' Readme.').encode()
        dwg = b'AC1015' + b'\x01' * 40
        entries = [('docs/tender notice.pdf', self.tender), ('nested.zip', nested.getvalue()), ('report.docx', report), ('readme.txt', readme),
                   ('Parole.xlsx', b'cipher bytes'), ('cad/plan.dwg', dwg), ('cad/copy.dxf', self.dxf)]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        self.put('Company/Misc/archive.zip', buffer.getvalue(), AR, year=2022)
        a = 'Company/Misc/archive.zip'
        self.member(a, 'docs/tender notice.pdf', self.tender, D, new=False, lang='en')
        self.member(a, 'nested.zip', nested.getvalue(), AR, new=False, container=True)
        self.member(a, 'nested.zip!/new.txt', inner_text, D, new=True, lang='en')
        self.member(a, 'nested.zip!/drawing.pdf', inner_drawing, D, new=True, lang=None, sheets={'A3': 1})
        self.member(a, 'report.docx', report, D, new=True, lang='ro')
        self.member(a, 'readme.txt', readme, D, new=True, lang='en')
        self.member(a, 'Parole.xlsx', b'cipher bytes', T.CREDENTIALS, new=True, cred=True, label='xlsx')
        self.member(a, 'cad/plan.dwg', dwg, T.CAD, new=True, cad=True)
        self.member(a, 'cad/copy.dxf', self.dxf, T.CAD, new=False, cad=True)
        self.archives_opened += 2
        # encrypted member
        plain = (S.EN * 2 + ' Plain member.').encode()
        locked = S.zip_with_encrypted_member([('plain.txt', plain), ('secret.txt', b'cipher')], ['secret.txt'])
        self.put('Company/Misc/locked.zip', locked, AR, year=2022)
        self.member('Company/Misc/locked.zip', 'plain.txt', plain, D, new=True, lang='en')
        self.archives_opened += 1
        self.lost_members += 1
        self.partly += 1
        # raw split archive: .001 and .002 are halves of one zip
        joined = (S.EN * 2 + ' Joined member.').encode()
        whole = io.BytesIO()
        with zipfile.ZipFile(whole, 'w', zipfile.ZIP_STORED) as archive:
            archive.writestr('joined.txt', joined)
        data = whole.getvalue()
        half = len(data) // 2
        self.put('Company/Misc/split.zip.001', data[:half], AR, year=2022, label='001')
        self.put('Company/Misc/split.zip.002', data[half:], AR, year=2022, label='002')
        self.member('Company/Misc/split.zip.001', 'joined.txt', joined, D, new=True, lang='en')
        self.archives_opened += 1

    def build_mail(self):
        D, EM = T.DOCUMENTS, T.EMAIL
        plan = S.pdf_bytes([('drawing', S.A3P, 51)])
        notes = S.pdf_bytes([('text', S.A4P, 52)])
        detail = S.pdf_bytes([('drawing', S.A1P, 53)])
        layout = b'0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n' * 2
        layout2 = b'0\nSECTION\n2\nTABLES\n0\nENDSEC\n0\nEOF\n' * 2
        box1 = 'Mail/Box1/'
        specs = [
            ('Water network plan', 'Ann <ann@synthetic.test>', OWNER, S.RO * 2, 2021, [('plan.pdf', plan, 'application/pdf'), ('layout.dxf', layout, 'application/octet-stream')], 'ro'),
            ('Second request', 'Bob <bob@synthetic.test>', OWNER, S.RO * 2 + ' Two.', 2021, [('site layout.pdf', self.site, 'application/pdf'), ('plan.pdf', plan, 'application/pdf')], 'ro'),
            ('Third request', 'Ann <ann@synthetic.test>', OWNER, S.RO * 2 + ' Three.', 2022, [('notes.pdf', notes, 'application/pdf')], 'ro'),
            ('Fourth request', 'Bob <bob@synthetic.test>', OWNER, S.RO * 2 + ' Four.', 2022, [], 'ro'),
            ('Fifth request', 'Ann <ann@synthetic.test>', OWNER, S.EN * 2, 2023, [], 'en'),
            ('Sechste Anfrage', 'Bob <bob@synthetic.test>', OWNER, S.DE * 2, 2023, [], 'de'),
            ('Sent reply', OWNER, 'Ann <ann@synthetic.test>', S.EN * 2 + ' Reply.', 2024, [], 'en'),
            ('Gesendet', OWNER, 'Bob <bob@synthetic.test>', S.DE * 2 + ' Gesendet.', 2024, [], 'de'),
        ]
        rows, raw_messages = [], []
        for number, (subject, sender, recipient, body, year, attachments, lang) in enumerate(specs, 1):
            received = sender != OWNER
            data = S.eml_bytes(subject, sender, recipient, body, year=year, attachments=attachments, delivered_to=OWNER if received else None)
            raw_messages.append(data)
            self.put(f'{box1}m{number:02d}.eml', data, EM, part='mailbox', lang=lang, message=True, label='eml', year=year)
            self.put(f'{box1}m{number:02d}.json', b'{"subject": "x", "n": %d}\n' % number, EM, part='mailbox', label='json')
            rows.append(dict(year=year, lang=lang, sent=not received))
        self.put(box1 + 'index.csv', b'id,subject\n1,x\n', EM, part='mailbox', label='csv')
        self.put(box1 + 'contacts.vcf', b'BEGIN:VCARD\nFN:Someone\nEND:VCARD\n', EM, part='mailbox', label='vcf')
        self.messages['Mailbox 1 (owner, Gmail)'] = rows
        # alternative exports of the same messages
        box = mailbox.mbox(str(self.base / 'tmp.mbox'))
        for data in raw_messages:
            box.add(data)
        box.flush()
        box.close()
        mbox_bytes = (self.base / 'tmp.mbox').read_bytes()
        (self.base / 'tmp.mbox').unlink()
        self.put(box1 + 'all.mbox', mbox_bytes, EM, part='alt', label='mbox')
        self.put(box1 + 'attachments/plan.pdf', plan, D, part='alt', lang='-', sheets={'A3': 1}, loc='mail')
        self.put(box1 + 'attachments/layout.dxf', layout, T.CAD, part='alt', cad=True)
        self.put(box1 + 'attachments/notes.pdf', notes, D, part='alt')
        for number in (1, 2, 3):
            message = email.message_from_bytes(raw_messages[number - 1], policy=policy.default)
            part = next(p for p in message.walk() if p.get_content_type() == 'text/plain')
            self.put(f'{box1}bodies/m{number:02d}.txt', part.get_payload(decode=True), D, part='alt', label='txt')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
            for number, data in enumerate(raw_messages, 1):
                zipped.writestr(f'm{number:02d}.eml', data)
        self.put(box1 + 'export.zip', archive.getvalue(), T.ARCHIVES, part='alt', label='zip')
        for number, data in enumerate(raw_messages, 1):
            self.member(box1 + 'export.zip', f'm{number:02d}.eml', data, EM, new=False, label='eml')
        self.archives_opened += 1
        self.put(box1 + 'run_export.py', b'print("export")\n', T.SOURCE, part='log', label='py')
        self.put(box1 + 'export.log', b'exported 8 messages\n', T.SYSTEM, part='log', label='log')
        # second mailbox: received mail only, one message with an impossible date
        box2 = 'Mail/Box2/'
        rows2 = []
        specs2 = [('Offer one', S.EN * 2 + ' A.', 2023, 'en'), ('Offer two', S.EN * 2 + ' B.', 2024, 'en'), ('Oferta trei', S.RO * 2 + ' C.', 2025, 'ro'),
                  ('Old one', S.EN * 2 + ' D.', 1985, 'en')]
        for number, (subject, body, year, lang) in enumerate(specs2, 1):
            attachments = [('detail.pdf', detail, 'application/pdf'), ('layout2.dxf', layout2, 'application/octet-stream')] if number == 1 else []
            data = S.eml_bytes(subject, 'Ann <ann@synthetic.test>', COOWNER, body, year=year, attachments=attachments, delivered_to=COOWNER)
            self.put(f'{box2}n{number}.eml', data, EM, part='mailbox', lang=lang, message=True, label='eml', year=year)
            rows2.append(dict(year=year, lang=lang, sent=False))
        self.put(box2 + 'attachments/detail.pdf', detail, D, part='alt', sheets={'A1': 1}, loc='mail')
        self.put(box2 + 'attachments/layout2.dxf', layout2, T.CAD, part='alt', cad=True)
        self.messages['Mailbox 2 (co-owner, Yahoo)'] = rows2
        self.config = {
            'company': 'Synthetic Works SRL', 'sector_line': 'Construction contractor (water, sewerage)', 'month': 'October 2026',
            'mailboxes': [{'root': str(self.data / 'Mail/Box1'), 'label': 'Mailbox 1 (owner, Gmail)'},
                          {'root': str(self.data / 'Mail/Box2'), 'label': 'Mailbox 2 (co-owner, Yahoo)'}],
            'project_roots': [str(self.data / 'Company/Projects')], 'project_depth': 1,
            'stages': {'title': 'project milestones', 'items': [
                {'name': 'tender documents', 'patterns': ['tender']}, {'name': 'contract', 'patterns': ['contract']},
                {'name': 'drawings', 'patterns': ['drawings']}, {'name': 'acceptance', 'patterns': ['acceptance']},
                {'name': 'monitoring', 'patterns': ['monitoring']}]},
            'bill_of_quantities_patterns': ['bill of quantities'],
        }
