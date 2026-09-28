# santa/phone_access.py
# Runs "phone mode": a second Santa listener, HTTPS only, for your iPhone on the
# home Wi-Fi. It shares the TranscriptionService with the Mac window, answers only
# private-network clients, and every request needs a paired device token (the
# security pieces live in phone.py). While it runs, the Mac is kept from idle
# sleep so the phone can reach it. A tiny plain-HTTP "setup" listener can be
# opened for 10 minutes to hand the certificate profile to the phone.

import ipaddress
import logging
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import phone

logger = logging.getLogger(__name__)

ADDRESS_CHECK_SECONDS = 60           # notice a new Wi-Fi address and re-issue the certificate
SETUP_WINDOW_SECONDS = 600           # the plain-HTTP profile download closes after this


def is_private_client(address: str) -> bool:
    """Home-network clients only (and this Mac itself, for tests)."""
    try:
        ip = ipaddress.ip_address(address.split('%')[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in net for net in phone.PRIVATE_NETWORKS)


class PhoneAccess:
    """Owns the HTTPS listener, certificates, paired devices and rate limits."""

    def __init__(self, service, home: str, port: int = phone.DEFAULT_PHONE_PORT):
        self.service = service
        self.home = home
        self.port = port
        self.certs = phone.PhoneCertificates(home)
        self.devices = phone.DeviceStore(home)
        self.pair_limiter = phone.RateLimiter(10, 60)
        self.api_limiter = phone.RateLimiter(240, 60)
        self.addresses = {'hostname': None, 'ips': []}
        self._httpd = None
        self._ctx = None
        self._caffeinate = None
        self._setup = None                # (server, closes_at) for the profile download
        self._stop = threading.Event()
        self.error = None

    # ── Start / stop ─────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self.running:
            return True
        self.error = None
        try:
            self.addresses = phone.local_addresses()
            self.certs.ensure_server_cert(self.addresses['hostname'], self.addresses['ips'])
            self._ctx = self.certs.ssl_context()
            from .server import SantaServer
            httpd = SantaServer(('0.0.0.0', self.port), self.service, phone_access=self)
            # Handshakes happen in each request thread (see SantaHandler.setup),
            # so one slow phone can't block the accept loop.
            httpd.socket = self._ctx.wrap_socket(httpd.socket, server_side=True,
                                                 do_handshake_on_connect=False)
        except ImportError:
            self.error = 'Phone access needs the "cryptography" package — run the Santa installer again.'
            return False
        except (OSError, ValueError) as exc:
            self.error = f'Could not start phone access: {exc}'
            logger.warning(self.error)
            return False
        self._httpd = httpd
        self._stop.clear()
        threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.5},
                         daemon=True, name='santa-phone').start()
        threading.Thread(target=self._watch_addresses, daemon=True, name='santa-phone-addr').start()
        self._keep_awake(True)
        logger.info('phone access on at %s', self.url())
        return True

    def stop(self) -> None:
        self._stop.set()
        self.close_setup()
        httpd, self._httpd = self._httpd, None
        if httpd:
            threading.Thread(target=lambda: (httpd.shutdown(), httpd.server_close()), daemon=True).start()
        self._keep_awake(False)

    def apply_settings(self, s: dict) -> None:
        """Start/stop to match Settings (called at startup and after changes)."""
        if s['phone_enabled'] and s['phone_port'] != self.port and self.running:
            self.stop()
        self.port = s['phone_port']
        if s['phone_enabled']:
            self.start()
        elif self.running:
            self.stop()

    # ── Addresses & certificate renewal ──────────────────────────────────
    def _watch_addresses(self) -> None:
        while not self._stop.wait(ADDRESS_CHECK_SECONDS):
            try:
                fresh = phone.local_addresses()
                if fresh != self.addresses and (fresh['hostname'] or fresh['ips']):
                    self.addresses = fresh
                    self.certs.ensure_server_cert(fresh['hostname'], fresh['ips'])
                    self._ctx.load_cert_chain(self.certs.cert_path, self.certs.key_path)
                    logger.info('phone access: network changed, certificate refreshed')
            except Exception as exc:          # keep serving with the old certificate
                logger.warning('phone access address check failed: %s', exc)

    def allowed_hosts(self) -> set:
        names = set(self.addresses['ips'])
        if self.addresses['hostname']:
            names.add(self.addresses['hostname'].lower())
        return names

    def url(self) -> str:
        host = self.addresses['hostname'] or (self.addresses['ips'] or ['?'])[0]
        return f'https://{host}:{self.port}/'

    # ── Keeping the Mac reachable ────────────────────────────────────────
    def _keep_awake(self, on: bool) -> None:
        """caffeinate -i: no idle sleep while phone access is on (display may sleep)."""
        if sys.platform != 'darwin':
            return
        if on and self._caffeinate is None:
            try:
                self._caffeinate = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])
            except OSError:
                self._caffeinate = None
        elif not on and self._caffeinate is not None:
            self._caffeinate.terminate()
            self._caffeinate = None

    # ── Pairing (driven from the Mac window) ─────────────────────────────
    def start_pairing(self) -> dict:
        pairing = self.devices.start_pairing()
        link = f"{self.url()}#pair={pairing['link_token']}"
        return {'code': pairing['code'], 'expires': pairing['expires'], 'link': link,
                'qr_svg': phone.qr_svg(link)}

    def status(self) -> dict:
        setup_url = self.setup_url()
        return {
            'running': self.running, 'error': self.error, 'port': self.port,
            'url': self.url() if self.running else None,
            'hostname': self.addresses['hostname'], 'ips': self.addresses['ips'],
            'fingerprint': self._fingerprint(),
            'devices': self.devices.list(),
            'setup_url': setup_url,
            'setup_qr_svg': phone.qr_svg(setup_url) if setup_url else None,
        }

    def _fingerprint(self):
        try:
            return self.certs.ca_fingerprint() if os.path.exists(self.certs.ca_cert_path) else None
        except Exception:
            return None

    # ── Certificate profile hand-over ────────────────────────────────────
    def save_profile(self) -> str:
        """Write the .mobileconfig to ~/Downloads (for AirDrop) and return its path."""
        self.certs.ensure_ca()
        folder = os.path.expanduser('~/Downloads')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, 'Santa iPhone certificate.mobileconfig')
        with open(path, 'wb') as fh:
            fh.write(self.certs.mobileconfig())
        return path

    def open_setup(self) -> str:
        """Serve only the certificate profile over plain HTTP for 10 minutes.
        It holds a public certificate (no secret), so plain HTTP is acceptable;
        the Mac shows the fingerprint so it can be compared on the phone."""
        self.close_setup()
        self.certs.ensure_ca()
        profile = self.certs.mobileconfig()
        httpd = ThreadingHTTPServer(('0.0.0.0', self.port + 1), _profile_handler(profile))
        httpd.daemon_threads = True
        closes_at = time.time() + SETUP_WINDOW_SECONDS
        self._setup = (httpd, closes_at)
        threading.Thread(target=httpd.serve_forever, daemon=True, name='santa-phone-setup').start()
        threading.Timer(SETUP_WINDOW_SECONDS, self.close_setup).start()
        return self.setup_url()

    def setup_url(self):
        if not self._setup or time.time() > self._setup[1]:
            return None
        host = self.addresses['hostname'] or (self.addresses['ips'] or ['?'])[0]
        return f'http://{host}:{self.port + 1}/santa.mobileconfig'

    def close_setup(self) -> None:
        setup, self._setup = self._setup, None
        if setup:
            threading.Thread(target=lambda: (setup[0].shutdown(), setup[0].server_close()), daemon=True).start()


def _profile_handler(profile: bytes):
    class ProfileHandler(BaseHTTPRequestHandler):
        server_version = 'Santa-setup'
        sys_version = ''

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if not is_private_client(self.client_address[0]):
                self.send_error(403)
                return
            if self.path.split('?')[0] != '/santa.mobileconfig':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', phone.MOBILECONFIG_CONTENT_TYPE)
            self.send_header('Content-Disposition', 'attachment; filename="Santa.mobileconfig"')
            self.send_header('Content-Length', str(len(profile)))
            self.end_headers()
            self.wfile.write(profile)
    return ProfileHandler


def handshake(request) -> bool:
    """Finish the TLS handshake in the request thread; False if the phone
    dropped out or doesn't trust the certificate yet."""
    if not isinstance(request, ssl.SSLSocket):
        return True
    try:
        request.settimeout(20)
        request.do_handshake()
        return True
    except (ssl.SSLError, OSError):
        return False
