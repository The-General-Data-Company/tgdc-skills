"""Turns the private per-file cache into the aggregate specification (spec.json).

Everything that leaves this module is a count, a size, a year or vocabulary the
partner declared. File names, folder names, hashes and text stay in the cache.
"""
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import spec_language
import spec_taxonomy as T
from spec_taxonomy import (A4_LIKE, ARCHIVE_EXTENSIONS, CREDENTIALS, EXAMINED_CATEGORIES, SCRIPT_LOG_EXTENSIONS, TEXT_EXTRACTABLE,
                           extension_label, fold, matches_any, raw_extension)

SCHEMA = 'tgdc-format-spec/1.0'
TOP_FORMATS = 35
MAIN_FORMATS = 6
OVERLAP = 0.95
MIN_YEAR = 1990
DEDICATED_SHARE = 0.8
MIN_LANGUAGE_FILES = 5
DRAWING_DEFAULTS = ['drawing', 'drawings', 'blueprint', 'blueprints', 'floor plan', 'site plan']
SCHEMATIC_DEFAULTS = ['schematic', 'schematics', 'wiring diagram', 'circuit diagram', 'single line diagram']
SIZE_LABEL = {'A4': 'A4', 'A3': 'A3', 'A2': 'A2', 'A1': 'A1', 'A0': 'A0', 'letter': T.US_LABEL, 'legal': T.US_LABEL,
              'tabloid': T.US_LABEL, 'oversize': T.OTHER_SIZE, 'other': T.OTHER_SIZE, 'image': T.IMAGE_SIZE}
AUTO_DRAWING = frozenset(['A3', 'A2', 'A1', 'A0', 'tabloid', 'oversize'])
REVISION_WORDS = frozenset(['rev', 'revision', 'ver', 'version', 'ed', 'edition', 'phase', 'ph', 'v', 'r'])
REVISION_TOKEN = re.compile(r'^(rev|revision|ver|version|ed|edition|phase|ph|v|r)\d{1,3}$')
COPY_WORDS = frozenset(['copy', 'copie', 'kopie', 'copia', 'final', 'draft', 'old'])
BUSINESS = frozenset(['eml', 'mbox_primary', 'meta', 'other'])
ALT = frozenset(['alt_mbox', 'alt_attachments', 'alt_bodies', 'alt_archive'])
ALT_ORDER = [('alt_mbox', 'mbox'), ('alt_attachments', 'attachments'), ('alt_bodies', 'bodies'), ('alt_archive', 'archive')]
NO_LANGUAGE = {'lang': None, 'chunks': {}}


class Loose:
    __slots__ = ('entry', 'name', 'ext', 'label', 'size', 'sha', 'facts', 'status', 'cred', 'mailbox', 'mclass', 'category',
                 'part', 'arch', 'arch_hashes', 'year', 'mtime_year', 'mail', 'mbox')


def revision_stem(name, extra=()):
    """File name without revision markers, dates and copy markers; '' when nothing meaningful is left."""
    stem = fold(name.rsplit('/', 1)[-1])
    stem = re.sub(r'\.[a-z0-9]{1,8}$', '', stem)
    stem = re.sub(r'\(\d+\)', ' ', stem)
    stem = re.sub(r'\b\d{4}[-_.]\d{1,2}[-_.]\d{1,2}\b|\b\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4}\b|\b\d{8}\b', ' ', stem)
    drop = {fold(token) for token in extra} | COPY_WORDS
    kept, skip_number = [], False
    for word in (w for w in re.split(r'[^a-z0-9]+', stem) if w):
        if word in drop or REVISION_TOKEN.match(word):
            skip_number = False
        elif word in REVISION_WORDS:
            skip_number = True
        elif skip_number and word.isdigit():
            skip_number = False
        else:
            skip_number = False
            kept.append(word)
    result = ' '.join(kept)
    return result if len(result) >= 3 else ''


def percentile(values, fraction):
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def trimmed_range(years, tail=0.005):
    """Year range left after cutting 0.5% of the dated documents from each end."""
    ordered = sorted(years)
    if not ordered:
        return None
    cut = int(len(ordered) * tail)
    kept = ordered[cut:len(ordered) - cut] or ordered
    return [kept[0], kept[-1]]


def top_formats(counter):
    return [label for label, _ in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:MAIN_FORMATS]]


