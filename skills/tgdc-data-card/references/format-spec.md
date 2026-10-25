# Data format specification guide

The `spec` command reads every selected file, and every file inside compressed archives, on the customer's machine. It writes `spec.json` and a private cache. The `render` command turns `spec.json` into the second part of the PDF. This guide lists the configuration file, the optional visual check and the method behind each figure.

## Commands

```
datacard.py spec --source <root> [--source <root> ...] --output <report> [--config <report>/spec-config.json]
                 [--workers N] [--parse-seconds S] [--archive-seconds S] [--max-archive-bytes N] [--max-archive-ratio N]
datacard.py spec-sample --spec-dir <report> --n 60
datacard.py render --profile <report>/profile.json --answers <report>/answers.json --spec <report>/spec.json --output <report>/TGDC-Data-Card.pdf
```

`spec` prints progress to stderr every 1,000 files. If it stops, the same command resumes: finished files are kept in `spec-cache.private.sqlite` and a file is read again only when its size or modification time changed. The cache holds file names, folder names and hashes, so it stays private. A run with a new `spec-config.json` reuses the cache and takes minutes.

Exit codes: 0 done, 2 invalid input or configuration, 3 stopped early (run the same command again).

## spec-config.json

Every key is optional. Keys that are missing leave the matching rows out of the PDF. The agent writes the file from the customer's answers.

```json
{
  "company": "Example Works SRL",
  "sector_line": "Construction contractor (water, sewerage, irrigation)",
  "month": "October 2026",
  "mailboxes": [
    {"root": "/absolute/path/inside/a/selected/root/mail-1", "label": "Mailbox 1 (owner, Gmail)"},
    {"root": "/absolute/path/inside/a/selected/root/mail-2", "label": "Mailbox 2 (co-owner, Yahoo)"}
  ],
  "project_roots": ["/absolute/path/inside/a/selected/root/projects"],
  "project_depth": 1,
  "stages": {
    "title": "project milestones",
    "items": [
      {"name": "tender documents", "patterns": ["caiet de sarcini", "licitatie"]},
      {"name": "contract", "patterns": ["contract"]}
    ],
    "thresholds": [4, 5, 6]
  },
  "bill_of_quantities_patterns": ["deviz", "lista de cantitati"],
  "drawing_patterns": ["plansa", "desen"],
  "schematic_patterns": ["schema"],
  "revision_tokens": ["pth", "dtac"],
  "limits": {"max_archive_bytes": 20000000000, "max_archive_ratio": 200}
}
```

- `company`, `sector_line`, `month` form the title block. They are shown as written.
- `mailboxes[].root` is a folder (or one `.mbox` file) inside a selected root. Everything below it belongs to that mailbox. A root that holds no `.eml` or `.mbox` file is treated as an ordinary folder. `label` is shown in the PDF; write it in English.
- `project_roots` and `project_depth` define projects: a project is the folder `project_depth` levels below a project root (default 1).
- `stages.items` lists the customer's own stages in order. `name` is shown in the PDF (English). `patterns` are keywords, in the language of the archive, that identify the stage in file and folder names. A stage counts as documented for a project when any file below the project matches one of its patterns. `thresholds` defaults to ceil(n/2), ceil(n/2)+1, ceil(n/2)+2 for n stages. `title` is `project milestones` or `lifecycle stages`.
- `bill_of_quantities_patterns`, `drawing_patterns`, `schematic_patterns` are keywords for file and folder names. Matching ignores case and diacritics and uses whole words. A keyword of five or more letters also matches the start of a longer word (for example `contract` matches `contractul`). Without `drawing_patterns` and `schematic_patterns`, a default list of common words in English, Romanian, German, French, Italian, Spanish, Polish and Dutch is used (for example `drawing`, `plan`, `plansa`, `sectiune`, `Zeichnung`, `Grundriss`, `tavola`, `plano`, `rysunek`, `tekening` and, for schematics, `schema`, `Schaltplan`). Words the customer gives replace the defaults of that kind, so a customer whose archive uses `plan` for other documents should list its own drawing words.
- `revision_tokens` are extra words, such as design-phase codes, that are removed from drawing file names before versions are compared. The built-in tokens are `rev`, `r01`, `v2`, `ver`, `version`, `ed`, `phase`, dates and copy markers.
- `limits` overrides the archive caps. The command-line options win over the file.

