# santa/server.py
# Localhost HTTP server for Santa: serves the browser UI and a small JSON API
# over the TranscriptionService. Standard library only (like local_server.py).
#
# Security posture (Phase 1 = this computer only):
#   * binds 127.0.0.1 by default; the CLI refuses non-loopback hosts
#   * Host header must be a loopback name (blocks DNS-rebinding attacks)
#   * state-changing requests need the custom X-Santa header and, if present, a
#     same-origin Origin header, so other websites can't drive the API
#   * request bodies are size-capped before reading
#   * no transcript text or audio is written to logs
# Phase 2 (phone access) adds TLS + pairing on top of this; see docs/santa/PHASE2.md.

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlparse, parse_qs, unquote

from . import __version__, APP_NAME
from .audio_input import AudioInputError
from .languages import language_modes_for_ui, hinglish_strategies_for_ui, UnknownLanguageMode
from .models import catalog_for_ui, total_ram_gb, CATALOG
from .service import TranscriptionService, QueueFull

logger = logging.getLogger(__name__)

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
MAX_JSON_BYTES = 64 * 1024
_STATIC_FILES = {
    'app.js': 'application/javascript; charset=utf-8',
    'style.css': 'text/css; charset=utf-8',
    'recorder-worklet.js': 'application/javascript; charset=utf-8',
    'santa.svg': 'image/svg+xml',
}
_LOOPBACK_NAMES = {'127.0.0.1', 'localhost', '[::1]', '::1'}
_MODEL_SETTINGS = {'model', 'compute_type', 'cpu_threads', 'engine'}

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'")


def _web_file(name: str) -> bytes:
    return resources.files('whisper_key.santa').joinpath('web', name).read_bytes()


class SantaServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: TranscriptionService):
        self.service = service
        super().__init__(address, SantaHandler)


