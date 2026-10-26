# santa/textproc.py
# Unicode-safe transcript post-processing. Rules:
#   * Always NFC-normalise, never strip combining marks (Hindi/Bengali matras,
#     viramas, nuktas, Arabic harakat all survive).
#   * Optional cleanup is deliberately tiny: whitespace tidy-up everywhere, and
#     sentence-initial capitalisation ONLY for Latin-script text. No English
#     punctuation rules are applied to Hindi, Arabic or Bengali.
#   * Digits, currency symbols, names and dates are never rewritten.
#   * The caller keeps the raw transcript; nothing here is destructive to it.

import re
import unicodedata

# ── Script detection ─────────────────────────────────────────────────────────
# Unicode blocks of the scripts Whisper writes in. Used for "wrong script"
# warnings, RTL layout, and the grammar model's guard (it must never move text
# into another script). Latin is matched separately below.
_SCRIPT_RANGES = {
    'Devanagari': [(0x0900, 0x097F), (0xA8E0, 0xA8FF)],
    'Bengali': [(0x0980, 0x09FF)],
    'Arabic': [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
               (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    'Hebrew': [(0x0590, 0x05FF), (0xFB1D, 0xFB4F)],
    'Cyrillic': [(0x0400, 0x052F)],
    'Greek': [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    'Armenian': [(0x0530, 0x058F)],
    'Georgian': [(0x10A0, 0x10FF)],
    'Gurmukhi': [(0x0A00, 0x0A7F)],
    'Gujarati': [(0x0A80, 0x0AFF)],
    'Tamil': [(0x0B80, 0x0BFF)],
    'Telugu': [(0x0C00, 0x0C7F)],
    'Kannada': [(0x0C80, 0x0CFF)],
    'Malayalam': [(0x0D00, 0x0D7F)],
    'Sinhala': [(0x0D80, 0x0DFF)],
    'Thai': [(0x0E00, 0x0E7F)],
    'Lao': [(0x0E80, 0x0EFF)],
    'Tibetan': [(0x0F00, 0x0FFF)],
    'Myanmar': [(0x1000, 0x109F)],
    'Ethiopic': [(0x1200, 0x139F)],
    'Khmer': [(0x1780, 0x17FF)],
    'Hangul': [(0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)],
    'Kana': [(0x3040, 0x30FF)],
    'Han': [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)],
}
_RTL_SCRIPTS = ('Arabic', 'Hebrew')


def _script_of(ch: str):
    cp = ord(ch)
    for name, ranges in _SCRIPT_RANGES.items():
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return name
    # Basic Latin through Latin Extended-B, plus Latin Extended Additional
    # (Vietnamese tone-marked letters such as "ộ").
    if ch.isalpha() and (cp < 0x0250 or 0x1E00 <= cp <= 0x1EFF):
        return 'Latin'
    return None


def script_counts(text: str) -> dict:
    counts = {}
    for ch in text or '':
        script = _script_of(ch)
        if script:
            counts[script] = counts.get(script, 0) + 1
    return counts


def dominant_script(text: str):
    counts = script_counts(text)
    return max(counts, key=counts.get) if counts else None


def is_rtl_dominant(text: str) -> bool:
    return dominant_script(text) in _RTL_SCRIPTS


def script_shares(text: str) -> dict:
    """Fraction of letters in each script, e.g. {'Devanagari': 0.7, 'Latin': 0.3}."""
    counts = script_counts(text)
    total = sum(counts.values())
    return {name: n / total for name, n in counts.items()} if total else {}


def script_warning(text: str, expected_script) -> str:
    """Human-readable warning when the output is not in the expected script
    (e.g. Hindi selected but Whisper produced Urdu/Arabic script)."""
    if not expected_script or not text:
        return ''
    counts = script_counts(text)
    total = sum(counts.values())
    if not total:
        return ''
    share = counts.get(expected_script, 0) / total
    if share < 0.5:
        found = dominant_script(text)
        return (f"Expected {expected_script} script but most of the text is {found}. "
                "Check the language selection, or try a larger model.")
    return ''


# ── Normalisation & cleanup ──────────────────────────────────────────────────
def normalize(text: str) -> str:
    """NFC only. Safe for all scripts; keeps every combining character."""
    return unicodedata.normalize('NFC', text or '')


_MULTI_SPACE = re.compile(r'[ \t ]{2,}')
_SPACE_BEFORE_PUNCT = re.compile(r'[ \t]+([,.;:!?،؛؟।])')


def light_cleanup(text: str) -> str:
    """Optional tidy-up. Whitespace only, plus Latin-only capitalisation."""
    text = normalize(text).strip()
    text = _MULTI_SPACE.sub(' ', text)
    text = _SPACE_BEFORE_PUNCT.sub(r'\1', text)
    if text and dominant_script(text) == 'Latin' and text[0].isalpha() and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def join_segments(segment_texts) -> str:
    """Join Whisper segments with single spaces, preserving each segment's text."""
    parts = [normalize(t).strip() for t in segment_texts]
    return ' '.join(p for p in parts if p)
