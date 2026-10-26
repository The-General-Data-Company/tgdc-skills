"""Pages of the data format specification, in the visual design of the Data Card.

The renderer reads only spec.json: counts, sizes, years and partner-declared
vocabulary. It opens no source file and writes no number that the file does not
hold. Sections without data are left out.
"""
import math
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import CondPageBreak, Flowable, KeepTogether, PageBreak, Paragraph, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

import report as card
import spec_taxonomy as T

SCHEMA = 'tgdc-format-spec/1.0'
ORANGE = colors.HexColor('#EC9A3C')
TRACK = colors.HexColor('#242424')
WIDTH = card.WIDTH - 12      # inner width of the page frame, so that tables line up with the headings
SECTIONS = ['Summary', 'Content by category', 'File formats (top 35 by volume)', 'Mailboxes', 'Languages',
            'Inside the compressed archives', 'Technical drawings and schematics', 'By year', 'Notes']


# ----------------------------------------------------------------------------- validation
def validate(spec):
    """Reject a file that is not a specification written by this skill."""
    if not isinstance(spec, dict) or spec.get('schema') != SCHEMA:
        raise ValueError('Unsupported specification schema.')
    for key in ('totals', 'parts', 'categories', 'formats', 'methodology', 'current_year'):
        if key not in spec:
            raise ValueError('Incomplete specification.')

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value < 0:
                raise ValueError('Invalid specification number.')
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None and not isinstance(value, str):
            raise ValueError('Invalid specification value.')
    walk(spec)
    check_vocabulary(spec)


ARCHIVE_KINDS = frozenset('zip tar tar.gz tar.bz2 tar.xz gz bz2 xz 7z rar arj cab other'.split())
REFUSAL = re.compile(r'([a-z0-9.]+):([a-z][a-z ]{0,39})')


