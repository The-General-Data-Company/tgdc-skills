"""The shipped detectors give the same results on Windows, macOS and Linux: file lists in every encoding a shell
writes, names outside ASCII, paths longer than 260 characters on Windows, CSV files from Excel, and tesseract and
the face model found where their installers put them. Needs tesseract and the packages of assets/requirements.txt.

Run: python -m unittest discover -s tests"""
import csv
import io
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pymupdf

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import id_signals
import table_signals

WINDOWS = os.name == "nt"
# The specimen passport of ICAO Doc 9303, with valid check digits.
MRZ = ["P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<", "L898902C36UTO7408122F1204159ZE184226B<<<<<10"]


def run(script, *arguments, env=None):
    result = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, arguments)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=env, timeout=600)
    return result


def counts(stdout):
    return dict(part.split("=", 1) for part in stdout.split())


def rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def pdf(path, lines=()):
    with pymupdf.open() as doc:
        page = doc.new_page(width=842, height=595)
        for i, line in enumerate(lines):
            page.insert_text((40, 300 + 34 * i), line, fontname="cour", fontsize=22)
        doc.save(id_signals.native(path))


class Folder(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="tgdc-clean-copy-"))
        self.addCleanup(shutil.rmtree, id_signals.native(self.base), True)
        self.data = self.base / "Prüfung Ausweis ä"
        self.data.mkdir()

    def deep(self):
        """A folder whose files have paths longer than 260 characters (Windows only)."""
        folder = id_signals.native(self.data)
        for i in range(6):
            folder = os.path.join(folder, f"Unterordner mit langem Namen Nummer {i} äöü")
        os.makedirs(folder)
        return folder.removeprefix("\\\\?\\")

    def listing(self, entries, encoding="utf-8"):
        path = self.base / "input.tsv"
        text = "".join(f"{fid}\t{p}\r\n" if WINDOWS else f"{fid}\t{p}\n" for fid, p in entries)
        path.write_bytes(text.encode(encoding))
        return path


class IdSignals(Folder):
    def test_passport_page_is_P4_and_a_blank_page_has_no_hint(self):
        passport, blank = self.data / "Scan 07 Ä.pdf", self.data / "Leer ß.pdf"
        pdf(passport, MRZ)
        pdf(blank)
        out, log = self.base / "id.csv", self.base / "id.log"
        result = run("id_signals.py", self.listing([("F000001", passport), ("F000002", blank)]), out, log)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(counts(result.stdout), {"files": "2", "pages": "2", "P4_files": "1", "review_files": "0",
                                                 "errors": "0"})
        self.assertEqual({row["id"]: row["hint"] for row in rows(out)}, {"F000001": "P4", "F000002": ""})
        self.assertNotIn("Scan", result.stdout + result.stderr)

    def test_file_lists_in_utf16_utf8_with_bom_and_the_code_page(self):
        passport = self.data / "Ausweis.pdf"
        pdf(passport, MRZ)
        encodings = ["utf-16", "utf-8-sig", "utf-8"]
        if locale.getpreferredencoding(False).lower().replace("-", "") not in ("utf8", "ascii"):
            encodings.append(locale.getpreferredencoding(False))
        for encoding in encodings:
            with self.subTest(encoding=encoding):
                out = self.base / f"id-{encoding}.csv"
                result = run("id_signals.py", self.listing([("F000001", passport)], encoding), out, self.base / "x.log")
                self.assertEqual(counts(result.stdout)["P4_files"], "1", result.stdout + result.stderr)

    @unittest.skipUnless(WINDOWS, "paths over 260 characters need an extended-length path only on Windows")
    def test_path_longer_than_260_characters(self):
        passport = os.path.join(self.deep(), "Ausweis Kopie.pdf")
        self.assertGreater(len(passport), 260)
        pdf(passport, MRZ)
        result = run("id_signals.py", self.listing([("F000001", passport)]), self.base / "id.csv", self.base / "id.log")
        self.assertEqual(counts(result.stdout)["P4_files"], "1", result.stdout + result.stderr)

    def test_face_model_loads_from_a_folder_with_letters_outside_ascii(self):
        import cv2
        folder = self.base / "Jürgen Ä"
        folder.mkdir()
        shutil.copy(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml", folder)
        self.assertFalse(id_signals.load_cascade(folder).empty())
        self.assertTrue(id_signals.load_cascade(self.base).empty())

    def test_missing_tesseract_stops_with_exit_code_2(self):
        elsewhere = {"PROGRAMFILES": str(self.base), "PROGRAMFILES(X86)": str(self.base), "LOCALAPPDATA": str(self.base)}
        arguments = ["id_signals.py", str(self.listing([])), str(self.base / "id.csv"), str(self.base / "id.log")]
        with mock.patch.object(id_signals.shutil, "which", return_value=None), mock.patch.dict(os.environ, elsewhere):
            self.assertIsNone(id_signals.find_tesseract())
            with mock.patch.object(sys, "argv", arguments), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                with self.assertRaises(SystemExit) as stop:
                    id_signals.main()
        self.assertEqual((stop.exception.code, out.getvalue().strip()), (2, "error=tesseract_not_found"))

    @unittest.skipUnless(WINDOWS, "the install folder of the Windows installer")
    def test_tesseract_is_found_in_its_install_folder_without_path(self):
        folder = self.base / "Tesseract-OCR"
        folder.mkdir()
        (folder / "tesseract.exe").write_bytes(b"")
        with mock.patch.object(id_signals.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"PROGRAMFILES": str(self.base)}):
            self.assertEqual(id_signals.find_tesseract(), str(folder / "tesseract.exe"))

    def test_tesseract_is_found(self):
        self.assertIsNotNone(id_signals.find_tesseract())


