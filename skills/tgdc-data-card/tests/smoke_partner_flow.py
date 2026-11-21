"""The partner's flow end to end, as SKILL.md describes it: a fresh virtual environment, then the one run command through
the command line, on a folder with non-ASCII names, nested archives, e-mail and documents.

Run: python tests/smoke_partner_flow.py <empty work folder>. Exits non-zero on any failure."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / 'scripts' / 'datacard.py'
TEXT = ('The quarterly inspection report covers the foundation, the steel frame and the roof of the warehouse. '
        'All findings were recorded by the site engineer and reviewed by the project manager before handover. ') * 4


def build(data):
    (data / 'Projekte' / 'Größe Übersicht').mkdir(parents=True)
    (data / 'Mail').mkdir()
    (data / 'Projekte' / 'Größe Übersicht' / 'Prüfbericht.txt').write_text(TEXT, encoding='utf-8')
    (data / 'Projekte' / 'Angebot 2024.csv').write_text('Position;Menge;Preis\nBeton;12;1400\nStahl;3;900\n', encoding='utf-8')
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Zeichnungen/Grundriss Erdgeschoß.txt', TEXT)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Abnahme/Protokoll.txt', TEXT)
        archive.writestr('Abnahme/inner.zip', inner.getvalue())
    (data / 'Projekte' / 'Archiv.zip').write_bytes(outer.getvalue())
    (data / 'Mail' / 'message.eml').write_text(
        'From: a@example.com\nTo: b@example.com\nSubject: Abnahme\nDate: Tue, 3 Sep 2024 10:00:00 +0200\n'
        'Message-ID: <1@example.com>\nContent-Type: text/plain; charset=utf-8\n\n' + TEXT, encoding='utf-8')
    seven = shutil.which('7zz') or shutil.which('7z')
    if seven:
        source = data.parent / 'seven-source'
        source.mkdir()
        (source / 'Schaltplan Übersicht.txt').write_text(TEXT, encoding='utf-8')
        subprocess.run([seven, 'a', '-bd', '-y', str(data / 'Projekte' / 'Pläne.7z'), '.'], cwd=source, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
    return bool(seven)


def snapshot(folder):
    return sorted((str(path.relative_to(folder)), hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                  for path in folder.rglob('*'))


def run(python, *arguments):
    result = subprocess.run([str(python), '-I', str(SCRIPT), *map(str, arguments)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=900)
    print(f'$ datacard.py {arguments[0]} -> {result.returncode}\n{result.stdout}{result.stderr}', flush=True)
    if result.returncode != 0:
        raise SystemExit(f'{arguments[0]} failed')
    return result


def main(work):
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    data, report = work / 'Archiv der Firma', work / 'Bericht'
    data.mkdir()
    report.mkdir()
    with_seven = build(data)
    before = snapshot(data)
    environment = report / '.venv'
    venv.create(environment, with_pip=True)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', '-q', '--index-url', 'https://pypi.org/simple',
                    '--requirement', str(SKILL / 'scripts' / 'requirements.txt')], check=True)
    run(python, 'run', '--source', data, '--output', report)                    # first run, before the customer has answered
    first = json.loads((report / 'profile.json').read_text(encoding='utf-8'))['inventory']
    (report / 'spec-config.json').write_text(json.dumps({
        'company': 'Muster Bau GmbH', 'sector_line': 'Construction and engineering', 'month': 'November 2026', 'scope': 'full archive',
        'mailboxes': [{'root': str(data / 'Mail'), 'label': 'Mailbox 1 (company)'}],
        'project_roots': [str(data / 'Projekte')], 'drawing_patterns': ['Grundriss', 'Zeichnung']}), encoding='utf-8')
    (report / 'answers.json').write_text(json.dumps({'provider_context': 'Synthetic construction company'}), encoding='utf-8')
    run(python, 'run', '--source', data, '--output', report)                    # the same command again; it finds both answer files
    spec = json.loads((report / 'spec.json').read_text(encoding='utf-8'))
    inventory = json.loads((report / 'profile.json').read_text(encoding='utf-8'))['inventory']
    archives = spec['methodology']['archives']
    held, members = spec['totals']['held_files'], spec['parts']['archive_contents']['files']
    problems = []
    if inventory != first or (inventory['files'], inventory['bytes']) != (held, spec['totals']['held_bytes']):
        problems.append(f'the first page counts {inventory["files"]} files, the specification {held}')
    if spec.get('scope') != 'full archive':
        problems.append(f"scope {spec.get('scope')!r} != 'full archive'")
    if held != 4 + with_seven:                                 # files on disk; archive members are counted apart
        problems.append(f'files on disk {held} != {4 + with_seven}')
    if members != 3 + with_seven:                              # Protokoll, inner.zip, Grundriss inside inner.zip, the 7z member
        problems.append(f'archive members {members} != {3 + with_seven}')
    if archives['not_opened'] or archives['partly_readable'] or archives['opened'] != 2 + with_seven:
        problems.append(f'archives not fully read: {archives}')
    if (report / 'spec-tmp').exists() or (report / 'spec-run.lock').exists():
        problems.append('temporary data or the lock was left behind')
    if not (report / 'TGDC-Data-Card.pdf').read_bytes().startswith(b'%PDF'):
        problems.append('no PDF')
    if snapshot(data) != before:
        problems.append('the selected folder changed')
    if problems:
        raise SystemExit('; '.join(problems))
    print(f'OK: {held} files on disk, {members} archive members, 7-Zip: {with_seven}, tools: {archives["tools"]}')


if __name__ == '__main__':
    main(sys.argv[1])
