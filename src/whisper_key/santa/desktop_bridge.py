# santa/desktop_bridge.py
# Lets the original Whisper Local desktop dictation (hold Fn+Ctrl, speak, text is
# pasted at the cursor of ANY app) use Santa as its speech engine. The hotkey,
# recording, paste-at-cursor and menu-bar parts stay in whisper_key; only the
# "audio -> text" step is replaced by a call to the local Santa server, so:
#   * one model in memory for both the Santa window and hotkey dictation,
#   * the language chosen in the Santa window (incl. Hinglish) applies to both,
#   * the Metal fast path and Unicode-safe post-processing are shared.
# Selected with `whisper.backend: santa` in ~/.whisperkey/user_settings.yaml
# (santa-dictation sets this for you).

import io
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get('SANTA_URL', 'http://127.0.0.1:8765')
_HEADERS = {'X-Santa': '1'}


# ── Tiny HTTP client ─────────────────────────────────────────────────────────
def _request(method: str, path: str, body: bytes = None, headers: dict = None, timeout: float = 10):
    req = urllib.request.Request(BASE_URL + path, data=body, method=method,
                                 headers={**_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def server_running() -> bool:
    try:
        return _request('GET', '/api/status', timeout=1.5).get('app') == 'Santa'
    except Exception:
        return False


def ensure_server(wait_seconds: float = 30.0) -> bool:
    """Start the Santa server in the background if it isn't running yet."""
    if server_running():
        return True
    from .settings import santa_home
    log_dir = os.path.join(santa_home(), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, 'server-from-dictation.log'), 'ab') as log:
        subprocess.Popen([sys.executable, '-m', 'whisper_key.santa.cli', '--no-browser'],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if server_running():
            return True
        time.sleep(0.3)
    return False


def to_wav_bytes(samples: np.ndarray, rate: int = 16000) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32).flatten(), -1.0, 1.0) * 32767).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ── Engine adapter (mirrors WhisperEngine's public surface) ──────────────────
class SantaHttpEngine:
    def __init__(self, model_key: str = 'santa', device: str = 'cpu', compute_type: str = 'int8',
                 language: str = None, beam_size: int = 5, initial_prompt: str = '',
                 hotwords: list = None, task: str = 'transcribe', vad_manager=None,
                 model_registry=None, log_transcriptions: bool = False, timeout: float = 600.0):
        # These attributes exist because state_manager/tray read and write them.
        # Santa decides language/prompt from its own settings (the Santa window).
        self.model_key = 'santa'
        self.device, self.compute_type, self.beam_size = device, compute_type, beam_size
        self.language, self.initial_prompt, self.hotwords, self.task = language, initial_prompt, hotwords, task
        self.timeout = timeout
        print('🎅 Using Santa as the speech engine (language is set in the Santa window).')
        if not ensure_server():
            print('   ⚠ Santa server did not start yet; dictation will retry on first use.')

    def is_loading(self) -> bool:
        return False

    def change_model(self, new_model_key: str, progress_callback=None) -> bool:
        if progress_callback:
            progress_callback('Choose the model in the Santa window (Settings).')
        return False

    def transcribe_audio(self, audio_data: Optional[np.ndarray]) -> Optional[str]:
        if audio_data is None or len(audio_data) == 0:
            return None
        if not ensure_server(wait_seconds=20):
            print('   ✗ Santa server is not running.')
            return None
        try:
            language = _request('GET', '/api/settings').get('settings', {}).get('language', 'auto')
            started = time.time()
            job = _request('POST', f'/api/transcribe?language={language}&filename=dictation.wav&source=recording',
                           body=to_wav_bytes(audio_data), headers={'Content-Type': 'audio/wav'})
            while job['state'] not in ('done', 'error', 'cancelled'):
                if time.time() - started > self.timeout:
                    _request('POST', f"/api/jobs/{job['id']}/cancel", body=b'')
                    print('   ✗ Santa timed out.')
                    return None
                time.sleep(0.08)
                job = _request('GET', f"/api/jobs/{job['id']}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode('utf-8'))['error']['message']
            except Exception:
                message = str(exc)
            print(f'   ✗ Santa: {message}')
            return None
        except Exception as exc:
            print(f'   ✗ Santa request failed: {type(exc).__name__}: {exc}')
            return None
        if job['state'] != 'done':
            if job.get('error'):
                print(f"   ✗ Santa: {job['error']['message']}")
            return None
        result = job['result']
        print(f"   ✓ Santa: {result['audio_seconds']:.1f}s → {len(result['text'])} chars in "
              f"{time.time() - started:.1f}s ({result.get('engine')}, {result['language_mode']})")
        return result['text'] or None
