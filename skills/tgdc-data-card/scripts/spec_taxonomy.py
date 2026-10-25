"""Static format knowledge for the data format specification.

Everything here is a fixed table or a pure function of a file name and a few
measured facts. File names are inspected only to choose a category; they never
reach any output.
"""
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import PurePosixPath

DOCUMENTS = 'Documents (text: Word, PDF with text layer, plain text)'
SCANNED = 'Scanned documents (image-only PDF, TIFF)'
SPREADSHEETS = 'Spreadsheets and financial models'
PRESENTATIONS = 'Presentations'
CAD = 'CAD and engineering drawings'
PHOTOS = 'Photos and images'
AUDIO_VIDEO = 'Audio and video'
EMAIL = 'E-mail messages and mailbox files (incl. per-message metadata)'
ESIGNED = 'E-signed document packages (CAdES .p7s / .p7m)'
ARCHIVES = 'Compressed archives (ZIP / RAR / 7z)'
STRUCTURED = 'Structured data files (XML, JSON, database / application data)'
CREDENTIALS = 'Credential and key files: certificates, key stores, password files (counted, not opened)'
SYSTEM = 'System and temporary files'
SOURCE = 'Software and website source code (scripts, style sheets, fonts)'
PROGRAMS = 'Programs and installers'
OTHER = 'Other formats'

CATEGORIES = [DOCUMENTS, SCANNED, SPREADSHEETS, PRESENTATIONS, CAD, PHOTOS, AUDIO_VIDEO, EMAIL, ESIGNED,
              ARCHIVES, STRUCTURED, CREDENTIALS, SYSTEM, SOURCE, PROGRAMS, OTHER]

NO_EXTENSION = '(no extension)'
IRREGULAR_EXTENSION = '(other / irregular extension)'

_EXTENSION_GROUPS = {
    DOCUMENTS: 'pdf doc docx docm dot dotx rtf odt ott txt md html htm xhtml mht wpd wps pages tex',
    SCANNED: 'tif tiff',
    SPREADSHEETS: 'xls xlsx xlsb xlsm xlt xltx ods csv tsv numbers',
    PRESENTATIONS: 'ppt pptx pptm pps ppsx odp key',
    CAD: ('dwg dxf dgn dws dwt dwf dwfx shx skp ctb stb pc3 lin pat step stp iges igs stl ifc rvt rfa ipt iam idw '
          'sldprt sldasm slddrw 3dm 3ds fcstd sat vwx'),
    PHOTOS: 'jpg jpeg jpe jfif png gif bmp svg webp heic heif cr2 nef arw dng ico',
    AUDIO_VIDEO: ('mp3 wav wma aac flac ogg m4a amr mid midi mp4 wmv avi mov mkv mpg mpeg 3gp flv webm m4v vob'),
    EMAIL: 'eml emlx msg mbox mbx pst ost vcf ics',
    ESIGNED: 'p7s p7m',
    ARCHIVES: 'zip zipx rar 7z arj cab tar gz tgz bz2 tbz2 xz txz lzh lha ace z',
    STRUCTURED: ('xml json jsonl yaml yml bak dbf dat mdb accdb sqlite sqlite3 sql parquet avro orc kml gml geojson '
                 'xsd rdf'),
    CREDENTIALS: 'p12 pfx pem key keystore jks kdbx kdb ppk cer crt der csr p8 p7b spc keychain',
    SYSTEM: 'db log tmp temp ini lnk dwl dwl2 swp swo crdownload part cache lock pid thumbs',
    SOURCE: ('php phtml js mjs css less scss sass ts tsx jsx py rb pl sh bash zsh java c cpp h hpp cs go rs swift kt '
             'lua ps1 vbs asp aspx jsp vue ttf otf woff woff2 eot mo po pot map'),
    PROGRAMS: 'exe msi dll sys bin com bat cmd inf apk app dmg pkg deb rpm jar scr ocx drv cpl',
}
CATEGORY_OF_EXTENSION = {extension: category for category, names in _EXTENSION_GROUPS.items() for extension in names.split()}

ARCHIVE_EXTENSIONS = frozenset(_EXTENSION_GROUPS[ARCHIVES].split())
SCRIPT_LOG_EXTENSIONS = frozenset('py sh bash zsh bat cmd ps1 js rb pl log'.split())
CREDENTIAL_EXTENSIONS = frozenset(_EXTENSION_GROUPS[CREDENTIALS].split())
IMAGE_SHEET_EXTENSIONS = frozenset('jpg jpeg jpe jfif png bmp gif webp'.split())
# Formats whose text the specification can read for the language table.
TEXT_EXTRACTABLE = frozenset('pdf doc docx docm rtf odt txt md html htm xhtml xlsx xlsm ods csv tsv pptx pptm ppsx odp eml tif tiff'.split())
EXAMINED_CATEGORIES = frozenset([DOCUMENTS, SCANNED, SPREADSHEETS, PRESENTATIONS, EMAIL])

_CREDENTIAL_SUBSTRINGS = ('password', 'passwort', 'kennwort', 'credential', 'parola', 'parole', 'passphrase',
                          'contrasena', 'privatekey', 'apikey')
_CREDENTIAL_TOKENS = frozenset(['pwd', 'passwd', 'secret', 'secrets'])


def fold(text):
    """Lower-case text without diacritics (NFKD, combining marks removed)."""
    decomposed = unicodedata.normalize('NFKD', str(text))
    return ''.join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def tokens(text):
    return re.findall(r'[a-z0-9]+', fold(text))


