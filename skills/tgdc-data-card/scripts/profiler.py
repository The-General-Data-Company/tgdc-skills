"""Read-only inventory and evidence aggregation for explicitly selected roots.

The inventory is every selected file, from the same walk as the full pass. Content
is examined on a bounded sample spread evenly over that whole inventory.
Parser/language workers receive bounded bytes, never paths. Workers are terminated
at a wall-clock deadline. No body text reaches the result or private manifest.
"""
import importlib.metadata
import json
import math
import multiprocessing
import os
import re
import stat
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from extractors import DEFAULT_LIMITS as PARSER_LIMITS, SUPPORTED, empty_result, extract
from questions import questions
import portable
import spec_language

DEFAULT_LIMITS = dict(PARSER_LIMITS)
SAMPLE_READERS = 8           # sampled files read and parsed at the same time
# 'accounts' is not excluded: in a business archive it is accounting, not a credential store.
EXCLUDED_NAMES = {'credentials', 'credential', 'secrets', 'secret', 'tokens', 'token',
                  'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'}
EXCLUDED_EXTENSIONS = {'pem', 'key', 'p12', 'pfx', 'keystore'}
PUBLIC_EXTENSIONS = SUPPORTED | frozenset({'dwg', 'dxf', 'dgn', 'step', 'stp', 'stl', 'ifc',
    'xls', 'xlsb', 'xlsm', 'doc', 'ppt', 'pptx', 'rtf', 'odt', 'ods', 'odp',
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tif', 'tiff', 'webp', 'heic', 'svg',
    'mp3', 'wav', 'flac', 'mp4', 'mov', 'avi', 'mkv', 'zip', 'gz', 'tar', '7z', 'rar',
    'xml', 'html', 'htm', 'sql', 'sqlite', 'db', 'parquet', 'avro', 'orc', 'log',
    'yaml', 'yml', 'ini', 'dat', 'bin', 'msg', 'pst', 'mbox'})
SUCCESS = {'ok', 'capped'}
ERRORS = {'malformed', 'binary', 'timeout', 'dependency_missing', 'io_error', 'changed'}
LANGUAGE_METHOD = 'py3langid 0.4.0 proposes a language from the first 10000 sampled text characters (normalized model score >=0.90; >=80 alphabetic characters and >=12 words); the proposal counts only when a common-word list or the script of the text confirms it, the same rule as the data format specification, otherwise the file is unknown; dominant document language inference (score is not calibrated probability)'


class UnsupportedPlatform(ValueError):
    """The platform cannot provide the scanner's required no-follow semantics."""


def require_supported_platform():
    if portable.WINDOWS:
        return
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
            or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd):
        raise UnsupportedPlatform('Profiling requires Windows, macOS or Linux; this system lacks directory descriptors and no-follow operations.')


def validate_limits(overrides):
    limits = {**DEFAULT_LIMITS, **(overrides or {})}
    if set(limits) != set(DEFAULT_LIMITS):
        raise ValueError('Unknown limit option.')
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError('Limits must be positive numbers.')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Limits must be finite.')
        if key != 'parser_seconds' and not isinstance(value, int):
            raise ValueError('Count and size limits must be integers.')
    return limits


def checked_path(path):
    if portable.WINDOWS:
        return portable.canonical(path)          # final \\?\ form; links and junctions on the way raise ValueError
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('Symlink roots, parents and outputs are not permitted.')
    return path


def open_nofollow(path, directory=False):
    """Anchor every ancestor with directory descriptors on POSIX; never follow links. Windows checks the final path instead."""
    if portable.WINDOWS:
        return portable.open_nofollow_windows(path, directory)
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    directory_flag = getattr(os, 'O_DIRECTORY', 0)
    require_supported_platform()
    descriptor = os.open(path.anchor, flags | directory_flag)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, flags | directory_flag, dir_fd=descriptor)
            os.close(descriptor); descriptor = following
        return os.open(path.name, flags | (directory_flag if directory else getattr(os, 'O_NONBLOCK', 0)), dir_fd=descriptor)
    finally:
        os.close(descriptor)


def excluded(path):
    stem = path.stem.lower()
    credential_words = {'credential', 'credentials', 'secret', 'secrets', 'token', 'tokens',
                        'password', 'passwords', 'apikey', 'privatekey'}
    return (path.name.startswith('.') or stem in EXCLUDED_NAMES
            or bool(set(re.split(r'[-_.]', stem)) & credential_words)
            or stem.startswith(('service-account', 'service_account', 'client_secret', 'oauth'))
            or path.suffix.lower().lstrip('.') in EXCLUDED_EXTENSIONS)


