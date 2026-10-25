"""Worker pool: deadlines, crashes and the per-file read primitives."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_pool_targets

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
        for name in ['a.part1.rar', 'a.part2.rar', 'b.rar', 'b.r00', 'b.r01', 'c.zip', 'c.z01', 'd.7z.001', 'd.7z.002', 'e.001', 'lone.002', 'plain.rar']:
            (root / name).write_bytes(b'x')
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


if __name__ == '__main__':
    unittest.main()
