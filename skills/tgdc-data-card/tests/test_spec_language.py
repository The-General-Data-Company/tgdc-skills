"""Language detection is proposed by py3langid and confirmed by common words or script."""
import sys
import unittest
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import spec_language as L

EN = ('The contractor shall deliver the completed works to the client within the agreed period and the client will '
      'inspect the works before the final acceptance. All drawings that are attached to this letter have been '
      'prepared by the design team and they describe the water supply network of the village. ')
RO = ('Antreprenorul va livra lucrarile finalizate beneficiarului in termenul convenit, iar beneficiarul va verifica '
      'lucrarile inainte de receptia finala. Toate desenele care sunt atasate la aceasta scrisoare au fost intocmite '
      'de echipa de proiectare si descriu reteaua de alimentare cu apa a satului. ')
DE = ('Der Auftragnehmer liefert die fertigen Arbeiten innerhalb der vereinbarten Frist an den Auftraggeber, und der '
      'Auftraggeber wird die Arbeiten vor der endgultigen Abnahme prufen. Alle Zeichnungen, die diesem Schreiben '
      'beigefugt sind, wurden vom Planungsteam erstellt und beschreiben das Wassernetz des Dorfes. ')
FR = ('Le maitre doeuvre livrera les travaux termines au client dans le delai convenu, et le client verifiera les '
      'travaux avant la reception finale. Tous les plans qui sont joints a cette lettre ont ete prepares par '
      'lequipe de conception et decrivent le reseau dalimentation en eau du village. ')
RU = ('Подрядчик должен передать завершенные работы заказчику в согласованный срок, а заказчик проверит работы '
      'перед окончательной приемкой. Все чертежи, приложенные к этому письму, подготовлены проектной группой '
      'и описывают сеть водоснабжения села. ')
EL = ('Ο ανάδοχος θα παραδώσει τα ολοκληρωμένα έργα στον πελάτη εντός της συμφωνημένης προθεσμίας και ο πελάτης '
      'θα ελέγξει τα έργα πριν από την τελική παραλαβή. Όλα τα σχέδια που επισυνάπτονται σε αυτή την επιστολή '
      'έχουν καταρτιστεί από την ομάδα μελέτης και περιγράφουν το δίκτυο ύδρευσης του χωριού. ')
HE = ('הקבלן יעביר את העבודות שהושלמו ללקוח בתוך התקופה המוסכמת, והלקוח יבדוק את העבודות לפני הקבלה הסופית. '
      'כל השרטוטים המצורפים למכתב זה הוכנו על ידי צוות התכנון והם מתארים את רשת אספקת המים של הכפר. ')
HI = ('ठेकेदार पूरे किए गए काम को सहमत समय के भीतर ग्राहक को सौंपेगा और ग्राहक अंतिम स्वीकृति से पहले काम का निरीक्षण करेगा। '
      'इस पत्र के साथ संलग्न सभी चित्र डिज़ाइन टीम द्वारा तैयार किए गए हैं और वे गाँव की जल आपूर्ति प्रणाली का वर्णन करते हैं। ')
TH = ('ผู้รับเหมาจะส่งมอบงานที่แล้วเสร็จให้แก่ลูกค้าภายในระยะเวลาที่ตกลงกันไว้ และลูกค้าจะตรวจสอบงานก่อนการรับมอบขั้นสุดท้าย '
      'แบบแปลนทั้งหมดที่แนบมากับจดหมายฉบับนี้จัดทำโดยทีมออกแบบและอธิบายระบบประปาของหมู่บ้าน ')
VI = ('Nhà thầu sẽ bàn giao các công trình đã hoàn thành cho khách hàng trong thời hạn đã thỏa thuận, và khách hàng sẽ kiểm tra '
      'các công trình trước khi nghiệm thu cuối cùng. Tất cả các bản vẽ đính kèm với thư này đã được nhóm thiết kế chuẩn bị '
      'và chúng mô tả mạng lưới cấp nước của ngôi làng. ')
ID = ('Pemerintah daerah mengumumkan bahwa pembangunan jaringan air bersih di desa akan dimulai pada bulan depan. Proyek ini dikerjakan '
      'oleh kontraktor yang telah dipilih melalui proses tender, dan seluruh gambar teknis sudah disetujui oleh dinas pekerjaan umum. '
      'Warga desa diharapkan dapat menikmati air bersih sebelum akhir tahun. ')
CA = ('El contractista lliurarà les obres acabades al client dins del termini acordat, i el client inspeccionarà les obres abans '
      'de la recepció final. Tots els plànols que s\'adjunten a aquesta carta han estat preparats per l\'equip de disseny i descriuen '
      'la xarxa de subministrament d\'aigua del poble. ')


