# santa/server.py
# HTTP server for Santa: serves the browser UI and a small JSON API over the
# TranscriptionService. Standard library only (like local_server.py). The same
# handler runs in two modes:
#
#   Mac mode (127.0.0.1:8765, the Santa window)
#     * loopback only; Host header must be a loopback name (blocks DNS rebinding)
#     * state-changing requests need the X-Santa header and a same-origin Origin
#   Phone mode (HTTPS on the home Wi-Fi, see phone_access.py) — adds:
#     * TLS with a name-constrained local certificate; private-network clients only
#     * every API call needs a paired device token (cookie, or Bearer for Shortcuts)
#     * Mac-only powers are refused: reading file paths, folders & exports,
#       models, quitting, pairing more devices
#
# In both modes bodies are size-capped before reading and no transcript text or
# audio is ever written to logs.

import json
import logging
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlparse, parse_qs, unquote

from . import __version__, APP_NAME, phone
from .audio_input import AudioInputError, check_upload, new_temp_path, remove_temp, decode_path
from .languages import language_modes_for_ui, hinglish_strategies_for_ui, UnknownLanguageMode
from .models import catalog_for_ui, total_ram_gb, CATALOG
from .service import TranscriptionService, QueueFull
from .translate import OUTPUT_LANGUAGES

logger = logging.getLogger(__name__)

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
MAX_JSON_BYTES = 64 * 1024
MAX_WAIT_SECONDS = 300               # ?wait= for Shortcuts (answer in one request)
_STATIC_FILES = {
    'app.js': 'application/javascript; charset=utf-8',
    'style.css': 'text/css; charset=utf-8',
    'recorder-worklet.js': 'application/javascript; charset=utf-8',
    'santa.svg': 'image/svg+xml',
    'icon-180.png': 'image/png',
    'icon-512.png': 'image/png',
    'manifest.webmanifest': 'application/manifest+json',
}
_LOOPBACK_NAMES = {'127.0.0.1', 'localhost', '[::1]', '::1'}
_MODEL_SETTINGS = {'model', 'compute_type', 'cpu_threads', 'engine'}
# What a paired phone may change (nothing that touches the Mac's files or models).
_PHONE_SETTINGS = {'language', 'hinglish_output', 'output_language', 'user_prompt',
                   'diarize_files', 'num_speakers'}
_PHONE_SAFE_SETTINGS = _PHONE_SETTINGS | {'model', 'engine', 'history_enabled', 'cleanup'}

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'; manifest-src 'self'")


def _web_file(name: str) -> bytes:
    return resources.files('whisper_key.santa').joinpath('web', name).read_bytes()


class SantaServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: TranscriptionService, phone_access=None):
        self.service = service
        self.phone_access = phone_access          # None = Mac (loopback) mode
        super().__init__(address, SantaHandler)

    def handle_error(self, request, client_address):
        # Phones that drop the connection or reject the certificate are routine.
        logger.debug('connection from %s ended with an error', client_address[0])


def _flag(query: dict, name: str):
    value = (query.get(name) or [None])[0]
    return None if value is None else value in ('1', 'true', 'yes', 'on')


def _job_options(values: dict) -> dict:
    """Per-request overrides from query/JSON: output_language, diarize, num_speakers."""
    opts = {}
    out = values.get('output_language')
    if out in OUTPUT_LANGUAGES:
        opts['output_language'] = out
    if isinstance(values.get('diarize'), bool):
        opts['diarize'] = values['diarize']
    try:
        n = int(values.get('num_speakers')) if values.get('num_speakers') not in (None, '') else None
        if n is not None and 0 <= n <= 10:
            opts['num_speakers'] = n
    except (TypeError, ValueError):
        pass
    return opts


