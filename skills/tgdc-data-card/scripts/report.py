"""Render evidence-labeled TGDC cards from aggregate JSON and explicit answers.

All strings are data. Rendering never opens source files, loads user images,
interprets markup, contacts a network or certifies provider declarations.
"""
import json
import math
import os
from pathlib import Path
import uuid
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Flowable, Indenter

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
BG = colors.HexColor('#121212')
WHITE = colors.HexColor('#EAEAEA')
MUTED = colors.HexColor('#A9A9A9')
BLUE = colors.HexColor('#4C90F0')
RULE = colors.HexColor('#303030')
WIDTH = A4[0] - 72
CONTACT = 'levin@thegeneraldata.com'
METRICS = {
    'selected_files': 'Selected files', 'selected_bytes': 'Selected size',
    'documents_processed': 'Successfully parsed files',
    'files_with_extracted_text': 'Files with extracted text',
    'duplicate_files': 'Exact duplicate copies',
    'rows_processed': 'Records processed in sample',
    'rows_known_total': 'Known record totals in sample',
    'pdf_pages_processed': 'PDF pages inspected',
    'pdf_pages_known_total': 'Known PDF page totals in sample',
    'pdf_no_text_pages': 'PDF pages without extracted text',
    'extracted_characters': 'Extracted sample characters',
    'extracted_words': 'Extracted sample words',
    'filesystem_mtime_bounds': 'Filesystem modification bounds',
    'eml_content_date_bounds': 'Sampled EML Date header bounds',
    'english_document_share': 'Classified-English share of attempted sample',
}
ANSWER_LABELS = {
    'dataset_name': 'Dataset name', 'business': 'Business', 'archive_description': 'Archive',
    'period': 'Period', 'notes': 'Notes', 'anonymization': 'Anonymization',
    'rights': 'Rights and permissions', 'archive_scope': 'Selected archive scope',
    'provider_context': 'Business context', 'history': 'Content history',
    'systems': 'Source systems', 'case_mapping': 'Case mapping',
    'workflow_outcomes': 'Workflow stages and outcomes', 'refresh': 'Refresh', 'privacy': 'Privacy',
}


class UnsupportedReportText(ValueError):
    """The bundled report fonts cannot display an explicit input character."""


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('Invalid count.')
    return value


