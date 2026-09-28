# tests/test_phone.py
# Tests for santa/phone.py: the name-constrained CA and leaf certificate (including
# real TLS handshakes that prove chain verification AND name-constraint rejection),
# the iOS profile, pairing/device tokens, rate limiting and token parsing.

import ipaddress
import json
import os
import plistlib
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from whisper_key.santa import phone
from whisper_key.santa.phone import (
    COOKIE_NAME, DeviceStore, PairingError, PhoneCertificates, RateLimiter,
    cookie_header, local_addresses, qr_svg, token_from_request)

LAN_IP = '192.168.50.10'
HOSTNAME = 'test-mac.local'


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _load_leaf(path):
    with open(path, 'rb') as fh:
        return x509.load_pem_x509_certificates(fh.read())


# ── TLS helpers ──────────────────────────────────────────────────────────────

class _OneShotTLSServer:
    """Accepts TLS connections on 127.0.0.1 in a thread; handshake errors are swallowed."""

    def __init__(self, context):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.context = context
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    tls.sendall(b'hello')
            except (ssl.SSLError, OSError):
                pass

    def close(self):
        self._stop = True
        self.thread.join(2)
        self.sock.close()


def _client_handshake(port, cadata, server_hostname):
    """Full verification (chain + hostname/IP) against only our CA; returns bytes read."""
    ctx = ssl.create_default_context(cadata=cadata)   # no system roots matter: ours only
    ctx.load_verify_locations(cadata=cadata)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=server_hostname) as tls:
            return tls.recv(5)


class PhoneTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)


# ── Certificates ─────────────────────────────────────────────────────────────

