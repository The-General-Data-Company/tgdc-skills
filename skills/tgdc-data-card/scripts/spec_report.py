"""Pages of the data format specification, in the visual design of the Data Card.

The renderer reads only spec.json: counts, sizes, years and partner-declared
vocabulary. It opens no source file and writes no number that the file does not
hold. Sections without data are left out.
"""
import math

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import CondPageBreak, Flowable, PageBreak, Paragraph, Spacer, Table, TableStyle
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
    for category in spec['categories']:
        if category['name'] not in T.CATEGORIES:
            raise ValueError('Unknown content category.')


# ----------------------------------------------------------------------------- text helpers
def n(value):
    return f'{int(value):,}'


def gb1(size):
    return T.size_text(size)


def gb2(size):
    return f'{size / 1e9:,.2f}'


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
                           alignment=TA_RIGHT if align == 'right' else TA_LEFT, splitLongWords=True)
    return Paragraph(safe, style)


def _table(header, rows, widths, aligns, size=8, total=False):
    data = [[_head(text, aligns[i]) for i, text in enumerate(header)]]
    for index, row in enumerate(rows):
        last = total and index == len(rows) - 1
        data.append([_cell(value, size, aligns[i], card.WHITE, 'OutfitMedium' if last else 'Outfit') for i, value in enumerate(row)])
    table = Table(data, colWidths=widths, repeatRows=1, splitByRow=1, hAlign='LEFT')
    style = [('LINEBELOW', (0, 0), (-1, -1), .4, card.RULE), ('LEFTPADDING', (0, 0), (0, -1), 0), ('LEFTPADDING', (1, 0), (-1, -1), 4),
             ('RIGHTPADDING', (0, 0), (-1, -1), 5),
             ('TOPPADDING', (0, 0), (-1, -1), 3.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5), ('VALIGN', (0, 0), (-1, -1), 'TOP')]
    if total:
        style.append(('LINEABOVE', (0, -1), (-1, -1), .8, card.MUTED))
    table.setStyle(TableStyle(style))
    return table


def _item_table(rows):
    return _table(['Item', 'Value'], rows, [165, WIDTH - 165], ['left', 'left'], size=8.4)


def _heading(title):
    """Section heading that never strands at the foot of a page, without pulling a long table onto the next page."""
    return card._paragraph(title, 20, card.WHITE, 'OutfitMedium')


def _section(title, intro=None):
    story = [CondPageBreak(120), _heading(title)]
    if intro:
        story.append(card._paragraph(intro, 8.2, card.MUTED))
    return story


class CategoryChart(Flowable):
    """Horizontal bars: volume by content category in GB, largest first."""

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

    def __init__(self, rows, title, with_mail):
        super().__init__()
        self.rows, self.title, self.with_mail = rows, title, with_mail
        self.width = WIDTH
        self.height = 205

    def draw(self):
        canvas = self.canv
        left, right, bottom, top = 34, self.width - (38 if self.with_mail else 8), 40, self.height - 30
        canvas.setFillColor(card.BLUE)
        canvas.setFont('Mono', 7.5)
        canvas.drawString(0, self.height - 9, self.title.upper())
        years = self.rows
        unit, divisor = ('MB', 1e6) if max(row['bytes'] for row in years) < 1e8 else ('GB', 1e9)
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
    return f"{gb1(totals['bytes'])} ({T.binary_text(totals['bytes'])}), {noun(totals['files'], 'file')}"


def mailbox_summary(spec):
    mailboxes = spec['parts']['mailboxes']
    messages, files = mailboxes['messages'], mailboxes['files']
    kind = '.mbox' if mailboxes['formats'] == ['mbox'] else '.eml'
    text = f"{gb1(mailboxes['bytes'])}, "
    parts = [f"{noun(messages, 'e-mail message')} ({kind})"]
    if mailboxes.get('metadata_files'):
        extensions = ', '.join('.' + extension for extension in mailboxes['metadata_extensions'])
        parts.append(f"{noun(mailboxes['metadata_files'], 'per-message metadata file')} ({extensions})")
    if mailboxes.get('other_files'):
        parts.append(f"{plural(mailboxes['other_files'], 'other file')}")
    text += (f'{noun(files, "file")}: ' if len(parts) > 1 else '') + ' + '.join(parts)
    for item in mailboxes['sent_missing']:
        text += (f"; {item['label']}: {plural(item['received'], 'received message')} held; volume and attachment figures "
                 'increase once the sent messages are added')
    return text