def _nonnegative(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0:
            raise ValueError('Invalid evidence number.')
    elif isinstance(value, dict):
        for item in value.values():
            _nonnegative(item)
    elif isinstance(value, list):
        for item in value:
            _nonnegative(item)


def _load(path):
    def invalid_constant(_):
        raise ValueError('Invalid JSON number.')
    value = json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object.')
    return value


def _validate(profile, answers):
    try:
        if profile['schema_version'] != '1.0' or profile['scope']['kind'] != 'selected_files':
            raise ValueError('Unsupported aggregate schema.')
        if not isinstance(profile['scope']['enumeration_complete'], bool):
            raise ValueError('Invalid scope.')
        generated = datetime.fromisoformat(profile['generated_at'].replace('Z', '+00:00'))
        if generated.utcoffset() is None or generated.utcoffset().total_seconds() != 0:
            raise ValueError('Expected UTC audit time.')
        inventory, extraction, languages = profile['inventory'], profile['extraction'], profile['languages']
        for key in ('files', 'bytes'):
            _count(inventory[key])
        for key in ('attempted', 'succeeded', 'errors', 'skipped'):
            _count(extraction[key])
        if extraction['attempted'] > inventory['files'] or extraction['succeeded'] > extraction['attempted']:
            raise ValueError('Inconsistent sample counts.')
        from profiler import PUBLIC_EXTENSIONS
        for ext, counts in inventory['by_extension'].items():
            if ext not in PUBLIC_EXTENSIONS | {'other'}:
                raise ValueError('Invalid public format.')
            _count(counts['files']); _count(counts['bytes'])
        for key in ('files', 'bytes'):
            if sum(item[key] for item in inventory['by_extension'].values()) != inventory[key]:
                raise ValueError('Inconsistent inventory.')
        for value in extraction['status_counts'].values():
            _count(value)
        for metric in profile['metrics'].values():
            if set(metric) != {'value', 'unit', 'basis', 'denominator', 'method', 'coverage'}:
                raise ValueError('Invalid evidence object.')
            if metric['basis'] not in ('measured', 'inferred', 'provider-declared'):
                raise ValueError('Invalid evidence basis.')
            if not isinstance(metric['unit'], str) or not isinstance(metric['method'], str) or not isinstance(metric['coverage'], dict):
                raise ValueError('Invalid evidence labels.')
            if metric['denominator'] is not None:
                _count(metric['denominator'])
            _nonnegative(metric['value']); _nonnegative(metric['coverage'])
        for metric_id, expected in [('selected_files', inventory['files']), ('selected_bytes', inventory['bytes']), ('documents_processed', extraction['succeeded'])]:
            if metric_id in profile['metrics'] and profile['metrics'][metric_id]['value'] != expected:
                raise ValueError('Inconsistent metric.')
        if 'files_with_extracted_text' in profile['metrics']:
            metric = profile['metrics']['files_with_extracted_text']
            if metric['denominator'] != extraction['attempted']:
                raise ValueError('Inconsistent extracted-text count.')
            if metric['value'] is None:
                if extraction['attempted']:
                    raise ValueError('Missing extracted-text count for an examined sample.')
            elif _count(metric['value']) > extraction['succeeded']:
                raise ValueError('Inconsistent extracted-text count.')
        _count(languages['denominator']); _count(languages['unknown_documents'])
        if languages['denominator'] != extraction['attempted']:
            raise ValueError('Inconsistent language denominator.')
        for key, value in languages['document_counts'].items():
            if key != 'unknown' and (not key.isalpha() or not 2 <= len(key) <= 3):
                raise ValueError('Invalid language code.')
            _count(value)
        if sum(languages['document_counts'].values()) != languages['denominator'] or languages['document_counts'].get('unknown', 0) != languages['unknown_documents']:
            raise ValueError('Inconsistent language counts.')
        if sum(_count(v) for v in languages['unknown_reasons'].values()) != languages['unknown_documents']:
            raise ValueError('Inconsistent unknown-language reasons.')
        english = profile['metrics'].get('english_document_share')
        if english:
            expected = languages['document_counts'].get('en', 0) / languages['denominator'] if languages['denominator'] > languages['unknown_documents'] else None
            if english['denominator'] != languages['denominator'] or english['value'] != expected:
                raise ValueError('Inconsistent language share.')
        if not all(isinstance(x, str) for x in profile['limitations']):
            raise ValueError('Invalid limitations.')
        for question in profile['questions']:
            if not all(isinstance(question[k], str) for k in ('id', 'prompt', 'reason', 'priority')):
                raise ValueError('Invalid question.')
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in answers.items()):
            raise ValueError('Answers must be plain strings.')
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError('Invalid aggregate profile or answers.') from error