def raw_extension(name):
    """Lower-case suffix of the final name component, without the dot."""
    suffix = PurePosixPath(name.replace('\\', '/')).suffix
    return suffix[1:].lower() if suffix else ''


def extension_label(name, multipart_part=False):
    """Format label: extension, '(no extension)' or '(other / irregular extension)'."""
    suffix = raw_extension(name)
    if not suffix:
        return NO_EXTENSION
    if (len(suffix) > 8 or ' ' in suffix or (suffix.isdigit() and not multipart_part)
            or not re.fullmatch(r'[a-z0-9_+~-]+', suffix)):
        return IRREGULAR_EXTENSION
    return suffix


def is_credential_name(name):
    """Extension and file-name test only. The file is never opened."""
    base = PurePosixPath(name.replace('\\', '/')).name
    extension = raw_extension(base)
    if extension in CREDENTIAL_EXTENSIONS:
        return True
    stem = fold(PurePosixPath(base).stem)
    if any(word in stem for word in _CREDENTIAL_SUBSTRINGS):
        return True
    return bool(set(re.findall(r'[a-z0-9]+', stem)) & _CREDENTIAL_TOKENS)


def is_system_name(name):
    base = PurePosixPath(name.replace('\\', '/')).name
    lowered = base.lower()
    return lowered.startswith('~$') or lowered.endswith('~') or lowered in ('thumbs.db', 'desktop.ini')


def category_of(name, pdf_scanned=False, multipart_part=False, mail_metadata=False):
    """Content category for a file name and the few measured facts that decide it."""
    extension = raw_extension(name)
    if is_credential_name(name):
        return CREDENTIALS
    if is_system_name(name):
        return SYSTEM
    if multipart_part:
        return ARCHIVES
    if mail_metadata and extension in ('json', 'csv'):
        return EMAIL
    if extension == 'pdf':
        return SCANNED if pdf_scanned else DOCUMENTS
    return CATEGORY_OF_EXTENSION.get(extension, OTHER)


def short_category(category):
    """Chart label: the category name without its parenthetical."""
    return category.split(' (')[0]


# Page size classes, millimetres.
_SIZES = {'A4': (210.0, 297.0), 'A3': (297.0, 420.0), 'A2': (420.0, 594.0), 'A1': (594.0, 841.0),
          'A0': (841.0, 1189.0), 'letter': (215.9, 279.4), 'legal': (215.9, 355.6), 'tabloid': (279.4, 431.8)}
US_LABEL = 'US Letter / Legal / Tabloid'
OTHER_SIZE = 'other / custom size'
IMAGE_SIZE = 'image file'
SIZE_ORDER = ['A4', 'A3', 'A2', 'A1', 'A0', US_LABEL, IMAGE_SIZE, OTHER_SIZE]
A4_AREA_MM = 210.0 * 297.0
# Classes that count as a drawing sheet by size alone, and the classes that are text-page sizes.
LARGE_CLASSES = frozenset(['A3', 'A2', 'A1', 'A0', 'tabloid'])
A4_LIKE = frozenset(['A4', 'letter', 'legal'])


def size_class(width_pt, height_pt, tolerance=0.03):
    """Return (internal class, report label, larger_than_a4). Orientation does not matter."""
    short, long_side = sorted((width_pt * 25.4 / 72.0, height_pt * 25.4 / 72.0))
    best = None
    for name, (s, l) in _SIZES.items():
        if abs(short - s) / s <= tolerance and abs(long_side - l) / l <= tolerance:
            error = abs(short - s) / s + abs(long_side - l) / l
            if best is None or error < best[0]:
                best = (error, name)
    if best:
        name = best[1]
        label = US_LABEL if name in ('letter', 'legal', 'tabloid') else name
        return name, label, name in LARGE_CLASSES
    larger = short * long_side > A4_AREA_MM * (1 + tolerance) ** 2
    return 'other', OTHER_SIZE, larger


# Name keyword matching, case-insensitive and diacritic-insensitive.
@lru_cache(maxsize=4096)
def keyword_tokens(keyword):
    return tuple(tokens(keyword))


def token_sequence_match(pattern, name_tokens):
    """Whole-token match of a keyword; long single keywords also match as a token prefix."""
    pattern_tokens = keyword_tokens(pattern)
    if not pattern_tokens:
        return False
    count = len(pattern_tokens)
    for start in range(len(name_tokens) - count + 1):
        window = name_tokens[start:start + count]
        if all(w == p or (len(p) >= 5 and w.startswith(p)) for w, p in zip(window, pattern_tokens)):
            return True
    return False


def matches_any(patterns, text):
    name_tokens = tokens(text)
    return any(token_sequence_match(pattern, name_tokens) for pattern in patterns)


def default_thresholds(stage_count):
    base = math.ceil(stage_count / 2)
    return [base, base + 1, base + 2]


# Number formats.
def count(value):
    return f'{int(value):,}'


def gigabytes(byte_count, places=2):
    return f'{byte_count / 1e9:,.{places}f}'


def gibibytes(byte_count):
    return f'{byte_count / 2 ** 30:,.1f}'


def size_text(byte_count):
    """Compact size: GB from 0.1 GB, otherwise MB or KB (one decimal)."""
    if byte_count >= 1e8:
        return f'{byte_count / 1e9:,.1f} GB'
    if byte_count >= 1e5:
        return f'{byte_count / 1e6:,.1f} MB'
    return f'{byte_count / 1e3:,.1f} KB'


def round_to(value, step):
    return int(value / step + 0.5) * step


def year_range(first, last):
    return str(first) if first == last else f'{first}-{last}'
