# TGDC Data Card

The General Data Company (TGDC) plans every collaboration with a data partner from one short, standard overview of the archive. This file lets your own AI coding agent build that overview on your computer. You review the result and decide whether to send it to us.

## What you get

One PDF in English, `TGDC-Data-Card.pdf`, in two parts:

1. **Data Card:** business, archive, main formats, languages, period, and the context questions you chose to answer.
2. **Data format specification:** every file counted, including the contents of compressed archives. In detail:
   - **Summary:** total volume and file count, company file archive, business mailboxes, the contents of compressed archives and how much of it is new content, unique files by content, the period of the documents and of the e-mails.
   - **Content by category:** documents with a text layer, scanned documents, spreadsheets, presentations, CAD and engineering drawings, photos, audio and video, e-mail, e-signed packages, compressed archives, structured data and more, each with files, unique files and volume.
   - **File formats:** the 35 largest formats by volume.
   - **Mailboxes:** messages, volume, period and format per mailbox, and alternative exports of the same mailbox.
   - **Languages:** the main language of every text file, files with substantial text in two languages, and files where no language could be confirmed.
   - **Compressed archives:** what they contain, how much of it is new content, and what could not be opened.
   - **Technical drawings:** native CAD files, drawing sheets and schematics, sheet sizes from A4 to A0, where the sheets are, and dedicated drawing files versus documents with drawings.
   - **Projects:** drawings within complete project files, projects by number of documented project stages, drawings with bills of quantities, and drawings kept in several versions.
   - **By year:** files, volume and e-mails per year.

The report folder also holds `profile.json` and `spec.json` with the same aggregate numbers.

## What stays private

- Everything runs on your computer. The scripts read only the folder you name and never change, move or delete a file.
- The PDF and the two JSON files contain counts only: no file names, folder names, client names, e-mail addresses or document text.
- Credential and key files are counted by name and never opened.
- Nothing is sent automatically. You decide whether to share the PDF.
- Your agent sees your instructions, the aggregate results and your answers, as with any other task you give it. Your AI provider processes this under the terms of your account.
- Three items hold the file-by-file record and stay on your computer: `manifest.private.json`, `spec-cache.private.sqlite` and the folder `calibration.private`. Do not send them.

## What you need

- macOS or Linux with Python 3.11 or newer. Windows is not supported directly.
- Claude Code, OpenAI Codex or another coding agent that can run commands and read files.
- The path of the folder to examine, and a separate empty folder for the report, outside the folder you examine.
- Free disk space at least as large as the largest compressed archive in the folder, for temporary unpacking.
- For RAR and 7z archives, a tool such as 7-Zip or The Unarchiver helps. ZIP archives need nothing extra. The agent asks before it installs anything.
- Files in Google Drive for desktop: mark the folder as "Available offline" first, or copy it to a local disk.

## Time

The agent reads every file once, including the files inside compressed archives, and reports progress every 1,000 files. A few thousand files take minutes. An archive of several hundred gigabytes can take several hours. If the run stops, starting it again continues where it stopped.

## How to run

1. Open Claude Code or Codex in an empty working folder.
2. Copy the text below into the agent, replace the two paths, and send it.
3. Answer the questions of the agent. You can skip any context question.
4. Review the PDF.
5. If you agree, send `TGDC-Data-Card.pdf` to your contact at TGDC. You may add `profile.json` and `spec.json`, which hold the same aggregate numbers.

```text
Please create a TGDC Data Card with the full data format specification.

1. Download two files from the release tgdc-data-card-v1.1.0 of
   https://github.com/The-General-Data-Company/tgdc-skills
   namely tgdc-data-card-v1.1.0.tar.gz and SHA256SUMS.
   Verify the checksum with: shasum -a 256 -c SHA256SUMS --ignore-missing
   (on Linux: sha256sum -c SHA256SUMS --ignore-missing). Stop if it fails.
2. Unpack the archive into this working folder.
3. Read tgdc-data-card/SKILL.md completely and follow it step by step.
4. The files are in: /path/to/the/archive
   Put the report in: /path/to/an/empty/report/folder
5. Run the Data Card profile and the full data format specification,
   then render one PDF and show it to me.
```

## Questions the agent may ask

- Your confirmation that you may use the selected files and that they contain no health records and no payment card data. The agent reads nothing before this confirmation.
- The company name, one line on the business, and the month of the report.
- Which folders hold exported mailboxes, whose mailbox each one is (for example owner, colleague or shared company mailbox) and the mail provider.
- Which folders hold projects and at which folder level one project begins.
- The usual steps of a project in the order they occur, with the words that mark each step in your file and folder names, in the language of your files, for example offer, contract, site report or acceptance.
- The words that mark a bill of quantities, a technical drawing and a schematic in your file and folder names.
- Any question you skip only leaves the matching rows out of the PDF.
- Optional: whether the agent may look at about 60 low-resolution page images to calibrate the drawing count. Without this, the PDF leaves out the estimate of individual views per drawing.

## Contact

For any question, reply to the person at TGDC who sent you this file. More about us: [thegeneraldata.com](https://thegeneraldata.com).