def _fonts():
    for name, path in [('Outfit', 'Outfit/Outfit-400.ttf'), ('OutfitLight', 'Outfit/Outfit-300.ttf'), ('OutfitMedium', 'Outfit/Outfit-500.ttf'), ('Mono', 'JetBrainsMono/JBMono-400.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(ASSETS / 'fonts' / path)))


def _plain(value, font='Outfit'):
    glyphs = pdfmetrics.getFont(font).face.charToGlyph
    result = []
    for char in str(value):
        if char in '\n\t':
            result.append(char)
        elif not char.isprintable():
            result.append(f'[U+{ord(char):04X}]')
        elif ord(char) not in glyphs or glyphs[ord(char)] == 0:
            raise UnsupportedReportText('Provide an English or Latin-script summary supported by the report fonts.')
        else:
            result.append(char)
    return ''.join(result)


def _paragraph(value, size=10, color=WHITE, font='Outfit', leading=None):
    safe = escape(_plain(value, font)).replace('\n', '<br/>').replace('\t', '    ')
    style = ParagraphStyle('card', fontName=font, fontSize=size, leading=leading or size * 1.4, textColor=color, alignment=TA_LEFT, spaceAfter=6, splitLongWords=True, allowWidows=0, allowOrphans=0)
    return Paragraph(safe, style)


def _heading(title):
    paragraph = _paragraph(title, 20, WHITE, 'OutfitMedium')
    paragraph.style.keepWithNext = True
    return paragraph


def _label(text):
    return _paragraph(text.upper(), 7.5, BLUE, 'Mono', 11)


def _human(key):
    return ANSWER_LABELS.get(key, key.replace('_', ' ').replace('-', ' ').capitalize())


def _short(value, limit=190):
    clean = ' '.join(_plain(value).split())
    suffix = '... (full declaration in appendix)'
    return clean if len(clean) <= limit else clean[:limit - len(suffix)].rsplit(' ', 1)[0] + suffix


def _number(value):
    return f'{value:,}' if isinstance(value, int) else str(value)


def _value(metric):
    value = metric['value']
    if value is None:
        return 'Not available'
    if isinstance(value, dict):
        return ' to '.join(str(value.get(k) or 'Not available') for k in ('earliest', 'latest'))
    if metric['unit'] == 'fraction':
        return f'{value * 100:.1f}%'
    return f"{_number(value)} {metric['unit']}"


def _header_footer(canvas, document):
    canvas.setFillColor(BG); canvas.rect(0, 0, *A4, fill=1, stroke=0)
    canvas.drawImage(str(ASSETS / 'tgdc-logo-horizontal.png'), 36, A4[1] - 90, width=198, height=74.25, mask=[0, 0, 0, 0, 0, 0], preserveAspectRatio=True)
    canvas.setFont('Mono', 7); canvas.setFillColor(MUTED)
    canvas.drawRightString(A4[0] - 36, A4[1] - 51, 'DATA CARD / FOR REVIEW')
    canvas.setStrokeColor(RULE); canvas.line(36, 51, A4[0] - 36, 51)
    canvas.setFillColor(MUTED); canvas.setFont('Mono', 7)
    canvas.drawString(36, 34, f'TGDC / {_number(document.page)}')
    canvas.setFillColor(WHITE); canvas.setFont('Outfit', 9)
    canvas.drawRightString(A4[0] - 36, 33, CONTACT)
    canvas.linkURL('mailto:' + CONTACT, (A4[0] - 175, 28, A4[0] - 36, 42), relative=0)


class LanguageChart(Flowable):
    """Optional compact visualization of classifications in the examined sample."""
    def __init__(self, languages):
        super().__init__()
        known = sorted(((key, count) for key, count in languages['document_counts'].items() if key != 'unknown'), key=lambda item: (-item[1], item[0]))
        self.rows = [(key.upper(), count) for key, count in known[:5]]
        if len(known) > 5:
            self.rows.append(('OTHER', sum(count for _, count in known[5:])))
        self.rows.append(('UNKNOWN', languages['unknown_documents']))
        self.denominator = languages['denominator']
        self.width = WIDTH - 12
        self.height = 35 + 18 * len(self.rows)

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(BLUE); canvas.setFont('Mono', 7.5)
        canvas.drawString(0, self.height - 9, 'Inferred / examined sample')
        canvas.setFont('Outfit', 7.5); canvas.setFillColor(MUTED)
        canvas.drawString(0, self.height - 22, f'Counts / {self.denominator} attempted sampled files')
        for index, (label, count) in enumerate(self.rows):
            y = self.height - 41 - index * 18
            canvas.setFillColor(MUTED); canvas.setFont('Mono', 7)
            canvas.drawString(0, y, label)
            canvas.setFillColor(colors.HexColor('#242424')); canvas.rect(70, y - 2, self.width - 148, 9, stroke=0, fill=1)
            canvas.setFillColor(MUTED if label == 'UNKNOWN' else BLUE)
            canvas.rect(70, y - 2, (self.width - 148) * count / self.denominator, 9, stroke=0, fill=1)
            canvas.setFillColor(WHITE); canvas.setFont('Outfit', 8)
            canvas.drawRightString(self.width, y, f'{count} of {self.denominator}')


def _overview(profile, answers):
    inventory, metrics, languages = profile['inventory'], profile['metrics'], profile['languages']
    story = [_label('Selected scope / evidence overview'), _paragraph(_short(answers.get('dataset_name') or 'Enterprise Data Card', 70), 27, WHITE, 'OutfitMedium', 32)]
    subline = answers.get('business') or answers.get('provider_context') or answers.get('archive_description') or answers.get('archive_scope')
    if subline:
        story.extend([_paragraph(_short(subline, 160), 12, MUTED, 'OutfitLight'), _paragraph('Provider-declared context', 7.5, BLUE, 'Mono')])
    if not profile['scope']['enumeration_complete']:
        story.append(_paragraph('INCOMPLETE INVENTORY / observed selected scope only', 9, colors.HexColor('#EC9A3C'), 'Mono'))
    else:
        story.append(_paragraph('Measured inventory of selected files / no full-archive claim', 8, MUTED))
    text_metric = metrics.get('files_with_extracted_text')
    text_value, text_scope = 'Not available', 'no evidence supplied'
    if text_metric:
        if not text_metric['denominator']:
            text_value, text_scope = 'Not examined', 'no extraction attempted'
        else:
            text_value = f"{_number(text_metric['value'])} of {_number(text_metric['denominator'])}"
            text_scope = 'examined sample / measured'
    kpis = [(_number(inventory['files']), 'Selected files', 'files / measured inventory'), (f"{inventory['bytes'] / 1024:.2f} KiB" if inventory['bytes'] < 1024 ** 2 else f"{inventory['bytes'] / 1024 ** 2:.2f} MiB", 'Selected size', f"{_number(inventory['bytes'])} bytes / measured"), (text_value, 'Files with extracted text', text_scope)]
    cells = [[_paragraph(v, 21, WHITE, 'OutfitMedium', 27), _paragraph(label, 9, MUTED), _paragraph(scope, 7.2, MUTED)] for v, label, scope in kpis]
    table = Table([cells], colWidths=[WIDTH / 3] * 3)
    table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 10), ('TOPPADDING', (0, 0), (-1, -1), 12), ('BOTTOMPADDING', (0, 0), (-1, -1), 12)]))
    story.append(table)
    rows = []
    declarations = [('Business', 'business', 'provider_context'), ('Archive', 'archive_description', 'archive_scope')]
    for label, key, fallback in declarations:
        value = answers.get(key) or answers.get(fallback)
        if value and value.strip(): rows.append((label, _short(value, 135), 'Provider-declared'))
    if inventory['by_extension']:
        rows.append(('Formats', ', '.join(ext.upper() for ext in sorted(inventory['by_extension'])), 'Measured inventory'))
    if languages['denominator']:
        counts = languages['document_counts']
        known = ', '.join(f"{key.upper()} {value}" for key, value in sorted(counts.items()) if key != 'unknown')
        rows.append(('Languages', f"{known + ' / ' if known else ''}{languages['unknown_documents']} unknown / {languages['denominator']} attempted sampled files", 'Inferred'))
    for label, key, fallback in [('Period', 'period', 'history'), ('Notes', 'notes', None), ('Anonymization', 'anonymization', 'privacy')]:
        value = answers.get(key) or answers.get(fallback)
        if value and value.strip(): rows.append((label, _short(value, 135), 'Provider-declared'))
    if rows:
        data = [[_paragraph(label.upper(), 7, MUTED, 'Mono'), [_paragraph(value, 9), _paragraph(basis, 7, BLUE, 'Mono')]] for label, value, basis in rows]
        table = Table(data, colWidths=[95, WIDTH - 95])
        table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LINEABOVE', (0, 0), (-1, -1), .5, RULE), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 9), ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
        story.append(table)
    if not inventory['files']:
        story.append(_paragraph('No files were inventoried. Select an eligible scope before interpreting data availability.', 11, MUTED))
    story.extend([Spacer(1, 12), _paragraph('Review this report before sharing it voluntarily. Provider declarations may contain confidential information. Profiling does not anonymize data or certify rights, quality or value.', 8, MUTED)])
    if languages['denominator']:
        chart = LanguageChart(languages)
        available = A4[1] - 96 - 68 - 12
        used = sum(item.wrap(WIDTH - 12, available)[1] + item.getSpaceBefore() + item.getSpaceAfter() for item in story)
        if used + chart.height + 18 <= available:
            story.extend([Spacer(1, 18), chart])
    return story


