"""Main-language detection with a common-word or script confirmation.

py3langid proposes a language for up to the first 10,000 characters. The
proposal counts only when a small stopword list (or the script of the text)
confirms it. Unconfirmed, very short or garbled text has no language.
"""
import re
from collections import Counter

from spec_taxonomy import fold

SAMPLE_CHARS = 10000
CHUNK_CHARS = 500
MAX_CHUNKS = 16
SCORE_THRESHOLD = 0.90
STOPWORD_RATIO = 0.10
MIN_STOPWORD_HITS = 4
MIN_LETTERS = 40          # least letters of any script; most scripts need 80

_RAW = {
    'en': 'the of and to in is that for it as with was on be by at this have from or an are not but they you which we all can has had were their will would there been one if more when who what so no out up about into than them could other these its also such do my your our',
    'ro': 'si in de la cu pe care este sunt un o a ca nu se din pentru mai au fost sa ce prin lui al ale dar sau iar fi cel cea acest aceasta acestei fie dupa intre doar cum nici decat cand unde tot toate lor ei ea el noi voi va am are avea',
    'de': 'der die das und in den von zu mit sich des auf fur ist im dem nicht ein eine als auch es an werden aus er hat dass sie nach wird bei einer um am sind noch wie einem uber einen so zum war haben nur oder aber vor zur bis mehr durch man sein wurde sei',
    'fr': 'le la les de des du et en un une est que qui dans pour pas sur au avec ce il elle se ne par plus sont ou mais nous vous leur ses son sa ont cette aux comme etre fait tout ete ces entre',
    'it': 'il lo la i gli le di del della dei delle e in un una che non per con su si da al alla come sono ha nel nella piu anche ma o se questo questa essere stato dal tra fra loro suo sua',
    'es': 'el la los las de del y en un una que es por con para se no al lo como mas su sus o este esta pero sin sobre entre ser fue han hay muy ya tambien nos',
    'pt': 'o a os as de do da dos das e em um uma que nao para com por se no na mais como mas ao foi sao ou seu sua ja tambem entre sobre sem ser tem',
    'nl': 'de het een en van in is dat op te zijn met voor niet aan er ook als bij door maar om dan nog uit zich ze wordt worden naar heeft hebben over deze dit',
    'pl': 'i w z na do nie sie to ze o jest a po jak od za przez dla ale czy przy tez tylko juz lub oraz sa byl ma co ktory ktore moze tak gdy bardzo',
    'hu': 'a az es hogy nem is egy van meg de el volt csak mint ki be fel vagy ez azt ami mar ha mert kell lesz kozott utan szerint ehhez ugy igen ezt ezek amit vannak lehet minden mely ahol akkor pedig azok mind ott itt tehat alatt elott szamara ellen nelkul miatt tobb nagy uj',
    'hr': 'i u je se na da za s sa od su kao koji koja koje ne to ili iz do o po te nije bi biti ce ali jos samo sve kod pri smo ste sam bio bila bilo ovaj ova ovo svi tko sto gdje kada jer izmedu pod nad bez prema prije poslije tijekom putem',
    'cs': 'a v se na je to z do s o ze pro ale jako po si ktery ktera ktere by byl byla bylo jsou nebo pri tak jak jeste uz ve k nez tomuto pred aby jsem bude byly jeho jejich vsechny tento tato toto musi muze take pouze kde kdyz co ani proto mezi pod nad bez podle od za',
    'sk': 'a v sa na je to z do s o zo pre ale ako po si ktory ktora ktore by bol bola bolo su alebo pri tak ze aj uz vo k nez som ste aby tento tato toto vsetky boli bude jeho ich mozu tiez len kde ked co ani preto medzi pod nad bez podla od za pred',
    'sl': 'in v je na za se da so s z ki ne o pa to kot ali pri iz do od bi bo tudi ter lahko ni po sta ker',
    'sv': 'och i att det som en pa ar av for med till den har de inte om ett han hon men var sig fran vid kan sa eller nar',
    'da': 'og i at det som en pa er af for med til den har de ikke om et han hun men var sig fra ved kan sa eller nar',
    'no': 'og i at det som en pa er av for med til den har de ikke om et han hun men var seg fra ved kan sa eller nar',
    'fi': 'ja on ei etta se oli han mutta kun tai myos niin joka kuin ovat ole sen kanssa nyt vain jos tama mukaan seka voi ne ennen jalkeen sitten kaikki tahan jotka mita kuka missa miten koska ilman vaan yli alle kautta lisaksi tassa siina kuitenkin hyvin kaksi yksi',
    'tr': 've bir bu da de icin ile ne ben o mi ki daha gibi ama cok olarak var olan ya kadar sonra veya her en degil tarafindan tum ise ancak bunun sonucu uzere bunlar sey hem ayrica diger boyle gore icinde arasinda once yeni kendi bazi',
    'vi': 'va la cua cho mot nhung cac co trong duoc khong den nay voi de da nguoi khi tu ra se nhu cung dang nhieu hay ve lai tren sau neu rat o tai theo nhat hon cung phai nen boi ma',
    'id': 'yang dan di dengan untuk dari dalam pada ini itu adalah akan tidak ke juga atau oleh sebagai ada telah dapat kami saya mereka kita lebih sudah karena bahwa saat sebuah seperti hanya jika para harus belum dua antara',
    'ca': 'el la els les de del i en un una que per amb es no al als com mes pero seu seva son ha han va ser molt tambe aquest aquesta aixo sobre entre sense fins quan hi li ens dins tot totes',
}
STOPWORDS = {code: frozenset(words.split()) for code, words in _RAW.items()}