def xlsx(path, sheets):
    """A minimal workbook with inline strings; sheets is a list of (name, hidden, rows)."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(table_signals.native(path), "w") as z:
        state = {True: ' state="hidden"', False: ""}
        entries = "".join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"{state[hidden]}/>'
                          for i, (name, hidden, _) in enumerate(sheets, start=1))
        z.writestr("xl/workbook.xml", f"<workbook {ns} {rel}><sheets>{entries}</sheets></workbook>")
        links = "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{links}</Relationships>')
        for i, (_, _, data) in enumerate(sheets, start=1):
            body = "".join("<row>" + "".join(f'<c t="inlineStr"><is><t>{v}</t></is></c>' for v in row) + "</row>"
                           for row in data)
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")


class TableSignals(Folder):
    def check(self, entries, expected, encoding="utf-8"):
        out = self.base / "tables.csv"
        result = run("table_signals.py", self.listing(entries, encoding), out, self.base / "tables.log")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(counts(result.stdout)["errors"], "0", result.stdout)
        self.assertEqual({row["id"]: row["hint"] for row in rows(out)}, expected)
        return rows(out)

    def test_csv_files_from_excel_in_every_encoding(self):
        staff = "Name;Straße;Geburtsdatum\nMüller;Hauptstraße 1;01.02.1980\nSchön;Weg 2;03.04.1985\n"
        pay = "Personalnummer,Name,Gehalt\n17,Jürgen Groß,4200\n"
        vendors = "Firma;Straße;Ort\nMüller GmbH;Hauptstraße 1;Köln\n"
        files = {"Mitarbeiter ä.csv": (staff, "cp1252"), "Lohn.csv": (pay, "utf-16"),
                 "Lieferanten.csv": (vendors, "utf-8-sig"), "Liste.tsv": (pay.replace(",", "\t"), "utf-8")}
        for name, (text, encoding) in files.items():
            (self.data / name).write_bytes(text.replace("\n", "\r\n").encode(encoding))
        entries = [(f"F{i:06d}", self.data / name) for i, name in enumerate(files, start=1)]
        self.check(entries, {"F000001": "P7", "F000002": "P1", "F000003": "", "F000004": "P1"})

    def test_mostly_utf8_csv_with_a_stray_byte_stays_utf8(self):
        staff = "Name;Straße;Geburtsdatum;Gehalt\n".encode("utf-8") + b"M\xfcller;Weg 1;01.01.1980;4200\n"
        (self.data / "Export.csv").write_bytes(staff)
        self.assertIn("Straße", table_signals.decode(staff, "cp1252"))
        self.check([("F000001", self.data / "Export.csv")], {"F000001": "P1"})

    @unittest.skipUnless(WINDOWS, "extended-length paths exist only on Windows")
    def test_extended_paths(self):
        cwd = os.getcwd()
        for given, expected in [("C:\\a\\b.csv", "\\\\?\\C:\\a\\b.csv"), ("C:/a/b.csv", "\\\\?\\C:\\a\\b.csv"),
                                ("\\\\server\\share\\a.csv", "\\\\?\\UNC\\server\\share\\a.csv"),
                                ("//server/share/a.csv", "\\\\?\\UNC\\server\\share\\a.csv"),
                                ("\\\\?\\C:\\a.csv", "\\\\?\\C:\\a.csv"), ("//?/C:/a.csv", "\\\\?\\C:\\a.csv"),
                                ("\\\\.\\C:\\a.csv", "\\\\.\\C:\\a.csv"), ("C:\\a\\..\\b.csv", "\\\\?\\C:\\b.csv"),
                                ("rel.csv", "\\\\?\\" + os.path.join(cwd, "rel.csv"))]:
            with self.subTest(given=given):
                self.assertEqual(table_signals.native(given), expected)
                self.assertEqual(id_signals.native(given), expected)

    def test_hidden_sheet_with_pay_data(self):
        book = self.data / "Übersicht.xlsx"
        xlsx(book, [("Projekte", False, [["Projekt", "Firma"], ["Halle 3", "Bau AG"]]),
                    ("Daten", True, [["Mitarbeiter", "Brutto", "Netto"], ["A. Groß", "4200", "2700"]])])
        sheets = self.check([("F000001", book)], {"F000001": "P1"})
        self.assertEqual([(row["sheet"], row["hidden"], row["hint"]) for row in sheets], [("1", "0", ""), ("2", "1", "P1")])

    def test_file_list_in_utf16(self):
        (self.data / "Lohn.csv").write_text("Name,Gehalt\nGroß,4200\n", encoding="utf-8")
        self.check([("F000001", self.data / "Lohn.csv")], {"F000001": "P1"}, encoding="utf-16")

    @unittest.skipUnless(WINDOWS, "paths over 260 characters need an extended-length path only on Windows")
    def test_path_longer_than_260_characters(self):
        csv_path = os.path.join(self.deep(), "Gehaltsliste.csv")
        Path(table_signals.native(csv_path)).write_bytes("Name;Gehalt\r\nGroß;4200\r\n".encode("cp1252"))
        self.check([("F000001", csv_path)], {"F000001": "P1"})


if __name__ == "__main__":
    unittest.main()