class LanguageTests(unittest.TestCase):
    def test_confirmed_languages(self):
        for code, text in [('en', EN), ('ro', RO), ('de', DE), ('fr', FR), ('ru', RU), ('el', EL)]:
            with self.subTest(code=code):
                self.assertEqual(L.classify(text * 2), code)

    def test_hebrew_hindi_and_thai_are_confirmed_by_script(self):
        for code, text in [('he', HE), ('hi', HI), ('th', TH)]:
            with self.subTest(code=code):
                self.assertEqual(L.classify(text * 2), code)
                self.assertEqual(L.language_name(code), {'he': 'Hebrew', 'hi': 'Hindi', 'th': 'Thai'}[code])

    def test_vietnamese_indonesian_and_catalan_are_confirmed_by_common_words(self):
        for code, text in [('vi', VI), ('id', ID), ('ca', CA)]:
            with self.subTest(code=code):
                self.assertEqual(L.classify(text * 2), code)
                self.assertNotEqual(L.language_name(code), code)

    def test_a_script_alone_does_not_confirm_a_language_it_was_not_proposed_for(self):
        self.assertFalse(L.confirmed('he', EN * 2))
        self.assertFalse(L.confirmed('th', RU * 2))

    def test_short_or_garbled_text_has_no_language(self):
        self.assertIsNone(L.classify('ok'))
        self.assertIsNone(L.classify(''))
        self.assertIsNone(L.classify('xq zv kk jj hh gg ff dd ss aa ' * 20))
        self.assertIsNone(L.classify('1234 5678 9012 3456 ' * 40))

    def test_floating_point_notices_inside_the_model_neither_warn_nor_change_the_result(self):
        class Noisy:
            def classify(self, text):
                np.float32(1e-30) * np.float32(1e-30)      # underflow
                np.float32(1e30) * np.float32(1e30)        # overflow
                np.float32(1.0) / np.float32(0.0)          # division by zero
                return 'en', 0.99
        expected = L.classify(EN * 2)
        original = L._identifier
        try:
            L._identifier = Noisy()
            for handling in ('warn', 'raise'):
                with self.subTest(handling=handling), warnings.catch_warnings(), np.errstate(all=handling):
                    warnings.simplefilter('error')
                    self.assertEqual(L.assess(EN * 2), (expected, 'confirmed'))
        finally:
            L._identifier = original

    def test_tiny_text_is_rejected_before_the_model_is_loaded(self):
        def refuse():
            raise AssertionError('the model was loaded')
        original = L._load_identifier
        try:
            L._load_identifier = refuse
            self.assertEqual(L.assess('ok'), (None, 'insufficient_text'))
            self.assertEqual(L.assess(' \n '), (None, 'insufficient_text'))
        finally:
            L._load_identifier = original

    def test_assess_gives_the_reason_a_text_has_no_language(self):
        self.assertEqual(L.assess(EN * 2), ('en', 'confirmed'))
        self.assertEqual(L.assess('ok'), (None, 'insufficient_text'))
        self.assertEqual(L.assess(''), (None, 'insufficient_text'))
        names = 'Gdansk Gdynia Sopot Krakow Wroclaw Poznan Lodz Szczecin Lublin Bialystok Katowice Gliwice Zabrze Bytom Rybnik ' * 3
        self.assertEqual(L.assess(names), (None, 'language_not_confirmed'))

    def test_a_proposal_without_a_word_or_script_check_is_not_confirmed(self):
        self.assertFalse(L.confirmed('ro', EN))
        self.assertFalse(L.confirmed('lt', EN))
        self.assertTrue(L.confirmed('en', EN))

    def test_chunk_counts_find_two_languages_in_one_text(self):
        text = (RO * 8) + (EN * 8)
        counts, total = L.chunk_counts(text)
        self.assertGreaterEqual(counts['ro'], 3)
        self.assertGreaterEqual(counts['en'], 3)
        self.assertEqual(total, sum(counts.values()))

    def test_analyze_text_returns_main_language_and_chunks(self):
        result = L.analyze_text(RO * 6)
        self.assertEqual(result['lang'], 'ro')
        self.assertGreaterEqual(result['chunks']['ro'], 3)
        self.assertEqual(L.analyze_text('tiny'), {'lang': None, 'chunks': {}})

    def test_stopword_lists_cover_the_required_languages(self):
        required = 'en ro de fr it es pt nl pl hu hr cs sk sl sv da fi tr'.split()
        for code in required:
            self.assertGreaterEqual(len(L.STOPWORDS[code]), 25, code)
        for code in ['el', 'ru', 'ar', 'zh']:
            self.assertIn(code, L.SCRIPT_OF_LANGUAGE)
        self.assertEqual(L.language_name('ro'), 'Romanian')


if __name__ == '__main__':
    unittest.main()