def _coverage(profile):
    inventory, extraction, languages = profile['inventory'], profile['extraction'], profile['languages']
    story = [_heading('Formats & coverage'), _paragraph('Measured / selected inventory', 8, BLUE, 'Mono')]
    if inventory['by_extension']:
        data = [[_label('Format'), _label('Files'), _label('Bytes'), _label('File share')]]
        for ext, item in sorted(inventory['by_extension'].items(), key=lambda pair: (-pair[1]['files'], pair[0])):
            data.append([_paragraph(ext.upper(), 9), _paragraph(_number(item['files']), 9), _paragraph(_number(item['bytes']), 9), _paragraph(f"{item['files'] / inventory['files'] * 100:.1f}%", 9)])
        table = Table(data, colWidths=[130, 90, 150, WIDTH - 370], repeatRows=1)
        table.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), .4, RULE), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(table)
        story.append(_paragraph(f"File shares use {_number(inventory['files'])} inventoried files as denominator. Size is filesystem bytes, including unsupported formats.", 8, MUTED))
    else:
        story.append(_paragraph('No format inventory is available.', 10, MUTED))
    story.append(_paragraph(f"Extraction attempted: {extraction['attempted']} / parsed: {extraction['succeeded']} / errors: {extraction['errors']} / skipped encounters: {extraction['skipped']}", 9))
    story.append(_paragraph('Skipped-count basis: ' + extraction.get('skipped_basis', 'encountered entries only'), 8, MUTED))
    statuses = ' / '.join(f"{key.replace('_', ' ')} {value}" for key, value in sorted(extraction['status_counts'].items()))
    if statuses: story.append(_paragraph('Statuses / ' + statuses, 8, MUTED))
    story.extend([Spacer(1, 13), _heading('Text, language & quality'), _paragraph(f"Inferred / {languages['denominator']} attempted sampled files, including unknown, failed and empty inputs", 8, BLUE, 'Mono')])
    for key, count in sorted(languages['document_counts'].items()):
        story.append(_paragraph(f"{key.upper() if key != 'unknown' else 'Unknown'}: {count} of {languages['denominator']}", 10))
    english = profile['metrics'].get('english_document_share')
    story.append(_paragraph('English share: ' + (_value(english) if english else 'Not available'), 10))
    story.append(_paragraph('This is the classified-English fraction of the examined sample. Unknown remains in the denominator. Classification may be incorrect, and this sample does not establish the full archive language mix.', 8, MUTED))
    for key, count in sorted(languages['unknown_reasons'].items()):
        story.append(_paragraph(f"{key.replace('_', ' ').capitalize()}: {count} unknown", 8, MUTED))
    story.append(_paragraph('Method: ' + languages['method'], 8, MUTED))
    story.append(_paragraph('Extracted text is evidence of a nonempty retained sample. It is not a readability or quality score. PDF pages without text are possible OCR candidates, not proven scans. Rows, files and messages do not establish cases or workflow completion.', 8, MUTED))
    return story


