# santa/romanize.py
# Optional post-processing step: Devanagari -> informal "Hinglish" Latin spelling
# (the way people type Hindi in WhatsApp: "aaj meeting hai", "main ghar ja raha hoon").
#
# This is a deterministic, dependency-free heuristic, NOT a Whisper feature and
# NOT a scholarly transliteration (IAST/ISO-15919). It handles the common cases
# (inherent-vowel deletion at word end and in the middle of words, nasalisation,
# nukta letters) and leaves Latin-script words, digits and punctuation untouched.
# Irregular spellings ("mein" vs "men", "nahi" vs "nahin") are best-effort.
# The raw Devanagari transcript is always kept alongside the romanized text.

import re
import unicodedata

# ── Character tables ─────────────────────────────────────────────────────────
_INDEPENDENT_VOWELS = {
    'अ': 'a', 'आ': 'aa', 'इ': 'i', 'ई': 'ee', 'उ': 'u', 'ऊ': 'oo', 'ऋ': 'ri',
    'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au', 'ऑ': 'o', 'ऍ': 'e', 'ऎ': 'e', 'ऒ': 'o',
}
_MATRAS = {
    'ा': 'aa', 'ि': 'i', 'ी': 'ee', 'ु': 'u', 'ू': 'oo', 'ृ': 'ri', 'े': 'e',
    'ै': 'ai', 'ो': 'o', 'ौ': 'au', 'ॉ': 'o', 'ॅ': 'e', 'ॆ': 'e', 'ॊ': 'o',
}
_CONSONANTS = {
    'क': 'k', 'ख': 'kh', 'ग': 'g', 'घ': 'gh', 'ङ': 'n',
    'च': 'ch', 'छ': 'chh', 'ज': 'j', 'झ': 'jh', 'ञ': 'n',
    'ट': 't', 'ठ': 'th', 'ड': 'd', 'ढ': 'dh', 'ण': 'n',
    'त': 't', 'थ': 'th', 'द': 'd', 'ध': 'dh', 'न': 'n',
    'प': 'p', 'फ': 'ph', 'ब': 'b', 'भ': 'bh', 'म': 'm',
    'य': 'y', 'र': 'r', 'ल': 'l', 'व': 'v', 'ळ': 'l',
    'श': 'sh', 'ष': 'sh', 'स': 's', 'ह': 'h',
}
# Letters written with a nukta (either precomposed or base + U+093C).
_NUKTA_FORMS = {'क': 'q', 'ख': 'kh', 'ग': 'gh', 'ज': 'z', 'फ': 'f', 'ड': 'r', 'ढ': 'rh', 'य': 'y'}
_PRECOMPOSED_NUKTA = {'क़': 'क', 'ख़': 'ख', 'ग़': 'ग', 'ज़': 'ज', 'ड़': 'ड', 'ढ़': 'ढ', 'फ़': 'फ', 'य़': 'य'}

NUKTA, VIRAMA = '़', '्'
ANUSVARA, CHANDRABINDU, VISARGA = 'ं', 'ँ', 'ः'
_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
_PUNCT = {'।': '.', '॥': '.', 'ॐ': 'om', '॰': '.'}

_DEVANAGARI_RE = re.compile(r'[ऀ-ॿ]+')


def contains_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI_RE.search(text or ''))


# ── Public API ───────────────────────────────────────────────────────────────
def romanize_hinglish(text: str) -> str:
    """Romanize every Devanagari run in `text`; everything else is unchanged."""
    if not text:
        return text
    text = unicodedata.normalize('NFC', text)
    # Decompose precomposed nukta letters so one code path handles both forms.
    for pre, base in _PRECOMPOSED_NUKTA.items():
        text = text.replace(pre, base + NUKTA)
    text = text.replace('‌', '').replace('‍', '')   # ZWNJ / ZWJ
    return _DEVANAGARI_RE.sub(lambda m: _romanize_word(m.group(0)), text)


