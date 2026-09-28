# santa/accel_cpp.py
# Optional Apple-GPU fast path: whisper.cpp (via pywhispercpp) running
# large-v3-turbo (q5_0 GGML) on Metal. Measured on an M5 it is ~4x faster than
# faster-whisper on the CPU for short dictation clips (docs/santa/EVALUATION.md).
#
# It is deliberately used ONLY where it measured safe:
#   * clips up to MAX_CLIP_SECONDS (one 30 s Whisper window). In timestamp-less
#     mode longer audio lost text at the window boundary; in timestamp mode a
#     Hindi clip produced a duplicated phrase and a broken character.
#   * the result is rejected (and the CPU engine used instead) if it contains
#     U+FFFD, i.e. any sign of a split multi-byte character.
# Text is rebuilt from the raw UTF-8 bytes of all segments, never per-segment
# decoded strings, so Devanagari/Bengali/Arabic characters are not cut.

import logging
import os
import platform
import sys
import threading
import time

logger = logging.getLogger(__name__)

REPO = 'ggerganov/whisper.cpp'
FILENAME = 'ggml-large-v3-turbo-q5_0.bin'
DOWNLOAD_MB = 547
SERVES_MODEL = 'large-v3-turbo'     # the faster-whisper model this accelerates
MAX_CLIP_SECONDS = 28.0
BEAM_SIZE = 5


def is_supported_platform() -> bool:
    return sys.platform == 'darwin' and platform.machine() == 'arm64'


def is_installed() -> bool:
    try:
        import pywhispercpp  # noqa: F401
        return True
    except Exception:
        return False


class AcceleratorError(Exception):
    pass


# ── Long audio: split at pauses into <= MAX_CLIP_SECONDS pieces ──────────────
def plan_chunks(samples, max_seconds: float = MAX_CLIP_SECONDS, rate: int = 16000):
    """Return [(start, end)] sample ranges covering the speech, each at most
    `max_seconds` long and cut only at pauses found by Silero VAD (bundled with
    faster-whisper). Returns [] when there is no speech."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=200,
                      max_speech_duration_s=max_seconds - 1.0)
    regions = get_speech_timestamps(samples, opts)
    if not regions:
        return []
    limit = int(max_seconds * rate)
    chunks = []
    start, end = regions[0]['start'], regions[0]['end']
    for region in regions[1:]:
        if region['end'] - start <= limit:
            end = region['end']                   # still fits: extend this chunk
        else:
            chunks.append((start, end))
            start, end = region['start'], region['end']
    chunks.append((start, end))
    return [(a, min(b, a + limit)) for a, b in chunks]


class MetalAccelerator:
    def __init__(self, log_dir: str = None):
        self._lock = threading.Lock()
        self._model = None
        self._thread = None
        self._log_path = os.path.join(log_dir, 'whispercpp.log') if log_dir else None
        self.status = {'state': 'off', 'message': 'GPU fast path off.'}

    def available(self) -> bool:
        return is_supported_platform() and is_installed()

    def ready(self) -> bool:
        return self._model is not None

    def get_status(self) -> dict:
        return dict(self.status)

    def ensure_loading(self) -> None:
        if not self.available():
            self.status = {'state': 'unavailable',
                           'message': 'GPU fast path unavailable (needs Apple Silicon + pywhispercpp).'}
            return
        if self._model is not None or (self._thread and self._thread.is_alive()):
            return
        self.status = {'state': 'loading', 'message': 'Preparing GPU fast path…'}
        self._thread = threading.Thread(target=self._load, daemon=True, name='santa-metal-loader')
        self._thread.start()

    def _load(self) -> None:
        try:
            from huggingface_hub import hf_hub_download
            try:
                path = hf_hub_download(REPO, FILENAME, local_files_only=True)
            except Exception:
                self.status = {'state': 'downloading',
                               'message': f'Downloading GPU fast-path model (~{DOWNLOAD_MB} MB, once)…'}
                path = hf_hub_download(REPO, FILENAME)
            from pywhispercpp.model import Model
            started = time.time()
            model = Model(path, params_sampling_strategy=1,
                          beam_search={'beam_size': BEAM_SIZE, 'patience': -1.0},
                          n_threads=4, no_timestamps=True, print_progress=False,
                          print_realtime=False, redirect_whispercpp_logs_to=self._log_path or None)
            self._model = model
            self.status = {'state': 'ready', 'message': 'GPU fast path ready (Metal).',
                           'load_seconds': round(time.time() - started, 2)}
        except Exception as exc:
            logger.warning('Metal accelerator unavailable: %s: %s', type(exc).__name__, exc)
            self.status = {'state': 'error', 'message': f'GPU fast path failed to load ({type(exc).__name__}); using CPU.'}

    def transcribe(self, samples, params, cancel_event=None) -> dict:
        """Same result shape as models.run_transcription. Raises AcceleratorError
        when the output must not be trusted (caller falls back to the CPU)."""
        if self._model is None:
            raise AcceleratorError('not loaded')
        import _pywhispercpp as pw
        abort = (lambda: cancel_event.is_set()) if cancel_event is not None else None
        with self._lock:
            started = time.time()
            self._model.transcribe(samples, language=params.language or 'auto',
                                   initial_prompt=params.initial_prompt or '',
                                   abort_callback=abort)
            elapsed = time.time() - started
            ctx = self._model._ctx
            n = pw.whisper_full_n_segments(ctx)
            raw_bytes = [pw.whisper_full_get_segment_text(ctx, i) for i in range(n)]
            try:
                detected = pw.whisper_lang_str(pw.whisper_full_lang_id(ctx))
            except Exception:
                detected = params.language
        if cancel_event is not None and cancel_event.is_set():
            from .models import TranscriptionCancelled
            raise TranscriptionCancelled()
        text = b''.join(raw_bytes).decode('utf-8', errors='replace')
        if '�' in text:
            raise AcceleratorError('split multi-byte character in output')
        duration = len(samples) / 16000.0
        return {
            'segments': [{'start': 0.0, 'end': round(duration, 2), 'text': text}],
            'detected_language': detected,
            'language_probability': None,
            'transcribe_seconds': round(elapsed, 2),
            'engine': 'whisper.cpp · Metal GPU',
        }
