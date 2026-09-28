# santa/phone.py
# Security core for "phone mode": a name-constrained local CA + server certificate
# (so iPhone Safari gets the secure context it needs for the microphone), a paired
# device store holding only token hashes, a tiny rate limiter, a QR helper and the
# cookie/Bearer token plumbing. No HTTP server code lives here; server.py wires it in.

import hashlib
import hmac
import ipaddress
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie

from .settings import santa_home

# ── Constants ────────────────────────────────────────────────────────────────

PHONE_DIRNAME = 'phone'
DEFAULT_PHONE_PORT = 8766

# The only address space phone mode will ever serve on, and the only space the CA
# may vouch for: RFC 1918 private ranges plus 100.64/10 (CGNAT, used by Tailscale).
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10'))
# DNS subtree the CA may vouch for: "local" permits "local" and every "*.local"
# (Bonjour / mDNS names such as "Shouviks-MacBook.local").
PERMITTED_DNS_SUBTREE = 'local'

CA_VALIDITY_DAYS = 3650
LEAF_VALIDITY_DAYS = 397          # Apple rejects TLS server certs valid > 398 days
LEAF_RENEW_BEFORE_DAYS = 30
CLOCK_SKEW = timedelta(hours=1)   # backdate notBefore so a slightly-off phone clock is fine

PAIRING_TTL_SECONDS = 300
PAIRING_MAX_WRONG_ATTEMPTS = 5
LAST_SEEN_WRITE_INTERVAL = 60     # seconds; limits disk writes from verify()
DEVICE_NAME_MAX = 40
DEVICE_KINDS = ('browser', 'shortcut')

COOKIE_NAME = 'santa_device'
COOKIE_MAX_AGE = 31536000         # one year
MOBILECONFIG_CONTENT_TYPE = 'application/x-apple-aspen-config'

# Anything we accept as a token must look like secrets.token_urlsafe output; this
# keeps junk (and huge headers) away from hashing and lookups.
_TOKEN_SHAPE = re.compile(r'^[A-Za-z0-9_-]{20,128}$')
_UUID_NAMESPACE = uuid.UUID('5b1c1c47-7d1e-4a55-9d0b-5a17a0c0ffee')  # fixed, Santa-specific


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_private_ip(addr) -> bool:
    """True for addresses inside the ranges phone mode (and the CA) is limited to."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and any(ip in net for net in PRIVATE_NETWORKS)


def _ensure_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)   # makedirs honours umask and leaves existing dirs alone


def _write_private_file(path: str, data: bytes) -> None:
    """Atomically write `data` to `path` with mode 0600 from the first byte on."""
    tmp = f'{path}.tmp-{os.getpid()}-{threading.get_ident()}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Local network addresses ──────────────────────────────────────────────────

def _run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3,
                              check=False).stdout or ''
    except (OSError, subprocess.SubprocessError):
        return ''


def _local_hostname() -> str | None:
    # macOS: LocalHostName is exactly the Bonjour name the iPhone resolves (+ ".local").
    name = ''
    if sys.platform == 'darwin':
        name = _run(['scutil', '--get', 'LocalHostName']).strip()
    if not name:
        name = socket.gethostname().split('.')[0]
    # DNS label rules; anything else would make an invalid SAN.
    name = re.sub(r'[^A-Za-z0-9-]', '-', name).strip('-')[:63]
    return f'{name}.local' if name else None


def _ips_from_ifconfig() -> list:
    """Parse `ifconfig` (macOS/BSD); skip interfaces reporting `status: inactive`."""
    out = _run(['ifconfig'])
    ips = []
    for block in re.split(r'\n(?=\S)', out):
        if 'status: inactive' in block:
            continue
        ips += re.findall(r'^\s+inet (\d+\.\d+\.\d+\.\d+)', block, re.MULTILINE)
    return ips


def _ips_from_ip_command() -> list:
    """Linux: `ip -4 -o addr show up`."""
    return re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)/', _run(['ip', '-4', '-o', 'addr', 'show', 'up']))


def _ip_from_udp_route() -> list:
    # Connecting a UDP socket sends nothing; it only asks the kernel which source
    # address it would use for that route. One probe per private range we care about.
    found = []
    for probe in ('192.168.255.254', '10.255.255.254', '172.31.255.254', '100.100.100.100'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))
                found.append(sock.getsockname()[0])
        except OSError:
            continue
    return found


def _ips_from_getaddrinfo() -> list:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def local_addresses() -> dict:
    """The Mac's Bonjour name and private IPv4s the phone can reach it on."""
    candidates = []
    if sys.platform == 'darwin' or shutil.which('ifconfig'):
        candidates += _ips_from_ifconfig()
    if not candidates and shutil.which('ip'):
        candidates += _ips_from_ip_command()
    if not candidates:
        candidates += _ip_from_udp_route() + _ips_from_getaddrinfo()
    ips = []
    for addr in candidates:
        # _is_private_ip also rules out loopback (127/8) and link-local (169.254/16).
        if _is_private_ip(addr) and addr not in ips:
            ips.append(addr)
    return {'hostname': _local_hostname(), 'ips': ips}