def alternative_summary(spec):
    exports = spec['parts']['alternative_exports']
    pieces = []
    for item in exports['items']:
        size = T.size_text(item['bytes'])
        if item['kind'] == 'mbox':
            pieces.append(f"{item['files']:,} .mbox file{'s' if item['files'] != 1 else ''} ({size})")
        elif item['kind'] == 'attachments':
            pieces.append(f"{noun(item['files'], 'extracted attachment file')} ({size})")
        elif item['kind'] == 'bodies':
            pieces.append(f"{noun(item['files'], 'extracted message body', 'extracted message bodies')} ({size})")
        else:
            pieces.append(f"{item['files']:,} export archive{'s' if item['files'] != 1 else ''} ({size})")
    return f"{T.size_text(exports['bytes'])}, {noun(exports['files'], 'file')}: " + ' + '.join(pieces)


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
        rows.append(('Where the sheets are', '; '.join(f'{names[key]}: {about_noun(count, step, "sheet")}' for key, count in drawings['locations'].items())))
    projects = drawings.get('projects')
    if projects:
        share = round(100 * projects['sheets'] / projects['total_sheets']) if projects['total_sheets'] else 0
        text = (f"{share}% of the sheets sit in {n(projects['projects_with_sheets'])} of the {noun(projects['projects'], 'project file')}, "
                'next to the other documents of the project')
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
        rows.append(('Files of text formats where no language could be confirmed (mainly scans without a text layer; also very short or garbled text)',
                     n(languages['none']['files'])))
    return rows


def archive_rows(spec):
    parts = spec['parts']
    rows = []
    if 'archive_contents' in parts:
        contents = parts['archive_contents']
        rows.append(('Contents of the compressed archives (unpacked; the archive files themselves are in the file archive)',
                     f"{gb1(contents['bytes'])}, {noun(contents['files'], 'file')} from {noun(contents['archives'], 'compressed archive')}"))
        held = parts['held_without_archives']
        rows.append(('Files as held, without the contents of the compressed archives',
                     f"{gb1(held['bytes'])} ({T.binary_text(held['bytes'])}), {noun(held['files'], 'file')}"))
        rows.append(('Contents of the compressed archives that are new content (not a copy of another file)',
                     f"{gb1(contents['new_bytes'])}, {noun(contents['new_files'], 'file')}"))
    return rows


def summary_rows(spec):
    parts, totals = spec['parts'], spec['totals']
    has_mail, has_archives = 'mailboxes' in parts, 'archive_contents' in parts
    content = 'Original company files' + (' and business mailboxes' if has_mail else '') + ', as held by the company'
    if has_archives:
        content += ', plus the contents of the compressed archives among them'
    rows = [('Content', content), ('Total volume (headline)', headline(spec)),
            ('Company file archive', f"{gb1(parts['file_archive']['bytes'])}, {noun(parts['file_archive']['files'], 'file')}")]
    if has_mail:
        rows.append(('Business mailboxes', mailbox_summary(spec)))
    if 'alternative_exports' in parts:
        rows.append(('Alternative exports of the same mailbox (same messages, not additional content)', alternative_summary(spec)))
    if 'export_logs' in parts:
        rows.append(('Mailbox export log and script files', f"{T.size_text(parts['export_logs']['bytes'])}, {noun(parts['export_logs']['files'], 'file')}"))
    rows.extend(archive_rows(spec))
    if totals['hashed_files']:
        rows.append(('Unique file contents (by SHA-256)', f"{n(totals['unique_contents'])} of {noun(totals['hashed_files'], 'file')}"))
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
        story.append(card._paragraph(' / '.join(pieces), 11, card.MUTED, 'OutfitLight'))
    return story + [Spacer(1, 6)]


def summary_section(spec):
    return title_block(spec) + [_heading('Summary'), _item_table(summary_rows(spec))]


