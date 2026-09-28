# santa/languages.py
# Maps the language choices shown to the user onto real faster-whisper decode
# parameters. Whisper has no "Hinglish" language code, so Hinglish is modelled
# as a *strategy* (which Whisper language to force, which prompt to prime with,
# whether to re-detect language per segment) plus an *output script* preference.
# We only ever use task="transcribe": Santa never silently translates.

from dataclasses import dataclass, field
from typing import Optional


# ── Language modes offered in the UI ─────────────────────────────────────────
@dataclass(frozen=True)
class LanguageMode:
    key: str
    label: str
    whisper_language: Optional[str]   # verified Whisper code, or None = detect
    script: Optional[str]             # expected dominant script (for warnings)
    rtl: bool = False


LANGUAGE_MODES = {
    'auto':     LanguageMode('auto', 'Auto-detect', None, None),
    'en':       LanguageMode('en', 'English', 'en', 'Latin'),
    'hi':       LanguageMode('hi', 'Hindi (हिन्दी)', 'hi', 'Devanagari'),
    'ar':       LanguageMode('ar', 'Arabic (العربية)', 'ar', 'Arabic', rtl=True),
    'hinglish': LanguageMode('hinglish', 'Hinglish (Hindi + English)', None, None),
    'bn':       LanguageMode('bn', 'Bengali (বাংলা)', 'bn', 'Bengali'),
}

# Whisper codes we accept from auto-detection and label nicely in the UI.
WHISPER_LANGUAGE_NAMES = {
    'en': 'English', 'hi': 'Hindi', 'ar': 'Arabic', 'bn': 'Bengali', 'ur': 'Urdu',
    'mr': 'Marathi', 'ne': 'Nepali', 'fa': 'Persian', 'pa': 'Punjabi', 'gu': 'Gujarati',
    'ta': 'Tamil', 'te': 'Telugu', 'ml': 'Malayalam', 'kn': 'Kannada', 'as': 'Assamese',
}


# ── Hinglish strategies ──────────────────────────────────────────────────────
# Each is a hypothesis about how to get faithful code-switched text out of a
# model trained per-language. The default is chosen from measured results (see
# docs/santa/EVALUATION.md); the others stay selectable in Settings.
@dataclass(frozen=True)
class HinglishStrategy:
    key: str
    label: str
    whisper_language: Optional[str]
    initial_prompt: Optional[str]
    multilingual: bool = False
    native_script: str = 'mixed'      # what script the raw output tends to be in


# Prompts are short, neutral and contain no names/numbers so they cannot leak
# plausible-looking content into the transcript.
_HINDI_MIXED_PROMPT = "आज की meeting में हमने project के बारे में बात की, और फिर email भेज दिया।"
_ROMAN_PROMPT = "Aaj ki meeting mein humne project ke baare mein baat ki, aur phir email bhej diya."

HINGLISH_STRATEGIES = {
    'hindi_mixed': HinglishStrategy(
        'hindi_mixed', 'Hindi-biased, mixed-script prompt',
        'hi', _HINDI_MIXED_PROMPT),
    'auto': HinglishStrategy(
        'auto', 'Plain auto-detect (no prompt)', None, None),
    'per_segment': HinglishStrategy(
        'per_segment', 'Auto-detect per segment', None, None, multilingual=True),
    'english_roman': HinglishStrategy(
        'english_roman', 'English-biased, romanized prompt',
        'en', _ROMAN_PROMPT, native_script='latin'),
}

DEFAULT_HINGLISH_STRATEGY = 'hindi_mixed'
HINGLISH_OUTPUTS = ('native', 'roman')   # native mixed script | romanized Latin


# ── Resolution ───────────────────────────────────────────────────────────────
@dataclass
class DecodeParams:
    language: Optional[str]
    initial_prompt: Optional[str] = None
    multilingual: bool = False
    task: str = 'transcribe'           # never 'translate'
    notes: list = field(default_factory=list)


class UnknownLanguageMode(ValueError):
    pass


def resolve_decode_params(mode_key: str, hinglish_strategy: str = DEFAULT_HINGLISH_STRATEGY,
                          user_prompt: str = '') -> DecodeParams:
    """Turn a UI language choice into faster-whisper keyword arguments."""
    if mode_key not in LANGUAGE_MODES:
        raise UnknownLanguageMode(f"Unknown language mode: {mode_key!r}")

    if mode_key == 'hinglish':
        strategy = HINGLISH_STRATEGIES.get(hinglish_strategy) or \
            HINGLISH_STRATEGIES[DEFAULT_HINGLISH_STRATEGY]
        prompt = _join_prompts(strategy.initial_prompt, user_prompt)
        return DecodeParams(language=strategy.whisper_language, initial_prompt=prompt,
                            multilingual=strategy.multilingual,
                            notes=[f"hinglish_strategy={strategy.key}"])

    mode = LANGUAGE_MODES[mode_key]
    return DecodeParams(language=mode.whisper_language,
                        initial_prompt=_join_prompts(None, user_prompt))


def _join_prompts(*parts) -> Optional[str]:
    text = ' '.join(p.strip() for p in parts if p and p.strip())
    return text or None


def language_modes_for_ui() -> list:
    return [{'key': m.key, 'label': m.label, 'rtl': m.rtl, 'script': m.script}
            for m in LANGUAGE_MODES.values()]


def hinglish_strategies_for_ui() -> list:
    return [{'key': s.key, 'label': s.label} for s in HINGLISH_STRATEGIES.values()]
