"""Worker pool: deadlines, crashes and the per-file read primitives."""
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_content
import spec_pool_targets
import spec_samples as S

SETTINGS = {**formatspec.SETTINGS_DEFAULTS, 'parse_seconds': 0.3, 'archive_seconds': 0.3, 'grace_seconds': 0.2, 'hash_floor_seconds': 0.5}


class PoolTests(unittest.TestCase):
    def make_tasks(self, folder, names):
        tasks = []
        for name in names:
            path = Path(folder) / name
            path.write_bytes(b'0123456789')
            info = path.stat()
            tasks.append({'id': name.encode(), 'name': name, 'path': str(path), 'identity': (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)})
        return tasks

    def test_hung_and_crashed_workers_are_replaced_and_the_rest_finishes(self):
        results = {}
        names = ['ok_one', 'hang_after_hash', 'crash_now', 'hang_before_hash', 'ok_two', 'ok_three']
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            tasks = self.make_tasks(folder, names)
            vanished = {**tasks[0], 'name': 'gone', 'path': str(Path(folder) / 'gone')}
            formatspec.run_pool(tasks + [vanished], 2, SETTINGS, lambda t, r: results.__setitem__(t['name'], r), target=spec_pool_targets.misbehaving_worker)
        self.assertEqual(sorted(results), sorted(names + ['gone']))
        self.assertEqual(results['ok_one']['status'], 'ok')
        self.assertEqual(results['hang_after_hash'], {'status': 'timeout', 'sha': 'abc123'})
        self.assertEqual(results['crash_now'], {'status': 'crashed', 'sha': 'def456'})
        self.assertEqual(results['hang_before_hash'], {'status': 'timeout'})
        self.assertEqual(results['ok_three']['status'], 'ok')
        self.assertEqual(results['gone'], {'status': 'io_error'})        # reported by the main process; no worker is involved


    def test_a_worker_that_dies_at_start_is_replaced_and_the_file_is_retried_once(self):
        if sys.platform != 'darwin':
            self.skipTest('the descriptor hand-over is acknowledged only on macOS, where a dead worker raises RuntimeError')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            results = {}
            tasks = self.make_tasks(folder, ['first_file', 'second_file'])
            settings = {**SETTINGS, 'marker': str(folder / 'marker')}
            formatspec.run_pool(tasks, 1, settings, lambda t, r: results.__setitem__(t['name'], r), target=spec_pool_targets.dies_at_first_start)
        self.assertEqual({name: result['status'] for name, result in results.items()}, {'first_file': 'ok', 'second_file': 'ok'})

    def test_a_worker_that_always_dies_at_start_marks_the_file_instead_of_stopping_the_run(self):
        if sys.platform != 'darwin':
            self.skipTest('the descriptor hand-over is acknowledged only on macOS, where a dead worker raises RuntimeError')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            results = {}
            tasks = self.make_tasks(folder, ['only_file'])
            formatspec.run_pool(tasks, 1, SETTINGS, lambda t, r: results.__setitem__(t['name'], r), target=spec_pool_targets.always_dies)
        self.assertEqual(results['only_file']['status'], 'crashed')

    def test_each_task_that_may_hold_an_archive_gets_a_private_folder_that_is_removed_afterwards(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            (folder / 'tmp').mkdir()
            tasks = self.make_tasks(folder, ['a.zip', 'plain.txt', 'hang_after_hash.7z', 'crash_now.rar', 'noextension'])
            seen = {}
            created = []
            original = formatspec.tempfile.mkdtemp
            with mock.patch.object(formatspec.tempfile, 'mkdtemp', lambda **kwargs: created.append(original(**kwargs)) or created[-1]):
                formatspec.run_pool(tasks, 2, {**SETTINGS, 'temp_dir': str(folder / 'tmp')}, lambda t, r: seen.__setitem__(t['name'], r),
                                    target=spec_pool_targets.temp_worker)
            self.assertEqual(sorted(seen), ['a.zip', 'crash_now.rar', 'hang_after_hash.7z', 'noextension', 'plain.txt'])
            self.assertEqual(len(created), 4)                                  # not for plain.txt, which cannot be an archive
            self.assertTrue(all(path.startswith(str(folder / 'tmp')) for path in created))
            self.assertEqual([path for path in created if os.path.exists(path)], [])
            self.assertEqual(seen['a.zip']['wrote'], True)
            self.assertFalse(seen['plain.txt']['wrote'])
            self.assertEqual(seen['hang_after_hash.7z']['status'], 'timeout')
            self.assertEqual(seen['crash_now.rar']['status'], 'crashed')
            self.assertEqual(os.listdir(folder / 'tmp'), [])

    def test_members_from_a_worker_go_to_the_callback_or_are_added_to_the_result(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            tasks = self.make_tasks(folder, ['a.zip'])
            merged = {}
            formatspec.run_pool(tasks, 1, SETTINGS, lambda t, r: merged.update(r), target=spec_pool_targets.members_worker)
            self.assertEqual([m['n'] for m in merged['arch']['members']], ['one', 'two', 'three'])
            batches, finished = [], {}
            formatspec.run_pool(self.make_tasks(folder, ['b.zip']), 1, SETTINGS, lambda t, r: finished.update(r),
                                target=spec_pool_targets.members_worker, on_members=lambda task, start, batch: batches.append((task['name'], start, len(batch))))
            self.assertEqual(batches, [('b.zip', 0, 2), ('b.zip', 2, 1)])
            self.assertNotIn('members', finished['arch'])

    def test_the_worker_count_follows_the_processors_and_the_memory(self):
        gib = 2 ** 30
        self.assertEqual(formatspec.default_workers(cpus=10, ram_bytes=64 * gib), 8)
        self.assertEqual(formatspec.default_workers(cpus=10, ram_bytes=8 * gib), 4)
        self.assertEqual(formatspec.default_workers(cpus=4, ram_bytes=64 * gib), 3)
        self.assertEqual(formatspec.default_workers(cpus=16, ram_bytes=1 * gib), 1)
        self.assertEqual(formatspec.default_workers(cpus=2, ram_bytes=None), 1)
        self.assertGreaterEqual(formatspec.default_workers(), 1)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.settings = {**formatspec.SETTINGS_DEFAULTS, 'tools': {}, 'temp_dir': str(self.base)}

    def make(self, name, data):
        path = self.base / name
        path.write_bytes(data)
        info = path.stat()
        return {'id': name.encode(), 'name': name, 'path': str(path), 'identity': (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)}

    def process(self, job, identity_check=True):
        descriptor, problem = formatspec.open_verified(job['path'], job['identity'] if identity_check else None)
        if problem:
            return {'status': problem}
        stages = []
        result = formatspec.process_task(job, self.settings, stages.append, [descriptor])
        return result if result.get('status') != 'ok' else {**result, 'stages': stages}

    def test_hash_matches_and_a_changed_file_is_reported(self):
        import hashlib
        job = self.make('a.txt', b'hello world ' * 3)
        result = self.process(job)
        self.assertEqual(result['sha'], hashlib.sha256(b'hello world ' * 3).hexdigest())
        self.assertEqual(result['stages'], [result['sha']])
        (self.base / 'a.txt').write_bytes(b'different size now')
        self.assertEqual(self.process(job), {'status': 'changed'})                          # caught when the main process opens it
        self.assertEqual(self.process(job, identity_check=False), {'status': 'changed'})    # and again by the worker itself

    def test_a_link_in_place_of_a_file_is_never_followed(self):
        job = self.make('b.txt', b'real content')
        (self.base / 'b.txt').unlink()
        (self.base / 'target.txt').write_bytes(b'secret target')
        (self.base / 'b.txt').symlink_to(self.base / 'target.txt')
        self.assertEqual(self.process(job), {'status': 'io_error'})
        self.assertEqual(self.process({**job, 'path': str(self.base / 'missing.txt')}), {'status': 'io_error'})
        (self.base / 'folder').mkdir()
        self.assertEqual(self.process({**job, 'path': str(self.base / 'folder')}), {'status': 'io_error'})

    def test_a_large_text_file_is_parsed_from_its_first_2_mb_and_hashed_whole(self):
        import hashlib
        line = (S.RO.replace('\n', ' ') + '\n').encode('utf-8')
        data = line * (5 * 1024 * 1024 // len(line) + 1)
        job = self.make('big.csv', data)
        captured = []
        original = spec_content.content_facts

        def spy(extension, payload, *args, **kwargs):
            captured.append((extension, len(payload)))
            return original(extension, payload, *args, **kwargs)
        with mock.patch.object(spec_content, 'content_facts', spy):
            result = self.process(job)
        self.assertEqual(result['sha'], hashlib.sha256(data).hexdigest())
        self.assertEqual(len(captured), 1)
        self.assertLessEqual(captured[0][1], spec_content.TEXT_HEAD_BYTES)
        self.assertGreater(captured[0][1], spec_content.TEXT_HEAD_BYTES - 8)
        self.assertEqual(result['lang']['lang'], 'ro')
        self.assertNotIn('unreadable', result)

    def test_a_cut_through_a_multibyte_character_still_reads_as_text(self):
        data = ('ăîșț' * 800000).encode('utf-8')           # 6.4 MB; 2 MiB is odd and falls inside a character
        result = self.process(self.make('cut.txt', data))
        self.assertNotIn('unreadable', result)
        self.assertEqual(result['status'], 'ok')

    def test_office_and_pdf_files_are_parsed_from_the_open_file_not_from_a_copy_in_memory(self):
        docx = S.docx_bytes(S.EN * 2, 2015)
        pdf = S.pdf_bytes([('text', S.A4P, 1)])
        kinds = {}
        original = spec_content.content_facts

        def spy(extension, payload, *args, **kwargs):
            kinds[extension] = type(payload).__name__
            return original(extension, payload, *args, **kwargs)
        with mock.patch.object(spec_content, 'content_facts', spy):
            first = self.process(self.make('a.docx', docx))
            self.process(self.make('b.pdf', pdf))
        self.assertEqual(first['lang']['lang'], 'en')
        self.assertEqual(kinds['docx'], 'BufferedReader')
        self.assertEqual(kinds['pdf'], 'BufferedReader')

    def test_a_loose_message_is_still_read_whole(self):
        data = S.eml_bytes('Water plan', 'Ann <ann@example.test>', 'Owner <owner@example.test>', S.RO * 3, year=2022)
        result = self.process(self.make('m.eml', data))
        self.assertEqual(result['mail']['year'], 2022)
        self.assertEqual(result['mail']['lang']['lang'], 'ro')

    def test_a_file_changed_in_place_while_it_is_read_is_marked_changed(self):
        data = b'0123456789' * 30
        job = self.make('live.txt', data)
        path = self.base / 'live.txt'

        def edit(sha):
            time.sleep(0.01)
            with open(path, 'r+b') as handle:             # same size, new content and a new modification time
                handle.write(b'X' * 10)
            os.utime(path, ns=(1, 1))
        descriptor, problem = formatspec.open_verified(job['path'], job['identity'])
        self.assertIsNone(problem)
        self.assertEqual(formatspec.process_task(job, self.settings, edit, [descriptor]), {'status': 'changed'})

    def test_archive_members_go_to_the_callback_in_batches_and_the_result_carries_only_counters(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for number in range(25):
                archive.writestr(f'd/f{number}.txt', f'member number {number} '.encode() * 4)
        job = self.make('many.zip', buffer.getvalue())
        descriptor, _ = formatspec.open_verified(job['path'], job['identity'])
        batches = []
        with mock.patch.object(formatspec, 'MEMBER_BATCH', 10):
            result = formatspec.process_task(job, self.settings, lambda sha: None, [descriptor], emit=lambda start, batch: batches.append((start, len(batch))))
        self.assertEqual(batches, [(0, 10), (10, 10), (20, 5)])
        self.assertNotIn('members', result['arch'])
        self.assertEqual(result['arch']['n'], 25)
        self.assertEqual((result['arch']['opened'], result['arch']['kind']), (1, 'zip'))

    def test_archive_temporary_files_live_in_the_folder_the_parent_made_for_the_task(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('a.txt', S.EN.encode() * 2)
        job = self.make('x.zip', buffer.getvalue())
        mine = self.base / 'mine'
        mine.mkdir()
        job['temp'] = str(mine)
        used = []
        original = formatspec.tempfile.mkdtemp
        with mock.patch.object(formatspec.tempfile, 'mkdtemp', lambda **kwargs: used.append(kwargs['dir']) or original(**kwargs)):
            descriptor, _ = formatspec.open_verified(job['path'], job['identity'])
            formatspec.process_task(job, self.settings, lambda sha: None, [descriptor])
        self.assertEqual(used, [str(mine)])
        self.assertEqual(list(mine.iterdir()), [])

    def test_walk_never_enters_links_hidden_or_credential_folders(self):
        (self.base / 'root').mkdir()
        root = self.base / 'root'
        (root / 'a.txt').write_text('a')
        (root / '.hidden').mkdir()
        (root / '.hidden' / 'x.txt').write_text('x')
        (root / 'Passwords').mkdir()
        (root / 'Passwords' / 'x.txt').write_text('x')
        (root / 'keys.p12').write_text('k')
        (root / 'outside').symlink_to(self.base, target_is_directory=True)
        os.mkfifo(root / 'pipe')
        entries, stats = formatspec.walk_roots([root])
        self.assertEqual([(e.rel, e.credential) for e in entries], [('a.txt', False), ('keys.p12', True)])
        self.assertEqual((stats['hidden_entries'], stats['credential_folders'], stats['symlinks'], stats['special_files']), (1, 1, 1, 1))

    def test_multi_part_archives_are_grouped_once_with_the_first_part_leading(self):
        root = self.base / 'parts'
        root.mkdir()
        sevenzip = b"7z\xbc\xaf'\x1c\x00\x04" + b'\x00' * 40
        for name in ['a.part1.rar', 'a.part2.rar', 'b.rar', 'b.r00', 'b.r01', 'c.zip', 'c.z01', 'e.001', 'lone.002', 'plain.rar']:
            (root / name).write_bytes(b'x')
        (root / 'd.7z.001').write_bytes(sevenzip)
        (root / 'd.7z.002').write_bytes(b'continued')
        entries, _ = formatspec.walk_roots([root])
        roles = {e.name: (e.role, e.style) for e in entries}
        self.assertEqual(roles['a.part1.rar'], ('leader', 'rarpart'))
        self.assertEqual(roles['a.part2.rar'], ('follower', 'rarpart'))
        self.assertEqual(roles['b.rar'], ('leader', 'rar'))
        self.assertEqual(roles['b.r01'], ('follower', 'rar'))
        self.assertEqual(roles['c.zip'], ('leader', 'zip'))
        self.assertEqual(roles['c.z01'], ('follower', 'zip'))
        self.assertEqual(roles['d.7z.001'], ('leader', 'num'))
        self.assertEqual(roles['d.7z.002'], ('follower', 'num'))
        for single in ('e.001', 'lone.002', 'plain.rar'):
            self.assertEqual(roles[single], (None, None))
        leader = next(e for e in entries if e.name == 'c.zip')
        self.assertEqual([os.path.basename(p) for p in leader.parts], ['c.zip', 'c.z01'])

    def test_a_numbered_series_of_ordinary_files_is_not_a_multi_part_archive(self):
        root = self.base / 'logs'
        root.mkdir()
        for name in ['syslog.001', 'syslog.002', 'syslog.003']:
            (root / name).write_bytes(b'Oct 25 12:00:00 host kernel: ordinary log text\n' * 10)
        (root / 'photo.jpg.001').write_bytes(b'\xff\xd8\xff\xe0' + b'\x00' * 20)
        (root / 'photo.jpg.002').write_bytes(b'\x00' * 20)
        entries, stats = formatspec.walk_roots([root])
        self.assertEqual({e.name: (e.role, e.style) for e in entries},
                         {name: (None, None) for name in ['syslog.001', 'syslog.002', 'syslog.003', 'photo.jpg.001', 'photo.jpg.002']})
        self.assertEqual(stats['orphan_volumes'], 0)

    def test_volumes_without_a_first_part_are_orphans_and_counted(self):
        root = self.base / 'orphans'
        root.mkdir()
        for name in ['o.part2.rar', 'p.part2.rar', 'p.part3.rar', 'q.r00', 'q.r01', 'r.z01', 's.7z.002', 's.7z.003', 'lone.002', 'fine.rar']:
            (root / name).write_bytes(b'x')
        entries, stats = formatspec.walk_roots([root])
        roles = {e.name: e.role for e in entries}
        for name in ['o.part2.rar', 'p.part2.rar', 'p.part3.rar', 'q.r00', 'q.r01', 'r.z01', 's.7z.002', 's.7z.003']:
            self.assertEqual(roles[name], 'orphan', name)
        self.assertIsNone(roles['lone.002'])
        self.assertIsNone(roles['fine.rar'])
        self.assertEqual(stats['orphan_volumes'], 8)


if __name__ == '__main__':
    unittest.main()