class SantaHandler(BaseHTTPRequestHandler):
    server_version = 'Santa/' + __version__
    sys_version = ''
    timeout = 120                      # a stalled phone connection can't hold a thread forever

    def setup(self):
        from .phone_access import handshake
        if self.server.phone_access is not None and not handshake(self.request):
            self.request.close()
            raise ConnectionAbortedError('TLS handshake failed')
        super().setup()

    def log_message(self, fmt, *args):   # paths only; bodies are never logged
        logger.debug('%s %s', self.command, self.path.split('?')[0])

    # ── Mode & guards ────────────────────────────────────────────────────
    @property
    def is_phone(self) -> bool:
        return self.server.phone_access is not None

    def _host_ok(self) -> bool:
        host = (self.headers.get('Host') or '').strip()
        name = host.rsplit(':', 1)[0] if not host.startswith('[') else host.split(']')[0] + ']'
        if self.is_phone:
            return name.lower() in self.server.phone_access.allowed_hosts()
        return name in _LOOPBACK_NAMES

    def _origin_ok(self) -> bool:
        origin = self.headers.get('Origin')
        if not origin:
            return True
        parsed = urlparse(origin)
        if self.is_phone:
            scheme = 'https' if isinstance(self.request, ssl.SSLSocket) else 'http'
            return (parsed.scheme == scheme and parsed.hostname is not None
                    and parsed.hostname.lower() in self.server.phone_access.allowed_hosts()
                    and parsed.port == self.server.server_address[1])
        return parsed.hostname in {'127.0.0.1', 'localhost', '::1'} and \
            parsed.port == self.server.server_address[1]

    def _bearer(self) -> bool:
        return (self.headers.get('Authorization') or '').lower().startswith('bearer ')

    def _write_ok(self) -> bool:
        # Bearer tokens (iOS Shortcuts) are never sent automatically by browsers,
        # so they need no cross-site protection; cookie/loopback requests do.
        if self.is_phone and self._bearer():
            return True
        return self.headers.get('X-Santa') == '1' and self._origin_ok()

    def _gate(self, write: bool) -> bool:
        """Common checks for every request; sends the error itself on failure."""
        if self.is_phone:
            from .phone_access import is_private_client
            if not is_private_client(self.client_address[0]):
                self._error(403, 'forbidden', 'Santa only answers on your home network.')
                return False
        if not self._host_ok():
            self._error(421, 'bad_host', 'Santa only answers on this computer or your paired phone.')
            return False
        if write and not self._write_ok():
            self._error(403, 'forbidden', 'Request rejected (cross-site or missing header).')
            return False
        return True

    def _device(self):
        """Phone mode: the paired device making this request, else a 401/429 is sent."""
        access = self.server.phone_access
        device = access.devices.verify(phone.token_from_request(self.headers))
        if device is None:
            self._error(401, 'not_paired', 'This phone is not paired with Santa yet.')
            return None
        if not access.api_limiter.allow(device['id']):
            self._error(429, 'slow_down', 'Too many requests; wait a moment.',
                        {'Retry-After': str(access.api_limiter.retry_after(device['id']))})
            return None
        return device

    def _mac_only(self) -> bool:
        if self.is_phone:
            self._error(403, 'mac_only', 'This can only be done in the Santa window on the Mac.')
            return True
        return False

    # ── Dispatch ─────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._gate(write=False):
            return
        path = urlparse(self.path).path
        # The page and static files are public in both modes (the phone page
        # itself shows the pairing screen when it isn't paired yet).
        if path in ('/', '/index.html'):
            return self._send(200, _web_file('index.html'), 'text/html; charset=utf-8')
        if path == '/manifest.webmanifest':
            path = '/static/manifest.webmanifest'
        if path.startswith('/static/'):
            name = path[len('/static/'):]
            if name in _STATIC_FILES:
                return self._send(200, _web_file(name), _STATIC_FILES[name], cache=name.endswith('.png'))
            return self._error(404, 'not_found', 'Not found')
        if self.is_phone and self._device() is None:
            return
        svc = self.server.service
        if path == '/api/status':
            return self._json(200, self._status())
        if path == '/api/settings':
            return self._json(200, {'settings': svc.settings.get()})
        if path == '/api/config':
            return self._json(200, self._config())
        if path.startswith('/api/jobs/'):
            job = svc.get_job(path.split('/')[3])
            if not job:
                return self._error(404, 'no_job', 'Unknown or expired job.')
            return self._json(200, job.public())
        if path == '/api/history':
            return self._json(200, {'enabled': svc.settings.get()['history_enabled'],
                                    'entries': svc.history.list()})
        if path == '/api/voices':
            return self._json(200, {'voices': svc.voices_list(), 'speakers': svc.speakers.status})
        if path == '/api/phone':
            if self._mac_only():
                return
            return self._json(200, self._phone_status())
        if path == '/phone/santa.mobileconfig':
            if self._mac_only():
                return
            access = self._phone_access_or_error()
            if access is None:
                return
            access.certs.ensure_ca()
            return self._send(200, access.certs.mobileconfig(), phone.MOBILECONFIG_CONTENT_TYPE)
        return self._error(404, 'not_found', 'Not found')

    def do_POST(self):
        if not self._gate(write=True):
            return
        url = urlparse(self.path)
        path = url.path
        svc = self.server.service
        if path == '/api/pair':
            return self._pair() if self.is_phone else self._error(404, 'not_found', 'Not found')
        device = None
        if self.is_phone:
            device = self._device()
            if device is None:
                return
        if path == '/api/transcribe':
            return self._transcribe(parse_qs(url.query), device)
        if path == '/api/transcribe-source':
            return self._transcribe_source()
        if path.startswith('/api/jobs/') and path.endswith('/cancel'):
            ok = svc.cancel(path.split('/')[3])
            return self._json(200, {'cancelled': ok})
        if path.startswith('/api/jobs/') and path.endswith('/name-speaker'):
            body = self._read_json()
            if body is None:
                return
            try:
                entry = svc.enroll_from_job(path.split('/')[3], str(body.get('speaker_id', '')),
                                            str(body.get('name', '')))
            except ValueError as exc:
                return self._error(400, 'bad_request', str(exc))
            return self._json(200, {'voice': entry, 'voices': svc.voices_list()})
        if path == '/api/voices':
            return self._enroll_voice(parse_qs(url.query))
        if path == '/api/models/load':
            if self._mac_only():
                return
            body = self._read_json()
            if body is None:
                return
            key = body.get('model')
            if key not in CATALOG:
                return self._error(400, 'bad_model', 'Unknown model.')
            svc.settings.update({'model': key})
            svc.preload()
            return self._json(202, {'model': svc.models.get_status()})
        if path.startswith('/api/phone/'):
            return self._phone_action(path)
        if path == '/api/shutdown':
            if self._mac_only():
                return
            self._json(200, {'stopping': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._error(404, 'not_found', 'Not found')

    def do_PUT(self):
        if not self._gate(write=True):
            return
        if self.is_phone and self._device() is None:
            return
        if urlparse(self.path).path != '/api/settings':
            return self._error(404, 'not_found', 'Not found')
        body = self._read_json()
        if body is None:
            return
        if self.is_phone:
            body = {k: v for k, v in body.items() if k in _PHONE_SETTINGS}
        svc = self.server.service
        before = svc.settings.get()
        after = svc.settings.update(body)
        if any(before[k] != after[k] for k in _MODEL_SETTINGS):
            svc.preload()
        if before['phone_enabled'] != after['phone_enabled'] or before['phone_port'] != after['phone_port']:
            access = getattr(svc, 'phone', None)
            if access is not None:
                access.apply_settings(after)
        return self._json(200, {'settings': after})

    def do_DELETE(self):
        if not self._gate(write=True):
            return
        if self.is_phone and self._device() is None:
            return
        path = urlparse(self.path).path
        svc = self.server.service
        if path == '/api/history':
            svc.history.clear()
            return self._json(200, {'cleared': True})
        if path.startswith('/api/history/'):
            return self._json(200, {'deleted': svc.history.delete(path.split('/')[3])})
        if path.startswith('/api/voices/'):
            return self._json(200, {'deleted': svc.voices.delete(unquote(path.split('/', 3)[3])),
                                    'voices': svc.voices_list()})
        if path.startswith('/api/phone/devices/'):
            if self._mac_only():
                return
            access = self._phone_access_or_error()
            if access is None:
                return
            return self._json(200, {'revoked': access.devices.revoke(path.split('/')[4]),
                                    'phone': self._phone_status()})
        return self._error(404, 'not_found', 'Not found')

    # ── Transcription ────────────────────────────────────────────────────
    def _transcribe(self, query: dict, device=None):
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
        language = (query.get('language') or [svc.settings.get()['language']])[0]
        filename = unquote((query.get('filename') or ['recording.wav'])[0])[:200]
        source = (query.get('source') or ['upload'])[0]
        source = source if source in ('recording', 'upload') else 'upload'
        options = _job_options({'output_language': (query.get('output_language') or [None])[0],
                                'diarize': _flag(query, 'diarize'),
                                'num_speakers': (query.get('speakers') or [None])[0]})
        try:
            ext = check_upload(filename, length, limit)     # reject bad types before reading
        except AudioInputError as exc:
            return self._error(400, exc.code, exc.message)
        # Stream the body to a private temp file: long interview videos can be
        # gigabytes and must not be held in memory.
        path = new_temp_path(ext)
        if not self._read_to_file(length, path):
            remove_temp(path)
            return self._error(400, 'upload_interrupted', 'The upload was interrupted. Please try again.')
        try:
            job = svc.submit_file(path, filename, language, source=source, delete_after=True,
                                  options=options)
        except AudioInputError as exc:
            remove_temp(path)
            return self._error(400, exc.code, exc.message)
        except UnknownLanguageMode as exc:
            remove_temp(path)
            return self._error(400, 'bad_language', str(exc))
        except QueueFull as exc:
            remove_temp(path)
            return self._error(429, exc.code, exc.message)
        wait = (query.get('wait') or [None])[0]
        if wait:
            return self._answer_when_done(job, wait, (query.get('format') or ['json'])[0])
        return self._json(202, job.public())

    def _answer_when_done(self, job, wait: str, fmt: str):
        """One-request mode for iOS Shortcuts: block until the job finishes."""
        try:
            deadline = time.time() + min(MAX_WAIT_SECONDS, max(1, int(wait)))
        except ValueError:
            deadline = time.time() + 60
        while job.state not in ('done', 'error', 'cancelled') and time.time() < deadline:
            time.sleep(0.1)
        if fmt == 'text':
            if job.state == 'done':
                return self._send(200, (job.result['text'] or '').encode('utf-8'), 'text/plain; charset=utf-8')
            message = (job.error or {}).get('message') or 'Santa is still working on it; try again.'
            return self._send(200 if job.state != 'error' else 422, ('⚠ ' + message).encode('utf-8'),
                              'text/plain; charset=utf-8')
        return self._json(200 if job.state == 'done' else 202, job.public())

    def _transcribe_source(self):
        body = self._read_json()
        if body is None:
            return
        svc = self.server.service
        source = str(body.get('source') or '').strip()
        if not source:
            return self._error(400, 'empty_source', 'Paste a file path or a link first.')
        from .links import classify
        if self.is_phone and classify(source) == 'path':
            return self._error(403, 'mac_only', 'File paths only work in the Santa window on the Mac.')
        language = body.get('language') or svc.settings.get()['language']
        try:
            job = svc.submit_source(source, language, options=_job_options(body))
        except AudioInputError as exc:
            return self._error(400, exc.code, exc.message)
        except UnknownLanguageMode as exc:
            return self._error(400, 'bad_language', str(exc))
        except QueueFull as exc:
            return self._error(429, exc.code, exc.message)
        return self._json(202, job.public())

    def _enroll_voice(self, query: dict):
        """Body = a short recording of one person; ?name= their name."""
        svc = self.server.service
        name = unquote((query.get('name') or [''])[0]).strip()
        if not name:
            return self._error(400, 'bad_name', 'Give the voice a name.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if length <= 0 or length > 20_000_000:
            return self._error(400, 'bad_audio', 'Send a recording of up to about 2 minutes.')
        filename = unquote((query.get('filename') or ['voice.wav'])[0])[:200]
        try:
            ext = check_upload(filename, length, 20_000_000)
        except AudioInputError as exc:
            return self._error(400, exc.code, exc.message)
        path = new_temp_path(ext)
        try:
            if not self._read_to_file(length, path):
                return self._error(400, 'upload_interrupted', 'The upload was interrupted.')
            decoded = decode_path(path, 180)
        except AudioInputError as exc:
            return self._error(400, exc.code, exc.message)
        finally:
            remove_temp(path)
        if not svc.speakers.available():
            return self._error(503, 'not_installed', 'Speaker recognition is not installed; run the Santa installer again.')
        try:
            entry = svc.enroll_voice(name, decoded.samples)
        except ValueError as exc:
            return self._error(400, 'bad_voice', str(exc))
        return self._json(200, {'voice': entry, 'voices': svc.voices_list()})

    def _read_to_file(self, length: int, path: str) -> bool:
        remaining = length
        with open(path, 'wb') as fh:
            os.chmod(path, 0o600)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    return False
                fh.write(chunk)
                remaining -= len(chunk)
        return True

    # ── Phone pairing (phone side) ───────────────────────────────────────
    def _pair(self):
        access = self.server.phone_access
        ip = self.client_address[0]
        if not access.pair_limiter.allow(ip):
            return self._error(429, 'slow_down', 'Too many pairing attempts; wait a minute.',
                               {'Retry-After': str(access.pair_limiter.retry_after(ip))})
        body = self._read_json()
        if body is None:
            return
        name = str(body.get('device_name') or 'iPhone')
        try:
            paired = access.devices.redeem(str(body.get('code') or ''), name, kind='browser')
        except phone.PairingError as exc:
            status = {'expired': 410, 'locked': 423}.get(exc.code, 403)
            return self._error(status, exc.code, exc.message)
        logger.info('phone paired: %s', paired['device_id'])
        return self._json(200, {'paired': True, 'device_id': paired['device_id']},
                          {'Set-Cookie': phone.cookie_header(paired['token'])})

    # ── Phone access management (Mac side) ───────────────────────────────
    def _phone_access_or_error(self):
        access = getattr(self.server.service, 'phone', None)
        if access is None:
            self._error(503, 'phone_unavailable', 'Phone access is not available in this Santa.')
        return access

    def _phone_status(self) -> dict:
        access = getattr(self.server.service, 'phone', None)
        if access is None:
            return {'available': False}
        return dict(access.status(), available=True,
                    enabled=self.server.service.settings.get()['phone_enabled'])

    def _phone_action(self, path: str):
        if self._mac_only():
            return
        access = self._phone_access_or_error()
        if access is None:
            return
        if path == '/api/phone/pairing':
            if not access.running:
                return self._error(409, 'phone_off', access.error or 'Switch phone access on first.')
            return self._json(200, dict(access.start_pairing(), phone=self._phone_status()))
        if path == '/api/phone/setup':
            try:
                access.open_setup()
            except OSError as exc:
                return self._error(500, 'setup_failed', f'Could not open the setup link: {exc}')
            return self._json(200, {'phone': self._phone_status()})
        if path == '/api/phone/airdrop':
            saved = access.save_profile()
            if sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', saved])        # show it in Finder, ready to AirDrop
            return self._json(200, {'path': saved})
        if path == '/api/phone/shortcut-token':
            body = self._read_json()
            if body is None:
                return
            if not access.running:
                return self._error(409, 'phone_off', 'Switch phone access on first.')
            minted = access.devices.create_token(str(body.get('name') or 'iPhone Shortcut'), kind='shortcut')
            return self._json(200, {'token': minted['token'], 'url': access.url(),
                                    'phone': self._phone_status()})
        return self._error(404, 'not_found', 'Not found')

    # ── Status & config ──────────────────────────────────────────────────
    def _status(self) -> dict:
        svc = self.server.service
        st = svc.status()
        watcher = getattr(svc, 'watcher', None)
        st['watch'] = watcher.status if watcher else None
        st['speakers'] = svc.speakers.status
        st['translation'] = svc.translator.status
        access = getattr(svc, 'phone', None)
        st['phone'] = {'running': access.running, 'devices': len(access.devices.list())} \
            if access is not None and not self.is_phone else None
        st.update({'app': APP_NAME, 'version': __version__, 'time': time.time()})
        return st

    def _config(self) -> dict:
        svc = self.server.service
        settings = svc.settings.get()
        if self.is_phone:              # the phone doesn't need to see Mac folders
            settings = {k: v for k, v in settings.items() if k in _PHONE_SAFE_SETTINGS}
        return {
            'app': APP_NAME, 'version': __version__,
            'client': 'phone' if self.is_phone else 'mac',
            'languages': language_modes_for_ui(),
            'output_languages': [{'key': k, 'label': v} for k, v in OUTPUT_LANGUAGES.items()],
            'hinglish_strategies': hinglish_strategies_for_ui(),
            'models': catalog_for_ui(),
            'ram_gb': round(total_ram_gb(), 1),
            'settings': settings,
            'model_status': svc.models.get_status(),
            'accelerator_available': bool(svc.accel and svc.accel.available()),
            'speakers_available': svc.speakers.available(),
            'phone_available': getattr(svc, 'phone', None) is not None,
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

    def _send(self, status: int, body: bytes, ctype: str, extra_headers: dict = None, cache: bool = False):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', _CSP)
        self.send_header('Permissions-Policy', 'microphone=(self)')
        if self.is_phone:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra_headers: dict = None):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8', extra_headers)

    def _error(self, status: int, code: str, message: str, extra_headers: dict = None):
        self._json(status, {'error': {'code': code, 'message': message}}, extra_headers)


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                service: TranscriptionService = None) -> SantaServer:
    return SantaServer((host, port), service or TranscriptionService())
