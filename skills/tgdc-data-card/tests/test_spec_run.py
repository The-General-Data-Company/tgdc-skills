"""Full specification run on a synthetic archive. Expected values come from the fixture registry."""
import json
import os
import stat
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_fixture as F
import spec_taxonomy as T

NOW = datetime(2026, 10, 25)
LOG = open(os.devnull, 'w')
YEAR = 2026


def run(fx, out, **kwargs):
    return formatspec.run_spec([fx.data], out, config=fx.config, workers=2, tools={}, report_date=NOW, log=LOG, **kwargs)


class SpecRunTests(unittest.TestCase):
    maxDiff = 3000

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.fx = F.Fixture(cls.base / 'fx')
        cls.out = cls.base / 'out'
        cls.spec = run(cls.fx, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------ oracle helpers
    @property
    def items(self):
        return self.fx.files

    def entries(self):
        """All files and members as (category, label, size, sha, inside archive, new, cred)."""
        for item in self.fx.files:
            yield item['category'], item['label'], item['size'], item['sha'], False, False, item['cred']
        for member in self.fx.members:
            yield member['category'], member['label'], member['size'], member['sha'], True, member['new'], member['cred']

    def test_totals_and_parts(self):
        spec, fx = self.spec, self.fx
        all_items = list(self.entries())
        self.assertEqual(spec['totals']['files'], len(all_items))
        self.assertEqual(spec['totals']['bytes'], sum(e[2] for e in all_items))
        self.assertEqual(spec['totals']['held_files'], len(fx.files))
        self.assertEqual(spec['totals']['held_bytes'], sum(f['size'] for f in fx.files))
        self.assertEqual(spec['totals']['hashed_files'], sum(1 for e in all_items if e[3]))
        self.assertEqual(spec['totals']['unique_contents'], len({e[3] for e in all_items if e[3]}))
        self.assertEqual(spec['totals']['hashed_files'], spec['totals']['files'])      # credential files are checksummed too
        for key, part in [('file_archive', 'file'), ('mailboxes', 'mailbox'), ('alternative_exports', 'alt'), ('export_logs', 'log')]:
            chosen = [f for f in fx.files if f['part'] == part]
            self.assertEqual(spec['parts'][key]['files'], len(chosen), key)
            self.assertEqual(spec['parts'][key]['bytes'], sum(f['size'] for f in chosen), key)
        members = fx.members
        archive = spec['parts']['archive_contents']
        self.assertEqual((archive['files'], archive['bytes']), (len(members), sum(m['size'] for m in members)))
        self.assertEqual(archive['archives'], fx.archives_opened)
        new = [m for m in members if m['new'] and not m['container']]
        self.assertEqual((archive['new_files'], archive['new_bytes']), (len(new), sum(m['size'] for m in new)))
        self.assertEqual(spec['parts']['held_without_archives']['files'], len(fx.files))
        self.assertEqual(spec['parts']['mailboxes']['messages'], 17)

    def test_categories_unique_counts_and_main_formats(self):
        stats = defaultdict(lambda: {'files': 0, 'bytes': 0, 'unique': set(), 'af': 0, 'ab': 0, 'ext': Counter()})
        for category, label, size, sha, inside, new, cred in self.entries():
            row = stats[category]
            row['files'] += 1
            row['bytes'] += size
            if sha:
                row['unique'].add(sha)
            row['af'] += inside
            row['ab'] += size if inside else 0
            if label != '(other / irregular extension)':
                row['ext'][label] += 1
        names = [c['name'] for c in self.spec['categories']]
        self.assertEqual(names, [c for c in T.CATEGORIES if stats[c]['files']])
        for row in self.spec['categories']:
            expected = stats[row['name']]
            self.assertEqual((row['files'], row['bytes'], row['archive_files'], row['archive_bytes']), (expected['files'], expected['bytes'], expected['af'], expected['ab']), row['name'])
            self.assertEqual(row['unique'], len(expected['unique']), row['name'])
            ranked = [label for label, _ in sorted(expected['ext'].items(), key=lambda i: (-i[1], i[0]))[:6]]
            self.assertEqual(row['formats'], ranked, row['name'])
        self.assertEqual(sum(1 for c in self.spec['categories'] if c['name'] == T.CREDENTIALS), 1)
        credentials = next(c for c in self.spec['categories'] if c['name'] == T.CREDENTIALS)
        self.assertEqual(credentials['files'], 4)       # three loose files and one archive member: checksum only, never parsed
        self.assertEqual(credentials['archive_files'], 1)

    def test_formats_table_groups_irregular_and_ranks_by_volume(self):
        formats = defaultdict(lambda: [0, 0, 0])
        for category, label, size, sha, inside, new, cred in self.entries():
            row = formats[(label, category)]
            row[0] += 1
            row[1] += size
            row[2] += inside
        expected = sorted(formats.items(), key=lambda i: (-i[1][1], i[0][1], i[0][0]))[:35]
        got = [((r['ext'], r['category']), [r['files'], r['bytes'], r['archive_files']]) for r in self.spec['formats']]
        self.assertEqual(got, [(k, v) for k, v in expected])
        labels = {r['ext'] for r in self.spec['formats']}
        self.assertIn('(other / irregular extension)', labels)
        self.assertIn('(no extension)', labels)
        for unknown in ('zzq', 'zzr'):       # a suffix outside the known formats could be a name, whatever the number of files
            self.assertNotIn(unknown, labels)
            self.assertNotIn(unknown, {c for row in self.spec['categories'] for c in row['formats']})
        self.assertIn('001', labels)       # volume of a multi-part archive keeps its number
        self.assertIn('002', labels)

    def test_inside_the_compressed_archives(self):
        archives = self.spec['archives']
        members = self.fx.members
        by_category = defaultdict(lambda: [0, 0, 0, 0])
        for m in members:
            row = by_category[m['category']]
            row[0] += 1
            row[1] += m['size']
            if m['new'] and not m['container']:
                row[2] += 1
                row[3] += m['size']
        self.assertEqual([c['name'] for c in archives['categories']], [c for c in T.CATEGORIES if c in by_category])
        for row in archives['categories']:
            self.assertEqual([row['files'], row['bytes'], row['new_files'], row['new_bytes']], by_category[row['name']], row['name'])
        self.assertEqual(archives['total']['files'], len(members))
        self.assertEqual(archives['total']['new_files'], sum(1 for m in members if m['new'] and not m['container']))
        method = self.spec['methodology']['archives']
        self.assertEqual(method['opened'], self.fx.archives_opened)
        self.assertEqual(method['members_not_extracted'], self.fx.lost_members)
        self.assertEqual(method['partly_readable'], self.fx.partly)
        self.assertEqual(method['not_opened'], self.fx.not_opened)
        self.assertEqual(method['tools'].get('zip:zipfile'), 5)     # archive, nested, locked, split (joined), export

    def test_mailboxes(self):
        rows = {row['label']: row for row in self.spec['mailboxes']}
        one, two, three = rows['Mailbox 1 (owner, Gmail)'], rows['Mailbox 2 (co-owner, Yahoo)'], rows['Mailbox 3 (company, Gmail)']
        box1 = [f for f in self.fx.files if f['rel'].startswith('Mail/Box1/')]
        business = [f for f in box1 if f['part'] == 'mailbox']
        self.assertEqual((one['messages'], one['files'], one['bytes']), (8, len(business), sum(f['size'] for f in business)))
        self.assertEqual(one['period'], [2021, 2024])
        self.assertEqual(one['format'], 'eml')
        self.assertEqual(one['metadata'], {'files': 9, 'extensions': ['json', 'csv'], 'counts': {'csv': 1, 'json': 8}})
        self.assertEqual(one['other_files'], 1)
        self.assertEqual((one['sent'], one['received'], one['sent_missing']), (2, 6, False))
        kinds = {a['kind']: a for a in one['alternatives']}
        self.assertEqual(list(kinds), ['mbox', 'attachments', 'bodies', 'archive'])
        self.assertEqual(kinds['mbox']['files'], 1)
        self.assertEqual(kinds['attachments']['files'], 3)
        self.assertEqual(kinds['bodies']['files'], 3)
        self.assertEqual(kinds['archive']['extensions'], ['zip'])
        self.assertEqual(kinds['mbox']['bytes'], next(f['size'] for f in box1 if f['label'] == 'mbox'))
        self.assertEqual((one['log_files'], one['log_bytes']), (2, sum(f['size'] for f in box1 if f['part'] == 'log')))
        self.assertEqual((two['messages'], two['period'], two['sent'], two['received'], two['sent_missing']), (4, [2023, 2025], 0, 4, True))
        self.assertNotIn('metadata', two)
        self.assertEqual([(a['kind'], a['files']) for a in two['alternatives']], [('attachments', 2)])
        summary = self.spec['parts']
        self.assertEqual(summary['mailboxes']['incomplete'], [{'label': 'Mailbox 2 (co-owner, Yahoo)', 'messages': 4, 'held': 4, 'account': False},
                                                              {'label': 'Mailbox 3 (company, Gmail)', 'messages': 5, 'held': 3, 'account': True}])
        # the .mbox holds the whole account (three received messages as .eml files plus two sent messages): same messages, plus the sent ones
        self.assertEqual((three['messages'], three['held_messages'], three['period'], three['format']), (5, 3, [2022, 2025], 'eml'))
        self.assertEqual((three['sent'], three['received'], three['sent_missing']), (2, 3, False))
        self.assertEqual([(a['kind'], a['files']) for a in three['alternatives']], [('mbox', 1)])
        self.assertNotIn('held_messages', one)
        self.assertEqual(summary['mailboxes']['metadata_files'], 9)
        self.assertEqual({i['kind']: i['files'] for i in summary['alternative_exports']['items']},
                         {'mbox': 2, 'attachments': 5, 'bodies': 3, 'archive': 1})
        self.assertEqual(self.spec['periods']['email'], [2021, 2025])

    def test_languages(self):
        groups = defaultdict(lambda: [0, 0, 0])
        examined = []
        for item in self.fx.files:
            if item['lang'] != '-' and not item['cred'] and item['part'] in ('file', 'mailbox'):
                examined.append((item['lang'], item['size'], item['message']))
        for m in self.fx.members:
            if m['lang'] != '-' and m['new'] and not m['container']:
                examined.append((m['lang'], m['size'], False))
        for extra in self.fx.extra_messages:        # sent messages that exist only inside an account .mbox
            examined.append((extra['lang'], extra['size'], True))
        for lang, size, message in examined:
            groups[lang][0] += 1
            groups[lang][1] += size
            groups[lang][2] += message
        languages = self.spec['languages']
        rows = {r['code']: r for r in languages['rows']}
        self.assertEqual(sorted(rows), ['en', 'ro'])
        for code in ('en', 'ro'):
            self.assertEqual([rows[code]['files'], rows[code]['messages']], [groups[code][0], groups[code][2]], code)
            self.assertAlmostEqual(rows[code]['bytes'], groups[code][1], delta=64, msg=code)      # message sizes inside an .mbox differ by a few bytes
        self.assertEqual([r['code'] for r in languages['rows']], sorted(rows, key=lambda c: -rows[c]['files']))
        self.assertEqual(languages['other']['files'], groups['de'][0])          # German is the main language of two files only
        self.assertEqual(languages['other']['messages'], 2)
        self.assertEqual([languages['none']['files'], languages['none']['bytes']], groups[None][:2])
        scans = sum(1 for item in self.fx.files if item['lang'] is None and item['category'] == T.SCANNED and not item['cred'] and item['part'] in ('file', 'mailbox'))
        scans += sum(1 for m in self.fx.members if m['lang'] is None and m['new'] and not m['container'] and m['category'] == T.SCANNED)
        self.assertEqual(scans, 2)
        self.assertEqual(languages['none']['scans'], scans)           # the other unconfirmed files are short or garbled text
        self.assertGreater(languages['none']['files'], scans)
        self.assertEqual(languages['total']['files'], len(examined))
        self.assertEqual(languages['total']['messages'], sum(e[2] for e in examined))
        self.assertEqual(languages['bilingual'], {'code': 'ro', 'name': 'Romanian', 'files': self.fx.bilingual})

    def test_dates_by_year_and_periods(self):
        files, size = Counter(), Counter()
        unreliable = [0, 0]
        for item in self.fx.files:
            if item['part'] != 'file':
                continue
            if 1990 <= item['year'] <= YEAR:
                files[item['year']] += 1
                size[item['year']] += item['size']
            else:
                unreliable[0] += 1
                unreliable[1] += item['size']
        messages = Counter()
        bad_messages = 0
        for rows in self.fx.messages.values():
            for row in rows:
                if 1990 <= row['year'] <= YEAR:
                    messages[row['year']] += 1
                else:
                    bad_messages += 1
        years = self.spec['years']
        self.assertTrue(years['has_mail'])
        got = {r['year']: (r['files'], r['bytes'], r['messages']) for r in years['rows']}
        keys = set(files) | set(messages)
        self.assertEqual(sorted(got), list(range(min(keys), max(keys) + 1)))
        for year in got:
            self.assertEqual(got[year], (files[year], size[year], messages[year]), year)
        self.assertEqual(years['unreliable'], {'files': unreliable[0], 'bytes': unreliable[1], 'messages': bad_messages})
        self.assertEqual(years['unreliable']['files'], 2)
        document_years = [f['year'] for f in self.fx.files if f['part'] == 'file' and 1990 <= f['year'] <= YEAR
                          and f['category'] in (T.DOCUMENTS, T.SCANNED, T.SPREADSHEETS, T.PRESENTATIONS)]
        self.assertEqual(self.spec['periods']['documents'], [min(document_years), max(document_years)])
        self.assertEqual(got[2016][0], 2)       # both copies of the tender notice carry their own PDF date, not their file date

    def test_native_cad_files(self):
        fx = self.fx
        loose = sum(1 for f in fx.files if f['cad'] and f['part'] == 'file')
        in_mail = {f['sha'] for f in fx.files if f['cad'] and f['part'] != 'file'}
        in_mail |= {fx_sha for fx_sha in [F.sha(b'0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n' * 2)]}
        archive = [m for m in fx.members if m['cad']]
        self.assertEqual(self.spec['cad'], {'file_archive': loose, 'archives': len(archive), 'archives_new': sum(1 for m in archive if m['new']),
                                            'mail': len(in_mail), 'total': loose + len(archive) + len(in_mail)})

    def test_drawing_sheets_sizes_locations_and_file_types(self):
        sheets = self.fx.sheets
        drawings = self.spec['drawings']
        total = sum(sum(s['sizes'].values()) for s in sheets.values())
        schematics = sum(sum(s['sizes'].values()) for s in sheets.values() if s['schematic'])
        self.assertEqual((drawings['sheets'], drawings['schematics'], drawings['engineering'], drawings['files']), (total, schematics, total - schematics, len(sheets)))
        sizes = Counter()
        for s in sheets.values():
            sizes.update(s['sizes'])
        self.assertEqual(drawings['sizes'], {k: sizes[k] for k in T.SIZE_ORDER if sizes[k]})
        self.assertEqual(drawings['sizes'], {'A4': 2, 'A3': 5, 'A1': 3, 'image file': 1})
        locations = Counter()
        for s in sheets.values():
            locations[s['loc']] += sum(s['sizes'].values())
        self.assertEqual(drawings['locations'], {'file': locations['file'], 'mail': locations['mail'], 'archive': locations['archive']})
        self.assertEqual(drawings['locations'], {'file': 8, 'mail': 2, 'archive': 1})
        dedicated = [s for s in sheets.values() if sum(s['sizes'].values()) / s['pages'] >= 0.8]
        documents = [s for s in sheets.values() if sum(s['sizes'].values()) / s['pages'] < 0.8]
        self.assertEqual(drawings['dedicated'], {'files': len(dedicated), 'sheets': sum(sum(s['sizes'].values()) for s in dedicated)})
        self.assertEqual(drawings['documents'], {'files': len(documents), 'sheets': sum(sum(s['sizes'].values()) for s in documents)})
        self.assertEqual((drawings['dedicated']['files'], drawings['documents']['files']), (9, 1))
        self.assertFalse(drawings['calibrated'])
        self.assertNotIn('views', drawings)

    def test_project_workflow_and_revisions(self):
        projects = self.spec['drawings']['projects']
        self.assertEqual((projects['projects'], projects['projects_with_sheets'], projects['sheets'], projects['total_sheets']), (2, 2, 6, 11))
        stages = projects['stages']
        self.assertEqual((stages['title'], stages['count']), ('project milestones', 5))
        self.assertEqual([(l['k'], l['projects'], l['sheets']) for l in stages['levels']], [(3, 2, 6), (4, 1, 6), (5, 0, 0)])
        self.assertEqual(stages['by_stage'], [{'name': 'drawings', 'sheets': 5}, {'name': 'acceptance', 'sheets': 1}])
        self.assertEqual(projects['boq'], {'projects': 1, 'sheets': 6})
        self.assertEqual(projects['revisions'], {'drawings': 1, 'projects': 1})

    def test_methodology_counts_and_exclusions(self):
        method = self.spec['methodology']
        self.assertEqual((method['hidden_entries'], method['credential_folders'], method['symlinks']), (2, 1, 1))
        self.assertEqual((method['unreadable_files'], method['time_limited_files']), (0, 0))
        self.assertEqual(method['unchecked_pdfs'], {'files': 0, 'bytes': 0, 'mailbox_attachments': {'files': 0, 'bytes': 0}})
        self.assertEqual(self.spec['schema'], 'tgdc-format-spec/1.0')
        self.assertEqual(self.spec['company'], 'Synthetic Works SRL')

    def test_outputs_private_cache_and_sources_unchanged(self):
        out = self.out
        self.assertEqual(json.loads((out / 'spec.json').read_text()), self.spec)
        cache = out / 'spec-cache.private.sqlite'
        if os.name != 'nt':                                   # Windows has no permission bits; the folder's access list applies
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        self.assertFalse((out / 'spec-tmp').exists())
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['spec-cache.private.sqlite', 'spec.json'])
        for path in self.fx.data.rglob('*'):
            self.assertNotEqual(path.suffix, '.sqlite')

    def test_spec_json_has_no_names_paths_addresses_hashes_or_text(self):
        text = (self.out / 'spec.json').read_text()
        for token in F.PRIVATE_TOKENS:
            self.assertNotIn(token.lower(), text.lower(), token)
        self.assertNotIn(str(self.base), text)
        import re
        self.assertIsNone(re.search(r'[0-9a-f]{32}', text))
        self.assertNotIn('@', text)