def category_section(spec):
    total = spec['totals']['bytes'] or 1
    split_inside = spec['totals']['files'] != spec['totals']['held_files']
    header = ['Content category', 'Files', 'Unique files', 'GB', '% of volume', 'Of which inside archives: files', 'Of which inside archives: GB', 'Main formats']
    rows = []
    for category in spec['categories']:
        rows.append([category['name'], n(category['files']), n(category['unique']) if 'unique' in category else '', gb2(category['bytes']),
                     f"{100 * category['bytes'] / total:.1f}", n(category['archive_files']), gb2(category['archive_bytes']), ', '.join(category['formats'])])
    rows.append(['Total', n(spec['totals']['files']), '', gb2(spec['totals']['bytes']), '100.0', n(sum(c['archive_files'] for c in spec['categories'])),
                 gb2(sum(c['archive_bytes'] for c in spec['categories'])), ''])
    widths = [138, 40, 40, 40, 34, 42, 42, WIDTH - 376]
    aligns = ['left', 'right', 'right', 'right', 'right', 'right', 'right', 'left']
    if not split_inside:
        for index in (6, 5):
            del header[index], widths[index], aligns[index]
            for row in rows:
                del row[index]
        widths[-1] = WIDTH - sum(widths[:-1])
    intro = ('All files held plus the contents of the compressed archives (unpacked); the archive contents are also shown in their own columns.'
             if split_inside else 'All files held.')
    chart_rows = sorted(((T.short_category(c['name']), c['bytes'] / 1e9) for c in spec['categories']), key=lambda item: -item[1])
    return _section('Content by category', intro) + [_table(header, rows, widths, aligns, size=7.6, total=True), Spacer(1, 12),
                                                       CategoryChart(chart_rows, 'Volume by content category (GB)')]


def formats_section(spec):
    rows = [[row['ext'], row['category'], n(row['files']), f"{row['bytes'] / 1e9:,.3f}", n(row['archive_files'])] for row in spec['formats']]
    header = ['Format (extension)', 'Category', 'Files', 'GB', 'Of which inside archives: files']
    widths = [78, WIDTH - 78 - 52 - 52 - 74, 52, 52, 74]
    aligns = ['left', 'left', 'right', 'right', 'right']
    if not any(row['archive_files'] for row in spec['formats']) and 'archive_contents' not in spec['parts']:
        for row in rows:
            del row[4]
        del header[4], widths[4], aligns[4]
        widths[1] = WIDTH - widths[0] - widths[2] - widths[3]
    return _section('File formats (top 35 by volume)') + [_table(header, rows, widths, aligns, size=7.8)]


EXTENSION_TEXT = {'mbox': '.mbox (single mailbox file)', 'eml': '.eml (RFC 822, attachments embedded)', 'eml+mbox': '.eml (RFC 822, attachments embedded) and .mbox'}


def mailbox_other_text(box):
    pieces = []
    if box.get('metadata'):
        extensions = ', '.join('.' + e for e in box['metadata']['extensions'])
        pieces.append(f"{noun(box['metadata']['files'], 'per-message metadata file')} ({extensions})")
    if box.get('sent_missing'):
        pieces.append(f"{noun(box['received'], 'received message')} held; volume and attachment figures increase once the sent messages are added")
    if box.get('other_files'):
        pieces.append(plural(box['other_files'], 'other file'))
    return '; '.join(pieces)


def mailbox_alt_rows(box):
    rows = []
    for item in box.get('alternatives', ()):
        files = item['files']
        count = f"{n(files)} file{'s' if files != 1 else ''}"
        if item['kind'] == 'mbox':
            rows.append([f"{box['label']} - .mbox export", '(same messages)', gb2(item['bytes']), '', EXTENSION_TEXT['mbox'], f'{count}; alternative export of the same mailbox'])
        elif item['kind'] == 'attachments':
            rows.append([f"{box['label']} - extracted attachments", '(same messages)', gb2(item['bytes']), '', 'original attachment files',
                         f'{count}; attachments of the same messages, also embedded in the .eml'])
        elif item['kind'] == 'bodies':
            rows.append([f"{box['label']} - extracted message bodies", '(same messages)', gb2(item['bytes']), '', '.txt / .html message bodies',
                         f'{count}; text and HTML bodies of the same messages, also inside the .eml'])
        else:
            extension = ', '.join('.' + e for e in item.get('extensions', ())) or 'archive'
            rows.append([f"{box['label']} - export archive ({extension})", '(same messages)', gb2(item['bytes']), '', f'{extension} archive of the whole export',
                         f'{count}; the mailbox export as delivered, same content as the unpacked folder'])
    return rows


