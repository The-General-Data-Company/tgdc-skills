# Configuration and artifacts

## B4. Configuration

Fill this block at checkpoint 1, write it to `_private/config.json`, and show it back to the operator.

```
SOURCE:            <absolute path, read-only>
RUN_DIR:           <the folder you were started in>
COUNTRIES:         <where the company and its employees are, for example DE, BR, IN, US with states such as US-CA, US-IL>
LANGUAGES:         <languages on the drive; detection covers all of them>
ACCOUNT:           <provider and plan, as stated by the operator>
CONTENT_MODE:      metadata-only | masked-excerpts | local-model
NDA_PARTNERS:      <companies the operator has NDAs with; completed at checkpoint 2 from the NDAs you find>
SKIP_FOLDERS:      <folders the operator wants left out without scanning; default none>
QA_SAMPLE:         299 (default) | 149 | 59 | 598
SIGNATORY:         <name and role of the person who signs the attestation>
```

## B10. Artifacts

```
clean-copy-run/
  .claude/skills/ or .agents/skills/   the installed skill (never edited)
  clean-copy/              deliverable: copied files only, nothing else
  report/                  shareable
    run_report.md
    SHA256SUMS.txt
    attestation_draft.md
  _private/                never leaves the company
    config.json  PROGRESS.md  manifest.sqlite (or manifest.csv)
    snapshot_before.csv  snapshot_after.csv  allow_list.txt
    gate_folders.csv  nda_found.csv  pilot.csv  review_list.csv  qa_sample.csv  overexclusion_sample.csv
    scripts/  logs/  cache/  tmp/
```

**Manifest columns:** manifest_id, relative_path, parent_id (for members of emails, archives, mailboxes, PDFs), size, mtime, sha256, magic_type, extension, placeholder, pages, ocr_pages, readable (yes or no), rule_hits (rule IDs with page numbers), category, verdict (`copy`, `exclude`, `review`, `skipped`), decided_by (`rule`, `operator`, `default`), reason (rule IDs only, no content), copied_sha256.

Write everything in `report/` in American English, whatever language you use in the chat.

**run_report.md** (counts only; no file names, no folder names below the top level, no content):
1. Run: dates, run duration, agent tool and version, account type, CONTENT_MODE, OS, tool versions (tesseract, poppler, qpdf, exiftool, readpst, Python packages).
2. Inventory: files and bytes, by type and by language where detected.
3. Reconciliation: inventory = copied + excluded by rule + excluded by operator + excluded by default (unanswered review) + skipped (junk, online-only). State that the totals match.
4. Exclusions by category code (P1 to P7, N1, N2), file counts each, and containers excluded because of single members.
5. Copied files whose content could not be read, by format (they were decided by name, folder, and metadata).
6. Method: gates, metadata, every page extracted, OCR with orientation, detectors, review list, number of NDA partners (without names).
7. Quality check: sample size, seed, findings, the supported claim from B9, over-exclusion check result.
8. Integrity: copied files verified by SHA-256; source snapshot unchanged.
9. Incidental personal data: copied files contain incidental names (contacts, signatures, metadata authors). They are pseudonymized by The General Data Company under the data processing agreement before any use.
10. Reminders for the responsible person (not legal advice): sign the data processing agreement before sharing; a lawful basis for passing on the incidental personal data (in the EU, Art. 6(1)(f) or a compatibility check under Art. 6(4) GDPR); privacy notices that name data licensees as recipients; whether a data protection impact assessment is needed; in Germany, whether the works council must be involved; in California, for a business subject to the CCPA, that licensing records with personal information of residents, employees and business contacts included, is a sale under the CCPA, which requires a notice at collection, an opt-out, and a risk assessment before the sale (CCPA regulations, Section 7150).

**attestation_draft.md** (filled only from the manifest and QA results):

> On [date], [company] ran an on-premise screening of [N] files ([size]) from [system description]. Records whose primary content is personal data of natural persons, including employee files, HR records, payroll documents, identity documents, applications, health records, and private correspondence, as well as non-disclosure agreements and material received under them, were identified and excluded before delivery. [E] files were excluded and [K] files were copied unchanged. A random sample of [n] copied files was reviewed by [name, role] with [findings] findings. Incidental personal data in business records remains for pseudonymization under the data processing agreement.
>
> Name, role, date, signature
