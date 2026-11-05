# Exclusion policy

## B5. What is excluded

Only these files are excluded. The test is the definition used in our data license agreements: records whose primary content is personal data of natural persons, including employee files, HR records, payroll documents, and correspondence containing private contact details. Business documents in which people appear as contacts, senders, signatories, or authors are not excluded.

**Personal data files**

| Code | Category | Typical files |
|---|---|---|
| P1 | Pay | Payslips and pay stubs, salary and wage transfers (purpose such as Gehalt, Lohn, salary, payroll), salary advances, payroll registers, staff cost lists, wage tax certificates (US Form W-2; Forms 1099-NEC and 1099-MISC issued to an individual), withholding elections (US Form W-4 and its state counterparts), per-employee wage reports to state agencies (for example California DE 9C), certified payroll on public works (US Form WH-347), PEO invoices that itemize each employee's gross and net pay, wage garnishment and child support withholding orders |
| P2 | Personnel records | Employment and freelance contracts with individuals, offer letters, amendments, terminations, separation agreements, loans to employees, performance reviews, warnings, references, criminal record certificates, background check reports and motor vehicle records, personnel questionnaires and registration forms (birth date, home address, social security number, health insurer, private bank details), self-identification forms (race, ethnicity, disability, veteran status), benefits enrollment and beneficiary forms |
| P3 | Leave and health | Vacation requests, sick notes, medical certificates, health insurance forms and claims, prescriptions, occupational health records, medical leave certifications (US FMLA forms), disability accommodation requests, workers' compensation claims and injury reports (US OSHA Form 301 and the OSHA 300 log), drug and alcohol test results |
| P4 | Identity documents | Passports, ID cards, driver's licenses, residence permits, visas, Social Security cards, permanent resident and employment authorization cards, US Form I-9 with its document copies, also as a single page inside another file (often shipping papers) |
| P5 | Applications | CVs, resumes, applications, cover letters, interview notes and scorecards |
| P6 | Private correspondence | Private letters and emails of employees or other individuals, private bank statements, individual tax returns (US Form 1040), consumer credit reports, family documents |
| P7 | Tables of people | Spreadsheets or databases whose rows are private individuals with private data: staff lists with birth dates, salaries, or home addresses; employee census files for benefit and retirement plans; consumer customer databases |

**NDAs**

| Code | Category | Typical files |
|---|---|---|
| N1 | NDA documents | Non-disclosure and confidentiality agreements themselves |
| N2 | Material under NDA | Files received from, or exchanged with, a partner in NDA_PARTNERS about the confirmed subject of the NDA; documents that state they are provided under an NDA. Normal trade documents with an NDA partner (offers, orders, price lists, invoices, payments, certificates, contracts, emails) are copied unless they concern the NDA subject. |

**Copied (examples, not exhaustive)**

Invoices, purchase orders, quotes, delivery notes, shipping papers (with driver names and plates), certificates, specifications, drawings, quality and test records, business correspondence, contracts between companies, price lists, legal and tax correspondence, internal documents marked confidential, expense receipts, commission tables, payments to individuals without a salary purpose, training certificates, empty form templates, aggregated HR statistics, employer filings with totals only (US Forms 941, 940, W-3, 1096, and 5500, OSHA Form 300A, EEO-1 reports), emails and mailboxes without B5 content, images, audio, video, CAD, databases, and files in any language or format. Names of suppliers, customers, sellers, and partners stay.

**Review list (operator decides; blank means exclude)**

A page that looks like an image of a document with a face but no readable text (possible ID copy); a file that looks like P1 to P7 but whose text could not be read reliably; a document with a single personal ID number and otherwise business content (for example a US Form W-9 of a sole proprietor in a vendor file); a document that mentions an NDA but whose partner is not in NDA_PARTNERS; any file where your signals disagree.

**Country notes**
- Germany: payslips also reveal religion (church tax), which makes them doubly sensitive; disability (Schwerbehinderung), maternity (Mutterschutz), and return-to-work (BEM) files are health records (P3).
- Brazil: health data must never be shared for economic advantage (LGPD Art. 11).
- United States: medical information held by an employer must stay in separate confidential files (ADA, FMLA, GINA; in California also the CMIA), so it is P3 wherever it sits. This includes drug and alcohol test results (49 CFR 40.321) and the OSHA 300 log and Form 301, which may be disclosed to outsiders only without names. Biometric data, such as time-clock fingerprint templates, belongs to P2: Illinois (BIPA) bars any profit from it and Colorado bars its sale, with or without consent. Motor vehicle records in driver files are P2 (Driver's Privacy Protection Act). Form I-9 and its document copies may be used only for immigration enforcement (8 CFR 274a.2) and are P4. Client tax returns held by an accountant, tax preparer, or payroll firm are P6 (26 U.S.C. 7216). A precise location history of a named person goes to the review list; telematics keyed to vehicles are copied.
- India: files about anyone under 18 are P2 or P6, never business records.

## B6. Decision rules

1. **Three questions for "primary content".** Ask them in this order:
   - Subject: would this file naturally sit in a personnel, payroll, patient, applicant, or ID file about one person? Yes: exclude.
   - Purpose: was it made to pay, employ, evaluate, or verify one individual, or is it someone's private matter? Yes: exclude.
   - Strip: remove all names, signatures, and contact details in your mind. Is what is left a business record? Yes: copy. A record of one person's pay, job, health, or private life? Exclude.
2. **Worst part wins.** An ID copy or a health record on any page excludes the whole file. One ID copy on page 7 of a shipping file excludes that file.
3. **Containers follow their members.** An email with its attachments, an archive with its members, and a mailbox store with its messages are one file. If any member is excluded, the whole container is excluded, because files are never edited. Record how many members caused it, so the operator can remove those items and re-run if the container matters. A mailbox store that cannot be expanded at all goes to the review list.
4. **Duplicates share a verdict.** Files with the same SHA-256 get the same verdict.
5. **HR folders are strong signals.** Files under folders named for HR, payroll, personnel, applications, IDs, health, or private matters (B8) are excluded by default. At checkpoint 2 the operator may release a folder after looking at it in `_private/gate_folders.csv`.
6. **Doubt goes to the review list.** Never resolve doubt about a B5 category in favor of copying.
