"""Optional visual calibration sample for the drawing-sheet figures.

Writes low-resolution page images of a random stratified sample of candidate and
non-candidate pages into a private folder. The images never leave this machine
unless the partner lets the agent view them. Nothing here uploads anything.
"""
import json
import os
import random
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

import spec_content
from formatspec import CACHE_NAME, _open_regular
from profiler import checked_path
from spec_aggregate import AUTO_DRAWING, DRAWING_DEFAULTS, SCHEMATIC_DEFAULTS
from spec_taxonomy import A4_LIKE, matches_any, raw_extension

FOLDER = 'calibration.private'
MAX_SIDE = 1000
STRATA = {'A4': 'A4 and letter', 'letter': 'A4 and letter', 'legal': 'A4 and letter', 'other': 'A4 and letter', 'A3': 'A3', 'tabloid': 'A3',
          'A2': 'A2', 'A1': 'A1 and A0', 'A0': 'A1 and A0', 'oversize': 'oversize', 'image': 'image'}


def is_candidate(name, vector, named):
    """Whether a page counts as a drawing sheet by the rules of the specification."""
    if name in AUTO_DRAWING:
        return True
    if name in A4_LIKE or name == 'other':
        return bool(vector or named)
    return False


def _slots(db, config, roots):
    """Candidate and non-candidate (file, class, pages) slots from the cache, loose files outside mailboxes only."""
    drawing = config.get('drawing_patterns') or DRAWING_DEFAULTS
    schematic = config.get('schematic_patterns') or SCHEMATIC_DEFAULTS
    candidates, others = defaultdict(list), defaultdict(list)
    for identifier, root, path, size, mtime_ns, result in db.execute('SELECT id, root, path, size, mtime_ns, result FROM files ORDER BY id'):
        relative = os.fsdecode(path)
        extension = raw_extension(relative)
        data = json.loads(result)
        facts = data.get('pdf') if extension == 'pdf' else data.get('img')
        if not facts or facts.get('unchecked') or not facts.get('classes') or data.get('status') != 'ok':
            continue
        if any(m['root'] == root and (not m['prefix'] or relative.startswith(m['prefix'] + '/')) for m in config.get('mailboxes', [])):
            continue
        named = matches_any(drawing, relative) or matches_any(schematic, relative)
        for key, (count, vector) in facts['classes'].items():
            if key in AUTO_DRAWING:
                found = count
            elif key in A4_LIKE or key == 'other':
                found = count if named else vector
            else:
                found = count if named else 0
            if found:
                candidates[STRATA.get(key, 'other')].append((root, relative, size, mtime_ns, key, found))
            rest = count - found
            if rest and key != 'image' and (key in A4_LIKE or key == 'other'):
                others[STRATA.get(key, 'other')].append((root, relative, size, mtime_ns, key, rest))
    return candidates, others, drawing, schematic


def _allocate(strata, total, rng):
    """Whole pages per stratum: at least one for each non-empty stratum, the rest in proportion to the pages held."""
    keys = sorted(key for key, slots in strata.items() if slots)
    if not keys or total <= 0:
        return {}
    weight = {key: sum(slot[5] for slot in strata[key]) for key in keys}
    chosen = {key: 1 for key in keys[:total]}
    remaining = total - sum(chosen.values())
    while remaining > 0:
        key = rng.choices(keys, weights=[weight[k] for k in keys])[0]
        chosen[key] = chosen.get(key, 0) + 1
        remaining -= 1
    return chosen


def _pick(slots, count, rng):
    """Pick slots weighted by the pages they hold; one page per pick, distinct files first."""
    picked, used = [], set()
    pool = list(slots)
    while pool and len(picked) < count:
        slot = rng.choices(pool, weights=[item[5] for item in pool])[0]
        pool.remove(slot)
        if (slot[0], slot[1]) in used:
            continue
        used.add((slot[0], slot[1]))
        picked.append(slot)
    return picked


def _render_pdf(stream, index):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(stream)
    try:
        page = document[index]
        try:
            width, height = page.get_size()
            scale = min(1.0, MAX_SIDE / max(width, height)) * 1.0
            return page.render(scale=max(scale, 0.05)).to_pil().convert('L')
        finally:
            page.close()
    finally:
        document.close()


def sample_pages(directory, count=60, seed=20261025):
    """Write sample page images and their candidate flags; returns the number of images."""
    directory = checked_path(directory)
    cache = directory / CACHE_NAME
    if not cache.is_file():
        raise ValueError('Run the specification step first.')
    db = sqlite3.connect(f'file:{cache}?mode=ro', uri=True)
    try:
        roots = json.loads(db.execute("SELECT value FROM meta WHERE key='roots'").fetchone()[0])
        config = json.loads(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
        candidates, others, drawing, schematic = _slots(db, config, roots)
    finally:
        db.close()
    rng = random.Random(f'{seed}:{count}')
    half = max(1, count // 2)
    plan = []
    for flag, strata, share in ((True, candidates, half), (False, others, count - half)):
        for stratum, number in sorted(_allocate(strata, share, rng).items()):
            for slot in _pick(strata[stratum], number, rng):
                plan.append((flag, slot))
    target = directory / FOLDER
    if target.exists():
        shutil.rmtree(target)
    os.mkdir(target, 0o700)
    index, written = [], 0
    for flag, (root, relative, size, mtime_ns, key, found) in plan:
        path = Path(roots[root]) / relative if Path(roots[root]).is_dir() else Path(roots[root])
        stream, problem = _open_regular(str(path), None)
        if stream is None:
            continue
        with stream:
            info = os.fstat(stream.fileno())
            if info.st_size != size or info.st_mtime_ns != mtime_ns:
                continue
            extension = raw_extension(relative)
            try:
                if extension == 'pdf':
                    rows = spec_content.page_candidates(stream)
                    named = matches_any(drawing, relative) or matches_any(schematic, relative)
                    wanted = [page for page, name, vector, larger in rows
                              if is_candidate(name, vector, named) == flag and STRATA.get(name) == STRATA.get(key)]
                    if not wanted:
                        continue
                    stream.seek(0)
                    image = _render_pdf(stream, rng.choice(wanted))
                else:
                    from PIL import Image
                    Image.MAX_IMAGE_PIXELS = None
                    image = Image.open(stream)
                    image.thumbnail((MAX_SIDE, MAX_SIDE))
                    image = image.convert('L')
            except Exception:
                continue
        written += 1
        name = f'sample-{written:03d}.png'
        image.save(target / name, format='PNG')
        os.chmod(target / name, 0o600)
        index.append({'file': name, 'candidate': flag})
    (target / 'index.private.json').write_text(json.dumps({'images': index}, indent=2) + '\n')
    os.chmod(target / 'index.private.json', 0o600)
    return written
