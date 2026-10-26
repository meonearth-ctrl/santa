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
    group: str = 'santa'              # 'santa' = tuned modes shown first, 'more' = the rest


LANGUAGE_MODES = {
    'auto':     LanguageMode('auto', 'Auto-detect', None, None),
    'en':       LanguageMode('en', 'English', 'en', 'Latin'),
    'hi':       LanguageMode('hi', 'Hindi (हिन्दी)', 'hi', 'Devanagari'),
    'ar':       LanguageMode('ar', 'Arabic (العربية)', 'ar', 'Arabic', rtl=True),
    'hinglish': LanguageMode('hinglish', 'Hinglish (Hindi + English)', None, None),
    'bn':       LanguageMode('bn', 'Bengali (বাংলা)', 'bn', 'Bengali'),
}

# ── Every other Whisper language ─────────────────────────────────────────────
# The five tuned modes above come first; every other language Whisper can
# transcribe is offered too (like Wispr Flow's 100+ languages), decoded with the
# plain Whisper language code. Columns: code, English name, expected dominant
# script (None when the language is commonly written in more than one script,
# so no misleading warning is shown), right-to-left.
# The code list is exactly faster-whisper's tokenizer._LANGUAGE_CODES (a test
# checks this), so every mode here is a language the model really knows.
_MORE_LANGUAGES = (
    ('af', 'Afrikaans', 'Latin', False), ('am', 'Amharic', 'Ethiopic', False),
    ('as', 'Assamese', 'Bengali', False), ('az', 'Azerbaijani', 'Latin', False),
    ('ba', 'Bashkir', 'Cyrillic', False), ('be', 'Belarusian', 'Cyrillic', False),
    ('bg', 'Bulgarian', 'Cyrillic', False), ('bo', 'Tibetan', 'Tibetan', False),
    ('br', 'Breton', 'Latin', False), ('bs', 'Bosnian', 'Latin', False),
    ('ca', 'Catalan', 'Latin', False), ('cs', 'Czech', 'Latin', False),
    ('cy', 'Welsh', 'Latin', False), ('da', 'Danish', 'Latin', False),
    ('de', 'German', 'Latin', False), ('el', 'Greek', 'Greek', False),
    ('es', 'Spanish', 'Latin', False), ('et', 'Estonian', 'Latin', False),
    ('eu', 'Basque', 'Latin', False), ('fa', 'Persian', 'Arabic', True),
    ('fi', 'Finnish', 'Latin', False), ('fo', 'Faroese', 'Latin', False),
    ('fr', 'French', 'Latin', False), ('gl', 'Galician', 'Latin', False),
    ('gu', 'Gujarati', 'Gujarati', False), ('ha', 'Hausa', 'Latin', False),
    ('haw', 'Hawaiian', 'Latin', False), ('he', 'Hebrew', 'Hebrew', True),
    ('hr', 'Croatian', 'Latin', False), ('ht', 'Haitian Creole', 'Latin', False),
    ('hu', 'Hungarian', 'Latin', False), ('hy', 'Armenian', 'Armenian', False),
    ('id', 'Indonesian', 'Latin', False), ('is', 'Icelandic', 'Latin', False),
    ('it', 'Italian', 'Latin', False), ('ja', 'Japanese', None, False),
    ('jw', 'Javanese', 'Latin', False), ('ka', 'Georgian', 'Georgian', False),
    ('kk', 'Kazakh', None, False), ('km', 'Khmer', 'Khmer', False),
    ('kn', 'Kannada', 'Kannada', False), ('ko', 'Korean', 'Hangul', False),
    ('la', 'Latin', 'Latin', False), ('lb', 'Luxembourgish', 'Latin', False),
    ('ln', 'Lingala', 'Latin', False), ('lo', 'Lao', 'Lao', False),
    ('lt', 'Lithuanian', 'Latin', False), ('lv', 'Latvian', 'Latin', False),
    ('mg', 'Malagasy', 'Latin', False), ('mi', 'Maori', 'Latin', False),
    ('mk', 'Macedonian', 'Cyrillic', False), ('ml', 'Malayalam', 'Malayalam', False),
    ('mn', 'Mongolian', 'Cyrillic', False), ('mr', 'Marathi', 'Devanagari', False),
    ('ms', 'Malay', 'Latin', False), ('mt', 'Maltese', 'Latin', False),
    ('my', 'Burmese', 'Myanmar', False), ('ne', 'Nepali', 'Devanagari', False),
    ('nl', 'Dutch', 'Latin', False), ('nn', 'Norwegian Nynorsk', 'Latin', False),
    ('no', 'Norwegian', 'Latin', False), ('oc', 'Occitan', 'Latin', False),
    ('pa', 'Punjabi', None, False), ('pl', 'Polish', 'Latin', False),
    ('ps', 'Pashto', 'Arabic', True), ('pt', 'Portuguese', 'Latin', False),
    ('ro', 'Romanian', 'Latin', False), ('ru', 'Russian', 'Cyrillic', False),
    ('sa', 'Sanskrit', 'Devanagari', False), ('sd', 'Sindhi', 'Arabic', True),
    ('si', 'Sinhala', 'Sinhala', False), ('sk', 'Slovak', 'Latin', False),
    ('sl', 'Slovenian', 'Latin', False), ('sn', 'Shona', 'Latin', False),
    ('so', 'Somali', 'Latin', False), ('sq', 'Albanian', 'Latin', False),
    ('sr', 'Serbian', None, False), ('su', 'Sundanese', 'Latin', False),
    ('sv', 'Swedish', 'Latin', False), ('sw', 'Swahili', 'Latin', False),
    ('ta', 'Tamil', 'Tamil', False), ('te', 'Telugu', 'Telugu', False),
    ('tg', 'Tajik', 'Cyrillic', False), ('th', 'Thai', 'Thai', False),
    ('tk', 'Turkmen', 'Latin', False), ('tl', 'Tagalog', 'Latin', False),
    ('tr', 'Turkish', 'Latin', False), ('tt', 'Tatar', 'Cyrillic', False),
    ('uk', 'Ukrainian', 'Cyrillic', False), ('ur', 'Urdu', 'Arabic', True),
    ('uz', 'Uzbek', None, False), ('vi', 'Vietnamese', 'Latin', False),
    ('yi', 'Yiddish', 'Hebrew', True), ('yo', 'Yoruba', 'Latin', False),
    ('yue', 'Cantonese', 'Han', False), ('zh', 'Chinese', 'Han', False),
)
for _code, _name, _script, _rtl in _MORE_LANGUAGES:
    LANGUAGE_MODES[_code] = LanguageMode(_code, _name, _code, _script, rtl=_rtl, group='more')

# Readable names for whatever auto-detection reports (all Whisper languages).
WHISPER_LANGUAGE_NAMES = {'en': 'English', 'hi': 'Hindi', 'ar': 'Arabic', 'bn': 'Bengali',
                          **{code: name for code, name, _, _ in _MORE_LANGUAGES}}


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
    return [{'key': m.key, 'label': m.label, 'rtl': m.rtl, 'script': m.script, 'group': m.group}
            for m in LANGUAGE_MODES.values()]


def language_name(mode_key: str, detected: Optional[str] = None) -> str:
    """Plain English name of what was spoken, for the grammar model's prompt:
    the detected language when auto-detecting, else the chosen mode."""
    if mode_key == 'hinglish':
        return 'Hinglish (Hindi and English mixed in one sentence)'
    if mode_key == 'auto':
        return WHISPER_LANGUAGE_NAMES.get(detected, '') if detected else ''
    return WHISPER_LANGUAGE_NAMES.get(mode_key) or LANGUAGE_MODES[mode_key].label


def hinglish_strategies_for_ui() -> list:
    return [{'key': s.key, 'label': s.label} for s in HINGLISH_STRATEGIES.values()]
