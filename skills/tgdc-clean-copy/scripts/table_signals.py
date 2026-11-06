"""Find tables of people and pay tables in spreadsheets and CSV files, hidden sheets included.

Input:  a UTF-8 TSV file with one "<id><TAB><absolute path>" per line (.xlsx, .xlsm, .xls, .csv, .tsv).
        A list that PowerShell wrote in UTF-16 or in the Windows code page is read as well.
        CSV files are read as UTF-8, UTF-16 with a byte order mark, or else Windows-1252 (Excel on
        Windows in Western Europe and the Americas), the same on every operating system.
Output: a CSV with one row per sheet: id, sheet, hidden, rows, person_cols, strong_cols, pay_cols,
        company_cols, hint. hint is P1 (named persons with pay data), P7 (rows are private
        individuals with private data), review, or empty.
Stdout: counts only. Errors go to the log file by id, never with paths or content.

Usage:  python table_signals.py INPUT.tsv OUTPUT.csv LOGFILE
"""
import csv
import locale
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import xlrd

PERSON = ["name", "vorname", "nachname", "first name", "last name", "surname", "employee", "mitarbeiter",
          "pers.-nr", "personalnummer", "personnel number", "imie", "nazwisko", "nome", "prenom", "nom", "naam"]
STRONG = ["geburtsdatum", "geb.", "date of birth", "birth", "dob", "data de nascimento", "data urodzenia",
          "fecha de nacimiento", "date de naissance", "geboortedatum", "strasse", "street", "anschrift",
          "home address", "wohnort", "adresse", "endereco", "adres", "privat", "private", "mobil", "handy",
          "sozialversicherung", "sv-nr", "social security", "steuer-id", "tax id", "cpf", "pesel", "bsn",
          "krankenkasse", "health insurer", "steuerklasse", "tax class", "e-mail privat", "personal email",
          "ssn", "ss#", "itin", "hire date", "date of hire", "termination date", "date of termination",
          "home phone", "personal phone", "filing status", "marital status"]
PAY = ["gehalt", "lohn", "brutto", "netto", "salary", "gross", "net pay", "wage", "salario", "salaire",
       "wynagrodzenie", "loon", "salaris", "steuerklasse", "tax class", "bonus",
       "wages", "pay rate", "hourly rate", "compensation", "withholding", "filing status", "fica"]
COMPANY = ["firma", "company", "unternehmen", "kunde (firma)", "empresa", "razao social", "societe", "firma nazwa",
           "bedrijf", "supplier", "lieferant", "ust-id", "vat", "account name"]
WINDOWS = os.name == "nt"
UTF8_SEQUENCE = re.compile(rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def native(path):
    """The path as the OS opens it. On Windows an extended-length path, so that paths longer than 260
    characters on local disks, mapped drives and network shares open without the long-path setting."""
    path = str(path)
    if not WINDOWS:
        return path
    path = path.replace("/", "\\")
    if path.startswith(("\\\\?\\", "\\\\.\\")):          # already extended, or a device path
        return path
    path = os.path.abspath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def decode(data, fallback):
    """Text from bytes: UTF-16 with a byte order mark, UTF-8 with or without one, else the fallback code page.
    A file that is mostly UTF-8 with a few stray bytes (for example two exports joined) stays UTF-8."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        in_sequences = sum(len(match) for match in UTF8_SEQUENCE.findall(data))
        stray = sum(byte >= 0x80 for byte in data) - in_sequences
        if in_sequences > stray:
            return data.decode("utf-8-sig", errors="replace")
        return data.decode(fallback, errors="replace")


def fold(text):
    text = str(text).lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).strip()


def has(cell, terms):
    return any(t == cell or re.search(r"(^|[^a-z])" + re.escape(t) + r"($|[^a-z])", cell) for t in terms)


def xlsx_sheets(path):
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        for sh in wb.find("m:sheets", NS):
            target = targets[sh.get("{%s}id" % NS["r"])].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            rows = []
            for row in ET.fromstring(z.read(target)).iter("{%s}row" % NS["m"]):
                cells = []
                for c in row.findall("m:c", NS):
                    v = c.find("m:v", NS)
                    t = c.find("m:is/m:t", NS)
                    if c.get("t") == "s" and v is not None:
                        cells.append(shared[int(v.text)])
                    elif t is not None:
                        cells.append(t.text or "")
                    else:
                        cells.append(v.text if v is not None else "")
                rows.append(cells)
            yield sh.get("name"), sh.get("state") in ("hidden", "veryHidden"), rows


def xls_sheets(path):
    book = xlrd.open_workbook(path)
    for sh in book.sheets():
        yield sh.name, sh.visibility != 0, [[str(c.value) for c in sh.row(i)] for i in range(sh.nrows)]


def csv_sheets(path):
    text = decode(Path(path).read_bytes(), "cp1252")
    dialect = csv.Sniffer().sniff(text[:4096]) if text.strip() else csv.excel
    yield "csv", False, list(csv.reader(text.splitlines(), dialect))


def classify(rows):
    best = None
    for header in rows[:5]:
        cells = [fold(c) for c in header if str(c).strip()]
        n = {"person_cols": sum(has(c, PERSON) for c in cells), "strong_cols": sum(has(c, STRONG) for c in cells),
             "pay_cols": sum(has(c, PAY) for c in cells), "company_cols": sum(has(c, COMPANY) for c in cells)}
        if best is None or sum(n.values()) > sum(best.values()):
            best = n
    data_rows = max(len([r for r in rows if any(str(c).strip() for c in r)]) - 1, 0)
    hint = ""
    if best["person_cols"] and best["pay_cols"] and data_rows >= 1:
        hint = "P1"
    elif best["company_cols"] == 0 and best["strong_cols"] >= 2 and data_rows >= 1:
        hint = "P7"
    elif best["company_cols"] == 0 and best["person_cols"] and best["strong_cols"] >= 1:
        hint = "review"
    return {**best, "rows": data_rows, "hint": hint}


def main():
    src, out, log = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    counts = {"files": 0, "sheets": 0, "P1_files": 0, "P7_files": 0, "review_files": 0, "errors": 0}
    fields = ["id", "sheet", "hidden", "rows", "person_cols", "strong_cols", "pay_cols", "company_cols", "hint"]
    with open(out, "w", newline="", encoding="utf-8") as fo, open(log, "a", encoding="utf-8") as lg:
        writer = csv.DictWriter(fo, fieldnames=fields)
        writer.writeheader()
        for line in decode(Path(native(src)).read_bytes(), locale.getpreferredencoding(False)).splitlines():
            if "\t" not in line:
                continue
            fid, p = line.split("\t", 1)
            ext = Path(p).suffix.lower()
            reader = {".xlsx": xlsx_sheets, ".xlsm": xlsx_sheets, ".xls": xls_sheets,
                      ".csv": csv_sheets, ".tsv": csv_sheets}.get(ext)
            if reader is None:
                continue
            counts["files"] += 1
            hints = set()
            try:
                for i, (name, hidden, rows) in enumerate(reader(native(p)), start=1):
                    row = classify(rows)
                    counts["sheets"] += 1
                    hints.add(row["hint"])
                    writer.writerow({"id": fid, "sheet": i, "hidden": int(hidden), **row})
            except Exception as exc:
                counts["errors"] += 1
                lg.write(f"{fid}\ttable_signals\t{type(exc).__name__}\n")
            for h in ("P1", "P7", "review"):
                if h in hints:
                    counts[f"{h}_files"] += 1
                    break
    print(" ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
