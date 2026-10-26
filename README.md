# TGDC Skills

Agent skills from [The General Data Company](https://thegeneraldata.com) for our data partners. Each skill is a folder in the open [Agent Skills](https://agentskills.io) format and works with Claude Code, OpenAI Codex, GitHub Copilot, Cursor, Gemini CLI, Qwen Code, and other coding agents that read `SKILL.md`.

## tgdc-clean-copy

Screens a company drive on your own machine and builds a separate folder with an unchanged copy of all your files, except two kinds:

1. **Personal data files:** files whose primary content is personal data of individuals, such as payroll, personnel records, ID copies, CVs, health records, and private correspondence.
2. **NDAs:** non-disclosure agreements and material you received under one.

Everything else is copied, in every language and every file type: documents, scans, spreadsheets, emails and mailboxes, archives, images, audio, video, CAD, databases. Usually only a small share of files is left out.

**What it never does**

- It never changes, moves, or deletes anything on your drive.
- It never edits a file. Every copied file is byte-identical to the original and verified by SHA-256.
- It never sends anything to The General Data Company. During the run, network access is off.
- It never decides a doubtful file on its own. You get one review list, and anything you leave blank stays out.
- Identity documents and tables of people are found by tested scripts that ship with the skill (`scripts/id_signals.py`, `scripts/table_signals.py`), not by code the agent improvises.

**What you get**

- `clean-copy/`: the files to share.
- `report/`: a run report with counts only (no file names, no content), a checksum list, and an attestation draft for a responsible person to sign.
- `_private/`: the file-by-file record, review lists, and logs. It stays inside your company.

### Before you start

- Use a business AI account with a data processing agreement that bars the provider from using your data to improve its models (for example Claude Team, Enterprise, or API, or ChatGPT Business or Enterprise), or a local model. On a personal plan the agent runs only in its strictest mode, where it never sees file names or document content.
- Run on a snapshot or a read-only copy of the drive if you can, on a local disk with free space of at least the drive size plus 20 percent.
- Share the result only after a data processing agreement with The General Data Company is signed.

### Install

**1. Tools** (once, with internet)

macOS:
```sh
brew install python tesseract tesseract-lang poppler qpdf exiftool sevenzip libpst
```
Linux (Debian or Ubuntu):
```sh
sudo apt install python3-venv tesseract-ocr tesseract-ocr-all poppler-utils qpdf libimage-exiftool-perl p7zip-full pst-utils catdoc bubblewrap socat
```
Windows: use WSL2 with Ubuntu and follow the Linux line.

**2. Run folder and Python packages** (on a local disk, outside the drive you screen)
```sh
mkdir clean-copy-run && cd clean-copy-run
python3 -m venv .venv
.venv/bin/pip install pymupdf python-stdnum msoffcrypto-tool extract-msg py7zr "opencv-python-headless<5" xlrd olefile
```

**3. The skill** (inside `clean-copy-run/`; download a pinned release and verify its checksum)
```sh
V=v1.3.0
curl -fsSLO https://github.com/lawoflarge/tgdc-skills/releases/download/$V/tgdc-clean-copy-$V.tar.gz
curl -fsSLO https://github.com/lawoflarge/tgdc-skills/releases/download/$V/SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing
mkdir -p <FOLDER> && tar -xzf tgdc-clean-copy-$V.tar.gz -C <FOLDER>
```
On Linux, use `sha256sum` instead of `shasum -a 256`.

| Agent | `<FOLDER>` | Start the skill |
|---|---|---|
| Claude Code | `.claude/skills` | `/tgdc-clean-copy The source drive is: /path/to/drive` |
| OpenAI Codex | `.agents/skills` | `$tgdc-clean-copy The source drive is: /path/to/drive` |
| GitHub Copilot, Cursor, Gemini CLI, Amp, OpenCode, Goose | `.agents/skills` | `/tgdc-clean-copy` or "Use the tgdc-clean-copy skill. The source drive is: /path/to/drive" |
| Qwen Code | `.qwen/skills` | `/tgdc-clean-copy The source drive is: /path/to/drive` |
| Trae | `.trae/skills` | "Use the tgdc-clean-copy skill. The source drive is: /path/to/drive" |
| Any other agent | `.agents/skills` | "Read .agents/skills/tgdc-clean-copy/SKILL.md completely and follow it. The source drive is: /path/to/drive" |

**4. Safe start**

Claude Code: copy `skills/tgdc-clean-copy/assets/claude-settings.json` to `clean-copy-run/.claude/settings.json` and replace `SOURCE` with the drive path without its first slash (`/Volumes/Share` becomes `Edit(//Volumes/Share/**)`). Then run `claude --strict-mcp-config` inside `clean-copy-run/` (this switches off MCP servers and connectors, which run outside the sandbox).

Codex:
```sh
codex --sandbox workspace-write --ask-for-approval on-request -c 'web_search="disabled"'
```
If `codex mcp list` shows enabled MCP servers, add `-c mcp_servers.<name>.enabled=false` for each one. If you use a personal `~/.codex/AGENTS.md` or memories, start Codex with a clean home that holds only your login: `mkdir -p ~/.codex-clean && ln -s ~/.codex/auth.json ~/.codex-clean/auth.json`, then prefix the command with `CODEX_HOME=~/.codex-clean`.

Never pass the drive with `--add-dir`, and deny any request to run outside the sandbox or to allow a network domain during the run.

The full operator guide is in [`skills/tgdc-clean-copy/references/operator-guide.md`](skills/tgdc-clean-copy/references/operator-guide.md).

### During the run

The agent asks you five times: a short interview, a check of about 200 pilot files, one review list, a go before copying, and a final quality check. In between it works on its own. Large drives can take many hours because scanned documents need OCR.

## tgdc-data-card

Reads files and folders you select on your own machine and builds one reviewable PDF in two parts. Every statement is labeled as measured, inferred, or declared by you:

1. **Data Card:** file counts and sizes, formats, duplicates, rows, pages, words, dates, and the share of English in a bounded text sample, computed locally by `scripts/datacard.py`, plus context you declare, such as your rights to share the data, the business behind it, and how records relate to cases. The agent asks only about gaps, at most three questions at a time, and you can skip any of them.
2. **Data format specification:** a full pass that reads every selected file completely, computes a SHA-256 checksum of each one, and does the same for every file inside compressed archives. Archives are unpacked into a temporary folder inside your report folder, which is removed afterward; for 7z, RAR, ARJ and CAB archives the pass runs an extraction tool that is already on your machine. It reports volume by content category and file format, mailboxes, languages, the contents of compressed archives, technical drawings (sheets, sizes, project workflow) and volume by year. It holds counts, sizes, years, the extensions of known file formats and the vocabulary you declare (company line, mailbox labels, project stages; keep the labels short and generic, with no person or client names). It holds no file names, folder names, e-mail addresses, hashes, or text.

**What it never does**

- It never changes, moves, or deletes your files, and it never follows symbolic links.
- It never emails, uploads, or submits anything by itself. You decide whether to share the PDF.
- Its scripts make no network requests and use no telemetry. Only the Python package setup downloads software, from PyPI, never your data.
- It never reads a file before you confirm that you are authorized to use the selection and that it excludes HIPAA protected health information and payment-card data.
- It never parses files that look like credentials (certificates, key stores, password files, found by extension and file name). It counts them and records a SHA-256 checksum of their bytes, which stays in the private cache, so that copies are recognized.
- It never executes anything found in an archive, and it rejects archive members whose path is absolute or contains `..`.
- It never certifies anything. The PDF is not an anonymization, a privacy check, or proof of your rights to share.

**What you get** (in a report folder you choose, outside the selected files)

- `TGDC-Data-Card.pdf`: the data card followed by the data format specification, in English.
- `profile.json` and `spec.json`: the aggregate evidence behind the two parts.
- `answers.json` and `spec-config.json`: your answers as you gave them.
- `manifest.private.json` and `spec-cache.private.sqlite`: file-by-file records that let an interrupted run resume. They stay on your machine. Do not share them.
- `calibration.private/`: only if you agree to the optional visual check described below.

### Before you start

- Python 3.11 or newer on macOS or Linux. Tested on macOS (Apple silicon) with Python 3.12, 3.13 and 3.14; Linux is not tested. Windows is not supported.
- A coding agent that can run commands and read the files you select.
- The full pass reads every file and can take hours on a large archive. It shows progress every 1,000 files and continues where it stopped if you run it again.
- A folder from Google Drive for desktop must be set to "Available offline", or copied to a local folder, before the run. Files that exist only online cannot be read.
- Optional: one of `7zz`, `7z`, `bsdtar` or `unar` on your PATH. Without one, 7z, RAR, ARJ and CAB archives are counted but not opened, and the notes of the PDF say so.
- Your agent's AI provider processes what the agent sees: your request, the folders you name, the aggregate results, and your answers. Check its account and permission settings first, and select only files you are authorized to use. The agent is instructed to read only `profile.json` and `spec.json`, but the skill cannot technically stop an agent with file access from reading more.
- Review the PDF, `profile.json` and `spec.json` before you share anything. Your answers can contain confidential details, and they appear in the PDF.

### Install

**1. The skill** (download a pinned release and verify its checksum)
```sh
V=1.1.0
R=https://github.com/The-General-Data-Company/tgdc-skills/releases/download/tgdc-data-card-v$V
curl -fsSLO $R/tgdc-data-card-v$V.tar.gz
curl -fsSLO $R/SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing
mkdir -p <FOLDER> && tar -xzf tgdc-data-card-v$V.tar.gz -C <FOLDER>
```
On Linux, use `sha256sum` instead of `shasum -a 256`.

| Agent | `<FOLDER>` | Start the skill |
|---|---|---|
| Claude Code | `.claude/skills` | `/tgdc-data-card The files are: /path/to/folder. Put the report in: /path/to/report` |
| OpenAI Codex | `.agents/skills` | `$tgdc-data-card The files are: /path/to/folder. Put the report in: /path/to/report` |
| GitHub Copilot, Cursor, Gemini CLI, Amp, OpenCode, Goose | `.agents/skills` | `/tgdc-data-card` or "Use the tgdc-data-card skill. The files are: /path/to/folder. Put the report in: /path/to/report" |
| Qwen Code | `.qwen/skills` | `/tgdc-data-card The files are: /path/to/folder. Put the report in: /path/to/report` |
| Trae | `.trae/skills` | "Use the tgdc-data-card skill. The files are: /path/to/folder. Put the report in: /path/to/report" |
| Any other agent | `.agents/skills` | "Read .agents/skills/tgdc-data-card/SKILL.md completely and follow it. The files are: /path/to/folder. Put the report in: /path/to/report" |

**2. Python packages.** If compatible packages are not already available, the skill explains the download, then creates an isolated virtual environment in your report folder and installs the pinned packages from PyPI (`scripts/requirements.txt`).

The privacy notes are in [`skills/tgdc-data-card/references/privacy.md`](skills/tgdc-data-card/references/privacy.md). The configuration file and the method behind each figure of the specification are in [`skills/tgdc-data-card/references/format-spec.md`](skills/tgdc-data-card/references/format-spec.md).

### Use

The agent works in this order:

1. It asks which files or folders to examine and where to put the report, and asks you to confirm the authorization and exclusions above.
2. It runs the profile script and reads the aggregate result.
3. It asks about missing context. You can skip any question or ask for a draft right away. For the specification it asks for the company line, which folders are mailboxes (with a short generic label for each), where your projects begin, your own project stages, and the words that mark a bill of quantities, a drawing and a schematic in your file names.
4. It runs the full pass and tells you that it can take hours. New answers are applied by running the pass again, which reuses the private cache and takes minutes.
5. Optional, only with your explicit consent: it writes about 60 low-resolution page images to `calibration.private/` and looks at them to correct the count of technical drawings. The agent host then processes those images. If you decline, the estimate of individual views is left out of the PDF.
6. It renders the PDF and shows it to you.
7. It gives you the file paths. The skill sends nothing, so you decide whether and how to share the PDF.

## Security

See [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contact

The General Data Company · [thegeneraldata.com](https://thegeneraldata.com)