# ── Certificates: local CA + server certificate ─────────────────────────────

class PhoneCertificates:
    """Owns <home>/phone/{ca,ca-key,server,server-key}.pem; creates/renews on demand."""

    def __init__(self, home: str = None):
        # Imported lazily so the rest of Santa runs without `cryptography`.
        from cryptography import x509  # noqa: F401
        self.home = home or santa_home()
        self.dir = os.path.join(self.home, PHONE_DIRNAME)
        self.ca_cert_path = os.path.join(self.dir, 'ca.pem')
        self.ca_key_path = os.path.join(self.dir, 'ca-key.pem')
        self.cert_path = os.path.join(self.dir, 'server.pem')         # leaf + CA (full chain)
        self.key_path = os.path.join(self.dir, 'server-key.pem')
        self._lock = threading.RLock()

    # ── CA ───────────────────────────────────────────────────────────────
    def ensure_ca(self):
        """Load the CA, or create it once. Returns (certificate, private_key)."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        with self._lock:
            if os.path.exists(self.ca_cert_path) and os.path.exists(self.ca_key_path):
                try:
                    with open(self.ca_cert_path, 'rb') as fh:
                        cert = x509.load_pem_x509_certificate(fh.read())
                    with open(self.ca_key_path, 'rb') as fh:
                        key = serialization.load_pem_private_key(fh.read(), password=None)
                    if cert.not_valid_after_utc > _utcnow() + timedelta(days=LEAF_VALIDITY_DAYS):
                        return cert, key
                except (ValueError, OSError):
                    pass   # corrupt or unreadable: fall through and start a new CA
            return self._create_ca()

    def _create_ca(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        _ensure_private_dir(self.dir)
        key = ec.generate_private_key(ec.SECP256R1())
        host = _local_hostname() or 'this Mac'
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f'Santa local CA ({host})'[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Santa (local)'),
        ])
        now = _utcnow()
        # Name constraints are the safety net: even if ca-key.pem leaked, a cert it
        # signs for e.g. "bank.com" or 8.8.8.8 fails validation on the phone.
        constraints = x509.NameConstraints(
            permitted_subtrees=[x509.DNSName(PERMITTED_DNS_SUBTREE)]
            + [x509.IPAddress(net) for net in PRIVATE_NETWORKS],
            excluded_subtrees=None)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SKEW)
            .not_valid_after(now + timedelta(days=CA_VALIDITY_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(constraints, critical=True)
            .sign(key, hashes.SHA256())
        )
        _write_private_file(self.ca_key_path, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        _write_private_file(self.ca_cert_path, cert.public_bytes(serialization.Encoding.PEM))
        # A new CA orphans any old leaf; drop it so ensure_server_cert re-issues.
        for path in (self.cert_path, self.key_path):
            if os.path.exists(path):
                os.unlink(path)
        return cert, key

    def ca_der(self) -> bytes:
        from cryptography.hazmat.primitives import serialization
        cert, _ = self.ensure_ca()
        return cert.public_bytes(serialization.Encoding.DER)

    def ca_fingerprint(self) -> str:
        """SHA-256 of the CA, 'AB:CD:…' — what the user compares on the phone."""
        digest = hashlib.sha256(self.ca_der()).hexdigest().upper()
        return ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2))

    # ── Server (leaf) certificate ────────────────────────────────────────
    @staticmethod
    def _wanted_sans(hostname, ips):
        # Only names the CA is allowed to vouch for; anything else would be rejected
        # by the phone anyway, so it's dropped here. Never 'localhost' (that stays
        # the plain-HTTP desktop UI).
        dns = []
        if hostname:
            h = hostname.strip().rstrip('.').lower()
            if h != 'localhost' and (h == PERMITTED_DNS_SUBTREE or h.endswith('.local')):
                dns.append(h)
        ip_list = []
        for addr in ips or ():
            if _is_private_ip(addr):
                ip = ipaddress.ip_address(addr)
                if ip not in ip_list:
                    ip_list.append(ip)
        return dns, ip_list

    def _leaf_needs_renewal(self, dns, ip_list, ca_cert) -> bool:
        """True if the leaf is missing, unreadable, near expiry, for other SANs or another CA."""
        from cryptography import x509
        if not (os.path.exists(self.cert_path) and os.path.exists(self.key_path)):
            return True
        try:
            with open(self.cert_path, 'rb') as fh:
                leaf = x509.load_pem_x509_certificates(fh.read())[0]
            san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
            ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        except (ValueError, OSError, IndexError, x509.ExtensionNotFound):
            return True
        if leaf.not_valid_after_utc - _utcnow() < timedelta(days=LEAF_RENEW_BEFORE_DAYS):
            return True
        if aki.key_identifier != ski.digest or leaf.issuer != ca_cert.subject:
            return True
        have = ({n.lower() for n in san.get_values_for_type(x509.DNSName)},
                set(san.get_values_for_type(x509.IPAddress)))
        return have != (set(dns), set(ip_list))

    def ensure_server_cert(self, hostname, ips):
        """Return (cert_path, key_path) for a leaf covering hostname + ips, (re)issuing if needed."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        dns, ip_list = self._wanted_sans(hostname, ips)
        if not dns and not ip_list:
            raise ValueError('no usable .local name or private IPv4 address for the certificate')
        with self._lock:
            ca_cert, ca_key = self.ensure_ca()
            if not self._leaf_needs_renewal(dns, ip_list, ca_cert):
                return self.cert_path, self.key_path

            key = ec.generate_private_key(ec.SECP256R1())
            now = _utcnow()
            # CN deliberately isn't a hostname (it has spaces), so OpenSSL never tries
            # to match it against the name constraints; SANs carry the real names.
            subject = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, 'Santa phone mode'),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Santa (local)'),
            ])
            ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject).issuer_name(ca_cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - CLOCK_SKEW)
                # Total span stays within Apple's 398-day cap even with the backdating.
                .not_valid_after(now - CLOCK_SKEW + timedelta(days=LEAF_VALIDITY_DAYS))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.DNSName(n) for n in dns] + [x509.IPAddress(i) for i in ip_list]),
                    critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                               critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                               critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                               critical=False)
                .sign(ca_key, hashes.SHA256())
            )
            _write_private_file(self.key_path, key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            _write_private_file(self.cert_path,
                                cert.public_bytes(serialization.Encoding.PEM)
                                + ca_cert.public_bytes(serialization.Encoding.PEM))
            return self.cert_path, self.key_path

    # ── TLS context ──────────────────────────────────────────────────────
    def ssl_context(self) -> ssl.SSLContext:
        """Server-side TLS ≥ 1.2 context from the current leaf (call ensure_server_cert first)."""
        if not (os.path.exists(self.cert_path) and os.path.exists(self.key_path)):
            raise FileNotFoundError('server certificate missing; call ensure_server_cert() first')
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_path, self.key_path)   # file holds leaf + CA
        return ctx

    # ── iOS configuration profile ────────────────────────────────────────
    def mobileconfig(self) -> bytes:
        """Unsigned .mobileconfig that installs the CA on iOS (serve as MOBILECONFIG_CONTENT_TYPE)."""
        der = self.ca_der()
        fingerprint = hashlib.sha256(der).hexdigest()
        # Stable UUIDs: re-downloading the same CA replaces, not duplicates, the profile.
        profile_uuid = str(uuid.uuid5(_UUID_NAMESPACE, 'profile:' + fingerprint)).upper()
        payload_uuid = str(uuid.uuid5(_UUID_NAMESPACE, 'root:' + fingerprint)).upper()
        ident = 'local.santa.phone.' + fingerprint[:16]
        display = 'Santa — this Mac’s local certificate'
        description = (
            'Lets this iPhone open Santa (your private dictation app) securely over your '
            'home Wi-Fi. It is a certificate created on your own Mac; it can only vouch '
            'for .local names and private network addresses, never for public websites. '
            'After installing, enable it in Settings › General › About › '
            'Certificate Trust Settings. To remove it: Settings › General › VPN & '
            'Device Management › this profile › Remove Profile. '
            f'SHA-256: {self.ca_fingerprint()}')
        profile = {
            'PayloadType': 'Configuration',
            'PayloadVersion': 1,
            'PayloadIdentifier': ident,
            'PayloadUUID': profile_uuid,
            'PayloadDisplayName': display,
            'PayloadDescription': description,
            'PayloadOrganization': 'Santa (local)',
            'PayloadRemovalDisallowed': False,
            'PayloadContent': [{
                'PayloadType': 'com.apple.security.root',
                'PayloadVersion': 1,
                'PayloadIdentifier': ident + '.root',
                'PayloadUUID': payload_uuid,
                'PayloadDisplayName': display,
                'PayloadDescription': 'Santa local certificate authority (name-constrained).',
                'PayloadOrganization': 'Santa (local)',
                'PayloadCertificateFileName': 'santa-local-ca.cer',
                'PayloadContent': der,   # plistlib writes bytes as <data>
            }],
        }
        return plistlib.dumps(profile, fmt=plistlib.FMT_XML)

    # ── Start over ───────────────────────────────────────────────────────
    def reset(self) -> None:
        """Delete the CA and server certificate/keys (paired devices are DeviceStore's job)."""
        with self._lock:
            for path in (self.cert_path, self.key_path, self.ca_cert_path, self.ca_key_path):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