# Script ranges for languages confirmed by script alone.
_SCRIPTS = {
    'greek': [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    'cyrillic': [(0x0400, 0x052F)],
    'arabic': [(0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    'cjk': [(0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF)],
    'hebrew': [(0x0590, 0x05FF)],
    'devanagari': [(0x0900, 0x097F)],
    'thai': [(0x0E00, 0x0E7F)],
}
NO_WORD_SPACES = frozenset(['cjk', 'thai'])         # scripts written without spaces between words: judged by letters, not by words
SCRIPT_OF_LANGUAGE = {'el': 'greek', 'ru': 'cyrillic', 'bg': 'cyrillic', 'uk': 'cyrillic', 'sr': 'cyrillic',
                      'mk': 'cyrillic', 'be': 'cyrillic', 'kk': 'cyrillic', 'mn': 'cyrillic', 'ky': 'cyrillic',
                      'ar': 'arabic', 'fa': 'arabic', 'ur': 'arabic', 'ps': 'arabic', 'ug': 'arabic',
                      'zh': 'cjk', 'ja': 'cjk', 'ko': 'cjk', 'he': 'hebrew', 'hi': 'devanagari', 'mr': 'devanagari', 'ne': 'devanagari',
                      'th': 'thai'}

NAMES = {
    'en': 'English', 'ro': 'Romanian', 'de': 'German', 'fr': 'French', 'it': 'Italian', 'es': 'Spanish',
    'pt': 'Portuguese', 'nl': 'Dutch', 'pl': 'Polish', 'hu': 'Hungarian', 'hr': 'Croatian', 'cs': 'Czech',
    'sk': 'Slovak', 'sl': 'Slovenian', 'sv': 'Swedish', 'da': 'Danish', 'no': 'Norwegian', 'fi': 'Finnish',
    'tr': 'Turkish', 'el': 'Greek', 'ru': 'Russian', 'bg': 'Bulgarian', 'uk': 'Ukrainian', 'sr': 'Serbian',
    'mk': 'Macedonian', 'be': 'Belarusian', 'kk': 'Kazakh', 'mn': 'Mongolian', 'ky': 'Kyrgyz', 'ar': 'Arabic',
    'fa': 'Persian', 'ur': 'Urdu', 'ps': 'Pashto', 'ug': 'Uyghur', 'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean',
    'he': 'Hebrew', 'hi': 'Hindi', 'mr': 'Marathi', 'ne': 'Nepali', 'th': 'Thai', 'vi': 'Vietnamese', 'id': 'Indonesian', 'ca': 'Catalan',
}

_identifier = None


def _load_identifier():
    global _identifier
    if _identifier is None:
        from py3langid.langid import LanguageIdentifier, MODEL_FILE
        _identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    return _identifier


def _propose(sample):
    """Model proposal (code, score). Floating-point notices inside the scoring are harmless and are silenced; the result is unchanged."""
    identifier = _load_identifier()
    import numpy as np
    with np.errstate(all='ignore'):
        return identifier.classify(sample)


def language_name(code):
    return NAMES.get(code, code)


def _script_share(text, script):
    letters = script_hits = 0
    ranges = _SCRIPTS[script]
    for char in text:
        if char.isalpha():
            letters += 1
            code = ord(char)
            if any(low <= code <= high for low, high in ranges):
                script_hits += 1
    return script_hits / letters if letters else 0.0, script_hits


def _enough_text(sample, script=None):
    letters = sum(char.isalpha() for char in sample)
    if script in NO_WORD_SPACES:
        return letters >= 40
    return letters >= 80 and len(re.findall(r'\b\w+\b', sample)) >= 12


def confirmed(code, text):
    """True when a common-word (or script) check supports the proposed language."""
    script = SCRIPT_OF_LANGUAGE.get(code)
    if script:
        share, hits = _script_share(text, script)
        return share >= 0.6 and hits >= (20 if script in NO_WORD_SPACES else 30)
    words = STOPWORDS.get(code)
    if not words:
        return False
    found = re.findall(r'[a-z0-9]+', fold(text))
    if len(found) < 12:
        return False
    hits = [word for word in found if word in words]
    ratio = len(hits) / len(found)
    return ratio >= STOPWORD_RATIO and len(set(hits)) >= MIN_STOPWORD_HITS


def assess(text, minimum=True):
    """(confirmed language code or None, reason). The one place where the language rule lives.

    The reason is one of confirmed, insufficient_text, low_model_score, language_not_confirmed,
    dependency_missing and detector_error."""
    sample = text[:SAMPLE_CHARS]
    if not sample.strip():
        return None, 'insufficient_text'
    if minimum and sum(char.isalpha() for char in sample) < MIN_LETTERS:
        return None, 'insufficient_text'      # also spares the model load for tiny files
    try:
        code, score = _propose(sample)
    except ImportError:
        return None, 'dependency_missing'
    except Exception:
        return None, 'detector_error'
    if minimum and not _enough_text(sample, SCRIPT_OF_LANGUAGE.get(code)):
        return None, 'insufficient_text'
    if code in ('und', 'zxx') or float(score) < SCORE_THRESHOLD:
        return None, 'low_model_score'
    if not confirmed(code, sample):
        return None, 'language_not_confirmed'
    return str(code), 'confirmed'


def classify(text, minimum=True):
    """Confirmed language code for a text sample, or None."""
    return assess(text, minimum)[0]


def chunk_counts(text, limit=40000):
    """Confirmed language of evenly spaced 500-character chunks: (Counter, classified chunk count)."""
    text = re.sub(r'\s+', ' ', text[:limit]).strip()
    if len(text) < CHUNK_CHARS // 2:
        return Counter(), 0
    starts = list(range(0, max(1, len(text) - CHUNK_CHARS // 2), CHUNK_CHARS))
    if len(starts) > MAX_CHUNKS:
        step = len(starts) / MAX_CHUNKS
        starts = [starts[int(index * step)] for index in range(MAX_CHUNKS)]
    result = Counter()
    for start in starts:
        code = classify(text[start:start + CHUNK_CHARS])
        if code:
            result[code] += 1
    return result, sum(result.values())


def analyze_text(text):
    """Language facts for one text: main language, or None, and its chunk counts."""
    if not text or not text.strip():
        return {'lang': None, 'chunks': {}}
    main = classify(text)
    if not main:
        return {'lang': None, 'chunks': {}}
    counts, _ = chunk_counts(text)
    return {'lang': main, 'chunks': dict(sorted(counts.items()))}
