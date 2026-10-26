"""Static format knowledge: categories, extension labels, credential names, page sizes, keywords."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import spec_taxonomy as T


class TaxonomyTests(unittest.TestCase):
    def test_sixteen_reference_categories_in_order(self):
        self.assertEqual(len(T.CATEGORIES), 16)
        self.assertEqual(T.CATEGORIES[0], 'Documents (text: Word, PDF with text layer, plain text)')
        self.assertEqual(T.CATEGORIES[1], 'Scanned documents (image-only PDF, TIFF)')
        self.assertEqual(T.CATEGORIES[7], 'E-mail messages and mailbox files (incl. per-message metadata)')
        self.assertEqual(T.CATEGORIES[8], 'E-signed document packages (CAdES .p7s / .p7m)')
        self.assertEqual(T.CATEGORIES[11], 'Credential and key files: certificates, key stores, password files (counted, never parsed)')
        self.assertEqual(T.CATEGORIES[-1], 'Other formats')
        self.assertEqual(T.short_category(T.CATEGORIES[11]), 'Credential and key files: certificates, key stores, password files')
        self.assertEqual(T.short_category(T.CATEGORIES[0]), 'Documents')

    def test_categories_by_extension(self):
        cases = {'a.docx': T.DOCUMENTS, 'a.txt': T.DOCUMENTS, 'a.tif': T.SCANNED, 'a.xlsx': T.SPREADSHEETS,
                 'a.pptx': T.PRESENTATIONS, 'a.dwg': T.CAD, 'a.skp': T.CAD, 'a.jpg': T.PHOTOS, 'a.mp4': T.AUDIO_VIDEO,
                 'a.eml': T.EMAIL, 'a.mbox': T.EMAIL, 'a.p7s': T.ESIGNED, 'a.p7m': T.ESIGNED, 'a.rar': T.ARCHIVES,
                 'a.xml': T.STRUCTURED, 'a.bak': T.STRUCTURED, 'a.log': T.SYSTEM, 'a.db': T.SYSTEM, 'a.php': T.SOURCE,
                 'a.ttf': T.SOURCE, 'a.exe': T.PROGRAMS, 'a.bat': T.PROGRAMS, 'a.cdr': T.OTHER, 'noext': T.OTHER}
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(T.category_of(name), category)

    def test_pdf_depends_on_text_layer_and_json_csv_in_a_mailbox_are_mail(self):
        self.assertEqual(T.category_of('a.pdf', pdf_scanned=False), T.DOCUMENTS)
        self.assertEqual(T.category_of('a.pdf', pdf_scanned=True), T.SCANNED)
        self.assertEqual(T.category_of('m.json'), T.STRUCTURED)
        self.assertEqual(T.category_of('m.json', mail_metadata=True), T.EMAIL)
        self.assertEqual(T.category_of('m.csv', mail_metadata=True), T.EMAIL)
        self.assertEqual(T.category_of('table.csv'), T.SPREADSHEETS)
        self.assertEqual(T.category_of('~$draft.docx'), T.SYSTEM)

    def test_credential_files_by_name_and_extension_only(self):
        for name in ['server.p12', 'cert.pfx', 'id.pem', 'x.key', 'x.keystore', 'x.jks', 'db.kdbx',
                     'Passwords 2020.xlsx', 'parola cont.docx', 'Parolă.txt', 'my_credentials.pdf', 'PASSWORD.doc']:
            with self.subTest(name=name):
                self.assertTrue(T.is_credential_name(name))
                self.assertEqual(T.category_of(name), T.CREDENTIALS)
        for name in ['report.pdf', 'passport scan.jpg', 'compass.dwg', 'plan.docx', 'bypass design.pdf']:
            with self.subTest(name=name):
                self.assertFalse(T.is_credential_name(name))

    def test_a_path_is_sensitive_when_any_component_is_hidden_or_credential_like(self):
        sensitive = [['home', '.ssh', 'known_hosts'], ['a', 'id_ed25519'], ['service-account.json'], ['oauth_client.json'], ['token.txt'],
                     ['tokens.json'], ['accounts.csv'], ['api_key.txt'], ['API-Key backup.txt'], ['private_key.txt'], ['My Passwords', 'list.txt'],
                     ['ACME Credentials', 'x.txt'], ['mail', 'key.asc'], ['x.gpg'], ['x.PGP'], ['vpn', 'office.ovpn'], ['.env'], ['a', 'x.pem'],
                     ['secrets', 'readme.txt'], ['passwort.txt']]
        for parts in sensitive:
            with self.subTest(parts=parts):
                self.assertTrue(T.is_sensitive_path(parts))
        for parts in [['docs', 'report.pdf'], ['passport scan.jpg'], ['compass', 'a.dwg'], ['.', 'a.txt'], ['', 'a.txt'], ['therapy keys.txt'],
                      ['api documentation.pdf'], ['private notes', 'a.txt']]:
            with self.subTest(parts=parts):
                self.assertFalse(T.is_sensitive_path(parts))

    def test_extension_labels(self):
        self.assertEqual(T.extension_label('a.PDF'), 'pdf')
        self.assertEqual(T.extension_label('README'), '(no extension)')
        self.assertEqual(T.extension_label('Report v2. final draft'), '(other / irregular extension)')
        self.assertEqual(T.extension_label('x.abcdefghij'), '(other / irregular extension)')
        self.assertEqual(T.extension_label('x.12345'), '(other / irregular extension)')
        self.assertEqual(T.extension_label('x.001', multipart_part=True), '001')
        self.assertEqual(T.extension_label('x.001'), '(other / irregular extension)')

    def test_only_known_formats_are_ever_written_as_extensions(self):
        for name in ['J.Smith', 'Report ACME.acmecorp', 'john.doe@acme.corp', 'Quarterly.zebracli', 'a.abc', 'x.r01', 'archive.z01']:
            with self.subTest(name=name):
                self.assertEqual(T.extension_label(name), T.IRREGULAR_EXTENSION)
        for name, label in [('a.DWG', 'dwg'), ('a.p12', 'p12'), ('a.mbox', 'mbox'), ('a.7z', '7z')]:
            self.assertEqual(T.extension_label(name), label)
        self.assertEqual(T.extension_label('x.r01', multipart_part=True), 'r01')
        self.assertEqual(T.extension_label('x.z12', multipart_part=True), 'z12')
        self.assertEqual(T.extension_label('x.Smith', multipart_part=True), T.IRREGULAR_EXTENSION)
        self.assertEqual(T.label_of_extension('bob miller backup 2020'), T.IRREGULAR_EXTENSION)
        self.assertEqual(T.label_of_extension(''), T.NO_EXTENSION)

    def test_the_label_allowlist(self):
        for label in ['pdf', 'dwg', '(no extension)', '(other / irregular extension)', '001', 'r01', 'z99']:
            self.assertTrue(T.is_extension_label(label), label)
        for label in ['smith', 'PDF', '.pdf', '', 'pdf ', '(other)', '0001', 'r1', None, 5, ['pdf']]:
            self.assertFalse(T.is_extension_label(label), label)

    def test_declared_labels_are_short_and_are_not_addresses_or_links(self):
        for label in ['Mailbox 1 (owner, Gmail)', 'x' * 60, 'tender documents']:
            self.assertTrue(T.is_short_label(label), label)
        for label in ['', '   ', 'x' * 61, 'owner@example.test', 'https://example.test', 'a\nb', None, 3]:
            self.assertFalse(T.is_short_label(label), label)

    def test_page_size_classes_with_three_percent_tolerance_in_either_orientation(self):
        mm = 72 / 25.4
        self.assertEqual(T.size_class(210 * mm, 297 * mm)[:2], ('A4', 'A4'))
        self.assertEqual(T.size_class(297 * mm, 210 * mm)[:2], ('A4', 'A4'))
        self.assertEqual(T.size_class(420 * mm * 1.02, 297 * mm * 0.98)[0], 'A3')
        self.assertEqual(T.size_class(594 * mm, 841 * mm)[0], 'A1')
        self.assertEqual(T.size_class(841 * mm, 1189 * mm)[0], 'A0')
        self.assertEqual(T.size_class(215.9 * mm, 279.4 * mm)[1], 'US Letter / Legal / Tabloid')
        self.assertEqual(T.size_class(279.4 * mm, 431.8 * mm)[1], 'US Letter / Legal / Tabloid')
        name, label, larger = T.size_class(300 * mm, 900 * mm)
        self.assertEqual((label, larger), ('other / custom size', True))
        name, label, larger = T.size_class(100 * mm, 150 * mm)
        self.assertEqual((label, larger), ('other / custom size', False))
        self.assertTrue(T.size_class(297 * mm, 420 * mm)[2])
        self.assertFalse(T.size_class(210 * mm, 297 * mm)[2])

    def test_keywords_ignore_case_and_diacritics_and_prefer_whole_tokens(self):
        self.assertTrue(T.matches_any(['studii și avize'], 'STUDII SI AVIZE/plan.pdf'))
        self.assertTrue(T.matches_any(['contract'], 'Contractul semnat.pdf'))   # long keyword, token prefix
        self.assertFalse(T.matches_any(['pt'], 'Receipt_final.pdf'))            # short keyword, whole token only
        self.assertTrue(T.matches_any(['pt'], 'pt faza.pdf'))
        self.assertTrue(T.matches_any(['bill of quantities'], 'x/Bill_of_Quantities_final.xlsx'))
        self.assertFalse(T.matches_any(['bill of quantities'], 'quantities bill.xlsx'))

    def test_default_thresholds_and_number_formats(self):
        self.assertEqual(T.default_thresholds(7), [4, 5, 6])
        self.assertEqual(T.default_thresholds(9), [5, 6, 7])
        self.assertEqual(T.default_thresholds(4), [2, 3, 4])
        self.assertEqual(T.default_thresholds(5), [3, 4, 5])

    def test_default_thresholds_never_exceed_the_number_of_stages(self):
        for stages in range(1, 12):
            with self.subTest(stages=stages):
                thresholds = T.default_thresholds(stages)
                self.assertTrue(thresholds)
                self.assertTrue(all(1 <= k <= stages for k in thresholds), thresholds)
                self.assertEqual(thresholds, sorted(set(thresholds)))
        self.assertEqual(T.default_thresholds(1), [1])
        self.assertEqual(T.default_thresholds(2), [1, 2])
        self.assertEqual(T.default_thresholds(3), [2, 3])
        self.assertEqual(T.count(1234567), '1,234,567')
        self.assertEqual(T.gigabytes(290.33e9), '290.33')
        self.assertEqual(T.gigabytes(12.3456e9, 3), '12.346')
        self.assertEqual(T.size_text(5.4e9), '5.4 GB')
        self.assertEqual(T.size_text(24.4e6), '24.4 MB')
        self.assertEqual(T.size_text(2500), '2.5 KB')
        self.assertEqual(T.round_to(10850, 100), 10900)
        self.assertEqual(T.round_to(25, 10), 30)
        self.assertEqual(T.year_range(2016, 2026), '2016-2026')
        self.assertEqual(T.year_range(2026, 2026), '2026')


if __name__ == '__main__':
    unittest.main()