# ── Paired devices ───────────────────────────────────────────────────────────

class PairingError(Exception):
    """Pairing refused. `code` is machine-readable: no_pairing | expired | invalid | locked."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _clean_device_name(name) -> str:
    text = ''.join(ch for ch in str(name or '') if ch.isprintable()).strip()
    return text[:DEVICE_NAME_MAX].strip() or 'iPhone'


class DeviceStore:
    """Paired phones/Shortcuts in <home>/phone/devices.json — token hashes only, never tokens."""

    def __init__(self, home: str = None):
        self.home = home or santa_home()
        self.dir = os.path.join(self.home, PHONE_DIRNAME)
        self.path = os.path.join(self.dir, 'devices.json')
        self._lock = threading.Lock()
        self._devices = self._load()        # device_id -> record (with token_sha256)
        # The active pairing lives only in memory: a restart cancels it, which is
        # what you'd want for a 5-minute one-time code.
        self._pairing = None

    # ── Persistence ──────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            devices = {}
            for rec in data.get('devices', []):
                if isinstance(rec, dict) and rec.get('id') and \
                        re.fullmatch(r'[0-9a-f]{64}', str(rec.get('token_sha256', ''))):
                    devices[rec['id']] = rec
            return devices
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        _ensure_private_dir(self.dir)
        body = json.dumps({'version': 1, 'devices': list(self._devices.values())}, indent=2)
        _write_private_file(self.path, body.encode('utf-8'))

    def _add_device(self, device_name, kind) -> dict:
        # Caller holds the lock.
        if kind not in DEVICE_KINDS:
            raise ValueError(f'kind must be one of {DEVICE_KINDS}')
        token = secrets.token_urlsafe(32)
        device_id = secrets.token_hex(8)
        now = int(time.time())
        self._devices[device_id] = {
            'id': device_id, 'name': _clean_device_name(device_name), 'kind': kind,
            'token_sha256': _hash_token(token), 'created': now, 'last_seen': now,
        }
        self._save()
        return {'device_id': device_id, 'token': token}

    # ── Pairing ──────────────────────────────────────────────────────────
    def start_pairing(self, ttl: int = PAIRING_TTL_SECONDS) -> dict:
        """Open a pairing window (replacing any previous one); return code + link token."""
        code = f'{secrets.randbelow(10 ** 6):06d}'
        link_token = secrets.token_urlsafe(32)
        expires = int(time.time()) + int(ttl)
        with self._lock:
            self._pairing = {'code_sha256': _hash_token(code),
                             'link_sha256': _hash_token(link_token),
                             'expires': expires, 'wrong': 0}
        return {'code': code, 'link_token': link_token, 'expires': expires}

    def cancel_pairing(self) -> None:
        with self._lock:
            self._pairing = None

    def pairing_active(self) -> bool:
        with self._lock:
            return bool(self._pairing and self._pairing['expires'] > time.time())

    def redeem(self, code_or_link_token, device_name=None, kind: str = 'browser') -> dict:
        """Exchange the one-time code (or QR link token) for a long-lived device token."""
        secret = str(code_or_link_token or '').strip()
        digits = re.sub(r'[\s-]', '', secret)
        with self._lock:
            pairing = self._pairing
            if pairing is None:
                raise PairingError('no_pairing', 'No pairing is in progress. Start one on the Mac.')
            if pairing['expires'] <= time.time():
                self._pairing = None
                raise PairingError('expired', 'That pairing code has expired. Start a new one on the Mac.')
            # A 6-digit code has 10^6 values; capping total wrong guesses at 5 makes
            # a blind guess succeed with ~1 in 200,000 odds before the code dies.
            if re.fullmatch(r'\d{6}', digits):
                ok = hmac.compare_digest(_hash_token(digits), pairing['code_sha256'])
            else:
                ok = hmac.compare_digest(_hash_token(secret), pairing['link_sha256'])
            if not ok:
                pairing['wrong'] += 1
                if pairing['wrong'] >= PAIRING_MAX_WRONG_ATTEMPTS:
                    self._pairing = None
                    raise PairingError('locked', 'Too many wrong codes. Start a new pairing on the Mac.')
                raise PairingError('invalid', 'That code is not right.')
            self._pairing = None                   # single use
            return self._add_device(device_name, kind)

    # ── Direct tokens (e.g. an iOS Shortcut minted from the Mac UI) ─────
    def create_token(self, device_name=None, kind: str = 'shortcut') -> dict:
        with self._lock:
            return self._add_device(device_name, kind)

    # ── Verification & management ────────────────────────────────────────
    def verify(self, token):
        """Return the public device record for a valid token, else None."""
        if not isinstance(token, str) or not _TOKEN_SHAPE.match(token):
            return None
        wanted = _hash_token(token)
        with self._lock:
            # Constant-time compare against every stored hash (the list is tiny).
            match = None
            for rec in self._devices.values():
                if hmac.compare_digest(rec['token_sha256'], wanted):
                    match = rec
            if match is None:
                return None
            now = int(time.time())
            if now - int(match.get('last_seen') or 0) >= LAST_SEEN_WRITE_INTERVAL:
                match['last_seen'] = now
                try:
                    self._save()
                except OSError:
                    pass   # a failed timestamp write must never lock a device out
            return self._public(match)

    @staticmethod
    def _public(rec) -> dict:
        return {k: rec.get(k) for k in ('id', 'name', 'kind', 'created', 'last_seen')}

    def list(self) -> list:
        with self._lock:
            return sorted((self._public(r) for r in self._devices.values()),
                          key=lambda r: r['created'] or 0)

    def revoke(self, device_id) -> bool:
        with self._lock:
            if self._devices.pop(str(device_id), None) is None:
                return False
            self._save()
            return True

    def revoke_all(self) -> None:
        with self._lock:
            self._devices.clear()
            self._pairing = None
            self._save()


# ── Rate limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window limiter: at most `limit` events per `window` seconds per key."""

    MAX_KEYS = 4096   # bounds memory if something sprays many source addresses

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = int(limit)
        self.window = float(window)
        self._events = {}
        self._lock = threading.Lock()

    def _prune(self, key, now):
        events = self._events.get(key)
        while events and events[0] <= now - self.window:
            events.popleft()
        return events

    def allow(self, key) -> bool:
        """Record one attempt for `key`; False (and not recorded) if over the limit."""
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, now)
            if events is None:
                if len(self._events) >= self.MAX_KEYS:
                    for k in [k for k in self._events if not self._prune(k, now)]:
                        del self._events[k]
                    if len(self._events) >= self.MAX_KEYS:
                        return False
                events = self._events[key] = deque()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def retry_after(self, key) -> int:
        """Seconds until `key` may try again (for a 429 Retry-After header); 0 if now."""
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, now)
            if not events or len(events) < self.limit:
                return 0
            return max(1, int(events[0] + self.window - now + 0.999))