def mailbox_section(spec):
    rows = []
    for box in spec['mailboxes']:
        rows.append([box['label'], n(box['messages']), gb2(box['bytes']), T.year_range(*box['period']) if box.get('period') else '',
                     EXTENSION_TEXT[box['format']], mailbox_other_text(box)])
        rows.extend(mailbox_alt_rows(box))
    logs = spec['parts'].get('export_logs')
    if logs:
        rows.append(['Mailbox export log and script files', '', gb2(logs['bytes']), '', 'scripts / logs', noun(logs['files'], 'file')])
    header = ['Mailbox', 'Messages', 'GB', 'Period', 'Format', 'Other files']
    widths = [128, 52, 36, 46, 100, WIDTH - 362]
    aligns = ['left', 'right', 'right', 'left', 'left', 'left']
    return _section('Mailboxes') + [_table(header, rows, widths, aligns, size=7.6)]


def language_section(spec):
    languages = spec['languages']
    with_mail = languages['total']['messages'] > 0
    rows = [[row['name'], n(row['files']), gb2(row['bytes']), n(row['messages'])] for row in languages['rows']]
    if languages.get('other'):
        rows.append(['Other languages (isolated detections)', n(languages['other']['files']), gb2(languages['other']['bytes']), n(languages['other']['messages'])])
    if languages.get('none'):
        rows.append(['No language confirmed (mainly scans without a text layer; also very short or garbled text)', n(languages['none']['files']),
                     gb2(languages['none']['bytes']), n(languages['none']['messages'])])
    total = languages['total']
    rows.append(['Total text-format files examined', n(total['files']), gb2(total['bytes']), n(total['messages'])])
    header = ['Language', 'Files (main language)', 'GB', 'E-mail messages']
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
    rows = [[c['name'], n(c['files']), gb2(c['bytes']), n(c['new_files']), gb2(c['new_bytes']), ', '.join(c['formats'])] for c in archives['categories']]
    total = archives['total']
    rows.append(['Total', n(total['files']), gb2(total['bytes']), n(total['new_files']), gb2(total['new_bytes']), ''])
    header = ['Content category', 'Files (all unpacked)', 'GB (all unpacked)', 'Files (new content)', 'GB (new content)', 'Main formats']
    widths = [150, 56, 56, 56, 56, WIDTH - 374]
    aligns = ['left', 'right', 'right', 'right', 'right', 'left']
    intro = ("Detail of the archive contents included in the totals. 'All unpacked' counts every file inside the archives; 'new content' leaves out "
             'files identical to another file, nested archives whose contents are counted, and repeats within the archives.')
    extra = [row for row in summary_rows(spec) if row[0] in dict(archive_rows(spec)) or row[0].startswith('Native CAD')]
    story = _section('Inside the compressed archives', intro) + [_table(header, rows, widths, aligns, size=7.8, total=True)]
    return story + ([Spacer(1, 10), _item_table(extra)] if extra else [])


def detail_rows(spec):
    drawings = spec['drawings']
    step, fine = steps(drawings['sheets'])
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
                                            ['left', 'left', 'right'], size=7.8)])
    return story


def years_section(spec):
    years = spec['years']
    with_mail = years['has_mail']
    rows = years['rows']
    unreliable = years.get('unreliable')
    title = 'Volume by year: archive (bars) and e-mails (line)' if with_mail else 'Volume by year: archive'
    table_rows = [[str(r['year']), n(r['files']), gb2(r['bytes']), n(r['messages'])] for r in rows]
    files = sum(r['files'] for r in rows)
    size = sum(r['bytes'] for r in rows)
    messages = sum(r['messages'] for r in rows)
    if unreliable:
        table_rows.append(['date not reliable', n(unreliable['files']), gb2(unreliable['bytes']), n(unreliable['messages'])])
        files, size, messages = files + unreliable['files'], size + unreliable['bytes'], messages + unreliable['messages']
    table_rows.append(['Total', n(files), gb2(size), n(messages)])
    header = ['Year', 'Archive files', 'Archive GB', 'E-mail messages']
    widths = [WIDTH - 3 * 110, 110, 110, 110]
    aligns = ['left', 'right', 'right', 'right']
    if not with_mail:
        for row in table_rows:
            del row[3]
        del header[3], widths[3], aligns[3]
        widths[0] = WIDTH - 2 * 110
    story = _section('By year')
    if rows:
        story.extend([YearChart(rows, title, with_mail), Spacer(1, 8)])
    return story + [_table(header, table_rows, widths, aligns, size=7.8, total=True)]


