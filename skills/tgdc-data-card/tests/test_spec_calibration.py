"""Optional visual calibration: corrected sheet totals and the individual-views estimate."""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import formatspec
import report
import spec_fixture as F
from pypdf import PdfReader

LOG = open(os.devnull, 'w')


class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.fx = F.Fixture(cls.base / 'fx')
        cls.out = cls.base / 'out'
        cls.plain = cls.execute(None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def execute(cls, calibration):
        config = copy.deepcopy(cls.fx.config)
        if calibration:
            config['calibration'] = calibration
        return formatspec.run_spec([cls.fx.data], cls.out, config=config, workers=2, tools={}, report_date=datetime(2026, 10, 25), log=LOG)

    @staticmethod
    def calibration(true_c, sampled_c, true_n, sampled_n, schematics=0, views=(2, 2, 2)):
        return {'sampled': {'candidates': sampled_c, 'non_candidates': sampled_n}, 'true_drawings_among_candidates': true_c,
                'drawings_among_non_candidates': true_n, 'schematics': schematics, 'views_per_sheet': list(views)}

    def test_without_calibration_there_is_no_views_estimate(self):
        self.assertFalse(self.plain['drawings']['calibrated'])
        self.assertNotIn('views', self.plain['drawings'])
        self.assertEqual(self.plain['drawings']['sheets'], 11)

    def test_a_perfect_sample_keeps_the_totals_and_adds_views(self):
        spec = self.execute(self.calibration(10, 10, 0, 20))
        drawings = spec['drawings']
        self.assertTrue(drawings['calibrated'])
        self.assertEqual(drawings['sampled_pages'], 30)
        self.assertEqual(drawings['sheets'], 11)
        self.assertEqual(drawings['views'], {'total': 22, 'per_sheet': 2.0, 'low': 2.0, 'high': 2.0})
        self.assertEqual(drawings['sizes'], self.plain['drawings']['sizes'])

    def test_precision_corrects_the_totals_and_scales_the_breakdowns(self):
        spec = self.execute(self.calibration(6, 10, 0, 20, schematics=1, views=(1, 2, 2, 3, 1, 2, 2)))
        drawings = spec['drawings']
        self.assertEqual(drawings['sheets'], 7)                          # 11 candidate sheets x 6/10, rounded
        self.assertEqual((drawings['schematics'], drawings['engineering']), (1, 6))
        self.assertEqual(drawings['files'], 10)                          # files are counted, not scaled
        self.assertEqual(sum(drawings['locations'].values()), 7)
        views = drawings['views']
        self.assertEqual(views['per_sheet'], round(13 / 7, 1))
        self.assertLessEqual(views['low'], views['per_sheet'])
        self.assertGreaterEqual(views['high'], views['per_sheet'])
        self.assertEqual(views['total'], round(7 * 13 / 7))
        again = self.execute(self.calibration(6, 10, 0, 20, schematics=1, views=(1, 2, 2, 3, 1, 2, 2)))
        self.assertEqual(again['drawings']['views'], views)               # fixed seed: the same range every time

    def test_drawings_found_among_non_candidates_raise_the_total(self):
        every = self.execute(self.calibration(10, 10, 5, 5))['drawings']['sheets']
        pages = every - 11                                                # non-candidate pages in the archive
        self.assertGreater(pages, 0)
        half = self.execute(self.calibration(10, 10, 5, 10))['drawings']['sheets']
        self.assertEqual(half, int(round(11 + pages * 0.5)))

    def test_the_pdf_shows_the_views_row_and_the_note_only_with_calibration(self):
        spec = self.execute(self.calibration(10, 10, 0, 20))
        path = self.base / 'spec.json'
        path.write_text(json.dumps(spec))
        profile, answers = ROOT / 'assets/examples/profile.synthetic.json', ROOT / 'assets/examples/answers.synthetic.json'
        report.render(profile, answers, self.base / 'with.pdf', path)
        text = ' '.join(' '.join(page.extract_text() for page in PdfReader(self.base / 'with.pdf').pages).split())
        self.assertIn('Estimated individual views (plans, sections, details)', text)
        self.assertIn('visual check of 30 sampled pages', text)
        path.write_text(json.dumps(self.execute(None)))
        report.render(profile, answers, self.base / 'without.pdf', path)
        text = ' '.join(' '.join(page.extract_text() for page in PdfReader(self.base / 'without.pdf').pages).split())
        self.assertNotIn('Estimated individual views', text)
        self.assertNotIn('visual check', text)


if __name__ == '__main__':
    unittest.main()
