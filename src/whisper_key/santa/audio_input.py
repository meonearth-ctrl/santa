# santa/audio_input.py
# Validation and decoding of audio coming from the browser recorder or from an
# uploaded file. Decoding uses faster-whisper's bundled PyAV decoder, so common
# formats (WAV, MP3, M4A/AAC, OGG/Opus, WebM, FLAC, CAF) work without ffmpeg.
# Temporary files are created in a private directory and always deleted.

import os
import tempfile
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000

SUPPORTED_EXTENSIONS = {
    '.wav', '.wave', '.mp3', '.m4a', '.mp4', '.aac', '.ogg', '.oga', '.opus',
    '.webm', '.flac', '.caf', '.aif', '.aiff',
    '.mov', '.m4v', '.mkv',                      # video files: the audio track is used
}

# Below this peak amplitude we treat a clip as silence (no speech possible).
SILENCE_PEAK = 0.003
MIN_DURATION_S = 0.3


class AudioInputError(Exception):
    """Base error carrying a short machine code and a user-facing message."""
    code = 'audio_error'

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnsupportedAudio(AudioInputError):
    code = 'unsupported_audio'


class AudioTooLarge(AudioInputError):
    code = 'audio_too_large'


class AudioTooLong(AudioInputError):
    code = 'audio_too_long'


class EmptyAudio(AudioInputError):
    code = 'empty_audio'


@dataclass
class DecodedAudio:
    samples: np.ndarray        # mono float32 @ 16 kHz
    duration_s: float
    peak: float


def check_upload(filename: str, size_bytes: int, max_bytes: int) -> str:
    """Cheap checks before we store anything. Returns the normalised extension."""
    ext = os.path.splitext(filename or '')[1].lower() or '.wav'
    if ext not in SUPPORTED_EXTENSIONS:
        allowed = ', '.join(sorted(e.lstrip('.') for e in SUPPORTED_EXTENSIONS))
        raise UnsupportedAudio(f"'{ext.lstrip('.')}' files are not supported. Use one of: {allowed}.")
    if size_bytes <= 0:
        raise EmptyAudio("The recording is empty.")
    if size_bytes > max_bytes:
        raise AudioTooLarge(f"File is {size_bytes / 1e6:.0f} MB; the limit is {max_bytes / 1e6:.0f} MB.")
    return ext


def decode_bytes(data: bytes, ext: str, max_duration_s: float) -> DecodedAudio:
    """Write to a private temp file, decode to 16 kHz mono, delete the file."""
    path = write_temp(data, ext)
    try:
        return decode_path(path, max_duration_s)
    finally:
        remove_temp(path)


def write_temp(data: bytes, ext: str) -> str:
    """Private temp file (0600) inside its own santa-* directory."""
    tmp_dir = tempfile.mkdtemp(prefix='santa-')
    path = os.path.join(tmp_dir, 'input' + ext)
    with open(path, 'wb') as fh:
        os.chmod(path, 0o600)
        fh.write(data)
    return path


def new_temp_path(ext: str) -> str:
    """Path for a streamed upload; caller writes it and later calls remove_temp()."""
    return os.path.join(tempfile.mkdtemp(prefix='santa-'), 'input' + ext)


def remove_temp(path: str) -> None:
    _remove_quietly(path)
    _remove_quietly(os.path.dirname(path), is_dir=True)


def decode_path(path: str, max_duration_s: float) -> DecodedAudio:
    """Decode any supported audio/video file to 16 kHz mono float32 (PyAV)."""
    try:
        from faster_whisper.audio import decode_audio
        samples = decode_audio(path, sampling_rate=SAMPLE_RATE)
    except Exception as exc:
        raise UnsupportedAudio(
            "Could not read this audio. The file may be damaged or in an unsupported "
            f"format ({type(exc).__name__}).") from exc
    return check_samples(np.asarray(samples, dtype=np.float32), max_duration_s)


def check_samples(samples: np.ndarray, max_duration_s: float) -> DecodedAudio:
    if samples.ndim > 1:
        samples = samples.mean(axis=1).astype(np.float32)
    duration = len(samples) / SAMPLE_RATE
    if duration < MIN_DURATION_S:
        raise EmptyAudio("The recording is too short — nothing to transcribe.")
    if duration > max_duration_s:
        raise AudioTooLong(f"Audio is {duration / 60:.1f} min; the limit is {max_duration_s / 60:.0f} min "
                           "(change it in Settings).")
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak < SILENCE_PEAK:
        raise EmptyAudio("The recording is silent. Check that the right microphone is selected "
                         "and not muted.")
    return DecodedAudio(samples=samples, duration_s=duration, peak=peak)


def _remove_quietly(path: str, is_dir: bool = False) -> None:
    try:
        os.rmdir(path) if is_dir else os.remove(path)
    except OSError:
        pass
