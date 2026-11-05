# Operator guide

What the person running the agent prepares and can expect (sections A1 to A6). The agent reads this file to answer operator questions. The same steps are in the repository README.

## A1. What you get

- `clean-copy/`: byte-identical copies of every file that passed, in the original folder structure. This is what you share.
- `report/`: a run report with counts only (no file names, no content), a checksum list of the clean copy, and an attestation for a responsible person to sign.
- `_private/`: the full file-by-file record, the review lists, logs, and scripts. It contains names and paths from your drive. It stays inside your company.

What this step does not do: it does not anonymize the files it keeps. Business records keep incidental names, such as a contact person on an invoice, a signature block, or an author name in file metadata. Those are pseudonymized by The General Data Company before any use, under a data processing agreement that must be signed before you share anything (A6).

## A2. Before you start

1. **Account.** Use a business account with a data processing agreement and no training on your data: Claude Team, Enterprise, or API; Claude via Amazon Bedrock or Google Vertex AI (in an EU region for EU data); ChatGPT Business or Enterprise; the OpenAI API; or a local model (see A4). Personal plans (Claude Free, Pro, Max; ChatGPT Free, Plus, Pro) carry no data processing agreement and may allow training. On a personal plan the agent runs only in its strictest mode and only after you confirm that the model-training setting is off (Claude: "Help improve Claude"; ChatGPT: "Improve the model for everyone").
2. **Authority.** You are authorized to run an inventory of this drive for this purpose.
3. **Source protection.** Run on a snapshot, a read-only mount, or with a read-only user account. The agent protects the source anyway, but a second layer is cheap. On a live share, edits by colleagues during the run will stop it.
4. **Disk space.** Free space on the run disk of at least the drive size plus 20 percent.
5. **Online-only files.** If the drive is OneDrive, SharePoint, Google Drive for desktop, Dropbox, or iCloud, download online-only files before the run. The agent never triggers downloads; it lists files that are still online-only and leaves them out.
6. **Time.** Plan for an interview at the start, a pilot check and a review list in the middle, and a quality check at the end (A5). The scanning in between runs by itself and can take many hours on large drives (scanned PDFs need OCR). Keep the computer awake (macOS: `caffeinate -i`).

## A3. Install the tools (once, with internet)

The agent runs without internet, so the tools must exist before it starts.

macOS (Homebrew):
```
brew install python tesseract tesseract-lang poppler qpdf exiftool sevenzip libpst
```
Linux (Debian or Ubuntu):
```
sudo apt install python3-venv tesseract-ocr tesseract-ocr-all poppler-utils qpdf libimage-exiftool-perl p7zip-full pst-utils catdoc bubblewrap socat
```
Windows: use WSL2 with Ubuntu for either tool and follow the Linux line. Map network shares to a drive letter first. On macOS, legacy `.doc` files are read with the built-in `textutil`.

Then create the run folder and its Python environment. The run folder must be on a local disk and must be outside the source drive.
```
mkdir clean-copy-run && cd clean-copy-run
python3 -m venv .venv
.venv/bin/pip install pymupdf python-stdnum msoffcrypto-tool extract-msg py7zr "opencv-python-headless<5" xlrd olefile
```
Then install the skill into the run folder, as described in the README (Claude Code: `.claude/skills/`; Codex and most other agents: `.agents/skills/`).

## A4. Start the agent

**Claude Code.** Create `clean-copy-run/.claude/settings.json` with the content below. Replace `SOURCE` with the absolute path of the drive without its first slash, so `/Volumes/Share` becomes `Edit(//Volumes/Share/**)` (inside WSL, for example `//mnt/z/**`).
```json
{
  "permissions": {
    "deny": [
      "WebFetch", "WebSearch",
      "Bash(rm *)", "Bash(rmdir *)", "Bash(shred *)", "Bash(rsync *)",
      "Bash(curl *)", "Bash(wget *)", "Bash(scp *)", "Bash(ssh *)", "Bash(git push *)",
      "Edit(//SOURCE/**)"
    ],
    "disableBypassPermissionsMode": "disable"
  },
  "sandbox": {
    "enabled": true,
    "allowUnsandboxedCommands": false,
    "failIfUnavailable": true
  },
  "env": {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1"
  }
}
```
Do not add a `Read` deny rule for the source: the sandboxed scripts must be able to read it. If Claude Code asks to open a source file with its own Read tool, answer no.

Then start Claude Code inside the run folder, with all MCP servers and connectors switched off (they run outside the sandbox). Do not add the source with `--add-dir`, because added folders become writable.
```
cd clean-copy-run
claude --strict-mcp-config
```

**Codex CLI.** Start Codex inside the run folder with network and web search off. Do not use `--add-dir` for the source.
```
cd clean-copy-run
codex --sandbox workspace-write --ask-for-approval on-request -c 'web_search="disabled"'
```
If `codex mcp list` shows enabled MCP servers, switch each one off for this run by adding `-c mcp_servers.<name>.enabled=false` (they run outside the sandbox). For a fully local run, add `--oss` (requires Ollama with a capable model installed). Then document content never leaves your machine.

During the run, deny every request to run a command outside the sandbox or to allow a network domain. One approval would defeat the safety checks.

If your agent has personal global instructions or memory, keep them out of this run. For Codex, start it with a clean home that holds only your login: `mkdir -p ~/.codex-clean && ln -s ~/.codex/auth.json ~/.codex-clean/auth.json`, then prefix the start command with `CODEX_HOME=~/.codex-clean`.

**First message:** in Claude Code `/tgdc-clean-copy The source drive is: <absolute path>`; in Codex `$tgdc-clean-copy The source drive is: <absolute path>`; in other agents `Use the tgdc-clean-copy skill. The source drive is: <absolute path>`.

## A5. What you will be asked

1. **Interview** (about 5 minutes): account, languages, folders to skip, partners you have NDAs with, and how strict the final check should be.
2. **Inventory and pilot:** file counts, formats, estimated run time, folders that look like HR or payroll folders, NDAs the agent found, and a pilot of about 200 files whose verdicts you spot-check before the long run.
3. **Review list:** files the agent could not decide. You open them on your computer and mark keep or exclude. Anything you leave blank is excluded.
4. **Go before copying:** final numbers.
5. **Quality check:** a random sample of copied files to look through, then the sign-off.

## A6. What to share

Share only `clean-copy/` and `report/`, and only after a data processing agreement with The General Data Company is signed. Never share `_private/`. To package on a Mac, use `COPYFILE_DISABLE=1 zip -r -X clean-copy.zip clean-copy report` (Finder's Compress adds hidden files). After the run, the agent tells you which folders hold extracted text and how to delete them, and where your agent tool keeps its local session logs (Claude Code: `~/.claude/projects/`; Codex: `~/.codex/`). Those logs contain what the agent saw.
