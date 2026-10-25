"""Read-only inventory and evidence aggregation for explicitly selected roots.

Parser/language workers receive bounded bytes, never paths. Workers are terminated
at a wall-clock deadline. No body text reaches the result or private manifest.
"""
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
import re
import stat
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from extractors import DEFAULT_LIMITS as PARSER_LIMITS, SUPPORTED, empty_result, extract
from questions import questions
import spec_language

DEFAULT_LIMITS = {**PARSER_LIMITS, 'max_hash_bytes': 1024 * 1024 * 1024}
EXCLUDED_NAMES = {'credentials', 'credential', 'secrets', 'secret', 'tokens', 'token',
                  'accounts', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'}
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
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
            or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd):
        raise UnsupportedPlatform('Profiling requires macOS or POSIX/Linux with directory descriptors and no-follow operations. Windows is unsupported in v1.')


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
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('Symlink roots, parents and outputs are not permitted.')
    return path


def open_nofollow(path, directory=False):
    """Anchor every ancestor with directory descriptors on POSIX; never follow links."""
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


def extension(path):
    suffix = path.suffix.lower().lstrip('.')
    return suffix if suffix in PUBLIC_EXTENSIONS else 'other'


def inventory(roots, limits):
    files = []; excluded_counts = Counter(); complete = True
    # Directory/file entries also have a bound, preventing empty-directory floods.
    entry_cap = limits['max_files'] * 10
    visited_entries = 0

    def add_file(path, details):
        files.append({'path': str(path), 'extension': extension(path), 'source_extension': path.suffix.lower().lstrip('.'), 'bytes': details.st_size,
                      'mtime': datetime.fromtimestamp(details.st_mtime, timezone.utc).isoformat(),
                      '_identity': (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)})

    def visit(path):
        nonlocal complete, visited_entries
        if len(files) >= limits['max_files'] or visited_entries >= entry_cap:
            complete = False; return
        if excluded(path):
            excluded_counts['excluded'] += 1; return
        try:
            descriptor = open_nofollow(path, directory=True)
        except OSError:
            excluded_counts['io_error'] += 1; complete = False; return
        try:
            # Read at most the remaining bounded entry budget before sorting.
            entries = []
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if visited_entries + len(entries) >= entry_cap:
                        complete = False; break
                    entries.append(entry)
            entries.sort(key=lambda entry: entry.name)
            for entry in entries:
                if len(files) >= limits['max_files']:
                    complete = False; break
                visited_entries += 1
                child = path / entry.name
                if entry.is_symlink():
                    excluded_counts['symlink'] += 1; continue
                if excluded(child):
                    excluded_counts['excluded'] += 1; continue
                try:
                    details = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(details.st_mode):
                        visit(child)
                    elif stat.S_ISREG(details.st_mode):
                        add_file(child, details)
                    else:
                        excluded_counts['special_file'] += 1
                except OSError:
                    excluded_counts['io_error'] += 1; complete = False
        finally:
            os.close(descriptor)

    for root in roots:
        if root.is_dir():
            visit(root)
        elif excluded(root):
            excluded_counts['excluded'] += 1
        elif len(files) >= limits['max_files']:
            complete = False
        else:
            details = root.stat(follow_symlinks=False)
            add_file(root, details)
    return sorted(files, key=lambda item: item['path']), excluded_counts, complete


def stratified_sample(files, maximum):
    groups = defaultdict(list)
    for index, item in enumerate(files):
        if item['extension'] in SUPPORTED:
            groups[item['extension']].append(index)
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
        # Even positions within each extension, avoiding a first-folder sample.
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


def read_complete(item, maximum):
    descriptor = open_nofollow(item['path'])
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError('Source is not regular.')
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != item['_identity']:
            return None, 'changed'
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        if len(data) > maximum:
            return None, 'size_cap'
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity:
            return None, 'changed'
    return data, None


def output_directory(path):
    """Create/open ancestors relative to descriptors; no pathname write can follow links."""
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
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600 if private else 0o644, dir_fd=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600 if private else 0o644)
            json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False); stream.write('\n')
        os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
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


