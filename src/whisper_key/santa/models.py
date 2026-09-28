# santa/models.py
# Model catalogue + lifecycle for Santa. One WhisperModel is loaded and reused
# for every request (no reload per transcription). Loading/downloading happens
# on a background thread and exposes a status the UI polls, including download
# progress measured from the Hugging Face cache folder.
#
# Hardware note: faster-whisper runs on CTranslate2, which has no Metal backend.
# On Apple Silicon it therefore uses the CPU (int8 by default), which is fast on
# M-series chips. Only multilingual models are listed: English-only (.en) and
# distil-*.en models cannot transcribe Hindi, Arabic or Bengali.

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# Plain HTTP downloads write growing *.incomplete files we can measure for a
# progress bar; the xet transfer path does not. Must be set before
# huggingface_hub is imported anywhere in this process.
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')

_MODEL_FILES = ['config.json', 'preprocessor_config.json', 'model.bin',
                'tokenizer.json', 'vocabulary.*']


@dataclass(frozen=True)
class ModelInfo:
    key: str
    repo: str
    label: str
    download_mb: int     # approximate size on disk
    ram_gb: float        # approximate resident memory when loaded (int8, CPU)
    note: str


CATALOG = {
    'base': ModelInfo('base', 'Systran/faster-whisper-base', 'Base', 145, 0.5,
                      'Fastest. Weak for Hindi, Arabic and Bengali — testing only.'),
    'small': ModelInfo('small', 'Systran/faster-whisper-small', 'Small', 485, 1.0,
                       'Fast. Usable English; noticeably weaker on Indic/Arabic.'),
    'medium': ModelInfo('medium', 'Systran/faster-whisper-medium', 'Medium', 1530, 2.0,
                        'Good accuracy, slower than Large-v3 Turbo on most text.'),
    'large-v3-turbo': ModelInfo('large-v3-turbo', 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
                                'Large-v3 Turbo', 1620, 2.5,
                                'Best speed/accuracy balance for multilingual dictation.'),
    'large-v3': ModelInfo('large-v3', 'Systran/faster-whisper-large-v3', 'Large-v3', 3090, 4.5,
                          'Most accurate, slowest. Large download.'),
}


class ModelUnavailable(Exception):
    code = 'model_unavailable'

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TranscriptionCancelled(Exception):
    code = 'cancelled'


def total_ram_gb() -> float:
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        return 0.0


def effective_cpu_threads(requested: int) -> int:
    """0 = automatic. CTranslate2's own default uses only 4 threads; on a 10-core
    Apple M5, 8 threads measured ~20% faster (docs/santa/EVALUATION.md). Leave two
    cores for the rest of the system."""
    if requested and requested > 0:
        return requested
    return max(1, min(8, (os.cpu_count() or 4) - 2))


def hub_cache_dir() -> str:
    try:
        from huggingface_hub import constants
        return constants.HF_HUB_CACHE
    except Exception:
        return os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')


def _repo_cache_dir(repo: str) -> str:
    return os.path.join(hub_cache_dir(), 'models--' + repo.replace('/', '--'))


def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


def cached_model_path(key: str) -> Optional[str]:
    """Local snapshot path when the model is fully downloaded, else None. No network."""
    info = CATALOG.get(key)
    if not info:
        return None
    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(info.repo, allow_patterns=_MODEL_FILES, local_files_only=True)
    except Exception:
        return None
    return path if os.path.exists(os.path.join(path, 'model.bin')) else None


def catalog_for_ui() -> list:
    ram = total_ram_gb()
    out = []
    for info in CATALOG.values():
        out.append({
            'key': info.key, 'label': info.label, 'download_mb': info.download_mb,
            'ram_gb': info.ram_gb, 'note': info.note,
            'cached': cached_model_path(info.key) is not None,
            'fits_memory': (ram == 0.0) or info.ram_gb <= ram * 0.5,
        })
    return out