class DateGuardTests(unittest.TestCase):
    def test_the_year_of_a_file_date_is_clamped_into_the_calendar_and_so_counts_as_not_reliable(self):
        import spec_aggregate as A
        from datetime import timezone
        self.assertEqual(A.year_of_ns(datetime(2020, 6, 1, tzinfo=timezone.utc).timestamp() * 1e9), 2020)
        for value in (10 ** 21, 10 ** 30, 2 ** 63 - 1):
            self.assertGreater(A.year_of_ns(value), 2200 - 1)
            self.assertLessEqual(A.year_of_ns(value), 9999)
        for value in (0, -1, -10 ** 19, -2 ** 63, -10 ** 30):
            self.assertEqual(A.year_of_ns(value), 1970)
        self.assertEqual(A.year_of_ns(10 ** 30), 9999)

    def test_the_cache_stores_a_date_beyond_the_integer_range_without_failing(self):
        import types
        self.assertEqual(formatspec.stored_ns(10 ** 21), 2 ** 63 - 1)
        self.assertEqual(formatspec.stored_ns(-10 ** 21), -2 ** 63)
        self.assertEqual(formatspec.stored_ns(5), 5)
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            descriptor = formatspec.output_directory(Path(folder).resolve())
            try:
                cache = formatspec.Cache(descriptor, Path(folder) / 'c.sqlite')
                entry = types.SimpleNamespace(id=b'0:a', root=0, rel='a', size=3, mtime_ns=10 ** 21)
                cache.put(entry, {'status': 'ok'})
                self.assertEqual(cache.index()[b'0:a'], (3, 2 ** 63 - 1))
                cache.close()
            finally:
                os.close(descriptor)

    def test_dates_at_the_ends_of_the_file_system_range_are_shown_as_not_reliable(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            data = base / 'data'
            data.mkdir()
            for name, nanoseconds in (('far.txt', 2 ** 63 - 1), ('past.txt', -2 ** 63), ('normal.txt', int(datetime(2021, 6, 1).timestamp() * 1e9))):
                (data / name).write_text(name * 30)
                os.utime(data / name, ns=(nanoseconds, nanoseconds))
            spec = formatspec.run_spec([data], base / 'out', workers=1, tools={}, report_date=NOW, log=LOG)
            self.assertEqual(spec['years']['unreliable']['files'], 2)
            self.assertEqual([row['year'] for row in spec['years']['rows']], [2021])


if __name__ == '__main__':
    unittest.main()