def extension(name):
    """Public format of a file name: its known lower-case suffix, else 'other'."""
    suffix = source_extension(name)
    return suffix if suffix in PUBLIC_EXTENSIONS else 'other'


def source_extension(name):
    return os.path.splitext(name)[1].lower().lstrip('.')


def iso_date(seconds):
    """A file date as ISO text in UTC. Windows refuses fromtimestamp before 1970 and after 3000; such a date is computed from the
    epoch instead, clamped into the calendar."""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=min(max(seconds, -62135596800), 253402300799))).isoformat()


def inventory(roots):
    """Every selected file and what the walk skipped: the walk of the full pass, so both parts of a card count the same files.
    There is no file limit."""
    from formatspec import walk_roots        # imported here: formatspec imports this module
    return walk_roots(roots)


def stratified_sample(groups, maximum):
    """Positions of the files whose content is examined. groups maps each supported extension to the positions of its files in
    path order. Every extension gets a share in turn, and the positions within an extension are evenly spaced over the whole
    inventory, so no folder is preferred."""
    quotas = Counter(); remaining = min(maximum, sum(map(len, groups.values())))
    while remaining:
        for kind in sorted(groups):
            if quotas[kind] < len(groups[kind]):
                quotas[kind] += 1; remaining -= 1
                if not remaining:
                    break
    selected = set()
    for kind, group in groups.items():
        count = quotas[kind]
        for index in range(count):
            selected.add(group[((2 * index + 1) * len(group)) // (2 * count)])
    return selected


def detect_language(text):
    """(language, reason): the language only when the specification's rule would confirm it too."""
    code, reason = spec_language.assess(text)
    return (code, 'model_inference') if code else ('unknown', reason)


def parser_worker(connection, kind, data, limits):
    # Suppress parser diagnostics: malformed source text must not leak to CLI.
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 1); os.dup2(sink.fileno(), 2)
        try:
            result = extract(kind, data, limits)
            text = result.pop('_text', '')
            result['language'], result['language_reason'] = detect_language(text)
            connection.send(result)
        except Exception:
            connection.send(empty_result('malformed'))
        finally:
            connection.close()


def bounded_extract(kind, data, limits):
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=parser_worker, args=(writer, kind, data, limits))
    try:
        process.start(); writer.close()
        if reader.poll(limits['parser_seconds']):
            try:
                result = reader.recv()
            except EOFError:
                result = empty_result('malformed')
        else:
            result = empty_result('timeout')
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.kill(); process.join(timeout=1)
        reader.close(); writer.close()
    return result


def read_complete(path, expected, maximum):
    descriptor = open_nofollow(path)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError('Source is not regular.')
        identity = portable.identity(before)
        if identity != expected:
            return None, 'changed'
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        if len(data) > maximum:
            return None, 'size_cap'
        if portable.identity(after) != identity:
            return None, 'changed'
    return data, None


