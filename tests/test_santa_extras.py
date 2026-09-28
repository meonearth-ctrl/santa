# tests/test_santa_extras.py
# Plumbing tests for Santa's newer features: output-language translation,
# speaker separation in the service/export, pasted paths & links, and the
# phone-mode HTTP rules (pairing, device tokens, Mac-only endpoints).
# Speech, speaker and translation models are faked — accuracy is measured
# separately (docs/santa/EVALUATION.md).

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from whisper_key.santa import translate, links, export  # noqa: E402
from test_santa import make_service, make_wav, FakeTranscriber, HAVE_FW  # noqa: E402


# ── Translation planning ─────────────────────────────────────────────────────
class TranslatePlanTests(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(translate.plan_route('hi', 'en'), [('hi', 'en')])
        self.assertEqual(translate.plan_route('hi', 'ar'), [('hi', 'en'), ('en', 'ar')])
        self.assertEqual(translate.plan_route('bn', 'ar'), [('bn', 'en'), ('en', 'ar')])
        self.assertEqual(translate.plan_route('ar', 'ar'), [])
        with self.assertRaises(translate.TranslationError):
            translate.plan_route('ta', 'en')
        with self.assertRaises(translate.TranslationError):
            translate.plan_route('en', 'hi')

    def test_sentence_split_keeps_all_text(self):
        text = 'आज मीटिंग है। कल नहीं? مرحبا بكم؟ Hello there. ' + 'x ' * 400
        pieces = translate.split_sentences(text)
        self.assertEqual(pieces[:3], ['आज मीटिंग है।', 'कल नहीं?', 'مرحبا بكم؟'])
        self.assertTrue(all(len(p) <= 400 for p in pieces))
        self.assertEqual(''.join(pieces).replace(' ', ''), text.replace(' ', ''))


class FakeTranslator:
    status = {'state': 'ready', 'message': ''}

    def __init__(self):
        self.calls = []

    def translate(self, text, source, target, cancel_event=None):
        self.calls.append((text, source, target))
        return f'<{target}>{text}</{target}>'


class FakeSpeakerEngine:
    status = {'state': 'ready', 'message': ''}

    @staticmethod
    def available():
        return True

    def diarize(self, samples, num_speakers=0, cancel_event=None, on_progress=None):
        return [{'start': 0.0, 'end': 0.6, 'speaker': 7}, {'start': 0.6, 'end': 2.0, 'speaker': 3}]

    def speaker_embeddings(self, samples, turns):
        import numpy as np
        return {7: np.array([1.0, 0.0], dtype=np.float32), 3: np.array([0.0, 1.0], dtype=np.float32)}

    def embedding(self, samples):
        import numpy as np
        return np.array([1.0, 0.0], dtype=np.float32)


class TwoSegmentTranscriber(FakeTranscriber):
    def __call__(self, *a, **k):
        out = super().__call__(*a, **k)
        out['segments'] = [{'start': 0.0, 'end': 0.5, 'text': ' नमस्ते'}, {'start': 0.7, 'end': 1.9, 'text': ' hello there'}]
        return out


# ── Service: translation + speakers ──────────────────────────────────────────
@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class ServiceExtrasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def svc(self, transcriber=None):
        svc = make_service(self.tmp.name, transcriber or FakeTranscriber(text='मैं ठीक हूँ', detected='hi'))
        svc.translator = FakeTranslator()
        svc.speakers = FakeSpeakerEngine()
        return svc

    def test_translation_is_labelled_and_original_kept(self):
        svc = self.svc()
        svc.settings.update({'output_language': 'en'})
        job = svc.transcribe_sync(make_wav(), 'r.wav', 'hi', timeout=30)
        r = job['result']
        self.assertEqual(r['text'], '<en>मैं ठीक हूँ</en>')
        self.assertEqual(r['original_text'], 'मैं ठीक हूँ')
        self.assertEqual(r['translation']['from'], 'hi')
        self.assertEqual(r['translation']['to'], 'en')

    def test_no_translation_when_already_target_or_same(self):
        svc = self.svc(FakeTranscriber(text='hello', detected='en'))
        svc.settings.update({'output_language': 'en'})
        r = svc.transcribe_sync(make_wav(), 'r.wav', 'en', timeout=30)['result']
        self.assertNotIn('translation', r)
        self.assertEqual(svc.translator.calls, [])

    def test_hinglish_translates_native_script_not_romanized(self):
        svc = self.svc(FakeTranscriber(text='मैं office जा रहा हूँ', detected='hi'))
        svc.settings.update({'output_language': 'ar', 'hinglish_output': 'roman'})
        r = svc.transcribe_sync(make_wav(), 'r.wav', 'hinglish', timeout=30)['result']
        self.assertEqual(svc.translator.calls[0][:2], ('मैं office जा रहा हूँ', 'hi'))
        self.assertTrue(r['original_text'].isascii())          # what the user saw before: romanized
        self.assertTrue(r['rtl'] is False or r['rtl'] is True)

    def test_dictation_is_never_diarized(self):
        svc = self.svc()
        svc.settings.update({'diarize_files': True})
        job = svc.submit(make_wav(), 'r.wav', 'hi', source='recording')
        self._wait(job)
        self.assertNotIn('speakers', job.result)

    def test_file_speakers_labels_export_and_naming(self):
        svc = self.svc(TwoSegmentTranscriber(detected='hi'))
        out = os.path.join(self.tmp.name, 'out')
        svc.settings.update({'diarize_files': True, 'export_enabled': True, 'export_dir': out})
        job = svc.submit(make_wav(2.0), 'interview.wav', 'hi', source='upload')
        self._wait(job)
        r = job.result
        self.assertEqual([s['id'] for s in r['speakers']], ['Speaker 1', 'Speaker 2'])
        self.assertEqual(r['text'], 'Speaker 1: नमस्ते\n\nSpeaker 2: hello there')
        data = json.loads(Path(out, 'interview.json').read_text(encoding='utf-8'))
        self.assertEqual([s['speaker'] for s in data['segments']], ['Speaker 1', 'Speaker 2'])
        self.assertIn('[Speaker 2] hello there', Path(out, 'interview.srt').read_text(encoding='utf-8'))
        # "Speaker 1 is Shouvik" -> saved as a known voice, recognised next time
        svc.enroll_from_job(job.id, 'Speaker 1', 'Shouvik')
        self.assertEqual([v['name'] for v in svc.voices.list()], ['Shouvik'])
        job2 = svc.submit(make_wav(2.0), 'interview2.wav', 'hi', source='upload')
        self._wait(job2)
        self.assertTrue(job2.result['text'].startswith('Shouvik: नमस्ते'))

    def test_speakers_with_translation_keep_labels(self):
        svc = self.svc(TwoSegmentTranscriber(detected='hi'))
        svc.settings.update({'diarize_files': True, 'output_language': 'en'})
        job = svc.submit(make_wav(2.0), 'i.wav', 'hi', source='upload')
        self._wait(job)
        self.assertEqual(job.result['text'], 'Speaker 1: <en>नमस्ते</en>\n\nSpeaker 2: <en>hello there</en>')

    def test_watch_folder_never_translated(self):
        from whisper_key.santa.service import resolve_options, Job
        opts = resolve_options(Job(id='x', language_mode='en', source='watch', audio_path=None, ext='.mp4'),
                               {'output_language': 'ar', 'diarize_files': True, 'num_speakers': 0})
        self.assertEqual(opts['output_language'], 'same')
        self.assertTrue(opts['diarize'])

    def test_pasted_path_is_read_in_place_not_deleted(self):
        svc = self.svc(FakeTranscriber(text='hello', detected='en'))
        path = os.path.join(self.tmp.name, 'My Interview.wav')
        Path(path).write_bytes(make_wav(1.0))
        job = svc.submit_source(path, 'en')
        self._wait(job)
        self.assertEqual(job.state, 'done')
        self.assertTrue(os.path.exists(path))
        self.assertEqual(job.result['source_name'], 'My Interview.wav')

    def _wait(self, job, timeout=30):
        end = time.time() + timeout
        while job.state not in ('done', 'error', 'cancelled') and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(job.state, 'done', job.error)


# ── Paths & links ────────────────────────────────────────────────────────────
class LinkTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(links.classify('https://youtu.be/x'), 'link')
        self.assertEqual(links.classify('~/Movies/a.mp4'), 'path')

    def test_path_errors_are_friendly(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(links.SourceError) as cm:
                links.resolve_path(os.path.join(d, 'missing.mp4'))
            self.assertEqual(cm.exception.code, 'not_found')
            with self.assertRaises(links.SourceError) as cm:
                links.resolve_path(d)
            self.assertEqual(cm.exception.code, 'is_folder')
            txt = os.path.join(d, 'notes.txt')
            Path(txt).write_text('x')
            with self.assertRaises(links.SourceError):
                links.resolve_path(txt)
            ok = os.path.join(d, 'a b.mp4')
            Path(ok).write_bytes(b'x')
            self.assertEqual(links.resolve_path('"' + ok + '"'), ok)
            self.assertEqual(links.resolve_path('file://' + ok.replace(' ', '%20')), ok)

    def test_links_to_local_network_refused(self):
        for url in ('http://127.0.0.1:8765/api/status', 'http://localhost/x.mp4',
                    'http://192.168.1.1/a.mp3', 'http://[::1]/a.mp3', 'ftp://example.com/a.mp3'):
            with self.assertRaises(links.SourceError, msg=url):
                links.check_public_url(url)


# ── Phone-mode HTTP rules (no TLS here; phone.py tests cover TLS) ────────────
class FakePhoneAccess:
    def __init__(self, home):
        from whisper_key.santa import phone
        self.devices = phone.DeviceStore(home)
        self.pair_limiter = phone.RateLimiter(10, 60)
        self.api_limiter = phone.RateLimiter(240, 60)
        self.running = True

    def allowed_hosts(self):
        return {'127.0.0.1', 'test-mac.local'}


@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class PhoneModeServerTests(unittest.TestCase):
    def setUp(self):
        from whisper_key.santa.server import SantaServer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = make_service(self.tmp.name, FakeTranscriber(text='hello from the phone'))
        self.access = FakePhoneAccess(self.tmp.name)
        self.httpd = SantaServer(('127.0.0.1', 0), self.svc, phone_access=self.access)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None, headers=None):
        h = {'X-Santa': '1', 'Origin': f'http://127.0.0.1:{self.port}'}   # plain HTTP here; TLS in phone.py tests
        h.update(headers or {})
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def pair(self):
        code = self.access.devices.start_pairing()['code']
        status, body, headers = self.req('POST', '/api/pair', {'code': code, 'device_name': 'iPhone'})
        self.assertEqual(status, 200, body)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('Secure', headers['Set-Cookie'])
        return {'Cookie': cookie}

    def test_page_public_but_api_needs_pairing(self):
        self.assertEqual(self.req('GET', '/')[0], 200)
        self.assertEqual(self.req('GET', '/manifest.webmanifest')[0], 200)
        status, body, _ = self.req('GET', '/api/config')
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)['error']['code'], 'not_paired')

    def test_wrong_code_then_pair(self):
        self.access.devices.start_pairing()
        status, _, _ = self.req('POST', '/api/pair', {'code': '000000'})
        self.assertIn(status, (403, 410))
        auth = self.pair()
        status, body, _ = self.req('GET', '/api/config', headers=auth)
        cfg = json.loads(body)
        self.assertEqual(cfg['client'], 'phone')
        self.assertNotIn('export_dir', cfg['settings'])       # Mac folders are not shown to the phone

    def test_mac_only_powers_refused(self):
        auth = self.pair()
        for method, path, body in (('POST', '/api/shutdown', {}), ('POST', '/api/phone/pairing', {}),
                                   ('GET', '/api/phone', None), ('POST', '/api/models/load', {'model': 'small'})):
            self.assertEqual(self.req(method, path, body, headers=auth)[0], 403, path)
        status, body, _ = self.req('POST', '/api/transcribe-source', {'source': '/etc/hosts'}, headers=auth)
        self.assertEqual(status, 403)
        # settings: only phone-safe keys are applied
        self.req('PUT', '/api/settings', {'language': 'ar', 'export_dir': '/tmp/x', 'phone_enabled': True}, headers=auth)
        s = self.svc.settings.get()
        self.assertEqual(s['language'], 'ar')
        self.assertEqual(s['export_dir'], '')
        self.assertFalse(s['phone_enabled'])

    def test_cross_site_and_wrong_host_refused(self):
        auth = self.pair()
        status, _, _ = self.req('PUT', '/api/settings', {'language': 'en'},
                                headers=dict(auth, Origin='https://evil.example'))
        self.assertEqual(status, 403)
        status, _, _ = self.req('GET', '/api/config', headers=dict(auth, Host='evil.example'))
        self.assertEqual(status, 421)

    def test_shortcut_bearer_one_request(self):
        token = self.access.devices.create_token('Shortcut')['token']
        status, body, headers = self.req(
            'POST', '/api/transcribe?filename=s.wav&source=recording&wait=30&format=text', make_wav(),
            headers={'Authorization': 'Bearer ' + token, 'X-Santa': '', 'Origin': '', 'Content-Type': 'audio/wav'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body.decode('utf-8'), 'Hello from the phone')
        # revoked -> refused
        self.access.devices.revoke(self.access.devices.list()[0]['id'])
        status, _, _ = self.req('GET', '/api/status', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 401)


class ExportSpeakerTests(unittest.TestCase):
    def test_srt_and_json_without_speakers_unchanged(self):
        segs = [{'start': 0, 'end': 1, 'text': 'hi'}]
        self.assertEqual(export.to_srt(segs), '1\n00:00:00,000 --> 00:00:01,000\nhi\n')
        with tempfile.TemporaryDirectory() as d:
            export.write_transcript(d, 'a.mp4', {'segments': segs})
            data = json.loads(Path(d, 'a.json').read_text())
            self.assertNotIn('speaker', data['segments'][0])
            self.assertNotIn('speakers', data)


if __name__ == '__main__':
    unittest.main()