def notes(spec):
    parts, totals, method = spec['parts'], spec['totals'], spec['methodology']
    archives = method['archives']
    has_archives = 'archive_contents' in parts
    items = ['All figures are for the original files as held by the company' + (', together with the contents of the compressed archives among them'
                                                                               if has_archives else '') + '; volumes in decimal GB (1 GB = 10^9 bytes).']
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
        text = (f"Inside archives: {n(archives['members_not_extracted'])} files could not be extracted (password-protected or damaged) and are not counted; "
                f"{n(archives['partly_readable'])} archives were only partly readable (password-protected members, damage, a size limit or missing parts "
                'of a multi-part archive). Multi-part archives are counted as one archive.')
        items.append(text)
    refused = archives['not_opened']
    if refused:
        no_tool = sorted({key.split(':')[0] for key in refused if key.endswith('no extraction tool')})
        others = sum(count for key, count in refused.items() if not key.endswith('no extraction tool'))
        sentence = []
        if no_tool:
            count = sum(count for key, count in refused.items() if key.endswith('no extraction tool'))
            sentence.append(f"{n(count)} archives ({', '.join(no_tool)}) could not be unpacked because no extraction tool was available on this machine; "
                            'their contents are not counted')
        if others:
            sentence.append(f'{n(others)} archives could not be opened at all (password-protected or damaged) and their contents are not counted')
        items.append('Inside archives: ' + '; '.join(sentence) + '.')
    scanned = ('Scanned vs text PDFs: determined from the text layer of the first page (first three pages for files inside archives).')
    unchecked = method['unchecked_pdfs']
    if unchecked['files']:
        scanned += (f" {n(unchecked['files'])} PDFs ({gb1(unchecked['bytes'])}) could not be checked for a text layer and are counted as text documents.")
    items.append(scanned)
    if any(c['name'] == T.CREDENTIALS for c in spec['categories']):
        items.append('Credential and key files (digital certificates, key stores, files holding passwords) are identified from their format and file name; '
                     'they were not opened and are reported by count and size only, and they are not hashed.')
    items.append("'Unique' = distinct file contents by SHA-256; the archive contains working copies of some documents across project folders.")
    if spec.get('drawings'):
        text = ('Technical drawings and schematics are counted as sheets (pages), across every location: file archive, mailboxes and the contents of the '
                'compressed archives, each distinct file once. The figures are rounded and conservative. Sheet sizes come from the page dimensions.')
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
            ' (files inside compressed archives are not dated)' if has_archives else '') + '; the own date of the document where known, otherwise the file date.'
        if years.get('unreliable'):
            count = years['unreliable']['files'] + years['unreliable']['messages']
            text += (f" {n(count)} files or messages carry a date that is clearly not reliable (before 1990 or after {spec['current_year']}) "
                     "and are shown as 'date not reliable'.")
        items.append(text)
    items.append('By format: GB values are rounded to three decimals; 0.000 means less than 0.5 MB. Irregular extensions (file-name fragments after a dot) '
                 "are grouped as '(other / irregular extension)'.")
    if method['unreadable_files'] or method['time_limited_files']:
        items.append(f"{n(method['unreadable_files'] + method['time_limited_files'])} files could not be read completely (access problem, change during "
                     'the run or a time limit); they are counted by size and, where possible, by name only.')
    skipped = method['hidden_entries'] + method['credential_folders']
    if skipped:
        items.append(f"{n(skipped)} hidden or credential-named folders and files were not entered or counted, and links were not followed.")
    items.append('No file names, client names or document content are included in this specification.')
    if spec.get('languages'):
        items.append('Languages: detected per file from the document or message text and confirmed by a common-word (or script) check; a file can contain '
                     'several languages. Isolated detections (languages that are the main language of only a few files) are grouped as \'Other languages\'. '
                     'Drawings, photos and archives are not language-classified' + (
                         '; inside the compressed archives only the new content is language-classified (copies are already counted).' if has_archives else '.'))
    return items


def notes_section(spec):
    story = [CondPageBreak(120), Spacer(1, 10), _heading('Notes')]
    for item in notes(spec):
        story.append(card._paragraph('• ' + item, 8, card.MUTED))
    return story


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