Never invent a value. A question the customer skips is a key left out.

## Optional visual check (calibration)

The drawing count is a heuristic: pages larger than A4 count as drawings, and an A4 or Letter page counts when it holds many vector paths and little text, or when its file or folder name matches a drawing keyword. A visual check on a small sample corrects the totals.

1. Ask the customer for explicit consent. Explain that the agent host will process about 60 low-resolution page images. If the customer declines, stop here: the estimate of individual views is left out.
2. Run `spec-sample --spec-dir <report> --n 60`. It writes `calibration.private/sample-001.png` and so on, plus `index.private.json`, which lists for each image whether it is a candidate (counted as a drawing by the rules) or not. The index holds no file names.
3. View each image. Count: candidates that are really drawing sheets, other pages that are really drawing sheets, drawings among them that are schematics or diagrams, and for every true drawing sheet the number of separate views (plans, sections, details, elevations).
4. Write the counts into `spec-config.json` and run `spec` again:

```json
"calibration": {
  "sampled": {"candidates": 30, "non_candidates": 30},
  "true_drawings_among_candidates": 27,
  "drawings_among_non_candidates": 1,
  "schematics": 4,
  "views_per_sheet": [1, 2, 2, 3, 1, 2]
}
```

The sheet total becomes (all candidate pages x share of true drawings among sampled candidates) + (all other pages x share of true drawings among sampled other pages). Breakdowns by size, location and file type are scaled by the same factor, file counts are not scaled. The views estimate is the corrected total times the mean views per sheet; the range is the 10th to 90th percentile of 1,000 bootstrap means with a fixed seed.

The images, `index.private.json` and `calibration.private/` stay on the machine and are never shared.

## Method notes

- Sizes are decimal GB (10^9 bytes); GiB appears only in the headline and in "Files as held".
- Every file and every archive member is hashed with SHA-256, streamed, without a size cap. Credential and key files are identified by extension and file name only; they are counted by size, never parsed or kept, and only their SHA-256 checksum is recorded (in the private cache).
- Content category comes from the extension, except PDF (text layer of the first page, first three pages inside archives), files with archive signatures and no known extension, and `.json` / `.csv` files inside a mailbox folder, which are per-message metadata.
- Compressed archives: zip, tar, gz, tgz, bz2, xz through Python; 7z, rar, arj, cab and multi-part sets through the first of `7zz`, `7z`, `bsdtar`, `unar` found on PATH. Nesting goes up to depth 5. An archive stops at 20 GB expanded or 200 times expansion. Member paths that are absolute or contain `..` are rejected. Nothing is executed. Archives without a usable tool are counted as not unpacked, with the reason.
- New content: an archive member whose hash matches no loose file and no earlier member; nested archives whose contents are counted are not new content.
- Alternative mailbox exports: an `.mbox` file whose message identifiers overlap at least 95% with the folder's `.eml` files, extracted files identical to attachments decoded from the messages, `.txt` and `.html` files identical to decoded bodies, and an archive whose members equal the unpacked export. They are shown as "same messages". Sent and received messages are counted from the sender address against the most frequent recipient address; no address is stored in any output.
- Languages: py3langid proposes a language for up to the first 10,000 characters; a common-word list or the script of the text must confirm it. Other text counts as "No language confirmed". Spreadsheets and presentations in the old binary formats (`.xls`, `.ppt`) are not read for text and are not part of the language table.
- Dates: the own date of a document (PDF creation date, Office creation date, e-mail Date header) where known, otherwise the file modification date. Years before 1990 or after the current year are shown as "date not reliable".
- Sheets: each distinct file content counts once, in the order file archive, e-mail, compressed archives. Page sizes match A4, A3, A2, A1, A0 and the US sizes within 3% in either orientation.
- Hidden entries (names that start with a dot), folders with credential-like names and symbolic links are not entered and not counted. Their number is stated in the notes.
- Folders from cloud drives must be available offline before the run.
