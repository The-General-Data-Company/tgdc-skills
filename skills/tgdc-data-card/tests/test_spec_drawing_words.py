"""Default drawing and schematic words in the languages of common archives, and the partner words that replace them."""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import spec_aggregate as A
import spec_samples as S

LOG = open(os.devnull, 'w')
NOW = datetime(2026, 10, 25)
ROMANIAN = ['Plansa A-03 fatada nord.pdf', 'Plan de situatie.pdf', 'Plan de situație.pdf', 'Planșa 2.pdf', 'Secțiune transversală.pdf',
            'Detaliu fundatie.pdf', 'Fațadă sud.pdf', 'Releveu teren.pdf', 'Schiță amplasare.pdf', 'Desene execuție.pdf', 'Plan parter.pdf']
GERMAN = ['Zeichnung 12.pdf', 'Plan 3.pdf', 'Schnitt A-A.pdf', 'Ansicht Nord.pdf', 'Grundriss EG.pdf', 'Detail Fenster.pdf', 'Lageplan.pdf',
          'Zeichnungen Rohbau.pdf']
OTHER = ['Plan de masse.pdf', 'Coupe BB.pdf', 'Façade sud.pdf', 'Tavola 4.pdf', 'Sezione A.pdf', 'Prospetto nord.pdf', 'Plano 2.pdf',
         'Sección A.pdf', 'Alzado frontal.pdf', 'Rysunek 5.pdf', 'Przekrój A-A.pdf', 'Tekening 7.pdf', 'Doorsnede C.pdf']
SCHEMATICS = ['Schema electrica tablou.pdf', 'Schaltplan Steuerung.pdf', 'Stromlaufplan Halle 2.pdf', 'Schéma électrique.pdf']
NOT_DRAWINGS = ['Offerte maart.pdf', 'invoice 2020.pdf', 'Rapport financier.pdf', 'Zahlungsplan.pdf', 'Planet report.pdf', 'Contract.pdf',
                'Factura 123.pdf', 'Protokoll Sitzung.pdf']


def evaluate(name, drawing=None, schematic=None):
    """(drawing pages, schematic flag) of a three-page, text-only A4 PDF with this name."""
    sheets = A.Sheets(drawing or A.DRAWING_DEFAULTS, schematic or A.SCHEMATIC_DEFAULTS)
    sheets.add('sha', {'classes': {'A4': [3, 0]}}, 'Company/' + name, 'file')
    _, drawn, _, flag, _ = sheets.evaluate('sha')
    return drawn, flag


class DefaultWordTests(unittest.TestCase):
    def test_romanian_german_and_other_names_mark_drawings(self):
        for name in ROMANIAN + GERMAN + OTHER:
            with self.subTest(name=name):
                self.assertEqual(evaluate(name)[0], 3)

    def test_schematic_words_mark_schematics_in_several_languages(self):
        for name in SCHEMATICS:
            with self.subTest(name=name):
                self.assertEqual(evaluate(name), (3, True))
        self.assertEqual(evaluate('Wiring diagram main.pdf'), (3, True))
        self.assertEqual(evaluate('Grundriss EG.pdf'), (3, False))

    def test_ordinary_business_names_stay_plain_text(self):
        for name in NOT_DRAWINGS:
            with self.subTest(name=name):
                self.assertEqual(evaluate(name), (0, False))

    def test_folder_names_count_and_matching_ignores_case_and_diacritics(self):
        self.assertEqual(evaluate('PLANSE/scan 001.pdf')[0], 3)
        self.assertEqual(evaluate('fatada/doc.pdf')[0], 3)
        self.assertEqual(evaluate('FAÇADE/doc.pdf')[0], 3)
        self.assertEqual(evaluate('PRZEKRÓJ/doc.pdf')[0], 3)

    def test_the_english_defaults_still_work(self):
        for name in ('Floor plan level 2.pdf', 'site plan.pdf', 'Blueprints.pdf', 'Drawings 01.pdf'):
            with self.subTest(name=name):
                self.assertEqual(evaluate(name)[0], 3)

    def test_partner_words_replace_the_defaults(self):
        self.assertEqual(evaluate('Plansa 1.pdf', drawing=['xyz'])[0], 0)
        self.assertEqual(evaluate('xyz 1.pdf', drawing=['xyz'])[0], 3)
        self.assertEqual(evaluate('Schaltplan.pdf', schematic=['circuitry'])[1], False)
        self.assertEqual(evaluate('Circuitry.pdf', schematic=['circuitry'])[1], True)

    def test_a_full_run_counts_the_named_sheets_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            data = base / 'data'
            data.mkdir()
            for index, name in enumerate(['Plansa fatada nord.pdf', 'Grundriss EG.pdf', 'Schema electrica.pdf', 'Oferta lucrari.pdf']):
                (data / name).write_bytes(S.pdf_bytes([('text', S.A4P, 700 + index)]))
            spec = formatspec.run_spec([data], base / 'out', config={}, workers=2, tools={}, report_date=NOW, log=LOG)
            self.assertEqual(spec['drawings']['sheets'], 3)
            self.assertEqual(spec['drawings']['schematics'], 1)

    def test_a_full_run_with_partner_words_ignores_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            data = base / 'data'
            data.mkdir()
            for index, name in enumerate(['Plansa fatada nord.pdf', 'Grundriss EG.pdf', 'Zeichnung 5.pdf']):
                (data / name).write_bytes(S.pdf_bytes([('text', S.A4P, 800 + index)]))
            spec = formatspec.run_spec([data], base / 'out', config={'drawing_patterns': ['zeichnung']}, workers=2, tools={},
                                       report_date=NOW, log=LOG)
            self.assertEqual(spec['drawings']['sheets'], 1)


if __name__ == '__main__':
    unittest.main()