# ── QR code ──────────────────────────────────────────────────────────────────

def qr_svg(text: str, target_px: int = 220):
    """Inline <svg> QR for `text` at roughly target_px, or None if segno isn't installed."""
    try:
        import segno
    except ImportError:
        return None
    qr = segno.make(text, error='m')
    border = 4                                     # the standard quiet zone
    modules = qr.symbol_size(border=border)[0]
    scale = max(1, target_px // modules)
    return qr.svg_inline(scale=scale, border=border, dark='#000000', light='#ffffff')


# ── Cookie / Bearer token plumbing ──────────────────────────────────────────

def cookie_header(token: str) -> str:
    """Value for a Set-Cookie header that stores the device token for a year."""
    return (f'{COOKIE_NAME}={token}; HttpOnly; Secure; SameSite=Strict; Path=/; '
            f'Max-Age={COOKIE_MAX_AGE}')


def clear_cookie_header() -> str:
    """Set-Cookie value that deletes the device cookie (e.g. after revoke)."""
    return f'{COOKIE_NAME}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0'


def token_from_request(headers):
    """Device token from `Authorization: Bearer …` (Shortcuts) or the cookie (browsers)."""
    auth = (headers.get('Authorization') or '').strip()
    if auth:
        scheme, _, value = auth.partition(' ')
        value = value.strip()
        if scheme.lower() == 'bearer' and _TOKEN_SHAPE.match(value):
            return value
    raw_cookie = headers.get('Cookie')
    if raw_cookie:
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except CookieError:
            pass
        morsel = jar.get(COOKIE_NAME)
        if morsel and _TOKEN_SHAPE.match(morsel.value):
            return morsel.value
        # SimpleCookie silently stops at the first malformed cookie (e.g. one set by
        # another app on the same host), which could hide ours; retry with a plain split.
        for part in raw_cookie.split(';'):
            name, _, value = part.strip().partition('=')
            if name == COOKIE_NAME and _TOKEN_SHAPE.match(value.strip()):
                return value.strip()
    return None