# ── Model manager ────────────────────────────────────────────────────────────
class ModelManager:
    """Owns the single loaded model. Thread-safe; loads in the background."""

    def __init__(self, compute_type: str = 'int8', cpu_threads: int = 0):
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self._lock = threading.Lock()
        self._model = None
        self._model_key = None
        self._thread = None
        self._pending = None
        self._ready = threading.Event()
        self.status = {'state': 'not_loaded', 'model': None, 'message': 'No model loaded yet.',
                       'progress': None, 'load_seconds': None}

    # Public ---------------------------------------------------------------
    def get_status(self) -> dict:
        with self._lock:
            return dict(self.status)

    def loaded_key(self) -> Optional[str]:
        return self._model_key if self._model is not None else None

    def ensure_loading(self, key: str, compute_type: str = None, cpu_threads: int = None) -> None:
        """Start loading `key` unless it is already loaded or loading."""
        compute_type = compute_type or self.compute_type
        cpu_threads = self.cpu_threads if cpu_threads is None else cpu_threads
        with self._lock:
            same_config = (compute_type == self.compute_type and cpu_threads == self.cpu_threads)
            if self._model is not None and self._model_key == key and same_config:
                return
            if self._thread and self._thread.is_alive():
                # Never load two models at once (memory). Remember the newest
                # request; the loader thread picks it up when it finishes.
                if self.status.get('model') != key:
                    self._pending = (key, compute_type, cpu_threads)
                return
            self._pending = None
            self.compute_type, self.cpu_threads = compute_type, cpu_threads
            self._ready.clear()
            self.status = {'state': 'checking', 'model': key, 'message': 'Checking model cache…',
                           'progress': None, 'load_seconds': None}
            self._thread = threading.Thread(target=self._load, args=(key,), daemon=True,
                                            name='santa-model-loader')
            self._thread.start()

    def wait_ready(self, key: str, timeout: float = None, cancel_event=None):
        """Block until `key` is loaded (or raise ModelUnavailable)."""
        self.ensure_loading(key)
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled()
            with self._lock:
                if self._model is not None and self._model_key == key and \
                        self.status['state'] == 'ready':
                    return self._model
                if self.status['state'] == 'error' and self.status.get('model') == key:
                    raise ModelUnavailable(self.status['message'])
            if deadline and time.time() > deadline:
                raise ModelUnavailable('Timed out waiting for the model to load.')
            self._ready.wait(0.25)

    # Internals ------------------------------------------------------------
    def _set(self, **changes):
        with self._lock:
            self.status.update(changes)

    def _load(self, key: str) -> None:
        try:
            self._load_one(key)
        finally:
            self._ready.set()   # wake waiters so they re-check state
            with self._lock:
                pending, self._pending = self._pending, None
                self._thread = None
            if pending and pending[0] != self.loaded_key():
                self.ensure_loading(*pending)

    def _load_one(self, key: str) -> None:
        info = CATALOG.get(key)
        if info is None:
            self._set(state='error', message=f"Unknown model '{key}'.")
            return
        ram = total_ram_gb()
        if ram and info.ram_gb > ram * 0.75:
            self._set(state='error', message=(
                f"{info.label} needs about {info.ram_gb:.1f} GB RAM; this computer has "
                f"{ram:.0f} GB. Choose a smaller model."))
            return
        started = time.time()
        try:
            path = cached_model_path(key)
            if path is None:
                path = self._download(info)
            self._set(state='loading', message=f'Loading {info.label} into memory…', progress=None)
            from faster_whisper import WhisperModel
            threads = effective_cpu_threads(self.cpu_threads)
            model = WhisperModel(path, device='cpu', compute_type=self.compute_type,
                                 cpu_threads=threads)
            self._warmup(model)
        except MemoryError:
            self._set(state='error', message=f'Not enough memory to load {info.label}. '
                                             'Close other apps or choose a smaller model.')
            return
        except ModelUnavailable as exc:
            self._set(state='error', message=exc.message)
            return
        except Exception as exc:  # corrupted cache, disk full, etc.
            self._set(state='error', message=f'Could not load {info.label}: {type(exc).__name__}: {exc}')
            return
        with self._lock:
            old = self._model
            self._model, self._model_key = model, key
            self.status = {'state': 'ready', 'model': key, 'progress': None,
                           'message': f'{info.label} ready (CPU, {self.compute_type}, {threads} threads).',
                           'load_seconds': round(time.time() - started, 2)}
        del old
        self._ready.set()

    def _download(self, info: ModelInfo) -> str:
        from huggingface_hub import snapshot_download
        expected = info.download_mb * 1024 * 1024
        cache_dir = _repo_cache_dir(info.repo)
        self._set(state='downloading', progress=0.0,
                  message=f'Downloading {info.label} (~{info.download_mb} MB, first time only)…')
        done = threading.Event()

        def watch():
            while not done.wait(0.5):
                got = _dir_bytes(cache_dir)
                pct = min(0.99, got / expected) if expected else None
                self._set(progress=pct, message=(
                    f'Downloading {info.label}: {got / 1e6:.0f} / ~{info.download_mb} MB'))

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            return snapshot_download(info.repo, allow_patterns=_MODEL_FILES)
        except Exception as exc:
            raise ModelUnavailable(
                f"Could not download {info.label}. Check the internet connection "
                f"(needed once per model). Details: {type(exc).__name__}") from exc
        finally:
            done.set()

    @staticmethod
    def _warmup(model) -> None:
        import numpy as np
        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language='en',
                                       beam_size=1, vad_filter=False)
        for _ in segments:
            pass


# ── Inference ────────────────────────────────────────────────────────────────
def run_transcription(model, samples, params, beam_size: int = 5, vad_filter: bool = True,
                      cancel_event=None, on_progress: Callable[[float], None] = None) -> dict:
    """Transcribe mono 16 kHz float32 samples with a loaded WhisperModel.
    Cancellation is checked between segments (Whisper cannot stop mid-segment)."""
    duration = len(samples) / 16000.0
    kwargs = dict(language=params.language, task='transcribe', beam_size=beam_size,
                  vad_filter=vad_filter, condition_on_previous_text=False,
                  initial_prompt=params.initial_prompt)
    if params.multilingual:
        kwargs['multilingual'] = True
    started = time.time()
    segments_iter, info = model.transcribe(samples, **kwargs)
    segments = []
    for seg in segments_iter:
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled()
        segments.append({'start': round(seg.start, 2), 'end': round(seg.end, 2), 'text': seg.text})
        if on_progress and duration:
            on_progress(min(0.99, seg.end / duration))
    return {
        'segments': segments,
        'detected_language': info.language,
        'language_probability': round(float(info.language_probability or 0.0), 3),
        'transcribe_seconds': round(time.time() - started, 2),
    }