def output_directory(path):
    """Create/open ancestors relative to descriptors; no pathname write can follow links."""
    if portable.WINDOWS:
        return portable.output_directory_windows(path)
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('Safe output requires POSIX descriptor operations.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                following = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def verify_output_directory(path, descriptor):
    current = open_nofollow(path, directory=True)
    try:
        original_info, current_info = os.fstat(descriptor), os.fstat(current)
        if (original_info.st_dev, original_info.st_ino) != (current_info.st_dev, current_info.st_ino):
            raise ValueError('Selected output directory changed during profiling.')
    finally:
        os.close(current)


def write_json(directory, filename, payload, private=False):
    """Atomically replace JSON within an anchored output fd, including temp cleanup."""
    temporary = '.datacard-' + uuid.uuid4().hex
    descriptor = portable.open_at(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            portable.fchmod(stream.fileno(), 0o600 if private else 0o644)
            json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False); stream.write('\n')
        portable.replace_at(directory, temporary, filename)
    finally:
        try:
            portable.unlink_at(directory, temporary)
        except FileNotFoundError:
            pass


def metric(value, unit, denominator, method, coverage, basis='measured'):
    return {'value': value, 'unit': unit, 'basis': basis, 'denominator': denominator,
            'method': method, 'coverage': coverage}


def prepare_roots(sources, output):
    """Validate explicit roots and a separate output directory; return (roots, output)."""
    roots = [checked_path(source) for source in sources]
    output = checked_path(output)
    if not roots or any(not root.exists() or not (root.is_dir() or root.is_file()) for root in roots):
        raise ValueError('Select existing regular files or directories.')
    if any(excluded(root) for root in roots):
        raise ValueError('Hidden and credential roots are excluded by default.')
    for index, root in enumerate(roots):
        if any(root == other or root in other.parents or other in root.parents for other in roots[index + 1:]):
            raise ValueError('Selected roots must not overlap.')
        # Single-file selections also prohibit writing into their containing folder.
        protected = root if root.is_dir() else root.parent
        if output == protected or protected in output.parents or output in protected.parents:
            raise ValueError('Output must be separate from selected input.')
    if output.exists() and not output.is_dir():
        raise ValueError('Output must be a directory.')
    return roots, output


def examine(entry, kind, limits):
    """Evidence of one sampled file: read at most the file size limit, then parse in a worker with a deadline."""
    if entry.size > limits['max_file_bytes']:
        return empty_result('size_cap')
    try:
        data, read_error = read_complete(entry.path, (entry.dev, entry.ino, entry.size, entry.mtime_ns), limits['max_file_bytes'])
    except OSError:
        return empty_result('io_error')
    if read_error:
        return empty_result(read_error)
    return bounded_extract(kind, data, limits)


def profile(sources, output, limits=None, walked=None, totals=None):
    """Return and save the aggregate profile; save the local-only manifest of the examined sample separately.

    walked: (roots, entries, walk statistics) of a walk the caller has already made, so that the full pass and the profile
    describe the same files. Without it the selected roots are walked here.
    totals: the totals of the full pass over the same walk; they give the count of exact duplicate copies."""
    require_supported_platform()
    limits = validate_limits(limits)
    if walked is None:
        roots, output = prepare_roots(sources, output)
        entries, walk = inventory(roots)
    else:
        roots, entries, walk = walked
        output = checked_path(output)
    complete = not (walk.get('unreadable_folders') or walk.get('unreadable_entries'))
    statuses = Counter({key: value for key, value in (
        ('excluded', walk.get('hidden_entries', 0) + walk.get('credential_folders', 0)), ('symlink', walk.get('symlinks', 0)),
        ('special_file', walk.get('special_files', 0)), ('io_error', walk.get('unreadable_folders', 0) + walk.get('unreadable_entries', 0))) if value})
    kinds = defaultdict(lambda: {'files': 0, 'bytes': 0})
    supported = defaultdict(list)
    total_bytes = 0; earliest = latest = None
    for position, entry in enumerate(entries):
        kind = extension(entry.name)
        kinds[kind]['files'] += 1; kinds[kind]['bytes'] += entry.size; total_bytes += entry.size
        if earliest is None or entry.mtime_ns < earliest:
            earliest = entry.mtime_ns
        if latest is None or entry.mtime_ns > latest:
            latest = entry.mtime_ns
        if entry.credential:                 # counted, never opened for content
            statuses['excluded'] += 1
        elif kind in SUPPORTED:
            supported[kind].append(position)
        else:
            statuses['unsupported'] += 1
    supported_files = sum(map(len, supported.values()))
    selected = sorted(stratified_sample(supported, limits['max_sample_files']))
    if supported_files > len(selected):
        statuses['sample_cap'] += supported_files - len(selected)
    language_counts = Counter(); language_reasons = Counter()
    attempted = succeeded = 0
    rows = pages = characters = words = no_text_pages = 0
    row_files = pdf_files = text_files = complete_text_files = files_with_extracted_text = 0
    known_row_totals = known_page_totals = 0
    row_totals = page_totals = 0; content_dates = []
    private_files = []
    with ThreadPoolExecutor(max_workers=SAMPLE_READERS) as readers:
        evidences = list(readers.map(lambda position: examine(entries[position], extension(entries[position].name), limits), selected))
    for position, evidence in zip(selected, evidences):
        entry = entries[position]; kind = extension(entry.name)
        attempted += 1
        language = evidence.get('language', 'unknown')
        language_counts[language] += 1
        if language == 'unknown':
            language_reasons[evidence.get('language_reason', evidence['status'])] += 1
        statuses[evidence['status']] += 1
        if evidence['status'] in SUCCESS:
            succeeded += 1; text_files += 1
            complete_text_files += bool(evidence['text_complete'])
            files_with_extracted_text += bool(evidence.get('has_extracted_text', False))
            rows += evidence['rows_processed']; pages += evidence['pages_processed']
            characters += evidence['characters']; words += evidence['words']
            no_text_pages += evidence['no_text_pages']
            if kind in ('csv', 'tsv', 'json', 'jsonl', 'xlsx'):
                row_files += 1
                if evidence['rows_total'] is not None:
                    known_row_totals += 1; row_totals += evidence['rows_total']
            if kind == 'pdf':
                pdf_files += 1
                if evidence['pages_total'] is not None:
                    known_page_totals += 1; page_totals += evidence['pages_total']
            if evidence['content_date']:
                content_dates.append(evidence['content_date'])
        private_files.append({'path': entry.path, 'extension': kind, 'source_extension': source_extension(entry.name), 'bytes': entry.size,
                              'mtime': iso_date(entry.mtime_ns / 1e9), 'status': evidence['status'], 'evidence': evidence})
    files = len(entries)
    scope_coverage = {'inventory_files': files, 'enumeration_complete': complete}
    hashed = totals['hashed_files'] if totals else 0
    sample_coverage = {**scope_coverage, 'sampled_files': attempted, 'successful_files': succeeded, 'selection': 'deterministic extension-stratified round-robin quotas; evenly spaced paths per extension over the whole inventory'}
    text_coverage = {**sample_coverage, 'complete_text_files': complete_text_files, 'text_character_limit_per_document': limits['max_characters']}
    row_coverage = {**sample_coverage, 'record_files': row_files, 'files_with_known_total': known_row_totals}
    page_coverage = {**sample_coverage, 'pdf_files': pdf_files, 'files_with_known_total': known_page_totals}
    languages_method = LANGUAGE_METHOD
    try:
        installed_language_version = importlib.metadata.version('py3langid')
        if installed_language_version != '0.4.0':
            languages_method = languages_method.replace('0.4.0', installed_language_version)
    except importlib.metadata.PackageNotFoundError:
        languages_method = 'unavailable; language unknown (install pinned py3langid dependency)'
    english_count = language_counts.get('en', 0)
    metrics = {
        'selected_files': metric(files, 'files', files, 'every regular non-hidden file in the selected roots; credential-like names are counted, never opened', scope_coverage),
        'documents_processed': metric(succeeded, 'documents', attempted, 'successfully extracted sampled files, including tables; not case counts', sample_coverage),
        'files_with_extracted_text': metric(files_with_extracted_text if attempted else None, 'files', attempted, 'successfully parsed sampled files with non-whitespace text in retained extraction sample; not a readability or content quality judgment', text_coverage),
        'selected_bytes': metric(total_bytes, 'bytes', files, 'filesystem file sizes', scope_coverage),
        'format_file_shares': metric({kind: counts['files'] / files for kind, counts in sorted(kinds.items())} if files else None, 'fraction', files, 'file count by normalized extension divided by enumerated file count', scope_coverage),
        'duplicate_files': metric(hashed - totals['unique_contents'] if hashed else None, 'files', hashed, 'exact full-byte SHA-256 duplicates among every file the full pass hashed, on disk and inside archives; extra copies beyond one per hash', {**scope_coverage, 'hashed_files': hashed}),
        'rows_processed': metric(rows if row_files else None, 'records', row_files, 'CSV/XLSX data rows exclude first non-empty header; JSON object=1, array elements, JSONL nonblank lines; processed sample only', row_coverage),
        'rows_known_total': metric(row_totals if known_row_totals else None, 'records', known_row_totals, 'sum of known row totals only; never a full selected archive count', row_coverage),
        'pdf_pages_processed': metric(pages if pdf_files else None, 'pages', pdf_files, 'pages inspected in sampled successfully parsed PDFs', page_coverage),
        'pdf_pages_known_total': metric(page_totals if known_page_totals else None, 'pages', known_page_totals, 'PDF page-tree totals from successfully parsed sample, distinct from processed pages', page_coverage),
        'pdf_no_text_pages': metric(no_text_pages if pdf_files else None, 'pages', pages, 'processed PDF pages yielding no extractable text; OCR candidate only, does not prove scan', page_coverage),
        'extracted_characters': metric(characters if text_files else None, 'characters', text_files, 'Unicode characters in bounded extracted text samples; includes separators', text_coverage),
        'extracted_words': metric(words if text_files else None, 'words', text_files, 'Unicode word-regex matches in extracted text samples; not tokens', text_coverage),
        'filesystem_mtime_bounds': metric({'earliest': iso_date(earliest / 1e9), 'latest': iso_date(latest / 1e9)} if files else None, 'UTC datetime bounds', files, 'filesystem last-modified timestamps; not content creation or history', scope_coverage),
        'eml_content_date_bounds': metric({'earliest': min(content_dates), 'latest': max(content_dates)} if content_dates else None, 'UTC datetime bounds', len(content_dates), 'parseable timezone-aware Date headers in sampled EML files; header assertions, not verified event dates', sample_coverage),
        'english_document_share': metric(english_count / attempted if attempted and attempted > language_counts.get('unknown', 0) else None, 'fraction', attempted, languages_method + '; classified-English fraction of attempted sample, including unknown and failed files in denominator; null when all languages unknown; classification may be incorrect; no population or true-language bound estimate', {**sample_coverage, 'unknown_documents': language_counts.get('unknown', 0)}, basis='inferred'),
    }
    limitations = [
        'Only explicitly selected local roots are profiled; archive completeness, rights and provider context require answers.',
        'The inventory counts every selected file. Skipped counts include encountered excluded filesystem entries; hidden subtrees and folders with a credential-like name are not traversed and their file counts are unknown. Credential exclusions use known filename patterns, not a complete secret or privacy audit.',
        'No networking, OCR, native CAD or legacy Office extraction; unsupported files remain inventory only.',
        'Known format extensions are counted separately; arbitrary or absent filename suffixes are grouped as other in shared inventory to avoid disclosing identifiers. Exact suffixes remain private.',
        'Text is a bounded sample. Words and characters are measured; tokens, cases and workflow completion are not inferred.',
        'Language is a model inference on document text samples, confirmed by a common-word or script check; normalized model scores are not statistical confidence, mixed-language proportions or full-archive estimates.',
        'CSV/XLSX first non-empty rows are treated as headers. Worksheet dimensions are declared metadata, not populated row totals; XLSX formulas use cached values only.',
        'DOCX extraction covers the main document only; headers, footers, comments, images and embedded objects are not extracted.',
        'EML extracts one preferred body, excludes attachments, and hashes message references privately; references do not establish complete cases.',
        'Parser deadlines start after source reading; ordinary filesystem/storage operations have no wall-clock read deadline. Non-regular replacements are opened without blocking and rejected.',
        'This local scanner does not anonymize content or guarantee offline isolation of the host model or application.',
    ]
    if not complete:
        limitations.append('Some folders or entries could not be read; inventory values describe only the readable part of the selection.')
    if len(selected) < supported_files:
        limitations.append('Content extraction was sampled; unsampled supported files have status sample_cap.')
    if any(statuses[status] for status in ('capped', 'size_cap', 'decompression_cap', 'timeout')):
        limitations.append('Some inputs reached parser, content, size or time limits; processed quantities must not be presented as full totals.')
    errors = sum(statuses[status] for status in ERRORS)
    skipped = sum(statuses.values()) - succeeded - errors
    result = {
        'schema_version': '1.0', 'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': {'kind': 'selected_files', 'selected_root_count': len(roots), 'enumeration_complete': complete, 'limits': limits, 'selection_method': sample_coverage['selection'], 'hidden_and_credentials_excluded': True, 'symlinks_followed': False},
        'inventory': {'files': files, 'bytes': total_bytes, 'by_extension': dict(sorted(kinds.items()))},
        'extraction': {'attempted': attempted, 'succeeded': succeeded, 'errors': errors, 'skipped': skipped, 'skipped_basis': 'encountered inventory files and excluded filesystem entries; unvisited subtree file counts unknown', 'status_counts': dict(sorted(statuses.items()))},
        'metrics': metrics,
        'languages': {'document_counts': dict(sorted(language_counts.items())), 'denominator': attempted, 'unknown_documents': language_counts.get('unknown', 0), 'method': languages_method, 'basis': 'inferred', 'coverage': sample_coverage, 'unknown_reasons': dict(sorted(language_reasons.items()))},
        'limitations': limitations, 'questions': questions(),
    }
    checked_path(output)
    output_fd = output_directory(output)
    manifest = {'schema_version': '1.0', 'generated_at': result['generated_at'], 'privacy': 'local_only_do_not_share', 'sources': [str(root) for root in roots],
                'files': private_files, 'files_basis': 'the examined content sample only; the private cache of the full pass holds the record of every file',
                'excluded_status_counts': {key: statuses[key] for key in ('excluded', 'io_error', 'special_file', 'symlink') if statuses[key]}, 'limits': limits}
    try:
        verify_output_directory(output, output_fd)
        write_json(output_fd, 'manifest.private.json', manifest, private=True)
        write_json(output_fd, 'profile.json', result)
        verify_output_directory(output, output_fd)
    finally:
        os.close(output_fd)
    return result