class Sheets:
    """Drawing sheets per distinct file content (SHA-256); each content is counted once."""

    def __init__(self, drawing_patterns, schematic_patterns):
        self.drawing_patterns, self.schematic_patterns = drawing_patterns, schematic_patterns
        self.facts = {}
        self.drawing_name, self.schematic_name = set(), set()
        self.locations = defaultdict(set)
        self.copies = defaultdict(list)

    def add(self, sha, facts, text, location, project=None):
        if not sha or not facts or facts.get('unchecked') or not facts.get('classes'):
            return
        self.facts.setdefault(sha, facts)
        if matches_any(self.drawing_patterns, text):
            self.drawing_name.add(sha)
        if matches_any(self.schematic_patterns, text):
            self.schematic_name.add(sha)
        self.locations[sha].add(location)
        if project is not None:
            self.copies[sha].append(project)

    def evaluate(self, sha):
        """(pages, drawing pages, drawing pages per size label, schematic flag, non-candidate pages)."""
        named = sha in self.drawing_name or sha in self.schematic_name
        pages = drawing = rest = 0
        by_label = Counter()
        for key, (count, vector) in self.facts[sha]['classes'].items():
            pages += count
            if key in AUTO_DRAWING:
                found = count
            elif key in A4_LIKE or key == 'other':
                found = count if named else vector
            else:
                found = count if named else 0
            if found:
                drawing += found
                by_label[SIZE_LABEL.get(key, T.OTHER_SIZE)] += found
            if key != 'image':
                rest += count - found
        return pages, drawing, by_label, sha in self.schematic_name, rest


def project_info(entry, cfg, stage_patterns):
    """Project key, matched stage indexes and bill-of-quantities flag of a file below a project root."""
    depth = cfg['project_depth']
    for root in cfg['project_roots']:
        if root['root'] != entry.root:
            continue
        prefix = root['prefix']
        if prefix:
            if not entry.rel.startswith(prefix + '/'):
                continue
            remainder = entry.rel[len(prefix) + 1:]
        else:
            remainder = entry.rel
        parts = remainder.split('/')
        if len(parts) < depth + 1:
            continue
        inner = '/'.join(parts[depth:])
        return {'key': f'{entry.root}:{prefix}/' + '/'.join(parts[:depth]),
                'stages': [index for index, patterns in enumerate(stage_patterns) if matches_any(patterns, inner)],
                'boq': bool(cfg['bill_of_quantities_patterns']) and matches_any(cfg['bill_of_quantities_patterns'], inner)}
    return None