class CertificateTests(PhoneTestCase):
    def setUp(self):
        super().setUp()
        self.certs = PhoneCertificates(self.home)
        self.ca_cert, self.ca_key = self.certs.ensure_ca()

    def test_ca_properties(self):
        ca = self.ca_cert
        bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(bc.critical)
        self.assertTrue(bc.value.ca)
        self.assertEqual(bc.value.path_length, 0)
        ku = ca.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(ku.critical)
        self.assertTrue(ku.value.key_cert_sign and ku.value.crl_sign)
        ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        self.assertIsInstance(ca.public_key(), ec.EllipticCurvePublicKey)
        self.assertEqual(ca.public_key().curve.name, 'secp256r1')
        cn = ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        self.assertTrue(cn.startswith('Santa local CA ('))
        span = ca.not_valid_after_utc - ca.not_valid_before_utc
        self.assertGreaterEqual(span.days, 3650)

    def test_ca_name_constraints_exact(self):
        nc = self.ca_cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(nc.critical)
        self.assertIsNone(nc.value.excluded_subtrees)
        permitted = nc.value.permitted_subtrees
        self.assertEqual(
            {n.value for n in permitted if isinstance(n, x509.DNSName)}, {'local'})
        self.assertEqual(
            {n.value for n in permitted if isinstance(n, x509.IPAddress)},
            {ipaddress.ip_network(n) for n in
             ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10')})
        self.assertEqual(len(permitted), 5)

    def test_ca_reused(self):
        again, _ = PhoneCertificates(self.home).ensure_ca()
        self.assertEqual(again.serial_number, self.ca_cert.serial_number)

    def test_leaf_properties(self):
        cert_path, key_path = self.certs.ensure_server_cert(
            HOSTNAME, [LAN_IP, '100.101.102.103', '127.0.0.1', '8.8.8.8', '169.254.1.1'])
        chain = _load_leaf(cert_path)
        leaf = chain[0]
        self.assertEqual(chain[1], self.ca_cert)   # file carries leaf + CA
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(san.get_values_for_type(x509.DNSName), [HOSTNAME])
        self.assertEqual({str(i) for i in san.get_values_for_type(x509.IPAddress)},
                         {LAN_IP, '100.101.102.103'})           # public/loopback dropped
        self.assertNotIn('localhost', san.get_values_for_type(x509.DNSName))
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertEqual(list(eku), [ExtendedKeyUsageOID.SERVER_AUTH])
        ku = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        self.assertTrue(ku.digital_signature)
        self.assertFalse(ku.key_cert_sign)
        self.assertFalse(leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
        self.assertLessEqual(span, timedelta(days=398))
        self.assertGreaterEqual(span, timedelta(days=390))
        aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
        ski = self.ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        self.assertEqual(aki.key_identifier, ski.digest)
        leaf.verify_directly_issued_by(self.ca_cert)   # raises if the signature is bad

    def test_localhost_never_in_sans(self):
        cert_path, _ = self.certs.ensure_server_cert('localhost', [LAN_IP])
        san = _load_leaf(cert_path)[0].extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        self.assertEqual(san.get_values_for_type(x509.DNSName), [])

    def test_no_usable_names_rejected(self):
        with self.assertRaises(ValueError):
            self.certs.ensure_server_cert('example.com', ['8.8.8.8', '127.0.0.1'])

    def test_leaf_reused_then_regenerated_when_ips_change(self):
        cert_path, _ = self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        first = _load_leaf(cert_path)[0].serial_number
        self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        self.assertEqual(_load_leaf(cert_path)[0].serial_number, first)
        self.certs.ensure_server_cert(HOSTNAME, ['10.0.0.7'])
        second = _load_leaf(cert_path)[0]
        self.assertNotEqual(second.serial_number, first)
        san = second.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual([str(i) for i in san.get_values_for_type(x509.IPAddress)], ['10.0.0.7'])

    def test_leaf_regenerated_when_near_expiry(self):
        cert_path, _ = self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        first = _load_leaf(cert_path)[0].serial_number
        future = datetime.now(timezone.utc) + timedelta(days=380)
        with mock.patch.object(phone, '_utcnow', return_value=future):
            self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        self.assertNotEqual(_load_leaf(cert_path)[0].serial_number, first)

    def test_leaf_regenerated_when_missing(self):
        cert_path, key_path = self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        os.unlink(key_path)
        self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        self.assertTrue(os.path.exists(key_path))

    def test_file_permissions(self):
        cert_path, key_path = self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        self.assertEqual(_mode(self.certs.dir), 0o700)
        for path in (key_path, self.certs.ca_key_path, cert_path, self.certs.ca_cert_path):
            self.assertEqual(_mode(path), 0o600, path)

    def test_fingerprint_and_mobileconfig(self):
        fp = self.certs.ca_fingerprint()
        self.assertRegex(fp, r'^([0-9A-F]{2}:){31}[0-9A-F]{2}$')
        profile = plistlib.loads(self.certs.mobileconfig())
        self.assertEqual(profile['PayloadType'], 'Configuration')
        self.assertIs(profile['PayloadRemovalDisallowed'], False)
        self.assertEqual(profile['PayloadOrganization'], 'Santa (local)')
        self.assertEqual(profile['PayloadDisplayName'],
                         'Santa — this Mac’s local certificate')
        self.assertIn('Remove Profile', profile['PayloadDescription'])
        (root,) = profile['PayloadContent']
        self.assertEqual(root['PayloadType'], 'com.apple.security.root')
        self.assertEqual(root['PayloadContent'], self.certs.ca_der())
        # UUIDs are stable for the same CA, and differ from each other.
        again = plistlib.loads(self.certs.mobileconfig())
        self.assertEqual(again['PayloadUUID'], profile['PayloadUUID'])
        self.assertEqual(again['PayloadContent'][0]['PayloadUUID'], root['PayloadUUID'])
        self.assertNotEqual(root['PayloadUUID'], profile['PayloadUUID'])

    def test_ssl_context_requires_leaf(self):
        with self.assertRaises(FileNotFoundError):
            self.certs.ssl_context()
        self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        ctx = self.certs.ssl_context()
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_reset(self):
        self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        self.certs.reset()
        for path in (self.certs.cert_path, self.certs.key_path,
                     self.certs.ca_cert_path, self.certs.ca_key_path):
            self.assertFalse(os.path.exists(path))
        new_ca, _ = self.certs.ensure_ca()
        self.assertNotEqual(new_ca.serial_number, self.ca_cert.serial_number)


# ── Real TLS handshakes ──────────────────────────────────────────────────────

class TLSHandshakeTests(PhoneTestCase):
    """The server listens on 127.0.0.1, but the client verifies as if it dialled the
    LAN IP / .local name (server_hostname), so chain + SAN + constraints are all checked."""

    def setUp(self):
        super().setUp()
        self.certs = PhoneCertificates(self.home)
        self.ca_cert, self.ca_key = self.certs.ensure_ca()
        self.certs.ensure_server_cert(HOSTNAME, [LAN_IP])
        self.cadata = self.ca_cert.public_bytes(serialization.Encoding.PEM).decode()

    def _serve(self, context):
        server = _OneShotTLSServer(context)
        self.addCleanup(server.close)
        return server

    def _rogue_context(self, san_entries):
        # A leaf the (leaked) CA key signs for a name OUTSIDE the constraints.
        key = ec.generate_private_key(ec.SECP256R1())
        ski = self.ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        now = datetime.now(timezone.utc)
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'rogue')]))
                .issuer_name(self.ca_cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                               critical=False)
                .sign(self.ca_key, hashes.SHA256()))
        leaf.verify_directly_issued_by(self.ca_cert)   # genuinely signed by our CA
        d = tempfile.mkdtemp(dir=self.home)
        cert_path, key_path = os.path.join(d, 'c.pem'), os.path.join(d, 'k.pem')
        with open(cert_path, 'wb') as fh:
            fh.write(leaf.public_bytes(serialization.Encoding.PEM))
        with open(key_path, 'wb') as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        return ctx

    def test_handshake_verifies_for_lan_ip(self):
        server = self._serve(self.certs.ssl_context())
        self.assertEqual(_client_handshake(server.port, self.cadata, LAN_IP), b'hello')

    def test_handshake_verifies_for_local_hostname(self):
        server = self._serve(self.certs.ssl_context())
        self.assertEqual(_client_handshake(server.port, self.cadata, HOSTNAME), b'hello')

    def test_handshake_rejects_wrong_ip(self):
        server = self._serve(self.certs.ssl_context())
        with self.assertRaises(ssl.SSLCertVerificationError):
            _client_handshake(server.port, self.cadata, '192.168.50.11')

    def test_handshake_rejects_unknown_ca(self):
        other = PhoneCertificates(os.path.join(self.home, 'other'))
        other_ca, _ = other.ensure_ca()
        server = self._serve(self.certs.ssl_context())
        with self.assertRaises(ssl.SSLCertVerificationError):
            _client_handshake(server.port,
                              other_ca.public_bytes(serialization.Encoding.PEM).decode(), LAN_IP)

    def test_name_constraints_reject_public_dns_name(self):
        server = self._serve(self._rogue_context([x509.DNSName('example.com')]))
        with self.assertRaises(ssl.SSLCertVerificationError) as caught:
            _client_handshake(server.port, self.cadata, 'example.com')
        self.assertIn('subtree', str(caught.exception).lower())   # "permitted subtree violation"

    def test_name_constraints_reject_public_and_loopback_ip(self):
        for addr in ('8.8.8.8', '127.0.0.1'):
            with self.subTest(addr=addr):
                server = self._serve(self._rogue_context([x509.IPAddress(ipaddress.ip_address(addr))]))
                with self.assertRaises(ssl.SSLCertVerificationError):
                    _client_handshake(server.port, self.cadata, addr)

    def test_old_tls_refused(self):
        server = self._serve(self.certs.ssl_context())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=self.cadata)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)   # TLS 1.1 is, on purpose
                ctx.minimum_version = ssl.TLSVersion.TLSv1_1
                ctx.maximum_version = ssl.TLSVersion.TLSv1_1
        except (ValueError, ssl.SSLError):
            self.skipTest('local OpenSSL cannot even offer TLS 1.1')
        with socket.create_connection(('127.0.0.1', server.port), timeout=5) as raw:
            with self.assertRaises((ssl.SSLError, OSError)):
                with ctx.wrap_socket(raw, server_hostname=LAN_IP) as tls:
                    tls.recv(5)