def profile(sources, output, limits=None):
    """Return and save aggregate profile; save local-only manifest separately."""
    require_supported_platform()
    limits = validate_limits(limits)
    roots, output = prepare_roots(sources, output)
    files, exclusions, complete = inventory(roots, limits)
    selected = stratified_sample(files, limits['max_sample_files'])
    statuses = Counter(exclusions); language_counts = Counter(); language_reasons = Counter()
    hashes = []; hashed_bytes = 0; attempted = succeeded = 0
    rows = pages = characters = words = no_text_pages = 0
    row_files = pdf_files = text_files = complete_text_files = files_with_extracted_text = 0
    known_row_totals = known_page_totals = 0
    row_totals = page_totals = 0; content_dates = []
    private_files = []
    for index, item in enumerate(files):
        item['sha256'] = None
        item['hash_status'] = 'file_size_cap' if item['bytes'] > limits['max_file_bytes'] else 'total_hash_byte_cap'
        data = None; read_error = None
        allowed_hash = item['bytes'] <= limits['max_file_bytes'] and hashed_bytes + item['bytes'] <= limits['max_hash_bytes']
        if allowed_hash or index in selected:
            if item['bytes'] <= limits['max_file_bytes']:
                try:
                    data, read_error = read_complete(item, limits['max_file_bytes'])
                except OSError:
                    read_error = 'io_error'
            else:
                read_error = 'size_cap'
        if data is not None and allowed_hash:
            item['sha256'] = hashlib.sha256(data).hexdigest(); item['hash_status'] = 'complete'; hashes.append(item['sha256']); hashed_bytes += len(data)
        if read_error:
            item['hash_status'] = read_error
        kind = item['extension']
        if kind not in SUPPORTED:
            evidence = empty_result('unsupported')
        elif index not in selected:
            evidence = empty_result('sample_cap')
        else:
            attempted += 1
            if read_error:
                evidence = empty_result(read_error)
            else:
                evidence = bounded_extract(kind, data, limits)
            language = evidence.get('language', 'unknown')
            language_counts[language] += 1
            if language == 'unknown':
                language_reasons[evidence.get('language_reason', evidence['status'])] += 1
        item['status'] = evidence['status']; item['evidence'] = evidence
        statuses[item['status']] += 1
        if item['status'] in SUCCESS:
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
        item.pop('_identity'); private_files.append(item)
    kinds = defaultdict(lambda: {'files': 0, 'bytes': 0})
    for item in files:
        kinds[item['extension']]['files'] += 1; kinds[item['extension']]['bytes'] += item['bytes']
    scope_coverage = {'inventory_files': len(files), 'enumeration_complete': complete}
    sample_coverage = {**scope_coverage, 'sampled_files': attempted, 'successful_files': succeeded, 'selection': 'deterministic extension-stratified round-robin quotas; evenly spaced paths per extension'}
    text_coverage = {**sample_coverage, 'complete_text_files': complete_text_files, 'text_character_limit_per_document': limits['max_characters']}
    row_coverage = {**sample_coverage, 'record_files': row_files, 'files_with_known_total': known_row_totals}
    page_coverage = {**sample_coverage, 'pdf_files': pdf_files, 'files_with_known_total': known_page_totals}
    mtime = sorted(item['mtime'] for item in files)
    hash_coverage = {**scope_coverage, 'hashed_files': len(hashes), 'hashed_bytes': hashed_bytes, 'max_hash_bytes': limits['max_hash_bytes']}
    languages_method = LANGUAGE_METHOD
    try:
        installed_language_version = importlib.metadata.version('py3langid')
        if installed_language_version != '0.4.0':
            languages_method = languages_method.replace('0.4.0', installed_language_version)
    except importlib.metadata.PackageNotFoundError:
        languages_method = 'unavailable; language unknown (install pinned py3langid dependency)'
    english_count = language_counts.get('en', 0)
    metrics = {
        'selected_files': metric(len(files), 'files', len(files), 'regular non-hidden non-credential files enumerated in selected roots', scope_coverage),
        'documents_processed': metric(succeeded, 'documents', attempted, 'successfully extracted sampled files, including tables; not case counts', sample_coverage),
        'files_with_extracted_text': metric(files_with_extracted_text if attempted else None, 'files', attempted, 'successfully parsed sampled files with non-whitespace text in retained extraction sample; not a readability or content quality judgment', text_coverage),
        'selected_bytes': metric(sum(item['bytes'] for item in files), 'bytes', len(files), 'filesystem file sizes', scope_coverage),
        'format_file_shares': metric({kind: counts['files'] / len(files) for kind, counts in sorted(kinds.items())} if files else None, 'fraction', len(files), 'file count by normalized extension divided by enumerated file count', scope_coverage),
        'duplicate_files': metric(len(hashes) - len(set(hashes)) if hashes else None, 'files', len(hashes), 'exact full-byte SHA-256 duplicates; extra copies beyond one per hash', hash_coverage),
        'rows_processed': metric(rows if row_files else None, 'records', row_files, 'CSV/XLSX data rows exclude first non-empty header; JSON object=1, array elements, JSONL nonblank lines; processed sample only', row_coverage),
        'rows_known_total': metric(row_totals if known_row_totals else None, 'records', known_row_totals, 'sum of known row totals only; never a full selected archive count', row_coverage),
        'pdf_pages_processed': metric(pages if pdf_files else None, 'pages', pdf_files, 'pages inspected in sampled successfully parsed PDFs', page_coverage),
        'pdf_pages_known_total': metric(page_totals if known_page_totals else None, 'pages', known_page_totals, 'PDF page-tree totals from successfully parsed sample, distinct from processed pages', page_coverage),
        'pdf_no_text_pages': metric(no_text_pages if pdf_files else None, 'pages', pages, 'processed PDF pages yielding no extractable text; OCR candidate only, does not prove scan', page_coverage),
        'extracted_characters': metric(characters if text_files else None, 'characters', text_files, 'Unicode characters in bounded extracted text samples; includes separators', text_coverage),
        'extracted_words': metric(words if text_files else None, 'words', text_files, 'Unicode word-regex matches in extracted text samples; not tokens', text_coverage),
        'filesystem_mtime_bounds': metric({'earliest': mtime[0], 'latest': mtime[-1]} if mtime else None, 'UTC datetime bounds', len(mtime), 'filesystem last-modified timestamps; not content creation or history', scope_coverage),
        'eml_content_date_bounds': metric({'earliest': min(content_dates), 'latest': max(content_dates)} if content_dates else None, 'UTC datetime bounds', len(content_dates), 'parseable timezone-aware Date headers in sampled EML files; header assertions, not verified event dates', sample_coverage),
        'english_document_share': metric(english_count / attempted if attempted and attempted > language_counts.get('unknown', 0) else None, 'fraction', attempted, languages_method + '; classified-English fraction of attempted sample, including unknown and failed files in denominator; null when all languages unknown; classification may be incorrect; no population or true-language bound estimate', {**sample_coverage, 'unknown_documents': language_counts.get('unknown', 0)}, basis='inferred'),
    }
    limitations = [
        'Only explicitly selected local roots are profiled; archive completeness, rights and provider context require answers.',
        'Skipped counts include encountered excluded filesystem entries; hidden subtrees are not traversed and their file counts are unknown. Credential exclusions use known filename patterns, not a complete secret or privacy audit.',
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
        limitations.append('Enumeration was capped or incomplete; inventory values describe only the enumerated subset.')
    if len(selected) < sum(item['extension'] in SUPPORTED for item in files):
        limitations.append('Content extraction was sampled; unsampled supported files have status sample_cap.')
    if len(hashes) < len(files):
        limitations.append('Duplicate measurement covers only fully read hashed files within file and total hashing byte limits.')
    if any(statuses[status] for status in ('capped', 'size_cap', 'decompression_cap', 'timeout')):
        limitations.append('Some inputs reached parser, content, size or time limits; processed quantities must not be presented as full totals.')
    errors = sum(statuses[status] for status in ERRORS)
    skipped = sum(statuses.values()) - succeeded - errors
    result = {
        'schema_version': '1.0', 'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': {'kind': 'selected_files', 'selected_root_count': len(roots), 'enumeration_complete': complete, 'limits': limits, 'selection_method': sample_coverage['selection'], 'hidden_and_credentials_excluded': True, 'symlinks_followed': False},
        'inventory': {'files': len(files), 'bytes': sum(item['bytes'] for item in files), 'by_extension': dict(sorted(kinds.items()))},
        'extraction': {'attempted': attempted, 'succeeded': succeeded, 'errors': errors, 'skipped': skipped, 'skipped_basis': 'encountered inventory files and excluded filesystem entries; unvisited subtree file counts unknown', 'status_counts': dict(sorted(statuses.items()))},
        'metrics': metrics,
        'languages': {'document_counts': dict(sorted(language_counts.items())), 'denominator': attempted, 'unknown_documents': language_counts.get('unknown', 0), 'method': languages_method, 'basis': 'inferred', 'coverage': sample_coverage, 'unknown_reasons': dict(sorted(language_reasons.items()))},
        'limitations': limitations, 'questions': questions(),
    }
    checked_path(output)
    output_fd = output_directory(output)
    manifest = {'schema_version': '1.0', 'generated_at': result['generated_at'], 'privacy': 'local_only_do_not_share', 'sources': [str(root) for root in roots], 'files': private_files, 'excluded_status_counts': dict(sorted(exclusions.items())), 'limits': limits}
    try:
        verify_output_directory(output, output_fd)
        write_json(output_fd, 'manifest.private.json', manifest, private=True)
        write_json(output_fd, 'profile.json', result)
        verify_output_directory(output, output_fd)
    finally:
        os.close(output_fd)
    return result