def _context(profile, answers):
    story = [_heading('Workflow & context declarations'), _paragraph('Provider-declared / unverified statements supplied for this report', 8, BLUE, 'Mono')]
    provided = [(key, value) for key, value in answers.items() if value.strip()]
    if not provided: story.append(_paragraph('No provider declarations were supplied.', 10, MUTED))
    for key, value in provided:
        story.append(KeepTogether([_label(_human(key)), _paragraph(value, 9.5)])) if len(value) < 300 else story.extend([_label(_human(key)), _paragraph(value, 9.5)])
    story.extend([Spacer(1, 10), _heading('Unanswered intake')])
    pending = [q for q in profile['questions'] if not answers.get(q['id'], '').strip()]
    if not pending: story.append(_paragraph('All listed intake questions have an explicit provider answer. This does not verify those answers.', 9, MUTED))
    for question in pending:
        story.append(KeepTogether([_label(question['priority'] + ' / ' + _human(question['id'])), _paragraph(question['prompt'], 9.5), _paragraph(question['reason'], 8, MUTED)]))
    return story


def _methodology(profile):
    story = [Indenter(right=24), _heading('Methodology & evidence'), _paragraph('Audit generated at: ' + profile['generated_at'], 8, MUTED, 'Mono'), _paragraph('Schema 1.0 / selected_files. Measured records local parser observations, Inferred records language classification, and Provider-declared records explicit answers. Missing evidence is shown as unavailable.', 8, MUTED)]
    for key, label in METRICS.items():
        metric = profile['metrics'].get(key)
        if not metric: continue
        denom = 'Not available' if metric['denominator'] is None else _number(metric['denominator'])
        block = [_paragraph(f"{label}: {_value(metric)} / {metric['basis'].capitalize()} / denominator {denom}", 9, WHITE, 'OutfitMedium'), _paragraph(metric['method'], 7.7, MUTED)]
        coverage = metric['coverage']
        counts = [f"{field.replace('_', ' ')} {coverage[field]}" for field in ('inventory_files', 'sampled_files', 'successful_files', 'hashed_files', 'record_files', 'pdf_files', 'files_with_known_total', 'complete_text_files', 'unknown_documents') if field in coverage]
        if counts: block.append(_paragraph('Coverage: ' + ' / '.join(counts), 7.2, MUTED))
        story.append(KeepTogether(block))
    story.extend([Spacer(1, 8), _heading('Limits & interpretation')])
    limits = profile['scope']['limits']
    story.append(_paragraph('Configured bounds: ' + ' / '.join(f"{key.replace('_', ' ')} {_number(value)}" for key, value in sorted(limits.items())), 7.5, MUTED))
    story.append(_paragraph('Selection: ' + profile['scope']['selection_method'], 7.5, MUTED))
    for text in profile['limitations']:
        story.append(_paragraph(text, 7.7, MUTED))
    story.append(_paragraph('Review provider declarations and the PDF preview before voluntary sharing. This report is not certified as sanitized. The private per-file manifest is not included.', 8, BLUE))
    return story