# ── Devices & pairing ────────────────────────────────────────────────────────

class DeviceStoreTests(PhoneTestCase):
    def setUp(self):
        super().setUp()
        self.store = DeviceStore(self.home)

    def test_pairing_shape(self):
        p = self.store.start_pairing()
        self.assertRegex(p['code'], r'^\d{6}$')
        self.assertGreaterEqual(len(p['link_token']), 40)
        self.assertAlmostEqual(p['expires'], time.time() + 300, delta=5)

    def test_redeem_by_code_single_use(self):
        p = self.store.start_pairing()
        result = self.store.redeem(p['code'], 'Shouvik’s iPhone')
        device = self.store.verify(result['token'])
        self.assertEqual(device['id'], result['device_id'])
        self.assertEqual(device['name'], 'Shouvik’s iPhone')
        self.assertEqual(device['kind'], 'browser')
        with self.assertRaises(PairingError) as caught:
            self.store.redeem(p['code'], 'again')
        self.assertEqual(caught.exception.code, 'no_pairing')

    def test_redeem_code_with_spaces(self):
        p = self.store.start_pairing()
        spaced = p['code'][:3] + ' ' + p['code'][3:]
        self.assertIn('token', self.store.redeem(spaced, None))

    def test_redeem_by_link_token(self):
        p = self.store.start_pairing()
        result = self.store.redeem(p['link_token'], None)
        self.assertEqual(self.store.verify(result['token'])['name'], 'iPhone')   # default name
        with self.assertRaises(PairingError):
            self.store.redeem(p['link_token'], None)

    def test_new_pairing_replaces_old(self):
        old = self.store.start_pairing()
        new = self.store.start_pairing()
        if old['code'] != new['code']:
            with self.assertRaises(PairingError):
                self.store.redeem(old['code'], None)
        self.store.redeem(new['link_token'], None)

    def test_expiry(self):
        p = self.store.start_pairing(ttl=1)
        with mock.patch.object(phone.time, 'time', return_value=time.time() + 5):
            with self.assertRaises(PairingError) as caught:
                self.store.redeem(p['code'], None)
        self.assertEqual(caught.exception.code, 'expired')

    def test_five_wrong_attempts_lock_out(self):
        p = self.store.start_pairing()
        wrong = f"{(int(p['code']) + 1) % 10**6:06d}"
        codes = []
        for _ in range(4):
            with self.assertRaises(PairingError) as caught:
                self.store.redeem(wrong, None)
            codes.append(caught.exception.code)
        self.assertEqual(codes, ['invalid'] * 4)
        with self.assertRaises(PairingError) as caught:
            self.store.redeem('not-the-link-token-xxxxxxxxxxxxxxx', None)   # 5th wrong, any form
        self.assertEqual(caught.exception.code, 'locked')
        with self.assertRaises(PairingError):
            self.store.redeem(p['code'], None)        # even the right code is dead now
        self.assertEqual(self.store.list(), [])

    def test_tokens_stored_hashed_only(self):
        p = self.store.start_pairing()
        paired = self.store.redeem(p['code'], 'Phone')
        shortcut = self.store.create_token('My Shortcut')
        with open(self.store.path, encoding='utf-8') as fh:
            raw = fh.read()
        for secret in (paired['token'], shortcut['token'], p['code'], p['link_token']):
            self.assertNotIn(secret, raw)
        records = json.loads(raw)['devices']
        self.assertEqual(len(records), 2)
        for rec in records:
            self.assertRegex(rec['token_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(_mode(self.store.path), 0o600)
        self.assertEqual(_mode(self.store.dir), 0o700)

    def test_persistence_and_create_token(self):
        made = self.store.create_token('Shortcut')
        reloaded = DeviceStore(self.home)
        device = reloaded.verify(made['token'])
        self.assertEqual(device['kind'], 'shortcut')
        self.assertEqual([d['id'] for d in reloaded.list()], [made['device_id']])
        self.assertEqual(set(reloaded.list()[0]), {'id', 'name', 'kind', 'created', 'last_seen'})

    def test_verify_rejects_junk(self):
        self.store.create_token('x')
        for junk in (None, '', 'short', 'x' * 500, 'bad token with spaces!!', 123):
            self.assertIsNone(self.store.verify(junk))
        self.assertIsNone(self.store.verify('A' * 43))

    def test_last_seen_throttled(self):
        made = self.store.create_token('x')
        created = self.store.list()[0]['last_seen']
        self.store.verify(made['token'])
        self.assertEqual(self.store.list()[0]['last_seen'], created)
        later = time.time() + 120
        with mock.patch.object(phone.time, 'time', return_value=later):
            self.store.verify(made['token'])
        self.assertEqual(self.store.list()[0]['last_seen'], int(later))
        self.assertEqual(DeviceStore(self.home).list()[0]['last_seen'], int(later))

    def test_revoke_and_revoke_all(self):
        a = self.store.create_token('a')
        b = self.store.create_token('b')
        self.assertTrue(self.store.revoke(a['device_id']))
        self.assertFalse(self.store.revoke(a['device_id']))
        self.assertIsNone(self.store.verify(a['token']))
        self.assertIsNotNone(self.store.verify(b['token']))
        self.store.revoke_all()
        self.assertIsNone(self.store.verify(b['token']))
        self.assertEqual(DeviceStore(self.home).list(), [])

    def test_device_name_sanitized(self):
        made = self.store.create_token('  Evil\x00\n\x1bName' + 'x' * 100)
        name = self.store.verify(made['token'])['name']
        self.assertLessEqual(len(name), 40)
        self.assertTrue(name.isprintable())
        self.assertTrue(name.startswith('EvilName'))
        self.assertEqual(self.store.verify(self.store.create_token('   ')['token'])['name'], 'iPhone')

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_token('x', kind='admin')

    def test_corrupt_file_tolerated(self):
        os.makedirs(self.store.dir, exist_ok=True)
        with open(self.store.path, 'w') as fh:
            fh.write('{not json')
        self.assertEqual(DeviceStore(self.home).list(), [])

    def test_concurrent_redeem_only_one_wins(self):
        p = self.store.start_pairing()
        wins, errors = [], []

        def attempt():
            try:
                wins.append(self.store.redeem(p['code'], None))
            except PairingError as exc:
                errors.append(exc.code)
        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(errors), 7)


# ── Rate limiter, QR, cookies, addresses ────────────────────────────────────

class RateLimiterTests(unittest.TestCase):
    def test_limit_and_window(self):
        clock = [1000.0]
        with mock.patch.object(phone.time, 'monotonic', side_effect=lambda: clock[0]):
            rl = RateLimiter(3, window=60)
            self.assertEqual([rl.allow('ip') for _ in range(4)], [True, True, True, False])
            self.assertTrue(rl.allow('other-ip'))
            self.assertEqual(rl.retry_after('ip'), 60)
            clock[0] += 61
            self.assertTrue(rl.allow('ip'))
            self.assertEqual(rl.retry_after('ip'), 0)


class HelperTests(unittest.TestCase):
    def test_qr_svg(self):
        svg = qr_svg('https://test-mac.local:8766/pair#abcdef')
        self.assertIsNotNone(svg)
        self.assertTrue(svg.startswith('<svg'))
        width = int(svg.split('width="')[1].split('"')[0])
        self.assertTrue(150 <= width <= 230, width)
        with mock.patch.dict(sys.modules, {'segno': None}):
            self.assertIsNone(qr_svg('x'))

    def test_cookie_header(self):
        value = cookie_header('tok_' + 'a' * 40)
        for part in ('santa_device=tok_', 'HttpOnly', 'Secure', 'SameSite=Strict',
                     'Path=/', 'Max-Age=31536000'):
            self.assertIn(part, value)

    def test_token_from_request(self):
        token = 'Abc_-' + 'x' * 38
        self.assertEqual(token_from_request({'Authorization': f'Bearer {token}'}), token)
        self.assertEqual(token_from_request({'Authorization': f'bearer  {token}'}), token)
        self.assertIsNone(token_from_request({'Authorization': f'Basic {token}'}))
        self.assertEqual(token_from_request({'Cookie': f'a=1; {COOKIE_NAME}={token}; b=2'}), token)
        # A malformed neighbour cookie must not hide ours.
        self.assertEqual(token_from_request({'Cookie': f'x="bad\\; {COOKIE_NAME}={token}'}), token)
        self.assertIsNone(token_from_request({'Cookie': f'{COOKIE_NAME}=short'}))
        self.assertIsNone(token_from_request({}))
        # Works with the real header object BaseHTTPRequestHandler uses.
        from email.message import Message
        msg = Message()
        msg['Cookie'] = f'{COOKIE_NAME}={token}'
        self.assertEqual(token_from_request(msg), token)

    def test_local_addresses_shape(self):
        info = local_addresses()
        self.assertIn('hostname', info)
        if info['hostname']:
            self.assertTrue(info['hostname'].endswith('.local'))
        for addr in info['ips']:
            ip = ipaddress.ip_address(addr)
            self.assertFalse(ip.is_loopback or ip.is_link_local)
            self.assertTrue(phone._is_private_ip(addr))

    def test_ifconfig_parsing(self):
        sample = (
            'lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n'
            '\tinet 127.0.0.1 netmask 0xff000000\n'
            'en0: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500\n'
            '\tinet6 fe80::1%en0 prefixlen 64\n'
            '\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255\n'
            '\tstatus: active\n'
            'en5: flags=8863<UP> mtu 1500\n'
            '\tinet 10.9.9.9 netmask 0xffffff00\n'
            '\tstatus: inactive\n'
            'utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280\n'
            '\tinet 100.88.1.2 --> 100.88.1.2 netmask 0xffffffff\n'
            'bridge0: flags=8863<UP>\n'
            '\tinet 169.254.3.3 netmask 0xffff0000\n')
        with mock.patch.object(phone, '_run', return_value=sample), \
                mock.patch.object(phone.sys, 'platform', 'darwin'):
            parsed = phone._ips_from_ifconfig()
            self.assertEqual(parsed, ['127.0.0.1', '192.168.1.23', '100.88.1.2', '169.254.3.3'])
            info = local_addresses()
        self.assertEqual(info['ips'], ['192.168.1.23', '100.88.1.2'])


if __name__ == '__main__':
    unittest.main()
