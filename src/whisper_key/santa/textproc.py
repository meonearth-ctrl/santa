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
_SCRIPT_RANGES = {
    'Devanagari': [(0x0900, 0x097F), (0xA8E0, 0xA8FF)],
    'Bengali': [(0x0980, 0x09FF)],
    'Arabic': [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
               (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
}


def _script_of(ch: str):
    cp = ord(ch)
    for name, ranges in _SCRIPT_RANGES.items():
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return name
    if ch.isalpha() and cp < 0x0250:
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
    return dominant_script(text) == 'Arabic'


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