class Build:
    def __init__(self, entries, cache, cfg, context):
        from formatspec import mailbox_of
        self.mailbox_of = mailbox_of
        self.entries, self.cache, self.cfg, self.context = entries, cache, cfg, context
        self.year_now = context['current_year']
        self.by_id = {entry.id: entry for entry in entries}
        self.sheets = Sheets(cfg['drawing_patterns'] or DRAWING_DEFAULTS, cfg['schematic_patterns'] or SCHEMATIC_DEFAULTS)
        stages = cfg.get('stages')
        self.stage_patterns = [item['patterns'] for item in stages['items']] if stages else []
        self.records = []
        self.archive_ids = []
        self.unreadable = Counter()
        self.mail = {}
        self.messages = []             # message facts of every non-duplicate e-mail, wherever it is held
        self.language = []
        self.projects = {}

    # ------------------------------------------------------------------ pass 1
    def load(self):
        for identifier, result in self.cache.results(wanted=self.by_id):
            entry = self.by_id[identifier]
            record = Loose()
            record.entry, record.name, record.size = entry, entry.name, entry.size
            record.ext = raw_extension(entry.name)
            record.status = result.get('status', 'ok')
            record.cred = entry.credential
            record.sha = result.get('sha')
            record.facts = result
            record.mail, record.mbox = result.get('mail'), result.get('mbox')
            record.mailbox = self.mailbox_of(entry, self.cfg)
            record.arch, record.arch_hashes = result.get('arch'), None
            if record.arch is not None:
                members = record.arch.get('members') or []
                if members:
                    self.archive_ids.append(identifier)
                if record.mailbox is not None:
                    record.arch_hashes = [m['h'] for m in members if m.get('h')]
                record.arch = {key: value for key, value in record.arch.items() if key != 'members'}
            record.year = result.get('year') or (record.mail or {}).get('year')
            record.mtime_year = datetime.fromtimestamp(entry.mtime_ns / 1e9, timezone.utc).year
            record.mclass, record.part, record.category = None, False, None
            if record.status in ('io_error', 'changed', 'error'):
                self.unreadable['unreadable'] += 1
            elif record.status in ('timeout', 'crashed'):
                self.unreadable['limited'] += 1
            self.records.append(record)
        leaders = {record.entry.id: record.arch for record in self.records}
        for record in self.records:
            entry = record.entry
            if entry.role == 'follower':
                leader = leaders.get(entry.leader)
                record.part = entry.style != 'num' or bool(leader and leader.get('kind'))
            elif entry.role == 'leader' and entry.style == 'num':
                record.part = bool(record.arch and record.arch.get('kind'))
            record.label = extension_label(entry.name, multipart_part=record.part)

    # ------------------------------------------------------------------ mailboxes
    def classify_mailboxes(self):
        for index in range(len(self.cfg['mailboxes'])):
            members = [r for r in self.records if r.mailbox == index]
            emls = [r for r in members if r.ext == 'eml']
            messages = [r.mail for r in emls if r.mail]
            eml_ids = {m['mid'] for m in messages if m.get('mid')}
            for record in emls:
                record.mclass = 'eml'
            alt_mbox, primary_mbox = [], []
            for record in members:
                if record.ext != 'mbox' or not record.mbox:
                    continue
                ids = {m['mid'] for m in record.mbox['messages'] if m.get('mid')}
                if eml_ids and ids and len(ids & eml_ids) / len(ids) >= OVERLAP:
                    record.mclass = 'alt_mbox'
                    alt_mbox.append(record)
                else:
                    record.mclass = 'mbox_primary'
                    primary_mbox.append(record)
                    messages.extend(m for m in record.mbox['messages'] if not m.get('known') and not m.get('timeout'))
            if not emls and not alt_mbox and not primary_mbox:
                # a declared mailbox root that holds no mail is an ordinary folder
                for record in members:
                    record.mailbox, record.mclass = None, None
                continue
            attachments = {a['sha'] for m in messages for a in m.get('atts', ())}
            bodies = {h for m in messages for h in m.get('bodies', ())}
            loose = {r.sha for r in members if r.sha and r.ext not in ARCHIVE_EXTENSIONS}
            for record in members:
                if record.mclass is not None:
                    continue
                if record.cred or (record.arch is not None and record.arch.get('opened')):
                    record.mclass = 'other'
                elif record.ext in ('json', 'csv'):
                    record.mclass = 'meta'
                elif record.ext in SCRIPT_LOG_EXTENSIONS:
                    record.mclass = 'log'
                elif record.sha and record.sha in attachments:
                    record.mclass = 'alt_attachments'
                elif record.ext in ('txt', 'html', 'htm') and record.sha in bodies:
                    record.mclass = 'alt_bodies'
                else:
                    record.mclass = 'other'
            referenced = loose | attachments | bodies
            for record in members:
                if record.arch is not None and record.arch.get('opened') and record.arch_hashes:
                    matched = sum(1 for sha in record.arch_hashes if sha in referenced)
                    if matched / len(record.arch_hashes) >= OVERLAP:
                        record.mclass = 'alt_archive'
            self.mail[index] = {'members': members, 'messages': messages, 'eml_count': len(emls), 'primary_messages': sum(
                1 for r in primary_mbox for m in r.mbox['messages'] if not m.get('known') and not m.get('timeout'))}
        # e-mail held outside configured mailboxes still contributes attachments and language
        for record in self.records:
            if record.mailbox is None:
                if record.mail:
                    self.messages.append(record.mail)
                if record.mbox:
                    self.messages.extend(m for m in record.mbox['messages'] if not m.get('known') and not m.get('timeout'))
        for result in self.mail.values():
            self.messages.extend(result['messages'])

    # ------------------------------------------------------------------ everything else
    def run(self):
        self.load()
        self.classify_mailboxes()
        cfg, sheets = self.cfg, self.sheets
        categories = {name: {'files': 0, 'bytes': 0, 'unique': set(), 'arch_files': 0, 'arch_bytes': 0, 'ext': Counter()} for name in T.CATEGORIES}
        formats = defaultdict(lambda: [0, 0, 0])
        all_hashes, loose_hashes = set(), set()
        hashed = total_files = total_bytes = 0
        parts = {'file_archive': [0, 0], 'mailboxes': [0, 0], 'alt': [0, 0], 'log': [0, 0]}
        unchecked = [0, 0]
        years, year_bytes = Counter(), Counter()
        unreliable = [0, 0]
        document_years = []
        cad = {'file_archive': 0, 'mail': set(), 'archives': 0, 'archives_new': 0}
        language = []
        projects = {}

        def add_category(category, label, size, sha, inside=False):
            stat = categories[category]
            stat['files'] += 1
            stat['bytes'] += size
            if sha:
                stat['unique'].add(sha)
            if label != T.IRREGULAR_EXTENSION:
                stat['ext'][label] += 1
            row = formats[(label, category)]
            row[0] += 1
            row[1] += size
            if inside:
                stat['arch_files'] += 1
                stat['arch_bytes'] += size
                row[2] += 1

        for record in self.records:
            pdf = record.facts.get('pdf') or {}
            if record.cred:
                record.category = CREDENTIALS
            else:
                record.category = T.category_of(record.name, pdf_scanned=bool(pdf.get('scanned')) and not pdf.get('unchecked'),
                                                multipart_part=record.part, mail_metadata=record.mclass == 'meta')
                if record.category == T.OTHER and record.arch is not None and record.arch.get('kind'):
                    record.category = T.ARCHIVES
            size = record.size
            total_files += 1
            total_bytes += size
            add_category(record.category, record.label, size, record.sha)
            if record.sha:
                hashed += 1
                all_hashes.add(record.sha)
                loose_hashes.add(record.sha)
            if (record.ext == 'pdf' and not record.cred and (pdf.get('unchecked') or record.status in ('timeout', 'crashed', 'error', 'io_error', 'changed'))):
                unchecked[0] += 1
                unchecked[1] += size
            if record.mailbox is None:
                bucket = 'file_archive'
            elif record.mclass in BUSINESS:
                bucket = 'mailboxes'
            elif record.mclass == 'log':
                bucket = 'log'
            else:
                bucket = 'alt'
            parts[bucket][0] += 1
            parts[bucket][1] += size
            if record.mailbox is None:
                year = record.year or record.mtime_year
                if year < MIN_YEAR or year > self.year_now:
                    unreliable[0] += 1
                    unreliable[1] += size
                else:
                    years[year] += 1
                    year_bytes[year] += size
                    if record.category in (T.DOCUMENTS, T.SCANNED, T.SPREADSHEETS, T.PRESENTATIONS):
                        document_years.append(year)
            if record.ext in ('dwg', 'dxf') and not record.cred:
                if record.mailbox is None:
                    cad['file_archive'] += 1
                elif record.sha:
                    cad['mail'].add(record.sha)
            if (record.mclass not in ('alt_mbox', 'alt_attachments', 'alt_bodies', 'alt_archive', 'meta', 'log') and not record.cred
                    and record.category in EXAMINED_CATEGORIES and record.ext in TEXT_EXTRACTABLE and record.ext != 'mbox'
                    and record.entry.role != 'follower'):
                lang = ((record.mail or {}).get('lang') if record.ext == 'eml' else record.facts.get('lang')) or NO_LANGUAGE
                language.append((lang['lang'], size, record.ext == 'eml', lang['chunks']))
            if record.sha and not record.cred and record.mclass not in ('alt_mbox', 'alt_archive'):
                facts = record.facts.get('pdf') if record.ext == 'pdf' else record.facts.get('img')
                if facts:
                    project = None
                    info = project_info(record.entry, cfg, self.stage_patterns) if (cfg['project_roots'] and record.mailbox is None) else None
                    if info:
                        project = (info['key'], info['stages'][0] if info['stages'] else None, record.name)
                    sheets.add(record.sha, facts, record.entry.rel, 'file' if record.mailbox is None else 'mail', project)
            if cfg['project_roots'] and record.mailbox is None and not record.cred:
                info = project_info(record.entry, cfg, self.stage_patterns)
                if info:
                    projects.setdefault(info['key'], {'stages': set(), 'boq': False})
                    projects[info['key']]['stages'].update(info['stages'])
                    projects[info['key']]['boq'] |= info['boq']
        for message in self.messages:
            for attachment in message.get('atts', ()):
                if attachment['ext'] in ('dwg', 'dxf'):
                    cad['mail'].add(attachment['sha'])
                facts = attachment.get('pdf') or attachment.get('img')
                if facts:
                    sheets.add(attachment['sha'], facts, attachment.get('name', ''), 'mail')
        for record in self.records:
            if record.mbox and record.mclass != 'alt_mbox':
                for message in record.mbox['messages']:
                    if message.get('lang') and not message.get('known'):
                        language.append((message['lang']['lang'], message.get('size', 0), True, message['lang']['chunks']))
        # ---------------------------------------------------------------- pass 2: archive members
        members_total, members_new = [0, 0], [0, 0]
        archive_stats = Counter()
        not_opened, tools_used = Counter(), Counter()
        member_cats = defaultdict(lambda: {'files': 0, 'bytes': 0, 'new_files': 0, 'new_bytes': 0, 'ext': Counter()})
        for record in self.records:
            arch = record.arch
            if arch is None or (arch.get('kind') is None and not arch.get('opened')):
                continue
            for key, field in (('opened', 'opened'), ('partly', 'partly'), ('lost', 'lost'), ('hidden', 'hidden')):
                archive_stats[key] += arch.get(field, 0)
            archive_stats['time_limited'] += 1 if arch.get('timeout') else 0
            for reason, number in arch.get('failed', {}).items():
                not_opened[reason] += number
            for tool, number in arch.get('used', {}).items():
                tools_used[tool] += number
        seen = set()
        for identifier in self.archive_ids:
            entry = self.by_id[identifier]
            for member in self.cache.get(identifier)['arch']['members']:
                size, sha, name = member['s'], member.get('h'), member['n']
                base = name.rsplit('/', 1)[-1]
                extension = raw_extension(base)
                cred = bool(member.get('cred'))
                pdf = member.get('pdf') or {}
                category = CREDENTIALS if cred else T.category_of(base, pdf_scanned=bool(pdf.get('scanned')) and not pdf.get('unchecked'))
                if category == T.OTHER and member.get('arch'):
                    category = T.ARCHIVES
                label = extension_label(base)
                container = bool(member.get('cont'))
                new = (not container) and (cred or (sha not in loose_hashes and sha not in seen))
                if sha:
                    seen.add(sha)
                    all_hashes.add(sha)
                    hashed += 1
                total_files += 1
                total_bytes += size
                members_total[0] += 1
                members_total[1] += size
                add_category(category, label, size, sha, inside=True)
                stat = member_cats[category]
                stat['files'] += 1
                stat['bytes'] += size
                if label != T.IRREGULAR_EXTENSION:
                    stat['ext'][label] += 1
                if new:
                    members_new[0] += 1
                    members_new[1] += size
                    stat['new_files'] += 1
                    stat['new_bytes'] += size
                if pdf.get('unchecked') and not cred:
                    unchecked[0] += 1
                    unchecked[1] += size
                if extension in ('dwg', 'dxf') and not cred:
                    cad['archives'] += 1
                    cad['archives_new'] += 1 if new else 0
                if new and category in EXAMINED_CATEGORIES and extension in TEXT_EXTRACTABLE:
                    lang = member.get('lang') or NO_LANGUAGE
                    language.append((lang['lang'], size, extension == 'eml', lang['chunks']))
                if sha and not cred:
                    facts = pdf if extension == 'pdf' else member.get('img')
                    if facts:
                        sheets.add(sha, facts, entry.rel + '/' + name, 'archive')
        return self.assemble({
            'categories': categories, 'formats': formats, 'all_hashes': all_hashes, 'hashed': hashed, 'total_files': total_files,
            'total_bytes': total_bytes, 'parts': parts, 'unchecked': unchecked, 'years': years, 'year_bytes': year_bytes,
            'unreliable': unreliable, 'document_years': document_years, 'cad': cad, 'language': language, 'projects': projects,
            'members_total': members_total, 'members_new': members_new, 'archive_stats': archive_stats, 'not_opened': not_opened,
            'tools_used': tools_used, 'member_cats': member_cats})

    def assemble(self, state):
        cfg = self.cfg
        categories, formats = state['categories'], state['formats']
        total_files, total_bytes = state['total_files'], state['total_bytes']
        members_total, members_new, archive_stats = state['members_total'], state['members_new'], state['archive_stats']
        parts = state['parts']
        held_files, held_bytes = total_files - members_total[0], total_bytes - members_total[1]
        spec = {'schema': SCHEMA, 'current_year': self.year_now}
        for key in ('company', 'sector_line', 'month'):
            if cfg.get(key):
                spec[key] = cfg[key]
        spec['totals'] = {'files': total_files, 'bytes': total_bytes, 'held_files': held_files, 'held_bytes': held_bytes,
                          'hashed_files': state['hashed'], 'unique_contents': len(state['all_hashes'])}
        spec['parts'] = {'file_archive': {'files': parts['file_archive'][0], 'bytes': parts['file_archive'][1]}}
        mailboxes = self.build_mailboxes(parts)
        if mailboxes:
            spec['mailboxes'] = mailboxes
            spec['parts']['mailboxes'] = self.business_summary(mailboxes, parts)
            if parts['alt'][0]:
                spec['parts']['alternative_exports'] = self.alt_summary(mailboxes, parts)
            if parts['log'][0]:
                spec['parts']['export_logs'] = {'files': parts['log'][0], 'bytes': parts['log'][1]}
        if members_total[0] or archive_stats['opened']:
            spec['parts']['archive_contents'] = {'files': members_total[0], 'bytes': members_total[1], 'archives': archive_stats['opened'],
                                                  'new_files': members_new[0], 'new_bytes': members_new[1]}
            spec['parts']['held_without_archives'] = {'files': held_files, 'bytes': held_bytes}
        # An extension carried by a single file and not a known format could be a personal or client name: group it as irregular.
        label_files = Counter()
        for (label, _), row in formats.items():
            label_files[label] += row[0]
        rare = {label for label, files in label_files.items() if files == 1 and label not in T.CATEGORY_OF_EXTENSION
                and label not in (T.NO_EXTENSION, T.IRREGULAR_EXTENSION) and not label.isdigit()}
        merged = defaultdict(lambda: [0, 0, 0])
        for (label, category), row in formats.items():
            target = merged[(T.IRREGULAR_EXTENSION if label in rare else label, category)]
            for position in range(3):
                target[position] += row[position]
        formats = merged
        for stat in list(categories.values()) + list(state['member_cats'].values()):
            for label in rare:
                stat['ext'].pop(label, None)
        spec['categories'] = []
        for name in T.CATEGORIES:
            stat = categories[name]
            if stat['files']:
                row = {'name': name, 'files': stat['files'], 'bytes': stat['bytes'], 'archive_files': stat['arch_files'],
                       'archive_bytes': stat['arch_bytes'], 'formats': top_formats(stat['ext'])}
                if name != CREDENTIALS:
                    row['unique'] = len(stat['unique'])
                spec['categories'].append(row)
        ranked = sorted(formats.items(), key=lambda item: (-item[1][1], item[0][1], item[0][0]))[:TOP_FORMATS]
        spec['formats'] = [{'ext': label, 'category': category, 'files': row[0], 'bytes': row[1], 'archive_files': row[2]}
                           for (label, category), row in ranked]
        if members_total[0]:
            member_cats = state['member_cats']
            spec['archives'] = {'categories': [{'name': name, 'files': member_cats[name]['files'], 'bytes': member_cats[name]['bytes'],
                                                'new_files': member_cats[name]['new_files'], 'new_bytes': member_cats[name]['new_bytes'],
                                                'formats': top_formats(member_cats[name]['ext'])}
                                               for name in T.CATEGORIES if name in member_cats and member_cats[name]['files']],
                                'total': {'files': members_total[0], 'bytes': members_total[1], 'new_files': members_new[0], 'new_bytes': members_new[1]}}
        languages = build_languages(state['language'])
        if languages:
            spec['languages'] = languages
        years = self.build_years(state['years'], state['year_bytes'], state['unreliable'])
        if years:
            spec['years'] = years
        spec['periods'] = {}
        period = trimmed_range(state['document_years'])
        if period:
            spec['periods']['documents'] = period
        mail_years = [m['year'] for index in self.mail for m in self.mail[index]['messages'] if m.get('year') and MIN_YEAR <= m['year'] <= self.year_now]
        if mail_years:
            spec['periods']['email'] = [min(mail_years), max(mail_years)]
        cad = state['cad']
        cad_total = cad['file_archive'] + cad['archives'] + len(cad['mail'])
        if cad_total:
            spec['cad'] = {'file_archive': cad['file_archive'], 'archives': cad['archives'], 'archives_new': cad['archives_new'],
                           'mail': len(cad['mail']), 'total': cad_total}
        drawings = self.build_drawings(state['projects'])
        if drawings:
            spec['drawings'] = drawings
        walk = self.context['walk']
        spec['methodology'] = {
            'hash': 'sha256', 'unreadable_files': self.unreadable.get('unreadable', 0), 'time_limited_files': self.unreadable.get('limited', 0),
            'unchecked_pdfs': {'files': state['unchecked'][0], 'bytes': state['unchecked'][1]},
            'hidden_entries': walk.get('hidden_entries', 0), 'credential_folders': walk.get('credential_folders', 0),
            'symlinks': walk.get('symlinks', 0), 'special_files': walk.get('special_files', 0), 'unreadable_folders': walk.get('unreadable_folders', 0),
            'archives': {'opened': archive_stats['opened'], 'partly_readable': archive_stats['partly'],
                         'members_not_extracted': archive_stats['lost'], 'hidden_members': archive_stats['hidden'],
                         'time_limited': archive_stats['time_limited'], 'not_opened': dict(sorted(state['not_opened'].items())),
                         'tools': dict(sorted(state['tools_used'].items())), 'tools_available': sorted(self.context['tools'])},
            'limits': {key: self.context['settings'][key] for key in ('max_archive_bytes', 'max_archive_ratio', 'max_depth')},
        }
        return spec

    # ------------------------------------------------------------------ mailbox rows
    def build_mailboxes(self, parts):
        rows = []
        for index, mailbox in enumerate(self.cfg['mailboxes']):
            result = self.mail.get(index)
            if not result or not result['members']:
                continue
            members = result['members']
            business = [r for r in members if r.mclass in BUSINESS]
            messages = result['eml_count'] + result['primary_messages']
            row = {'label': mailbox['label'], 'messages': messages, 'bytes': sum(r.size for r in business), 'files': len(business)}
            years = [m['year'] for m in result['messages'] if m.get('year') and MIN_YEAR <= m['year'] <= self.year_now]
            if years:
                row['period'] = [min(years), max(years)]
            kinds = {r.ext for r in business if r.mclass in ('eml', 'mbox_primary')}
            row['format'] = 'eml' if 'eml' in kinds and 'mbox' not in kinds else ('mbox' if kinds == {'mbox'} else 'eml+mbox')
            meta = Counter(r.ext for r in business if r.mclass == 'meta')
            if meta:
                row['metadata'] = {'files': sum(meta.values()), 'extensions': top_formats(meta), 'counts': dict(sorted(meta.items()))}
            other = sum(1 for r in business if r.mclass == 'other')
            if other:
                row['other_files'] = other
            counted = [m for m in result['messages'] if m.get('from') or m.get('to')]
            owners = Counter(h for m in counted for h in set(m.get('to', ())))
            if owners:
                owner = sorted(owners.items(), key=lambda item: (-item[1], item[0]))[0][0]
                sent = sum(1 for m in counted if m.get('from') == owner)
                row['sent'] = sent
                row['received'] = len(counted) - sent
                row['sent_missing'] = sent == 0
            alternatives = []
            for mclass, kind in ALT_ORDER:
                chosen = [r for r in members if r.mclass == mclass]
                if chosen:
                    item = {'kind': kind, 'files': len(chosen), 'bytes': sum(r.size for r in chosen)}
                    if kind in ('archive', 'mbox'):
                        item['extensions'] = top_formats(Counter(r.ext for r in chosen))
                    alternatives.append(item)
            if alternatives:
                row['alternatives'] = alternatives
            logs = [r for r in members if r.mclass == 'log']
            if logs:
                row['log_files'] = len(logs)
                row['log_bytes'] = sum(r.size for r in logs)
            rows.append(row)
        return rows

    def business_summary(self, rows, parts):
        summary = {'files': parts['mailboxes'][0], 'bytes': parts['mailboxes'][1], 'messages': sum(r['messages'] for r in rows)}
        meta = sum(r.get('metadata', {}).get('files', 0) for r in rows)
        if meta:
            counter = Counter()
            for r in rows:
                counter.update(r.get('metadata', {}).get('counts', {}))
            summary['metadata_files'] = meta
            summary['metadata_extensions'] = top_formats(counter)
        other = sum(r.get('other_files', 0) for r in rows)
        if other:
            summary['other_files'] = other
        summary['formats'] = sorted({'eml' if r['format'] != 'mbox' else 'mbox' for r in rows})
        summary['sent_missing'] = [{'label': r['label'], 'received': r['received']} for r in rows if r.get('sent_missing')]
        return summary

    def alt_summary(self, rows, parts):
        items = {}
        for row in rows:
            for item in row.get('alternatives', ()):
                entry = items.setdefault(item['kind'], {'kind': item['kind'], 'files': 0, 'bytes': 0, 'extensions': Counter()})
                entry['files'] += item['files']
                entry['bytes'] += item['bytes']
                for ext in item.get('extensions', ()):
                    entry['extensions'][ext] += 1
        ordered = []
        for _, kind in ALT_ORDER:
            if kind in items:
                entry = items[kind]
                entry['extensions'] = top_formats(entry['extensions'])
                ordered.append(entry)
        return {'files': parts['alt'][0], 'bytes': parts['alt'][1], 'items': ordered}

    # ------------------------------------------------------------------ years
    def build_years(self, years, year_bytes, unreliable):
        mail = Counter()
        mail_unreliable = 0
        for result in self.mail.values():
            for message in result['messages']:
                year = message.get('year')
                if not year:
                    continue
                if MIN_YEAR <= year <= self.year_now:
                    mail[year] += 1
                else:
                    mail_unreliable += 1
        keys = set(years) | set(mail)
        if not keys and not unreliable[0] and not mail_unreliable:
            return None
        rows = []
        if keys:
            for year in range(min(keys), max(keys) + 1):
                rows.append({'year': year, 'files': years.get(year, 0), 'bytes': year_bytes.get(year, 0), 'messages': mail.get(year, 0)})
        result = {'rows': rows, 'has_mail': bool(self.mail)}
        if unreliable[0] or mail_unreliable:
            result['unreliable'] = {'files': unreliable[0], 'bytes': unreliable[1], 'messages': mail_unreliable}
        return result

    # ------------------------------------------------------------------ drawings
    def build_drawings(self, projects):
        sheets, cfg = self.sheets, self.cfg
        per = {}
        non_candidates = 0
        for sha in sorted(sheets.facts):
            pages, drawing, labels, schematic, rest = sheets.evaluate(sha)
            non_candidates += rest
            if drawing:
                locations = sheets.locations[sha]
                per[sha] = (pages, drawing, labels, schematic, 'file' if 'file' in locations else ('mail' if 'mail' in locations else 'archive'))
        if not per:
            return None
        uncorrected = sum(item[1] for item in per.values())
        scale, calibration = 1.0, cfg.get('calibration')
        result = {'calibrated': False}
        if calibration:
            sampled = calibration['sampled']
            precision = calibration['true_drawings_among_candidates'] / sampled['candidates']
            miss = calibration['drawings_among_non_candidates'] / sampled['non_candidates'] if sampled['non_candidates'] else 0.0
            corrected = int(round(uncorrected * precision + non_candidates * miss))
            scale = corrected / uncorrected if uncorrected else 1.0
            result['calibrated'] = True
            result['sampled_pages'] = sampled['candidates'] + sampled['non_candidates']
        def sc(value):
            return int(round(value * scale)) if result['calibrated'] else value

        total = sc(uncorrected)
        truth = (calibration['true_drawings_among_candidates'] + calibration['drawings_among_non_candidates']) if calibration else 0
        if calibration and truth:
            schematics = int(round(total * calibration['schematics'] / truth))
        else:
            schematics = sum(item[1] for item in per.values() if item[3])
        result.update({'sheets': total, 'schematics': schematics, 'engineering': total - schematics, 'files': len(per)})
        dedicated = [item for item in per.values() if item[1] / item[0] >= DEDICATED_SHARE]
        documents = [item for item in per.values() if item[1] / item[0] < DEDICATED_SHARE]
        result['dedicated'] = {'files': len(dedicated), 'sheets': sc(sum(item[1] for item in dedicated))}
        result['documents'] = {'files': len(documents), 'sheets': sc(sum(item[1] for item in documents))}
        sizes = Counter()
        locations = Counter()
        for pages, drawing, labels, schematic, location in per.values():
            sizes.update(labels)
            locations[location] += drawing
        result['sizes'] = {label: sc(sizes[label]) for label in T.SIZE_ORDER if sizes.get(label)}
        result['locations'] = {key: sc(locations[key]) for key in ('file', 'mail', 'archive') if locations.get(key)}
        if calibration:
            views = calibration['views_per_sheet']
            mean = sum(views) / len(views)
            generator = random.Random(20261025)
            means = sorted(sum(generator.choice(views) for _ in views) / len(views) for _ in range(1000))
            result['views'] = {'total': int(round(total * mean)), 'per_sheet': round(mean, 1),
                               'low': round(percentile(means, 0.10), 1), 'high': round(percentile(means, 0.90), 1)}
        if cfg['project_roots'] and projects:
            result['projects'] = self.project_workflow(per, projects, sc)
        return result

    def project_workflow(self, per, projects, sc):
        sheets, cfg = self.sheets, self.cfg
        by_project = defaultdict(set)
        for sha, copies in sheets.copies.items():
            if sha in per:
                for key, _, _ in copies:
                    by_project[key].add(sha)

        def sheet_count(shas):
            return sc(sum(per[sha][1] for sha in shas))
        in_projects = set().union(*by_project.values()) if by_project else set()
        result = {'projects': len(projects), 'projects_with_sheets': len(by_project), 'sheets': sheet_count(in_projects),
                  'total_sheets': sc(sum(item[1] for item in per.values()))}
        stages = cfg.get('stages')
        if stages:
            names = [item['name'] for item in stages['items']]
            thresholds = stages['thresholds'] or T.default_thresholds(len(names))
            levels = []
            for k in thresholds:
                chosen = [key for key, info in projects.items() if len(info['stages']) >= k]
                levels.append({'k': k, 'projects': len(chosen), 'sheets': sheet_count(set().union(*(by_project[key] for key in chosen)) if chosen else set())})
            by_stage = Counter()
            for sha in sorted(in_projects):
                stage = next((s for _, s, _ in sorted(sheets.copies[sha], key=lambda c: (c[0], c[2])) if s is not None), None)
                if stage is not None:
                    by_stage[stage] += per[sha][1]
            result['stages'] = {'title': stages['title'], 'count': len(names), 'names': names, 'levels': levels,
                                'by_stage': [{'name': names[i], 'sheets': sc(n)} for i, n in sorted(by_stage.items(), key=lambda item: (-item[1], item[0]))]}
        if cfg['bill_of_quantities_patterns']:
            chosen = [key for key, info in projects.items() if info['boq'] and by_project.get(key)]
            result['boq'] = {'projects': len(chosen), 'sheets': sheet_count(set().union(*(by_project[key] for key in chosen)) if chosen else set())}
        groups = defaultdict(set)
        for sha in in_projects:
            for key, _, name in sheets.copies[sha]:
                stem = revision_stem(name, cfg['revision_tokens'])
                if stem:
                    groups[(key, stem)].add(sha)
        versions = [key for key, shas in groups.items() if len(shas) >= 2]
        if versions:
            result['revisions'] = {'drawings': len(versions), 'projects': len({key for key, _ in versions})}
        return result