def render(profile_path, answers_path, output_path):
    """Validate first and atomically replace only a successfully built PDF."""
    from profiler import checked_path, output_directory, verify_output_directory, require_supported_platform
    require_supported_platform()
    profile_path, answers_path, output = map(Path, (profile_path, answers_path, output_path))
    output = checked_path(output)
    if output.resolve() in (profile_path.resolve(), answers_path.resolve()):
        raise ValueError('Output must differ from input files.')
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ValueError('Symlink output is not permitted.')
    profile, answers = _load(profile_path), _load(answers_path)
    _validate(profile, answers)
    _fonts()
    story = _overview(profile, answers) + [PageBreak()] + _coverage(profile) + [PageBreak()] + _context(profile, answers) + [PageBreak()] + _methodology(profile)
    directory = output_directory(output.parent)
    temporary = '.tgdc-card-' + uuid.uuid4().hex + '.pdf'
    created = False
    try:
        verify_output_directory(output.parent, directory)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, 'wb') as stream:
            document = SimpleDocTemplate(stream, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=96, bottomMargin=68, title='TGDC Enterprise Data Card', author='TGDC', subject='Selected scope evidence and provider declarations', creator='TGDC Data Card')
            document.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
        verify_output_directory(output.parent, directory)
        os.replace(temporary, output.name, src_dir_fd=directory, dst_dir_fd=directory)
        verify_output_directory(output.parent, directory)
    finally:
        try:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory)
    return output
