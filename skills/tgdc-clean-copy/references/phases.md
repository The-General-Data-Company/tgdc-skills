# Phases and checkpoints

## B7. Phases and checkpoints

Keep `_private/PROGRESS.md` current after every phase: what is done, counts, what comes next. Every session starts the same way: confirm you are in the run folder, read `_private/PROGRESS.md` if it exists, run a status script that prints manifest counts by phase and verdict, and resume the first unfinished phase. Do not redo finished phases. All state lives on disk, so do not stop early because your context is filling up; it will be compacted and you can continue from the files.

Write every script so it can be re-run safely (skip work already recorded, write files atomically). Your tool kills long foreground commands, so run long phases as background jobs that log to `_private/logs/`, or as resumable batches that fit your command limit, and poll their counts. Give every file a time limit (for example 120 seconds per page for OCR); after three failures, decide the file by its name, folder, and metadata alone and record that. Before you report progress, check each claim against a tool result from this session.

Between checkpoints, work autonomously. Do not stop to ask about routine steps that this prompt already settles. Do stop at each checkpoint and at every stop condition in B11.

**Phase 0. Preflight (checkpoint 1)**
1. Check the environment: OS, free disk space, Python virtual environment, and available tools (tesseract with languages, pdftotext, pdftoppm, qpdf, exiftool, 7z or 7zz, readpst, catdoc on Linux or antiword on Windows, the Python packages from A3, and OpenCV's `CascadeClassifier` with its bundled face model). On Windows, if a tool is not on PATH, also look in `C:\Program Files\Tesseract-OCR`, `C:\Program Files\7-Zip`, `C:\Program Files\qpdf*\bin`, `C:\Program Files\ExifTool`, and `C:\msys64\ucrt64\bin` before you call it missing. Report what is missing and wait for the operator.
2. Prove the safety layers without touching SOURCE. With a short Python script: try to create `clean-copy-probe.txt` in the operator's home folder (it must fail), try to open `https://example.com` (it must fail), and count the entries at the top level of SOURCE (print the count only; it must work and must not be zero, since macOS privacy controls can make a network volume look empty). If a probe file was created, tell the operator to delete it. If either of the first two succeeds, stop. If SOURCE cannot be counted on Windows, the sandbox cannot read it (a network share, or a disk its accounts may not read): stop and point the operator to the local copy in A4. The Windows sandbox of Codex also allows writes to the system temp folder; that is expected, but keep every temporary file in `_private/tmp/`: set `TMP` and `TEMP` to it in the environment of every command you start, the shipped scripts and the tools included.
3. Confirm that RUN_DIR is not inside SOURCE and SOURCE is not inside RUN_DIR.
4. Interview the operator for every B4 field. Ask the questions in one message, grouped and short. Explain CONTENT_MODE in two sentences and the QA_SAMPLE options with the table in B9. NDA_PARTNERS may stay open until checkpoint 2.
5. Write `_private/config.json` and show it back.

**Phase 1. Inventory and pilot**
1. Walk SOURCE without following symlinks or shortcuts. Record every folder or file that cannot be listed or opened, by manifest ID, and report their count at checkpoint 2; a walk that skips them silently (`os.walk` does unless you pass `onerror`) makes the inventory smaller without any sign. On Windows, in this and every later phase, open every path as an extended-length path (`\\?\C:\...`), so that paths longer than 260 characters are read too; the shipped scripts do this themselves. Per file, record in the manifest: manifest ID, relative path, size, modification time, file type by magic bytes, extension, placeholder state, symlink or hard link, and SHA-256. Check placeholder flags with `os.lstat` before opening any file, and do not read placeholder files, since reading them triggers a download (Windows attributes `RECALL_ON_DATA_ACCESS`, `RECALL_ON_OPEN`, `OFFLINE`; macOS flag `SF_DATALESS`; Google Docs pointer files such as `.gdoc`).
2. Write the source snapshot `_private/snapshot_before.csv` (path, size, mtime, SHA-256).
3. Mark OS junk (`.DS_Store`, `Thumbs.db`, `desktop.ini`, `~$*` lock files, `._*`) and online-only placeholders as skipped. They are not copied and are counted separately.
4. Benchmark extraction and OCR on 200 random pages to estimate run time.
5. Write `_private/gate_folders.csv` (folder ID, full path, gate category, file count, empty `release` column) for every folder with a gate hit.
6. Find NDA documents (B8) and record them in `_private/nda_found.csv` (NDA ID, manifest ID, full path, counterparty, subject keywords such as a project or product name, empty `confirm` column).
7. Pilot: run Phases 2 to 4 on a stratified sample of about 200 files (spread over folders, types, scans, spreadsheets, and emails, plus some gate hits). Write `_private/pilot.csv` (manifest ID, full path, verdict, category, rule IDs, empty `operator_check` column).

**Checkpoint 2.** Show: total files and size; counts by type; skipped placeholders and junk; folder-gate hits by top-level folder and category; NDA counterparties found, with file counts; estimated run time; pilot verdict counts. Ask the operator to (a) confirm NDA_PARTNERS and, for each NDA, its counterparty and subject in `nda_found.csv`, (b) mark folders to release in `gate_folders.csv`, and (c) spot-check the pilot sheet and mark wrong verdicts. Read back only IDs and marks. For each wrong verdict, ask the operator to describe in general words which signal was missed (for example "ID copy on page 3"), fix the rule, and re-run the pilot. Then ask for go.

**Phase 2. Gates and metadata (cheap)**
Apply SKIP_FOLDERS and the folder and file-name gates (B8). Extract metadata: PDF info and XMP, Office `docProps`, comments and tracked-change authors, hidden sheets, embedded files, EXIF, email headers. Author names feed the employee roster; they never exclude a file on their own. List archives before extracting them (limits: depth 5, 10,000 members, 10 GB unpacked; reject path traversal). Expand mailbox stores with `readpst` or Python's `mailbox` module into `_private/cache/`.

**Phase 3. Content extraction**
For every page of every readable file, and for every embedded image, attachment, and mailbox message: use the text layer where it is good. OCR the page if it has under about 200 characters of text, is mostly image, or its text looks like garbage. Use OCR with orientation detection, and for image-heavy pages also run a sparse-text pass on the page rotated 90 and 270 degrees, because rotated stamps and ID cards are missed otherwise. Store page text only in `_private/cache/`.

**Phase 4. Detection and decision**
First run the shipped identity-document detector `scripts/id_signals.py` on every PDF, image, and Office file, and on every extracted attachment and archive member: write a UTF-8 TSV of manifest ID and absolute path, run `.venv/bin/python <skill folder>/scripts/id_signals.py INPUT.tsv _private/id_signals.csv _private/logs/id_signals.log` (on Windows `.venv\Scripts\python.exe`), and import the results. A `P4` hint on any page excludes the file as P4; a `review` hint puts it on the review list. Run the shipped table detector the same way on every spreadsheet and CSV file (`scripts/table_signals.py INPUT.tsv _private/table_signals.csv _private/logs/table_signals.log`): a `P1` or `P7` hint on any sheet, hidden sheets included, excludes the file with that code; `review` puts it on the review list. Never replace these scripts with your own; you may add detectors on top of them.
Then run the detectors in B8 per page and per image, write every hit as a rule ID with page number to the manifest (never the matched text), and apply B5 and B6. Build the employee roster from P1 and P2 files, staff lists, and metadata; keep it only in `_private/`. Use it to find personnel documents elsewhere: a roster name as the subject or addressee, together with a pay, HR, leave, or health term, means P1 to P3. A roster name as sender, contact, or author means nothing.
If CONTENT_MODE allows, use masked excerpts or the local model only for files the rules could not settle, and record the label and a short reason.

**Checkpoint 3. Review list**
Write `_private/review_list.csv` with columns: manifest ID, full path, suspected category, rule IDs, pages, and an empty `decision` column (`keep` or `exclude`). Group rows by folder so the operator can decide in batches. Tell the operator how many rows there are, by category, and ask them to fill the decision column and tell you when done. Read back only the decision counts. Blank or unclear decisions are exclude.

**Checkpoint 4. Before copying**
Show the final counts per verdict and category, the containers excluded because of single members, and the total size to copy. Ask for go.

**Phase 5. Copy**
1. Write the allow-list `_private/allow_list.txt` from the manifest (verdict copy only).
2. Copy with a Python script: create parent folders, copy file content to `_private/tmp/`, verify its SHA-256 against the hash recorded at inventory, move it into place with `os.replace`, and set the source modification time with `os.utime`. On Windows, use extended-length paths for the target as well (Phase 1). If a target already exists with the manifest hash, record it and continue (resume); any other existing target is a stop condition.
3. A hash mismatch against the manifest means the source changed after screening. That is a stop condition.
4. Check that `clean-copy/` contains exactly the files on the allow-list, no more and no fewer.
5. Re-run the detectors over `clean-copy/` (expect zero exclusion hits). On any hit, repeat checkpoint 3 for those rows and rebuild into the next `clean-copy-N/` from the corrected allow-list.
6. Write the source snapshot `_private/snapshot_after.csv` and compare it with the one from before. Any difference is a stop condition.

**Phase 6. Quality check (checkpoint 5)**
1. Draw QA_SAMPLE random files from `clean-copy/` with a recorded seed. Add up to 50 random copied files from each released gate folder, as a separate risk sample.
2. Write `_private/qa_sample.csv` (manifest ID, path in `clean-copy/`, empty `finding` column). The operator opens each file locally and marks `ok` or the category that should have excluded it.
3. Any finding means: ask the operator to describe the missed signal in general words, fix the rule, re-run detection over the whole drive, rebuild into the next `clean-copy-N/` (never remove files from a copy by hand), and draw a new sample. Phase 7 and the package use only the newest copy; tell the operator to delete older ones.
4. Also draw 30 random excluded files for the operator to check for over-exclusion. This is informational.

**Phase 7. Report and handover**
Generate `report/run_report.md`, `report/SHA256SUMS.txt`, and `report/attestation_draft.md` with a script from the manifest (templates in B10). Then tell the operator, in five lines or fewer: the result, what to share (`clean-copy/` and `report/`, after the data processing agreement is signed), what never to share (`_private/`), which folders hold extracted text (`_private/cache/`) and how to delete them, and where the agent tool keeps its session logs.

## B9. Quality-check sample sizes

If the operator finds zero excludable files in a random sample of copied files, the claim supported at 95 percent confidence is:

| Random copied files checked | Excludable share in the clean copy is below |
|---|---|
| 59 | 5 percent |
| 149 | 2 percent |
| 299 (default) | 1 percent |
| 598 | 0.5 percent |

Any finding invalidates the claim until the rule is fixed, the drive is re-run, and a new sample is clean. For drives with fewer than about 600 files, check every copied file instead.