# ── Internals ────────────────────────────────────────────────────────────────
# A "unit" is one consonant (or independent vowel) plus the vowel that follows
# it. Inherent 'a' vowels are marked so the schwa-deletion pass can drop them.
class _Unit:
    __slots__ = ('cons', 'vowel', 'inherent', 'nasal', 'extra')

    def __init__(self, cons='', vowel='', inherent=False):
        self.cons = cons          # Latin consonant(s), '' for independent vowel
        self.vowel = vowel        # Latin vowel, '' when killed by virama
        self.inherent = inherent  # vowel is the implicit schwa
        self.nasal = False
        self.extra = ''           # visarga, digits, punctuation


def _parse(word: str) -> list:
    units, i = [], 0
    while i < len(word):
        ch = word[i]
        nxt = word[i + 1] if i + 1 < len(word) else ''
        if ch in _CONSONANTS:
            latin = _CONSONANTS[ch]
            if nxt == NUKTA:
                latin = _NUKTA_FORMS.get(ch, latin)
                i += 1
                nxt = word[i + 1] if i + 1 < len(word) else ''
            if nxt == VIRAMA:
                units.append(_Unit(latin, ''))
                i += 2
                continue
            if nxt in _MATRAS:
                units.append(_Unit(latin, _MATRAS[nxt]))
                i += 2
                continue
            units.append(_Unit(latin, 'a', inherent=True))
            i += 1
            continue
        if ch in _INDEPENDENT_VOWELS:
            units.append(_Unit('', _INDEPENDENT_VOWELS[ch]))
        elif ch in (ANUSVARA, CHANDRABINDU):
            if units:
                units[-1].nasal = True
            else:
                units.append(_Unit('', 'n'))
        elif ch == VISARGA:
            (units[-1] if units else units.append(_Unit()) or units[-1]).extra += 'h'
        elif ch in _DIGITS or ch in _PUNCT:
            u = _Unit()
            u.extra = _DIGITS.get(ch) or _PUNCT.get(ch)
            units.append(u)
        elif ch in _MATRAS:          # stray matra (bad input): keep its sound
            units.append(_Unit('', _MATRAS[ch]))
        # anything else in the block (rare signs) is dropped
        i += 1
    return units


def _delete_schwas(units: list) -> None:
    letters = [u for u in units if u.cons or u.vowel]
    if len(letters) <= 1:
        return
    # Word-final inherent vowel is silent: "kal", "ghar", "kaam".
    last = letters[-1]
    if last.inherent and not last.nasal:
        last.vowel, last.inherent = '', False
    # Medial deletion (right to left): V C[a] C V -> V C C V, e.g. "samajhna".
    for idx in range(len(letters) - 2, 0, -1):
        cur, prev, nxt = letters[idx], letters[idx - 1], letters[idx + 1]
        if cur.inherent and not cur.nasal and prev.vowel and nxt.cons and nxt.vowel:
            cur.vowel, cur.inherent = '', False


def _romanize_word(word: str) -> str:
    units = _parse(word)
    _delete_schwas(units)
    out = []
    for n, u in enumerate(units):
        vowel = u.vowel
        is_last_letter = all(not (x.cons or x.vowel) for x in units[n + 1:])
        if is_last_letter and vowel:
            # Hinglish convention: final long vowels written short ("raha", "meri", "tu").
            vowel = {'aa': 'a', 'ee': 'i', 'oo': 'u'}.get(vowel, vowel) if (u.cons or len(units) > 1) else vowel
        piece = u.cons + vowel
        if u.nasal:
            if is_last_letter and vowel == 'e':
                piece = u.cons + 'ein'         # में -> mein, हैं handled by 'ai'
            elif is_last_letter and vowel == 'u' and u.cons:
                piece = u.cons + 'oon'         # हूँ -> hoon
            else:
                piece += 'n'
        out.append(piece + u.extra)
    return ''.join(out)