def build_languages(entities):
    if not entities:
        return None
    files, size, messages = Counter(), Counter(), Counter()
    for code, byte_count, is_message, _ in entities:
        files[code] += 1
        size[code] += byte_count
        messages[code] += 1 if is_message else 0
    named = {code: n for code, n in files.items() if code and n >= MIN_LANGUAGE_FILES}
    rows = [{'code': code, 'name': spec_language.language_name(code), 'files': files[code], 'bytes': size[code], 'messages': messages[code]}
            for code in sorted(named, key=lambda c: (-files[c], spec_language.language_name(c)))]
    rare = [code for code in files if code and code not in named]
    result = {'rows': rows}
    if rare:
        result['other'] = {'files': sum(files[c] for c in rare), 'bytes': sum(size[c] for c in rare), 'messages': sum(messages[c] for c in rare)}
    if files.get(None):
        result['none'] = {'files': files[None], 'bytes': size[None], 'messages': messages[None]}
    result['total'] = {'files': len(entities), 'bytes': sum(size.values()), 'messages': sum(messages.values())}
    locals_ = [row for row in rows if row['code'] != 'en']
    if locals_ and any(row['code'] == 'en' for row in rows):
        local = locals_[0]['code']
        count = 0
        for code, _, _, chunks in entities:
            classified = sum(chunks.values())
            if classified >= 5 and chunks.get(local, 0) / classified >= 0.2 and chunks.get('en', 0) / classified >= 0.2:
                count += 1
        if count:
            result['bilingual'] = {'code': local, 'name': spec_language.language_name(local), 'files': count}
    return result


def aggregate(entries, cache, cfg, context):
    return Build(entries, cache, cfg, context).run()
