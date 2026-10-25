# Evidence field guide

This is a target catalog for describing selected enterprise data, not a mandatory questionnaire or 123 automatic features.

## Current automatic evidence

V1 produces exactly 16 evidence metrics, listed below. Scope, configuration, extraction outcomes and language unknown reasons accompany them. Each metric carries value, unit, basis, denominator, method and coverage. Null is not zero. Most metrics are measurements; English share is bounded language inference. Provider claims never overwrite these values.

- `selected_files`
- `documents_processed`
- `files_with_extracted_text`
- `selected_bytes`
- `format_file_shares`
- `duplicate_files`
- `rows_processed`
- `rows_known_total`
- `pdf_pages_processed`
- `pdf_pages_known_total`
- `pdf_no_text_pages`
- `extracted_characters`
- `extracted_words`
- `filesystem_mtime_bounds`
- `eml_content_date_bounds`
- `english_document_share`

Inventory counts only enumerated selected roots. Incomplete enumeration is explicit. Sampling, extraction, hashing, rows, PDF pages and text have distinct coverage. Arbitrary extensions are grouped as other to avoid exposing identifiers. Duplicate-file evidence is exact byte identity of completely hashed files, not semantic deduplication.

The first non-empty row of CSV/TSV and each XLSX worksheet is treated as a header. This is a parsing assumption, not evidence that a header exists. Headerless exports may lose their first record; multi-row headers may be counted as records. Do not claim unconditional record totals without checking that assumption. Capped processed rows and known sample totals remain distinct; worksheet dimensions are not populated-row totals. Files, rows, messages and completed cases are different units.

Language inference uses bounded dominant-language samples. The denominator is all attempted sampled supported files, including unknown/failed/empty content; unknown reasons remain visible. Classified-English share describes this examined sample, not the true population or a mathematical lower bound. Extension stratification does not prove representativeness across topics or years. Filesystem modification times do not establish original content dates; sampled EML Date headers are assertions, not verified business events.

## Optional descriptions and missing evidence

The [structured catalog](field-catalog.json) contains 123 stable field IDs grouped by dataset, system, document, record, case and governance. `v1_status` distinguishes automatic scope metadata, partial/private automatic evidence, optional provider context, and external evidence not implemented by the scanner. A mapping is qualified; for example extension inventory is not verified MIME distribution, and bounded language inference is not all-corpus language coverage. The scanner does not count real tokens or infer source systems, legal rights, topics, confirmed joins, complete history, case outcomes or action traces. The catalog entries are design choices; none implies that the local scanner implements OCR or source-system integrations.

These descriptions may guide future analysis or explicit provider answers. They are not permission to read raw identifiers, table values, source paths, body text or confidential links into model context or shared aggregates. Document text and per-file references in a target data model remain private by default; v1 does not retain body text in either JSON output.

Ask at most three focused questions per message, starting with relevant missing context and reusing previous explicit answers. Allow skipped questions and immediate drafts. A provider may add plain-string `answers.json` values under catalog IDs; the renderer retains them as provider declarations in the appendix. Do not populate empty fields with guessed values or certify rights/privacy. All provider answers require preview because the answers themselves may be sensitive.

A passive archive records what exists; it does not by itself show that workflows ran to completion. Confirmed case links require a declared join method and checked denominator. An outcome requires actual evidence; action traces must be recorded actions, never invented reasoning.

Only on a separate request, describe a sampling plan with its unit, frame, selection method, exclusions and uncertainty. Preserve documented join keys in the proposed plan while avoiding raw-value copying. A sample of 100 documents is not a universal minimum, a representative-population proof or 100 complete cases.
