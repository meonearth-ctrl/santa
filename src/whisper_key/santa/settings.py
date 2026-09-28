# santa/settings.py
# Santa's own settings, stored as JSON outside the repository:
#   macOS   ~/Library/Application Support/Santa/settings.json
#   Windows %APPDATA%\Santa\settings.json
#   other   ~/.local/share/santa/settings.json
# SANTA_HOME overrides the directory (used by tests). Unknown keys are dropped
# and bad values fall back to defaults, so a hand-edited file can't crash us.

import json
import os
import sys
import threading

from .languages import LANGUAGE_MODES, HINGLISH_STRATEGIES, HINGLISH_OUTPUTS, DEFAULT_HINGLISH_STRATEGY

DEFAULTS = {
    'model': 'large-v3-turbo',     # chosen from measurements (docs/santa/EVALUATION.md)
    'engine': 'auto',              # auto = Metal GPU for short clips when available, else CPU
    'compute_type': 'int8',
    'cpu_threads': 0,              # 0 = automatic (cores - 2, max 8)
    'beam_size': 5,
    'language': 'auto',
    'hinglish_strategy': DEFAULT_HINGLISH_STRATEGY,
    'hinglish_output': 'native',
    'cleanup': True,               # tiny, script-aware tidy-up; raw text always kept
    'vad_filter': True,            # skip silence, reduces hallucinations
    'user_prompt': '',             # optional vocabulary/names hint for Whisper
    'history_enabled': False,      # transcripts are NOT kept unless you opt in
    'microphone_id': '',
    'microphone_label': '',
    'max_upload_mb': 200,
    'max_duration_min': 60,
    'preload_model': True,
    # Transcript files for other tools (e.g. Hiring Right). Off unless set.
    'export_enabled': False,
    'export_dir': '',              # e.g. ~/Documents/Hiring Right/03_Transcripts
    # Watch folder: new audio/video files dropped here are transcribed automatically.
    'watch_enabled': False,
    'watch_dir': '',               # e.g. ~/Documents/Hiring Right/02_Batch_Videos
    'watch_language': 'en',
    # Output language: 'same' keeps what was spoken. 'en'/'ar' add an explicit,
    # labelled offline translation (the original is always kept alongside).
    'output_language': 'same',
    # Speaker separation for files (interviews). Never applied to dictation.
    'diarize_files': False,
    'num_speakers': 0,             # 0 = work it out automatically
    # iPhone access over the home Wi-Fi (HTTPS + pairing). Off unless switched on.
    'phone_enabled': False,
    'phone_port': 8766,
}

_CHOICES = {
    'language': set(LANGUAGE_MODES),
    'hinglish_strategy': set(HINGLISH_STRATEGIES),
    'hinglish_output': set(HINGLISH_OUTPUTS),
    'compute_type': {'int8', 'int8_float32', 'float32'},
    'engine': {'auto', 'cpu'},
    'watch_language': set(LANGUAGE_MODES),
    'output_language': {'same', 'en', 'ar'},
}
_RANGES = {
    'cpu_threads': (0, 64), 'beam_size': (1, 10),
    'max_upload_mb': (1, 2000), 'max_duration_min': (1, 240),
    'num_speakers': (0, 10), 'phone_port': (1024, 65535),
}


def santa_home() -> str:
    override = os.environ.get('SANTA_HOME')
    if override:
        return override
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/Santa')
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'Santa')
    return os.path.expanduser('~/.local/share/santa')


def validate(values: dict) -> dict:
    """Return a complete, type-checked settings dict (defaults for anything invalid)."""
    clean = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in values:
            continue
        value = values[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                clean[key] = value
        elif isinstance(default, int):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lo, hi = _RANGES.get(key, (-10**9, 10**9))
                clean[key] = int(min(max(value, lo), hi))
        elif isinstance(default, str):
            if isinstance(value, str) and (key not in _CHOICES or value in _CHOICES[key]):
                clean[key] = value[:2000]
    return clean


class SettingsStore:
    def __init__(self, home: str = None):
        self.home = home or santa_home()
        self.path = os.path.join(self.home, 'settings.json')
        self._lock = threading.Lock()
        self._values = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                return validate(json.load(fh))
        except (OSError, ValueError):
            return dict(DEFAULTS)

    def get(self) -> dict:
        with self._lock:
            return dict(self._values)

    def update(self, changes: dict) -> dict:
        with self._lock:
            merged = dict(self._values)
            merged.update({k: v for k, v in (changes or {}).items() if k in DEFAULTS})
            self._values = validate(merged)
            self._save()
            return dict(self._values)

    def _save(self) -> None:
        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self._values, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