def _items(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('Invalid specification value.')
    return value


def _fields(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid specification value.')
    return value


def _extensions(labels):
    """Every extension in the file is a known format or one of the sentinel labels: file-name fragments never pass."""
    for label in _items(labels):
        if not T.is_extension_label(label):
            raise ValueError('Unknown file format label.')


def _short_label(value):
    if not T.is_short_label(value):
        raise ValueError('Invalid label.')


def check_vocabulary(spec):
    """Reject text that did not come from this skill's fixed vocabulary or from a short declared label."""
    for category in _items(spec['categories']):
        if _fields(category).get('name') not in T.CATEGORIES:
            raise ValueError('Unknown content category.')
        _extensions(category.get('formats'))
    for row in _items(spec['formats']):
        _extensions([_fields(row).get('ext')])
        if row.get('category') not in T.CATEGORIES:
            raise ValueError('Unknown content category.')
    for category in _items(_fields(spec.get('archives') or {}).get('categories')):
        if _fields(category).get('name') not in T.CATEGORIES:
            raise ValueError('Unknown content category.')
        _extensions(category.get('formats'))
    parts = _fields(spec['parts'])
    mailbox_part = _fields(parts.get('mailboxes') or {})
    _extensions(mailbox_part.get('metadata_extensions'))
    for item in _items(mailbox_part.get('incomplete')):
        _short_label(_fields(item).get('label'))
    for item in _items(_fields(parts.get('alternative_exports') or {}).get('items')):
        _extensions(_fields(item).get('extensions'))
    for box in _items(spec.get('mailboxes')):
        _short_label(_fields(box).get('label'))
        for item in _items(box.get('alternatives')):
            _extensions(_fields(item).get('extensions'))
        metadata = _fields(box.get('metadata') or {})
        _extensions(metadata.get('extensions'))
        _extensions(list(_fields(metadata.get('counts') or {})))
    stages = _fields(_fields(_fields(spec.get('drawings') or {}).get('projects') or {}).get('stages') or {})
    if stages:
        for label in [stages.get('title')] + _items(stages.get('names')) + [_fields(item).get('name') for item in _items(stages.get('by_stage'))]:
            _short_label(label)
    for key in _fields(_fields(spec['methodology']).get('archives') or {}).get('not_opened', {}):
        match = REFUSAL.fullmatch(key)
        if not match or match.group(1) not in ARCHIVE_KINDS:
            raise ValueError('Invalid archive refusal reason.')


# ----------------------------------------------------------------------------- text helpers
def n(value):
    return f'{int(value):,}'


class Unit:
    """Unit of every volume in the specification, chosen once per document from the total volume."""

    def __init__(self, name, divisor, floor, half):
        self.name, self.divisor, self.floor, self.half = name, divisor, floor, half

    def table(self, size):
        return f'{size / self.divisor:,.2f}'

    def fine(self, size):
        return f'{size / self.divisor:,.3f}'

    def short(self, size):
        """Headline size with one decimal. A part below the floor reads in the next smaller unit, as small parts do in the reference."""
        if size >= self.floor:
            return f'{size / self.divisor:,.1f} {self.name}'
        return T.size_text(size)


# floor: the size from which a part reads in this unit (the ladder of T.size_text); half: what 0.000 stands for in the formats table
GB, MB, KB = Unit('GB', 1e9, 1e8, '0.5 MB'), Unit('MB', 1e6, 1e5, '0.5 KB'), Unit('KB', 1e3, 1e3, '0.5 byte')


def display_unit(total):
    """GB from 10 GB, as in the reference; MB from 10 MB; KB below. Small selections would read 0.00 GB everywhere."""
    return GB if total >= 10e9 else MB if total >= 10e6 else KB


def unit_of(spec):
    return display_unit(spec['totals']['bytes'])


def dotted(labels):
    """Extension labels as running text: '.json, .csv'; a sentinel label reads as words."""
    words = {T.NO_EXTENSION: 'no extension', T.IRREGULAR_EXTENSION: 'other extension'}
    return ', '.join(words.get(label) or '.' + label for label in labels)


def plural(count, singular, many=None):
    return f'{n(count)} {singular if count == 1 else (many or singular + "s")}'


noun = plural


def about_noun(value, step, one, many=None):
    return f'{about(value, step)} {one if value == 1 else (many or one + "s")}'


def steps(total):
    """Rounding steps for a sheet total: (summary text, detail table)."""
    if total >= 1000:
        return 100, 10
    if total >= 100:
        return 10, 1
    return 1, 1


def about(value, step):
    """'about 15,800' with the step the reference uses; exact counts below the step carry no 'about'."""
    if step == 1:
        return n(value)
    rounded = T.round_to(value, step)
    return 'fewer than ' + n(step) if value and not rounded else 'about ' + n(rounded)


def detail(value, step):
    if step == 1:
        return n(value)
    rounded = T.round_to(value, step)
    return 'fewer than ' + n(step) if value and not rounded else n(rounded)


def singular(title):
    return title[:-1] if title.endswith('s') else title


# ----------------------------------------------------------------------------- flowable helpers
def _cell(value, size=8, align='left', color=card.WHITE, font='Outfit'):
    safe = escape(card._plain(value, font)).replace('\n', '<br/>')
    style = ParagraphStyle('spec', fontName=font, fontSize=size, leading=size * 1.35, textColor=color,
                           alignment=TA_RIGHT if align == 'right' else TA_LEFT, splitLongWords=True, spaceAfter=0)
    return Paragraph(safe, style)


def _head(text, align='left'):
    safe = escape(card._plain(text.upper(), 'Mono'))
    style = ParagraphStyle('spechead', fontName='Mono', fontSize=6.3, leading=8.4, textColor=card.BLUE,
                           alignment=TA_RIGHT if align == 'right' else TA_LEFT, splitLongWords=False)
    return Paragraph(safe, style)


class BalancedTable(Table):
    """A table that never leaves fewer than MIN_ROWS body rows on either side of a page break."""
    MIN_ROWS = 4

    def split(self, availWidth, availHeight):
        parts = Table.split(self, availWidth, availHeight)
        if len(parts) != 2:
            return parts
        header = self.repeatRows if isinstance(self.repeatRows, int) else len(self.repeatRows)
        height = availHeight
        while True:
            head, tail = parts
            if len(head._cellvalues) - header < self.MIN_ROWS:
                return []                       # no decent first part fits here: start the table on the next page
            if len(tail._cellvalues) - header >= self.MIN_ROWS:
                return parts
            height -= 8
            if height <= 0:
                return []
            parts = Table.split(self, availWidth, height)
            if len(parts) != 2:
                return []


def _table(header, rows, widths, aligns, size=8, total=False, pad=3.5):
    data = [[_head(text, aligns[i]) for i, text in enumerate(header)]]
    for index, row in enumerate(rows):
        last = total and index == len(rows) - 1
        data.append([_cell(value, size, aligns[i], card.WHITE, 'OutfitMedium' if last else 'Outfit') for i, value in enumerate(row)])
    table = BalancedTable(data, colWidths=widths, repeatRows=1, splitByRow=1, hAlign='LEFT')
    style = [('LINEBELOW', (0, 0), (-1, -1), .4, card.RULE), ('LEFTPADDING', (0, 0), (0, -1), 0), ('LEFTPADDING', (1, 0), (-1, -1), 4),
             ('RIGHTPADDING', (0, 0), (-1, -1), 5),
             ('TOPPADDING', (0, 0), (-1, -1), pad), ('BOTTOMPADDING', (0, 0), (-1, -1), pad), ('VALIGN', (0, 0), (-1, -1), 'TOP')]
    if total:
        style.append(('LINEABOVE', (0, -1), (-1, -1), .8, card.MUTED))
    table.setStyle(TableStyle(style))
    return table


def _item_table(rows):
    return _table(['Item', 'Value'], rows, [166, WIDTH - 166], ['left', 'left'], size=8, pad=1.9)


def _heading(title):
    """Section heading that never strands at the foot of a page, without pulling a long table onto the next page."""
    return card._paragraph(title, 20, card.WHITE, 'OutfitMedium')


def _section(title, intro=None):
    story = [CondPageBreak(120), _heading(title)]
    if intro:
        story.append(card._paragraph(intro, 8.2, card.MUTED))
    return story


class CategoryChart(Flowable):
    """Horizontal bars: volume by content category in the unit of the document, largest first."""

    def __init__(self, rows, title):
        super().__init__()
        self.rows, self.title = rows, title
        self.width = WIDTH
        self.height = 30 + 13 * len(rows)

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(card.BLUE)
        canvas.setFont('Mono', 7.5)
        canvas.drawString(0, self.height - 9, self.title.upper())
        label_width, value_width = 232, 44
        track = self.width - label_width - value_width
        top = max(value for _, value in self.rows) or 1
        for index, (label, value) in enumerate(self.rows):
            y = self.height - 30 - index * 13
            canvas.setFillColor(card.MUTED)
            canvas.setFont('Outfit', 7)
            canvas.drawString(0, y, card._plain(label))
            canvas.setFillColor(TRACK)
            canvas.rect(label_width, y - 2, track, 8, stroke=0, fill=1)
            canvas.setFillColor(card.BLUE)
            canvas.rect(label_width, y - 2, max(0.5, track * value / top), 8, stroke=0, fill=1)
            canvas.setFillColor(card.WHITE)
            canvas.setFont('Outfit', 7.2)
            canvas.drawString(label_width + track + 5, y, f'{value:,.1f}')


def nice_ceiling(value):
    if value <= 0:
        return 1.0
    exponent = 10 ** math.floor(math.log10(value))
    for factor in (1, 2, 2.5, 5, 10):
        if value <= factor * exponent:
            return factor * exponent
    return 10 * exponent


class YearChart(Flowable):
    """Volume of the file archive by year as bars; e-mail messages by year as a line on a second axis."""

    def __init__(self, rows, title, with_mail, unit):
        super().__init__()
        self.rows, self.title, self.with_mail, self.unit = rows, title, with_mail, unit
        self.width = WIDTH
        self.height = 205

    def draw(self):
        canvas = self.canv
        left, right, bottom, top = 34, self.width - (38 if self.with_mail else 8), 40, self.height - 30
        canvas.setFillColor(card.BLUE)
        canvas.setFont('Mono', 7.5)
        canvas.drawString(0, self.height - 9, self.title.upper())
        years = self.rows
        unit, divisor = self.unit.name, self.unit.divisor
        top_gb = nice_ceiling(max(row['bytes'] / divisor for row in years) or 1)
        top_mail = nice_ceiling(max(row['messages'] for row in years) or 1)
        canvas.setStrokeColor(card.RULE)
        canvas.setLineWidth(.4)
        canvas.setFont('Outfit', 6.5)
        for tick in range(5):
            y = bottom + (top - bottom) * tick / 4
            canvas.line(left, y, right, y)
            canvas.setFillColor(card.MUTED)
            canvas.drawRightString(left - 4, y - 2, f'{top_gb * tick / 4:g}')
            if self.with_mail:
                canvas.drawString(right + 4, y - 2, f'{top_mail * tick / 4:,.0f}')
        canvas.saveState()
        canvas.setFillColor(card.MUTED)
        canvas.translate(8, (top + bottom) / 2 - 12)
        canvas.rotate(90)
        canvas.drawString(0, 0, f'archive {unit}')
        canvas.restoreState()
        if self.with_mail:
            canvas.saveState()
            canvas.setFillColor(card.MUTED)
            canvas.translate(self.width - 2, (top + bottom) / 2 - 12)
            canvas.rotate(90)
            canvas.drawString(0, 0, 'e-mails')
            canvas.restoreState()
        slot = (right - left) / len(years)
        points = []
        every = 1 if len(years) <= 32 else 2
        for index, row in enumerate(years):
            x = left + slot * index
            height = (top - bottom) * (row['bytes'] / divisor) / top_gb
            canvas.setFillColor(card.BLUE)
            canvas.rect(x + slot * .15, bottom, slot * .7, max(height, 0.4 if row['bytes'] else 0), stroke=0, fill=1)
            points.append((x + slot / 2, bottom + (top - bottom) * row['messages'] / top_mail))
            if index % every == 0:
                canvas.saveState()
                canvas.setFillColor(card.MUTED)
                canvas.setFont('Outfit', 6.3)
                canvas.translate(x + slot / 2 + 2, bottom - 6)
                canvas.rotate(90)
                canvas.drawRightString(0, 0, str(row['year']))
                canvas.restoreState()
        if self.with_mail:
            canvas.setStrokeColor(ORANGE)
            canvas.setLineWidth(1.1)
            for first, second in zip(points, points[1:]):
                canvas.line(first[0], first[1], second[0], second[1])
            canvas.setFillColor(ORANGE)
            for x, y in points:
                canvas.circle(x, y, 1.4, stroke=0, fill=1)


# ----------------------------------------------------------------------------- summary rows
def headline(spec):
    totals = spec['totals']
    return f"{unit_of(spec).short(totals['bytes'])} ({T.binary_text(totals['bytes'])}), {noun(totals['files'], 'file')}"


def mailbox_summary(spec):
    mailboxes = spec['parts']['mailboxes']
    messages, files = mailboxes['messages'], mailboxes['files']
    kind = '.mbox' if mailboxes['formats'] == ['mbox'] else '.eml'
    text = f"{unit_of(spec).short(mailboxes['bytes'])}, "
    parts = [f"{noun(messages, 'e-mail message')} ({kind})"]
    if mailboxes.get('metadata_files'):
        parts.append(f"{noun(mailboxes['metadata_files'], 'per-message metadata file')} ({dotted(mailboxes['metadata_extensions'])})")
    if mailboxes.get('other_files'):
        parts.append(f"{plural(mailboxes['other_files'], 'other file')}")
    text += (f'{noun(files, "file")}: ' if len(parts) > 1 else '') + ' + '.join(parts)
    for item in mailboxes['incomplete']:
        if item['account']:
            text += (f"; {item['label']}: {plural(item['messages'], 'message')} in the account, volume and attachment figures cover the "
                     f"{plural(item['held'], 'received message')} and increase once the sent messages are added")
        else:
            text += (f"; {item['label']}: {plural(item['held'], 'received message')} held; volume and attachment figures "
                     'increase once the sent messages are added')
    return text


def alternative_summary(spec):
    exports = spec['parts']['alternative_exports']
    unit = unit_of(spec)
    pieces = []
    for item in exports['items']:
        size = unit.short(item['bytes'])
        if item['kind'] == 'mbox':
            pieces.append(f"{item['files']:,} .mbox file{'s' if item['files'] != 1 else ''} ({size})")
        elif item['kind'] == 'attachments':
            pieces.append(f"{noun(item['files'], 'extracted attachment file')} ({size})")
        elif item['kind'] == 'bodies':
            pieces.append(f"{noun(item['files'], 'extracted message body', 'extracted message bodies')} ({size})")
        else:
            pieces.append(f"{item['files']:,} export archive{'s' if item['files'] != 1 else ''} ({size})")
    return f"{unit.short(exports['bytes'])}, {noun(exports['files'], 'file')}: " + ' + '.join(pieces)


def cad_text(spec):
    cad = spec['cad']
    pieces = []
    if cad['file_archive']:
        pieces.append(f"{n(cad['file_archive'])} in the file archive")
    if cad['archives']:
        text = f"{n(cad['archives'])} inside the compressed archives"
        if cad['archives_new']:
            text += f" (of which {n(cad['archives_new'])} new content)"
        pieces.append(text)
    if cad['mail']:
        pieces.append(f"{noun(cad['mail'], 'extracted e-mail attachment')}")
    return f"{n(cad['total'])} in total: " + ' + '.join(pieces)


def drawing_rows(spec):
    """Reference rows about drawings, in reference order. Missing values leave their row out."""
    rows = []
    drawings = spec.get('drawings')
    if 'cad' in spec:
        rows.append(('Native CAD files (DWG / DXF)', cad_text(spec)))
    if not drawings:
        return rows
    step, _ = steps(drawings['sheets'])
    text = f"{about_noun(drawings['sheets'], step, 'sheet')}: {about_noun(drawings['engineering'], step, 'engineering drawing')}"
    if drawings['schematics']:
        text += f" + {about_noun(drawings['schematics'], step, 'schematic / diagram', 'schematics / diagrams')}"
    rows.append(('Technical drawings and schematics (sheets)', text + f", in {about_noun(drawings['files'], step, 'file')}"))
    dedicated, documents = drawings['dedicated'], drawings['documents']
    pieces = []
    if dedicated['files']:
        pieces.append(f"{about_noun(dedicated['files'], step, 'drawing file')} ({about_noun(dedicated['sheets'], step, 'sheet')})")
    if documents['files']:
        pieces.append(f"{about_noun(documents['files'], step, 'document')} with drawings among other pages, e.g. technical projects and tender files "
                      f"({about_noun(documents['sheets'], step, 'sheet')})")
    if pieces:
        rows.append(('Dedicated drawing files vs documents with drawings', ' + '.join(pieces)))
    if drawings['sizes']:
        rows.append(('Sheet sizes', ', '.join(f'{label} {n(T.round_to(count, step))}' if step > 1 else f'{label} {n(count)}'
                                              for label, count in drawings['sizes'].items())))
    if 'views' in drawings:
        views = drawings['views']
        rows.append(('Estimated individual views (plans, sections, details)',
                     f"about {n(T.round_to(views['total'], 100 if views['total'] >= 1000 else 10))} (about {views['per_sheet']} per sheet, "
                     f"range {views['low']}-{views['high']})"))
    names = {'file': 'company file archive', 'mail': 'only in the e-mail (attachments)', 'archive': 'only inside compressed archives'}
    if drawings['locations']:
        rows.append(('Where the sheets are', '; '.join(f'{names[key]}: {detail(count, step)} sheet{"" if count == 1 else "s"}'
                                                          for key, count in drawings['locations'].items())))
    projects = drawings.get('projects')
    if projects:
        share = round(100 * projects['sheets'] / projects['total_sheets']) if projects['total_sheets'] else 0
        text = (f"{share}% of the sheets sit in {n(projects['projects_with_sheets'])} of the {noun(projects['projects'], 'project file')}, "
                "next to the project's other documents")
        stages = projects.get('stages')
        if stages:
            for level in stages['levels']:
                if level['projects']:
                    text += (f"; {noun(level['projects'], 'project')} {'documents' if level['projects'] == 1 else 'document'} {level['k']} or more "
                             f"{stages['title']} ({about_noun(level['sheets'], step, 'sheet')})")
            text += f" (of {stages['count']}: {', '.join(stages['names'])})"
        rows.append(('Drawings within complete project files', text))
        if projects.get('boq'):
            boq = projects['boq']
            rows.append(('Drawings with bills of quantities',
                         f"{noun(boq['projects'], 'project')} {'holds' if boq['projects'] == 1 else 'hold'} both drawings and a bill of quantities "
                         f"({about_noun(boq['sheets'], step, 'sheet')})"))
        if projects.get('revisions'):
            revisions = projects['revisions']
            rows.append(('Drawing revisions', f"{about_noun(revisions['drawings'], step, 'drawing')} kept in several versions or design phases, "
                                              f"in {noun(revisions['projects'], 'project')}"))
    return rows


def no_language_reason(none):
    """Why files have no confirmed language, ordered by the counts: scans without a text layer against short or garbled text."""
    scans = none.get('scans')
    if scans is None:
        return 'scans without a text layer, very short or garbled text'
    text = none['files'] - scans
    if scans and text:
        if scans > text:
            return 'mainly scans without a text layer; also very short or garbled text'
        return 'mainly very short or garbled text; also scans without a text layer'
    return 'scans without a text layer' if scans else 'very short or garbled text'


def language_rows(spec):
    rows = []
    languages = spec.get('languages')
    if not languages:
        return rows
    if languages['rows']:
        rows.append(('Languages (main language per file, files)', ', '.join(f"{row['name']} ({n(row['files'])})" for row in languages['rows'])))
    if languages.get('bilingual'):
        local = languages['bilingual']
        rows.append((f"Files with substantial text in both {local['name']} and English", n(local['files'])))
    if languages.get('none'):
        rows.append((f"Files of text formats where no language could be confirmed ({no_language_reason(languages['none'])})",
                     n(languages['none']['files'])))
    return rows


def archive_rows(spec):
    parts = spec['parts']
    short = unit_of(spec).short
    rows = []
    if 'archive_contents' in parts:
        contents = parts['archive_contents']
        rows.append(('Contents of the compressed archives (unpacked; the archive files themselves are in the file archive)',
                     f"{short(contents['bytes'])}, {noun(contents['files'], 'file')} from {noun(contents['archives'], 'compressed archive')}"))
        held = parts['held_without_archives']
        rows.append(('Files as held, without the contents of the compressed archives',
                     f"{short(held['bytes'])} ({T.binary_text(held['bytes'])}), {noun(held['files'], 'file')}"))
        rows.append(('Contents of the compressed archives that are new content (not a copy of another file)',
                     f"{short(contents['new_bytes'])}, {noun(contents['new_files'], 'file')}"))
    return rows


def summary_rows(spec):
    parts, totals = spec['parts'], spec['totals']
    short = unit_of(spec).short
    has_mail, has_archives = 'mailboxes' in parts, 'archive_contents' in parts
    content = 'Original company files' + (' and business mailboxes' if has_mail else '') + ', as held by the company'
    if has_archives:
        content += ', plus the contents of the compressed archives among them'
    rows = [('Content', content), ('Total volume (headline)', headline(spec)),
            ('Company file archive', f"{short(parts['file_archive']['bytes'])}, {noun(parts['file_archive']['files'], 'file')}")]
    if has_mail:
        rows.append(('Business mailboxes', mailbox_summary(spec)))
    if 'alternative_exports' in parts:
        rows.append(('Alternative exports of the same mailbox (same messages, not additional content)', alternative_summary(spec)))
    if 'export_logs' in parts:
        rows.append(('Mailbox export log and script files', f"{short(parts['export_logs']['bytes'])}, {noun(parts['export_logs']['files'], 'file')}"))
    rows.extend(archive_rows(spec))
    if totals['hashed_files']:
        rows.append(('Unique file contents (by SHA-256)', f"{n(totals['unique_contents'])} of {noun(totals['files'], 'file')}"))
    periods = spec.get('periods', {})
    if 'documents' in periods:
        rows.append(('Document period (file archive, 99% of dated documents)', T.year_range(*periods['documents'])))
    if 'email' in periods:
        rows.append(('E-mail period', T.year_range(*periods['email'])))
    rows.extend(drawing_rows(spec))
    rows.extend(language_rows(spec))
    return rows


# ----------------------------------------------------------------------------- sections
def title_block(spec):
    company = spec.get('company')
    big = company or 'Data format specification of the original files'
    pieces = ['Data format specification of the original files'] if company else []
    pieces += [spec[key] for key in ('sector_line', 'month') if spec.get(key)]
    story = [card._label('Data format specification / original files'), card._paragraph(card._short(big, 70), 27, card.WHITE, 'OutfitMedium', 32)]
    if pieces:
        story.append(card._paragraph(' \u00b7 '.join(pieces), 9.6, card.MUTED, 'OutfitLight', 12))
    return story


def summary_section(spec):
    return title_block(spec) + [_heading('Summary'), _item_table(summary_rows(spec))]


def category_section(spec):
    total = spec['totals']['bytes'] or 1
    split_inside = spec['totals']['files'] != spec['totals']['held_files']
    unit = unit_of(spec)
    header = ['Content category', 'Files', 'Unique files', unit.name, '% of volume', 'Of which inside archives: files', f'Of which inside archives: {unit.name}', 'Main formats']
    rows = []
    for category in spec['categories']:
        rows.append([category['name'], n(category['files']), n(category['unique']) if 'unique' in category else '', unit.table(category['bytes']),
                     f"{100 * category['bytes'] / total:.1f}", n(category['archive_files']), unit.table(category['archive_bytes']), ', '.join(category['formats'])])
    rows.append(['Total', n(spec['totals']['files']), '', unit.table(spec['totals']['bytes']), '100.0', n(sum(c['archive_files'] for c in spec['categories'])),
                 unit.table(sum(c['archive_bytes'] for c in spec['categories'])), ''])
    widths = [128, 38, 38, 38, 34, 50, 50, WIDTH - 376]
    aligns = ['left', 'right', 'right', 'right', 'right', 'right', 'right', 'left']
    if not split_inside:
        for index in (6, 5):
            del header[index], widths[index], aligns[index]
            for row in rows:
                del row[index]
        widths[-1] = WIDTH - sum(widths[:-1])
    intro = ('All files held plus the contents of the compressed archives (unpacked); the archive contents are also shown in their own columns.'
             if split_inside else 'All files held.')
    chart_rows = sorted(((T.short_category(c['name']), c['bytes'] / unit.divisor) for c in spec['categories']), key=lambda item: -item[1])
    return _section('Content by category', intro) + [_table(header, rows, widths, aligns, size=8, total=True), Spacer(1, 12),
                                                       CategoryChart(chart_rows, f'Volume by content category ({unit.name})')]


def formats_section(spec):
    unit = unit_of(spec)
    rows = [[row['ext'], row['category'], n(row['files']), unit.fine(row['bytes']), n(row['archive_files'])] for row in spec['formats']]
    header = ['Format (extension)', 'Category', 'Files', unit.name, 'Of which inside archives: files']
    widths = [78, WIDTH - 78 - 52 - 52 - 74, 52, 52, 74]
    aligns = ['left', 'left', 'right', 'right', 'right']
    if not any(row['archive_files'] for row in spec['formats']) and 'archive_contents' not in spec['parts']:
        for row in rows:
            del row[4]
        del header[4], widths[4], aligns[4]
        widths[1] = WIDTH - widths[0] - widths[2] - widths[3]
    return _section('File formats (top 35 by volume)') + [_table(header, rows, widths, aligns, size=8)]


EXTENSION_TEXT = {'mbox': '.mbox (single mailbox file)', 'eml': '.eml (RFC 822, attachments embedded)', 'eml+mbox': '.eml (RFC 822, attachments embedded) and .mbox'}


def mailbox_other_text(box):
    pieces = []
    if box.get('metadata'):
        pieces.append(f"{noun(box['metadata']['files'], 'per-message metadata file')} ({dotted(box['metadata']['extensions'])})")
    if box.get('sent_missing') or box.get('held_messages'):
        held = box.get('held_messages') or box['received']
        pieces.append(f"{noun(held, 'received message')} held; volume and attachment figures increase once the sent messages are added")
    if box.get('other_files'):
        pieces.append(plural(box['other_files'], 'other file'))
    incomplete = box.get('incomplete')
    if incomplete and incomplete['messages']:
        pieces.append(f"{plural(incomplete['messages'], 'message')} not analyzed in full")
    return '; '.join(pieces)


def mailbox_alt_rows(box, unit):
    rows = []
    for item in box.get('alternatives', ()):
        files = item['files']
        count = f"{n(files)} file{'s' if files != 1 else ''}"
        if item['kind'] == 'mbox':
            rows.append([f"{box['label']} - .mbox export", '(same messages)', unit.table(item['bytes']), '', EXTENSION_TEXT['mbox'], f'{count}; alternative export of the same mailbox'])
        elif item['kind'] == 'attachments':
            rows.append([f"{box['label']} - extracted attachments", '(same messages)', unit.table(item['bytes']), '', 'original attachment files',
                         f'{count}; attachments of the same messages, also embedded in the .eml'])
        elif item['kind'] == 'bodies':
            rows.append([f"{box['label']} - extracted message bodies", '(same messages)', unit.table(item['bytes']), '', '.txt / .html message bodies',
                         f'{count}; text and HTML bodies of the same messages, also inside the .eml'])
        else:
            known = [e for e in item.get('extensions', ()) if e not in (T.NO_EXTENSION, T.IRREGULAR_EXTENSION)]
            extension = dotted(item.get('extensions', ())) or 'archive'
            rows.append([f"{box['label']} - export archive ({extension})", '(same messages)', unit.table(item['bytes']), '',
                         f"{dotted(known) + ' ' if known else ''}archive of the whole export",
                         f'{count}; the mailbox export as delivered, same content as the unpacked folder'])
    return rows


def mailbox_section(spec):
    unit = unit_of(spec)
    rows = []
    for box in spec['mailboxes']:
        rows.append([box['label'], n(box['messages']), unit.table(box['bytes']), T.year_range(*box['period']) if box.get('period') else '',
                     EXTENSION_TEXT[box['format']], mailbox_other_text(box)])
        rows.extend(mailbox_alt_rows(box, unit))
    logs = spec['parts'].get('export_logs')
    if logs:
        rows.append(['Mailbox export log and script files', '', unit.table(logs['bytes']), '', 'scripts / logs', noun(logs['files'], 'file')])
    header = ['Mailbox', 'Messages', unit.name, 'Period', 'Format', 'Other files']
    widths = [108, 70, 34, 58, 92, WIDTH - 362]
    aligns = ['left', 'right', 'right', 'left', 'left', 'left']
    return _section('Mailboxes') + [_table(header, rows, widths, aligns, size=8)]


def language_section(spec):
    languages = spec['languages']
    unit = unit_of(spec)
    with_mail = languages['total']['messages'] > 0
    rows = [[row['name'], n(row['files']), unit.table(row['bytes']), n(row['messages'])] for row in languages['rows']]
    if languages.get('other'):
        rows.append(['Other languages (isolated detections)', n(languages['other']['files']), unit.table(languages['other']['bytes']),
                     n(languages['other']['messages'])])
    if languages.get('none'):
        rows.append([f"No language confirmed ({no_language_reason(languages['none'])})", n(languages['none']['files']),
                     unit.table(languages['none']['bytes']), n(languages['none']['messages'])])
    total = languages['total']
    rows.append(['Total text-format files examined', n(total['files']), unit.table(total['bytes']), n(total['messages'])])
    header = ['Language', 'Files (main language)', unit.name, 'E-mail messages']
    widths = [WIDTH - 3 * 82, 82, 82, 82]
    aligns = ['left', 'right', 'right', 'right']
    if not with_mail:
        for row in rows:
            del row[3]
        del header[3], widths[3], aligns[3]
        widths[0] = WIDTH - 2 * 82
    intro = ('Main language per text-bearing file, detected and confirmed by a common-word / script check. '
             'Drawings, photos and archives are not language-classified.')
    return _section('Languages', intro) + [_table(header, rows, widths, aligns, size=8, total=True)]


def archive_section(spec):
    archives = spec['archives']
    unit = unit_of(spec)
    rows = [[c['name'], n(c['files']), unit.table(c['bytes']), n(c['new_files']), unit.table(c['new_bytes']), ', '.join(c['formats'])]
            for c in archives['categories']]
    total = archives['total']
    rows.append(['Total', n(total['files']), unit.table(total['bytes']), n(total['new_files']), unit.table(total['new_bytes']), ''])
    header = ['Content category', 'Files (all unpacked)', f'{unit.name} (all unpacked)', 'Files (new content)', f'{unit.name} (new content)', 'Main formats']
    widths = [150, 56, 56, 56, 56, WIDTH - 374]
    aligns = ['left', 'right', 'right', 'right', 'right', 'left']
    intro = ("Detail of the archive contents included in the totals. 'All unpacked' counts every file inside the archives; 'new content' leaves out "
             'files identical to another file, nested archives whose contents are counted, and repeats within the archives.')
    extra = [row for row in summary_rows(spec) if row[0] in dict(archive_rows(spec)) or row[0].startswith('Native CAD')]
    story = _section('Inside the compressed archives', intro) + [_table(header, rows, widths, aligns, size=8, total=True)]
    return story + ([Spacer(1, 10), _item_table(extra)] if extra else [])


def detail_rows(spec):
    drawings = spec['drawings']
    _, fine = steps(drawings['sheets'])
    rows = [('Total', 'sheets', detail(drawings['sheets'], fine)), ('Total', 'engineering drawings', detail(drawings['engineering'], fine))]
    if drawings['schematics']:
        rows.append(('Total', 'schematics / diagrams', detail(drawings['schematics'], fine)))
    rows.append(('Total', 'files with drawings', detail(drawings['files'], fine)))
    if 'views' in drawings:
        rows.append(('Total', 'estimated individual views', detail(drawings['views']['total'], 100 if drawings['views']['total'] >= 1000 else 10)))
    for key, label in (('dedicated', 'dedicated drawing files'), ('documents', 'documents with drawings among other pages')):
        if drawings[key]['files']:
            rows.append(('File type', f'{label}: files', detail(drawings[key]['files'], fine)))
            rows.append(('File type', f'{label}: sheets', detail(drawings[key]['sheets'], fine)))
    for label, count in drawings['sizes'].items():
        rows.append(('Sheet size', label, detail(count, fine)))
    names = {'file': 'company file archive', 'mail': 'only in the e-mail (attachments)', 'archive': 'only inside compressed archives'}
    for key, count in drawings['locations'].items():
        rows.append(('Location', names[key], detail(count, fine)))
    projects = drawings.get('projects')
    if projects:
        rows.append(('Project workflow', f"sheets kept in project files ({n(projects['projects_with_sheets'])} of {n(projects['projects'])} projects)",
                     detail(projects['sheets'], fine)))
        stages = projects.get('stages')
        if stages:
            for level in stages['levels']:
                if level['projects']:
                    prefix = f"projects documenting {level['k']}+ {stages['title']}"
                    rows.append(('Project workflow', prefix + ': projects', n(level['projects'])))
                    rows.append(('Project workflow', prefix + ': sheets', detail(level['sheets'], fine)))
        if projects.get('boq'):
            rows.append(('Project workflow', 'projects with drawings and a bill of quantities', n(projects['boq']['projects'])))
        if stages:
            for item in stages['by_stage']:
                rows.append((f"Sheets by {singular(stages['title'])}", item['name'], detail(item['sheets'], fine)))
        if projects.get('revisions'):
            rows.append(('Revisions', 'drawings kept in several versions / phases', detail(projects['revisions']['drawings'], fine)))
    return rows


def drawings_section(spec):
    rows = drawing_rows(spec)
    story = _section('Technical drawings and schematics')
    if rows:
        story.append(_item_table(rows))
    if spec.get('drawings'):
        story.extend([Spacer(1, 10), _table(['Group', 'Item', 'Value'], [list(row) for row in detail_rows(spec)], [118, WIDTH - 118 - 80, 80],
                                            ['left', 'left', 'right'], size=8)])
    return story


def years_section(spec):
    years = spec['years']
    unit = unit_of(spec)
    with_mail = years['has_mail']
    rows = years['rows']
    unreliable = years.get('unreliable')
    title = 'Volume by year: archive (bars) and e-mails (line)' if with_mail else 'Volume by year: archive'
    table_rows = [[str(r['year']), n(r['files']), unit.table(r['bytes']), n(r['messages'])] for r in rows]
    files = sum(r['files'] for r in rows)
    size = sum(r['bytes'] for r in rows)
    messages = sum(r['messages'] for r in rows)
    if unreliable:
        table_rows.append(['date not reliable', n(unreliable['files']), unit.table(unreliable['bytes']), n(unreliable['messages'])])
        files, size, messages = files + unreliable['files'], size + unreliable['bytes'], messages + unreliable['messages']
    table_rows.append(['Total', n(files), unit.table(size), n(messages)])
    header = ['Year', 'Archive files', f'Archive {unit.name}', 'E-mail messages']
    widths = [WIDTH - 3 * 110, 110, 110, 110]
    aligns = ['left', 'right', 'right', 'right']
    if not with_mail:
        for row in table_rows:
            del row[3]
        del header[3], widths[3], aligns[3]
        widths[0] = WIDTH - 2 * 110
    story = _section('By year')
    if rows:
        story.extend([YearChart(rows, title, with_mail, unit), Spacer(1, 8)])
    return story + [_table(header, table_rows, widths, aligns, size=8, total=True)]


def notes(spec):
    parts, method = spec['parts'], spec['methodology']
    unit = unit_of(spec)
    archives = method['archives']
    has_archives = 'archive_contents' in parts
    items = ['All figures are for the original files as held by the company' + (', together with the contents of the compressed archives among them'
                                                                               if has_archives else '') + f'; volumes in decimal {unit.name} (1 {unit.name} = 10^{round(math.log10(unit.divisor))} bytes).']
    if 'alternative_exports' in parts:
        kinds = [{'mbox': 'one .mbox file', 'attachments': 'the extracted attachment files', 'bodies': 'the extracted message bodies',
                  'archive': 'the export archive'}[i['kind']] for i in parts['alternative_exports']['items']]
        items.append('Totals cover every original file held, including alternative exports of the same mailbox (' + ', '.join(kinds) + '). '
                     'They hold the same messages and attachments as the .eml files, delivered in several formats.')
    if has_archives:
        items.append('Compressed archives: the totals count both the archive files (category \'Compressed archives\') and every file inside them, unpacked, '
                     'including archives nested in archives together with their contents. Many files inside the archives are copies of files held loose: '
                     "'new content' leaves out files identical to another file, nested archives whose contents are counted, and repeats within the archives. "
                     "'By category' and 'By format' show the archive contents in separate columns; 'Inside archives' gives the detail.")
        lost, partly = archives['members_not_extracted'], archives['partly_readable']
        text = (f"Inside archives: {plural(lost, 'file')} could not be extracted (password-protected or damaged) and {'is' if lost == 1 else 'are'} not counted; "
                f"{plural(partly, 'archive')} {'was' if partly == 1 else 'were'} only partly readable (password-protected members, damage, a size limit "
                'or missing parts of a multi-part archive). Multi-part archives are counted as one archive.')
        items.append(text)
    refused = archives['not_opened']
    if refused:
        no_tool = sorted({key.split(':')[0] for key in refused if key.endswith('no extraction tool')})
        others = sum(count for key, count in refused.items() if not key.endswith('no extraction tool'))
        sentence = []
        if no_tool:
            count = sum(count for key, count in refused.items() if key.endswith('no extraction tool'))
            sentence.append(f"{plural(count, 'archive')} ({', '.join(no_tool)}) could not be unpacked because no extraction tool was available on this machine; "
                            'their contents are not counted')
        if others:
            sentence.append(f"{plural(others, 'archive')} could not be opened at all (password-protected, damaged, too large or too deeply nested) "
                            'and their contents are not counted')
        items.append('Inside archives: ' + '; '.join(sentence) + '.')
    scanned = ('Scanned vs text PDFs: determined from the text layer of the first page (first three pages for files inside archives).')
    unchecked = method['unchecked_pdfs']
    if unchecked['files']:
        mailbox = unchecked.get('mailbox_attachments', {'files': 0, 'bytes': 0})
        one = unchecked['files'] == 1
        if mailbox['files'] == unchecked['files']:
            scanned += (f" {noun(unchecked['files'], 'PDF')} ({unit.short(unchecked['bytes'])}) among the extracted mailbox attachments "
                        f"{'was' if one else 'were'} not checked for a text layer and {'is' if one else 'are'} counted as text documents.")
        else:
            scanned += (f" {noun(unchecked['files'], 'PDF')} ({unit.short(unchecked['bytes'])}) could not be checked for a text layer and "
                        f"{'is' if one else 'are'} counted as text documents.")
            if mailbox['files']:
                scanned += f" {n(mailbox['files'])} of them {'is' if mailbox['files'] == 1 else 'are'} among the extracted mailbox attachments."
    items.append(scanned)
    if any(c['name'] == T.CREDENTIALS for c in spec['categories']):
        items.append('Credential and key files (digital certificates, key stores, files holding passwords) are identified from their format and file name; '
                     'they are never read as documents or kept, and are reported by count and size. Only a SHA-256 checksum of their bytes is computed, '
                     'so that copies of the same file are counted once.')
    items.append("'Unique' = distinct file contents by SHA-256; copies of the same document in several folders count once.")
    if spec.get('drawings'):
        text = ('Technical drawings and schematics are counted as sheets (pages), across every location: file archive, mailboxes and the contents of the '
                'compressed archives, each distinct file once. The figures are rounded. Sheet sizes come from the page dimensions.')
        if spec['drawings'].get('calibrated'):
            text += (f" Sheet totals are corrected with a visual check of {n(spec['drawings']['sampled_pages'])} sampled pages, and individual views "
                     '(plans, sections, details, elevations) are an estimate from that sample.')
        text += (' Native CAD files are counted separately, as files, and are not added to the sheets (many drawing sheets are prints of the same CAD '
                 "files). Drawing sheets are counted within the document categories of 'By category'.")
        if spec['drawings'].get('projects'):
            text += ' Project workflow figures relate each sheet to the project file it is kept in.'
        items.append(text)
    years = spec.get('years')
    if years:
        text = 'By year: file archive' + (' and e-mails' if years['has_mail'] else '') + ' only' + (
            ' (files inside compressed archives are not dated)' if has_archives else '') + "; the document's own date where known, otherwise the file date."
        if years.get('unreliable'):
            count = years['unreliable']['files'] + years['unreliable']['messages']
            text += (f" {plural(count, 'file or message', 'files or messages')} {'carries' if count == 1 else 'carry'} a date that is clearly not reliable "
                     f"(before 1990 or after {spec['current_year']}) and {'is' if count == 1 else 'are'} shown as 'date not reliable'.")
        items.append(text)
    items.append(f'By format: {unit.name} values are rounded to three decimals; 0.000 means less than {unit.half}. Only the extensions of known file '
                 "formats are listed; every other extension (file-name fragments after a dot) is grouped as '(other / irregular extension)'.")
    if method['unreadable_files'] or method['time_limited_files']:
        count = method['unreadable_files'] + method['time_limited_files']
        items.append(f"{plural(count, 'file')} could not be read completely (access problem, change during the run or a time limit); "
                     f"{'it is' if count == 1 else 'they are'} counted by size and, where available, by content hash only.")
    for box in spec.get('mailboxes', ()):
        incomplete = box.get('incomplete')
        if not incomplete:
            continue
        pieces = []
        if incomplete['messages']:
            count = incomplete['messages']
            pieces.append(f"{plural(count, 'message')} {'was' if count == 1 else 'were'} counted but not analyzed in full (time or size limit), so "
                          f"{'its' if count == 1 else 'their'} attachments, dates and languages are missing from the figures")
        if incomplete['files']:
            count = incomplete['files']
            pieces.append(f"{plural(count, 'mailbox file')} could not be read (time limit or access problem) and {'is' if count == 1 else 'are'} not counted")
        items.append(f"{box['label']}: " + '; '.join(pieces) + '. Run the pass again with a larger time limit to complete the figures.')
    if method['hidden_entries']:
        items.append(f"{plural(method['hidden_entries'], 'hidden file or folder', 'hidden files and folders')} (names that start with a dot) "
                     f"{'was' if method['hidden_entries'] == 1 else 'were'} not entered and not counted.")
    if method['credential_folders']:
        items.append(f"{plural(method['credential_folders'], 'folder')} with a credential-like name {'was' if method['credential_folders'] == 1 else 'were'} "
                     'not entered and its contents are not counted.')
    if method['symlinks']:
        items.append(f"{plural(method['symlinks'], 'symbolic link')} {'was' if method['symlinks'] == 1 else 'were'} not followed and not counted.")
    items.append('No file names, client names or document content are included in this specification.')
    if spec.get('languages'):
        items.append('Languages: detected per file from the document or message text and confirmed by a common-word (or script) check; a file can contain '
                     'several languages. Isolated detections (languages that are the main language of only a few files) are grouped as \'Other languages\'. '
                     'Drawings, photos and archives are not language-classified' + (
                         '; inside the compressed archives only the new content is language-classified (copies are already counted).' if has_archives else '.'))
    return items


NOTES_TAIL_LINES = 8     # a page that opens with the end of the notes carries at least this many lines of them


def notes_section(spec):
    """The notes, with the heading kept with the first two notes and the last notes (at least eight lines) kept together.

    A page break therefore never leaves a few lines of notes alone on a last page: either they stay with the end of the previous section or
    a block of at least eight lines moves to the next page.
    """
    bullets = [card._paragraph('• ' + item, 8, card.MUTED) for item in notes(spec)]
    tail, lines = len(bullets), 0
    while tail > 0 and lines < NOTES_TAIL_LINES:
        tail -= 1
        bullets[tail].wrap(WIDTH, 10 ** 6)
        lines += len(bullets[tail].blPara.lines)
    heading = _heading('Notes')
    if tail <= 2:
        return [Spacer(1, 10), KeepTogether([heading] + bullets)]
    return [Spacer(1, 10), KeepTogether([heading] + bullets[:2])] + bullets[2:tail] + [KeepTogether(bullets[tail:])]


def spec_story(spec):
    """All pages of the specification part, starting with its own page."""
    validate(spec)
    story = [PageBreak()] + summary_section(spec)
    story += [PageBreak()] + category_section(spec)
    story += [PageBreak()] + formats_section(spec)
    if spec.get('mailboxes'):
        story += [PageBreak()] + mailbox_section(spec)
        if spec.get('languages'):
            story += [Spacer(1, 12)] + language_section(spec)
    elif spec.get('languages'):
        story += [PageBreak()] + language_section(spec)
    if spec.get('archives'):
        story += [PageBreak()] + archive_section(spec)
    if spec.get('drawings') or spec.get('cad'):
        story += [PageBreak()] + drawings_section(spec)
    if spec.get('years'):
        story += [PageBreak()] + years_section(spec)
    return story + notes_section(spec)
