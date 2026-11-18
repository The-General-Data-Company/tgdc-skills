"""Engineering formats in a full pass: each keeps its own label and category, and every format reaches spec.json even when it
is too small for the 35 largest."""
import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
import formatspec
import spec_report
import spec_taxonomy as T

LOG = open(os.devnull, 'w')
# Formats the full pass counts without parsing, 36 of them, each larger than every engineering file below.
FILLERS = ('mp3 wav wma aac flac ogg m4a amr mp4 wmv avi mov mkv mpg mpeg 3gp flv webm m4v vob exe msi dll bin jar ttf otf woff '
           'woff2 eot parquet avro orc sqlite mdb accdb').split()
# Name, label, category: CATIA, NX and Creo, SolidWorks, CAE, CAM, electrical design, PLC, CNC and CMM.
ENGINEERING = [
    ('bracket.CATPart', 'catpart', T.CAD), ('frame.CATProduct', 'catproduct', T.CAD), ('frame.CATDrawing', 'catdrawing', T.CAD),
    ('mill.CATProcess', 'catprocess', T.CAD), ('load.CATAnalysis', 'catanalysis', T.CAD), ('frame.3dxml', '3dxml', T.CAD),
    ('frame.jt', 'jt', T.CAD), ('housing.prt', 'prt', T.CAD), ('housing_fem1.fem', 'fem', T.CAD), ('housing_sim1.sim', 'sim', T.CAD),
    ('pump.prt.4', 'prt', T.CAD), ('pump.asm.2', 'asm', T.CAD), ('bracket.SLDPRT', 'sldprt', T.CAD), ('frame.SLDASM', 'sldasm', T.CAD),
    ('frame.SLDDRW', 'slddrw', T.CAD), ('bracket study.cwr', 'cwr', T.CAD), ('job.inp', 'inp', T.CAD), ('job.odb', 'odb', T.CAD),
    ('crash.k', 'k', T.CAD), ('wing.cas.h5', 'cas', T.CAD), ('wing.dat.h5', T.IRREGULAR_EXTENSION, T.OTHER), ('cell.mcam', 'mcam', T.CAD),
    ('panel.zw1', 'zw1', T.CAD), ('line.zap17', 'zap17', T.OTHER), ('press.L5X', 'l5x', T.OTHER), ('press.ACD', 'acd', T.OTHER),
    ('part.nc', 'nc', T.OTHER), ('check.dmis', 'dmis', T.OTHER),
]


class EngineeringFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name).resolve()
        data = base / 'data'
        (data / 'media').mkdir(parents=True)
        (data / 'models').mkdir()
        for index, extension in enumerate(FILLERS):
            (data / 'media' / f'file{index}.{extension}').write_bytes(bytes([index]) * (5000 + index))
        for index, (name, _, _) in enumerate(ENGINEERING):
            (data / 'models' / name).write_bytes(b'model %d ' % index * (10 + index))
        cls.spec = formatspec.run_spec([data], base / 'out', workers=2, tools={}, report_date=datetime(2026, 11, 17), log=LOG)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_each_format_has_its_own_label_and_category(self):
        rows = {(row['ext'], row['category']): row['files'] for row in self.spec['all_formats']}
        expected = {}
        for _, label, category in ENGINEERING:
            expected[(label, category)] = expected.get((label, category), 0) + 1
        for key, files in expected.items():
            with self.subTest(key=key):
                self.assertEqual(rows.get(key), files)

    def test_small_formats_are_outside_the_pdf_table_but_in_all_formats(self):
        spec = self.spec
        self.assertEqual(len(spec['formats']), 35)
        self.assertEqual(spec['formats'], spec['all_formats'][:35])
        self.assertEqual(len(spec['all_formats']), len(FILLERS) + len({(label, category) for _, label, category in ENGINEERING}))
        self.assertNotIn('catdrawing', {row['ext'] for row in spec['formats']})
        self.assertEqual(sum(row['files'] for row in spec['all_formats']), spec['totals']['files'])
        cad = next(c for c in spec['categories'] if c['name'] == T.CAD)
        self.assertEqual(cad['files'], sum(1 for _, _, category in ENGINEERING if category == T.CAD))

    def test_the_specification_validates_and_the_notes_say_where_the_rest_is(self):
        spec_report.validate(copy.deepcopy(self.spec))
        text = ' '.join(spec_report.notes(self.spec))
        self.assertIn(f"The table holds the 35 largest of {len(self.spec['all_formats'])} rows; spec.json lists every row.", text)
        smaller = copy.deepcopy(self.spec)
        smaller['all_formats'] = smaller['formats']
        self.assertNotIn('spec.json lists every row', ' '.join(spec_report.notes(smaller)))
        older = copy.deepcopy(self.spec)
        del older['all_formats']                  # a specification of version 1.2 and earlier
        spec_report.validate(older)
        self.assertNotIn('spec.json lists every row', ' '.join(spec_report.notes(older)))


if __name__ == '__main__':
    unittest.main()