class SantaHandler(BaseHTTPRequestHandler):
    server_version = 'Santa/' + __version__
    sys_version = ''

    def log_message(self, fmt, *args):   # paths only; bodies are never logged
        logger.debug('%s %s', self.command, self.path.split('?')[0])

    # ── Guards ───────────────────────────────────────────────────────────
    def _host_ok(self) -> bool:
        host = (self.headers.get('Host') or '').strip()
        name = host.rsplit(':', 1)[0] if not host.startswith('[') else host.split(']')[0] + ']'
        return name in _LOOPBACK_NAMES

    def _write_ok(self) -> bool:
        if self.headers.get('X-Santa') != '1':
            return False
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or \
                    parsed.port != self.server.server_address[1]:
                return False
        return True

    # ── Dispatch ─────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._host_ok():
            return self._error(421, 'bad_host', 'Santa only answers on localhost.')
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            return self._send(200, _web_file('index.html'), 'text/html; charset=utf-8')
        if path.startswith('/static/'):
            name = path[len('/static/'):]
            if name in _STATIC_FILES:
                return self._send(200, _web_file(name), _STATIC_FILES[name])
            return self._error(404, 'not_found', 'Not found')
        if path == '/api/status':
            return self._json(200, self._status())
        if path == '/api/settings':
            return self._json(200, {'settings': self.server.service.settings.get()})
        if path == '/api/config':
            return self._json(200, self._config())
        if path.startswith('/api/jobs/'):
            job = self.server.service.get_job(path.split('/')[3])
            if not job:
                return self._error(404, 'no_job', 'Unknown or expired job.')
            return self._json(200, job.public())
        if path == '/api/history':
            return self._json(200, {'enabled': self.server.service.settings.get()['history_enabled'],
                                    'entries': self.server.service.history.list()})
        return self._error(404, 'not_found', 'Not found')

    def do_POST(self):
        if not self._host_ok():
            return self._error(421, 'bad_host', 'Santa only answers on localhost.')
        if not self._write_ok():
            return self._error(403, 'forbidden', 'Request rejected (cross-site or missing header).')
        url = urlparse(self.path)
        path = url.path
        svc = self.server.service
        if path == '/api/transcribe':
            return self._transcribe(parse_qs(url.query))
        if path.startswith('/api/jobs/') and path.endswith('/cancel'):
            ok = svc.cancel(path.split('/')[3])
            return self._json(200, {'cancelled': ok})
        if path == '/api/models/load':
            body = self._read_json()
            if body is None:
                return
            key = body.get('model')
            if key not in CATALOG:
                return self._error(400, 'bad_model', 'Unknown model.')
            svc.settings.update({'model': key})
            svc.preload()
            return self._json(202, {'model': svc.models.get_status()})
        if path == '/api/shutdown':
            self._json(200, {'stopping': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._error(404, 'not_found', 'Not found')

    def do_PUT(self):
        if not self._host_ok():
            return self._error(421, 'bad_host', 'Santa only answers on localhost.')
        if not self._write_ok():
            return self._error(403, 'forbidden', 'Request rejected (cross-site or missing header).')
        if urlparse(self.path).path != '/api/settings':
            return self._error(404, 'not_found', 'Not found')
        body = self._read_json()
        if body is None:
            return
        svc = self.server.service
        before = svc.settings.get()
        after = svc.settings.update(body)
        if any(before[k] != after[k] for k in _MODEL_SETTINGS):
            svc.preload()
        return self._json(200, {'settings': after})

    def do_DELETE(self):
        if not self._host_ok():
            return self._error(421, 'bad_host', 'Santa only answers on localhost.')
        if not self._write_ok():
            return self._error(403, 'forbidden', 'Request rejected (cross-site or missing header).')
        path = urlparse(self.path).path
        hist = self.server.service.history
        if path == '/api/history':
            hist.clear()
            return self._json(200, {'cleared': True})
        if path.startswith('/api/history/'):
            return self._json(200, {'deleted': hist.delete(path.split('/')[3])})
        return self._error(404, 'not_found', 'Not found')

    # ── Handlers ─────────────────────────────────────────────────────────
    def _transcribe(self, query: dict):
        svc = self.server.service
        limit = svc.settings.get()['max_upload_mb'] * 1_000_000
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if length <= 0:
            return self._error(400, 'empty_audio', 'No audio received.')
        if length > limit:
            return self._error(413, 'audio_too_large',
                               f'File is {length / 1e6:.0f} MB; the limit is {limit / 1e6:.0f} MB.')
        data = self._read_exact(length)
        if data is None:
            return self._error(400, 'upload_interrupted', 'The upload was interrupted. Please try again.')
        language = (query.get('language') or ['auto'])[0]
        filename = unquote((query.get('filename') or ['recording.wav'])[0])[:200]
        source = (query.get('source') or ['upload'])[0]
        source = source if source in ('recording', 'upload') else 'upload'
        try:
            job = svc.submit(data, filename, language, source=source)
        except AudioInputError as exc:
            return self._error(400, exc.code, exc.message)
        except UnknownLanguageMode as exc:
            return self._error(400, 'bad_language', str(exc))
        except QueueFull as exc:
            return self._error(429, exc.code, exc.message)
        return self._json(202, job.public())

    def _status(self) -> dict:
        st = self.server.service.status()
        st.update({'app': APP_NAME, 'version': __version__, 'time': time.time()})
        return st

    def _config(self) -> dict:
        svc = self.server.service
        return {
            'app': APP_NAME, 'version': __version__,
            'languages': language_modes_for_ui(),
            'hinglish_strategies': hinglish_strategies_for_ui(),
            'models': catalog_for_ui(),
            'ram_gb': round(total_ram_gb(), 1),
            'settings': svc.settings.get(),
            'model_status': svc.models.get_status(),
            'accelerator_available': bool(svc.accel and svc.accel.available()),
        }

    # ── I/O helpers ──────────────────────────────────────────────────────
    def _read_exact(self, length: int):
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_JSON_BYTES:
            self._error(413, 'too_large', 'Request too large.')
            return None
        raw = self._read_exact(length) if length else b'{}'
        try:
            body = json.loads(raw.decode('utf-8'))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, UnicodeDecodeError, AttributeError):
            self._error(400, 'bad_json', 'Invalid JSON body.')
            return None

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', _CSP)
        self.send_header('Permissions-Policy', 'microphone=(self)')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def _error(self, status: int, code: str, message: str):
        self._json(status, {'error': {'code': code, 'message': message}})


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                service: TranscriptionService = None) -> SantaServer:
    return SantaServer((host, port), service or TranscriptionService())
