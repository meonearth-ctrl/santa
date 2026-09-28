# tests/test_santa.py
# Focused tests for the Santa layer (language routing, Unicode safety,
# romanization, settings, audio validation, job service, localhost API).
# The Whisper model is replaced by a fake so these run in seconds and offline;
# they prove plumbing, NOT recognition accuracy (see docs/santa/EVALUATION.md).

import io
import json
import os
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from whisper_key.santa import languages, romanize, textproc, audio_input
from whisper_key.santa.settings import SettingsStore, validate, DEFAULTS

try:
    import faster_whisper  # noqa: F401
    HAVE_FW = True
except ImportError:
    HAVE_FW = False


def make_wav(seconds=1.0, rate=16000, amplitude=0.3, freq=220.0) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    samples = (amplitude * np.sin(2 * np.pi * freq * t) * 32767).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


# ── Language routing ─────────────────────────────────────────────────────────
class LanguageRoutingTests(unittest.TestCase):
    def test_fixed_languages_use_whisper_codes(self):
        for mode, code in [('en', 'en'), ('hi', 'hi'), ('ar', 'ar'), ('bn', 'bn')]:
            p = languages.resolve_decode_params(mode)
            self.assertEqual(p.language, code)
            self.assertEqual(p.task, 'transcribe')

    def test_auto_detect_has_no_language(self):
        self.assertIsNone(languages.resolve_decode_params('auto').language)

    def test_hinglish_never_invents_a_code(self):
        valid = {None, 'hi', 'en'}
        for key in languages.HINGLISH_STRATEGIES:
            p = languages.resolve_decode_params('hinglish', key)
            self.assertIn(p.language, valid)
            self.assertEqual(p.task, 'transcribe')
        self.assertTrue(languages.resolve_decode_params('hinglish', 'per_segment').multilingual)

    def test_user_prompt_is_appended(self):
        p = languages.resolve_decode_params('en', user_prompt='Kout Food Group')
        self.assertEqual(p.initial_prompt, 'Kout Food Group')
        p = languages.resolve_decode_params('hinglish', 'hindi_mixed', 'Salmiya')
        self.assertTrue(p.initial_prompt.endswith('Salmiya'))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(languages.UnknownLanguageMode):
            languages.resolve_decode_params('hinglish-xx')


# ── Unicode & cleanup ────────────────────────────────────────────────────────
class UnicodeSafetyTests(unittest.TestCase):
    SAMPLES = [
        'क्षमा कीजिए, मैं कल ऑफ़िस नहीं आ पाऊँगा।',           # conjuncts, nukta, chandrabindu
        'আমি আজ অফিসে যাব না, কাল দেখা হবে।',                  # Bengali vowel signs
        'مرحباً، كيف حالك؟ الاجتماع الساعة ١٠:٣٠ صباحاً.',      # Arabic harakat, Arabic digits
        'Meeting at 10:30 AM, budget KWD 1,250.500 for Q3.',
    ]

    def test_cleanup_keeps_every_non_space_character(self):
        for s in self.SAMPLES:
            out = textproc.light_cleanup('  ' + s.replace(' ', '  ') + ' ')
            strip = lambda x: ''.join(ch for ch in unicodedata.normalize('NFC', x) if not ch.isspace())
            # Only the first Latin letter may change case; nothing else may change.
            self.assertEqual(strip(out).lower(), strip(s).lower())

    def test_combining_marks_survive_utf8_round_trip(self):
        for s in self.SAMPLES:
            with tempfile.NamedTemporaryFile('w+', encoding='utf-8', suffix='.txt', delete=False) as fh:
                fh.write(textproc.normalize(s)); path = fh.name
            try:
                with open(path, encoding='utf-8') as fh:
                    self.assertEqual(fh.read(), unicodedata.normalize('NFC', s))
            finally:
                os.remove(path)

    def test_capitalisation_only_for_latin(self):
        self.assertEqual(textproc.light_cleanup('hello there'), 'Hello there')
        self.assertEqual(textproc.light_cleanup('मैं ठीक हूँ'), 'मैं ठीक हूँ')

    def test_numbers_untouched(self):
        s = 'Pay 3,450.75 KWD on 12/10/2026 to IBAN KW81CBKU0000000000001234560101'
        self.assertEqual(textproc.light_cleanup(s), s)

    def test_script_detection_and_warning(self):
        self.assertTrue(textproc.is_rtl_dominant('الاجتماع غداً at 10'))
        self.assertFalse(textproc.is_rtl_dominant('Meeting غداً'))
        self.assertEqual(textproc.script_warning('मैं ठीक हूँ', 'Devanagari'), '')
        self.assertIn('Arabic', textproc.script_warning('میں ٹھیک ہوں', 'Devanagari'))


# ── Romanization ─────────────────────────────────────────────────────────────
class RomanizeTests(unittest.TestCase):
    def test_common_hinglish(self):
        cases = {
            'मैं आज office जा रहा हूँ।': 'main aaj office ja raha hoon.',
            'क्या तुम कल meeting में आओगे?': 'kya tum kal meeting mein aaoge?',
            'करना है': 'karna hai',
            'समझना': 'samajhna',
            'ज़िंदगी': 'zindgi',
            '१२३': '123',
        }
        for src, expected in cases.items():
            self.assertEqual(romanize.romanize_hinglish(src), expected, src)

    def test_latin_and_other_scripts_untouched(self):
        s = 'Budget 1,200 KWD — مرحبا — OK'
        self.assertEqual(romanize.romanize_hinglish(s), s)


# ── Settings ─────────────────────────────────────────────────────────────────
class SettingsTests(unittest.TestCase):
    def test_validate_rejects_bad_values(self):
        v = validate({'language': 'klingon', 'beam_size': 99, 'history_enabled': 'yes', 'unknown': 1})
        self.assertEqual(v['language'], DEFAULTS['language'])
        self.assertEqual(v['beam_size'], 10)
        self.assertFalse(v['history_enabled'])
        self.assertNotIn('unknown', v)

    def test_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as home:
            s = SettingsStore(home)
            s.update({'language': 'hinglish', 'hinglish_output': 'roman', 'user_prompt': 'سالمية'})
            again = SettingsStore(home).get()
            self.assertEqual(again['language'], 'hinglish')
            self.assertEqual(again['hinglish_output'], 'roman')
            self.assertEqual(again['user_prompt'], 'سالمية')

    def test_history_off_by_default(self):
        self.assertFalse(DEFAULTS['history_enabled'])


# ── Audio validation ─────────────────────────────────────────────────────────
class AudioInputTests(unittest.TestCase):
    def test_rejects_unsupported_extension(self):
        with self.assertRaises(audio_input.UnsupportedAudio):
            audio_input.check_upload('notes.docx', 100, 10_000)

    def test_rejects_oversize_and_empty(self):
        with self.assertRaises(audio_input.AudioTooLarge):
            audio_input.check_upload('a.wav', 20_000, 10_000)
        with self.assertRaises(audio_input.EmptyAudio):
            audio_input.check_upload('a.wav', 0, 10_000)

    def test_silence_short_and_long(self):
        with self.assertRaises(audio_input.EmptyAudio):
            audio_input.check_samples(np.zeros(16000, dtype=np.float32), 60)
        with self.assertRaises(audio_input.EmptyAudio):
            audio_input.check_samples(np.ones(100, dtype=np.float32) * 0.5, 60)
        with self.assertRaises(audio_input.AudioTooLong):
            audio_input.check_samples(np.ones(16000 * 5, dtype=np.float32) * 0.5, 2)

    @unittest.skipUnless(HAVE_FW, 'faster-whisper not installed')
    def test_decode_wav_and_garbage(self):
        d = audio_input.decode_bytes(make_wav(1.0, rate=48000), '.wav', 60)
        self.assertAlmostEqual(d.duration_s, 1.0, places=1)
        with self.assertRaises(audio_input.UnsupportedAudio):
            audio_input.decode_bytes(b'this is not audio' * 100, '.mp3', 60)

    @unittest.skipUnless(HAVE_FW, 'faster-whisper not installed')
    def test_temp_files_removed(self):
        before = set(os.listdir(tempfile.gettempdir()))
        audio_input.decode_bytes(make_wav(0.5), '.wav', 60)
        leftovers = [n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith('santa-')]
        self.assertEqual(leftovers, [])


# ── Service with a fake model ────────────────────────────────────────────────
class FakeModelManager:
    def __init__(self):
        self.load_calls = 0
        self.key = None

    def ensure_loading(self, key, *a, **k):
        if self.key != key:
            self.load_calls += 1
            self.key = key

    def wait_ready(self, key, timeout=None, cancel_event=None):
        self.ensure_loading(key)
        return object()

    def loaded_key(self):
        return self.key

    def get_status(self):
        return {'state': 'ready', 'model': self.key, 'message': 'fake', 'progress': None}


class FakeTranscriber:
    """Records what reached the 'backend' and returns canned segments."""
    def __init__(self, text='hello world', delay=0.0, detected='en'):
        self.calls, self.text, self.delay, self.detected = [], text, delay, detected

    def __call__(self, model, samples, params, beam_size=5, vad_filter=True,
                 cancel_event=None, on_progress=None):
        self.calls.append(params)
        end = time.time() + self.delay
        while time.time() < end:
            if cancel_event is not None and cancel_event.is_set():
                from whisper_key.santa.models import TranscriptionCancelled
                raise TranscriptionCancelled()
            time.sleep(0.01)
        return {'segments': [{'start': 0, 'end': 1, 'text': ' ' + self.text}],
                'detected_language': self.detected, 'language_probability': 0.9,
                'transcribe_seconds': 0.01}


def make_service(home, transcriber):
    from whisper_key.santa.service import TranscriptionService
    from whisper_key.santa.history import HistoryStore
    settings = SettingsStore(home)
    return TranscriptionService(settings=settings, model_manager=FakeModelManager(),
                                history=HistoryStore(home), transcribe_fn=transcriber)


@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_language_reaches_backend_and_model_loaded_once(self):
        fake = FakeTranscriber()
        svc = make_service(self.tmp.name, fake)
        for mode in ['en', 'hi', 'ar', 'bn', 'auto', 'hinglish']:
            job = svc.transcribe_sync(make_wav(), 'r.wav', mode, timeout=30)
            self.assertEqual(job['state'], 'done', job)
        self.assertEqual([p.language for p in fake.calls], ['en', 'hi', 'ar', 'bn', None, 'hi'])
        self.assertEqual(svc.models.load_calls, 1)

    def test_hinglish_roman_output_keeps_raw(self):
        fake = FakeTranscriber(text='मैं आज office जा रहा हूँ।')
        svc = make_service(self.tmp.name, fake)
        svc.settings.update({'hinglish_output': 'roman'})
        r = svc.transcribe_sync(make_wav(), 'r.wav', 'hinglish', timeout=30)['result']
        self.assertEqual(r['text'], 'main aaj office ja raha hoon.')
        self.assertEqual(r['raw_text'], 'मैं आज office जा रहा हूँ।')

    def test_arabic_result_flagged_rtl(self):
        svc = make_service(self.tmp.name, FakeTranscriber(text='الاجتماع غداً الساعة 10', detected='ar'))
        r = svc.transcribe_sync(make_wav(), 'r.wav', 'ar', timeout=30)['result']
        self.assertTrue(r['rtl'])
        self.assertEqual(r['script_warning'], '')

    def test_history_only_when_enabled(self):
        svc = make_service(self.tmp.name, FakeTranscriber())
        svc.transcribe_sync(make_wav(), 'r.wav', 'en', timeout=30)
        self.assertEqual(svc.history.list(), [])
        svc.settings.update({'history_enabled': True})
        svc.transcribe_sync(make_wav(), 'r.wav', 'en', timeout=30)
        entries = svc.history.list()
        self.assertEqual(len(entries), 1)
        svc.history.delete(entries[0]['id'])
        self.assertEqual(svc.history.list(), [])

    def test_silent_recording_is_a_clear_error(self):
        svc = make_service(self.tmp.name, FakeTranscriber())
        job = svc.transcribe_sync(make_wav(amplitude=0.0), 'r.wav', 'en', timeout=30)
        self.assertEqual(job['state'], 'error')
        self.assertEqual(job['error']['code'], 'empty_audio')

    def test_cancel_and_queue_limit(self):
        from whisper_key.santa.service import QueueFull
        svc = make_service(self.tmp.name, FakeTranscriber(delay=1.0))
        jobs = [svc.submit(make_wav(), 'r.wav', 'en') for _ in range(3)]
        with self.assertRaises(QueueFull):
            svc.submit(make_wav(), 'r.wav', 'en')
        for j in jobs:
            svc.cancel(j.id)
        deadline = time.time() + 5
        while time.time() < deadline and any(j.state not in ('cancelled', 'done') for j in jobs):
            time.sleep(0.05)
        self.assertTrue(all(j.state == 'cancelled' for j in jobs), [j.state for j in jobs])
        self.assertTrue(all(j.audio_bytes is None for j in jobs if j.state != 'queued'))


class FakeAccelerator:
    """Stands in for the Metal fast path; can be told to produce damaged text."""
    def __init__(self, text='fast text', damaged=False):
        self.calls, self.text, self.damaged = 0, text, damaged

    def available(self): return True
    def ready(self): return True
    def ensure_loading(self): pass
    def get_status(self): return {'state': 'ready', 'message': 'fake gpu'}

    def transcribe(self, samples, params, cancel_event=None):
        from whisper_key.santa.accel_cpp import AcceleratorError
        self.calls += 1
        if self.damaged:
            raise AcceleratorError('split multi-byte character in output')
        return {'segments': [{'start': 0, 'end': 1, 'text': self.text}], 'detected_language': params.language,
                'language_probability': None, 'transcribe_seconds': 0.01, 'engine': 'whisper.cpp · Metal GPU'}


@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class AcceleratorRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _svc(self, accel, cpu_text='cpu text'):
        svc = make_service(self.tmp.name, FakeTranscriber(text=cpu_text))
        svc.accel = accel
        return svc

    def test_short_clip_uses_gpu_path(self):
        accel = FakeAccelerator()
        svc = self._svc(accel)
        r = svc.transcribe_sync(make_wav(2.0), 'r.wav', 'hi', timeout=30)['result']
        self.assertEqual(r['text'], 'Fast text')
        self.assertIn('Metal', r['engine'])
        self.assertEqual(svc._transcribe_fn.calls, [])

    def test_long_clip_is_split_and_language_locked(self):
        import unittest.mock as mock
        from whisper_key.santa import accel_cpp
        accel = FakeAccelerator()
        seen = []
        orig = accel.transcribe
        accel.transcribe = lambda samples, params, cancel_event=None: (
            seen.append((len(samples), params.language)) or dict(orig(samples, params), detected_language='hi'))
        svc = self._svc(accel)
        with mock.patch.object(accel_cpp, 'plan_chunks', return_value=[(0, 16000 * 20), (16000 * 21, 16000 * 40)]):
            r = svc.transcribe_sync(make_wav(40.0), 'r.wav', 'auto', timeout=30)['result']
        self.assertEqual([n for n, _ in seen], [16000 * 20, 16000 * 19])
        self.assertEqual([lang for _, lang in seen], [None, 'hi'])   # detected once, then fixed
        self.assertIn('2 pieces', r['engine'])
        self.assertEqual(svc._transcribe_fn.calls, [])

    def test_cpu_setting_bypasses_gpu(self):
        accel = FakeAccelerator()
        svc = self._svc(accel)
        svc.settings.update({'engine': 'cpu'})
        r = svc.transcribe_sync(make_wav(2.0), 'r.wav', 'en', timeout=30)['result']
        self.assertIn('CPU', r['engine'])
        self.assertEqual(accel.calls, 0)

    def test_long_clip_without_detectable_speech_uses_cpu(self):
        svc = self._svc(FakeAccelerator())      # a pure tone has no speech for the VAD
        r = svc.transcribe_sync(make_wav(30.0), 'r.wav', 'en', timeout=60)['result']
        self.assertIn('CPU', r['engine'])

    def test_damaged_gpu_output_falls_back_to_cpu(self):
        accel = FakeAccelerator(damaged=True)
        svc = self._svc(accel, cpu_text='मैं ठीक हूँ')
        r = svc.transcribe_sync(make_wav(2.0), 'r.wav', 'hi', timeout=30)['result']
        self.assertEqual(accel.calls, 1)
        self.assertEqual(r['text'], 'मैं ठीक हूँ')
        self.assertIn('CPU', r['engine'])

    def test_per_segment_strategy_skips_gpu(self):
        accel = FakeAccelerator()
        svc = self._svc(accel)
        svc.settings.update({'hinglish_strategy': 'per_segment'})
        svc.transcribe_sync(make_wav(2.0), 'r.wav', 'hinglish', timeout=30)
        self.assertEqual(accel.calls, 0)


# ── Localhost HTTP API ───────────────────────────────────────────────────────
@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class ServerTests(unittest.TestCase):
    def setUp(self):
        from whisper_key.santa.server import make_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = make_service(self.tmp.name, FakeTranscriber(text='مرحبا Santa ١٢٣'))
        self.httpd = make_server('127.0.0.1', 0, self.svc)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None, headers=None, host=None):
        h = {'X-Santa': '1'}
        h.update(headers or {})
        if host:
            h['Host'] = host
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_ui_and_static_served_with_csp(self):
        status, body, headers = self.req('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Santa', body)
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        for name in ('app.js', 'style.css', 'recorder-worklet.js', 'santa.svg'):
            self.assertEqual(self.req('GET', '/static/' + name)[0], 200)
        self.assertEqual(self.req('GET', '/static/../server.py')[0], 404)

    def test_foreign_host_and_cross_site_rejected(self):
        self.assertEqual(self.req('GET', '/api/status', host='evil.example:80')[0], 421)
        status, _, _ = self.req('POST', '/api/transcribe?language=en', make_wav(), headers={'X-Santa': ''})
        self.assertEqual(status, 403)
        status, _, _ = self.req('POST', '/api/transcribe?language=en', make_wav(),
                                headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_transcribe_round_trip_preserves_unicode(self):
        status, body, _ = self.req('POST', '/api/transcribe?language=ar&filename=a.wav&source=recording', make_wav())
        self.assertEqual(status, 202)
        job_id = json.loads(body)['id']
        for _ in range(100):
            job = json.loads(self.req('GET', f'/api/jobs/{job_id}')[1])
            if job['state'] in ('done', 'error'):
                break
            time.sleep(0.05)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(job['result']['text'], 'مرحبا Santa ١٢٣')
        self.assertTrue(job['result']['rtl'])

    def test_bad_inputs(self):
        self.assertEqual(self.req('POST', '/api/transcribe?language=en&filename=x.exe', b'abc')[0], 400)
        self.assertEqual(self.req('POST', '/api/transcribe?language=xx&filename=x.wav', make_wav())[0], 400)
        status, body, _ = self.req('POST', '/api/transcribe?language=en&filename=x.mp3', b'junk' * 500)
        job = json.loads(body)
        for _ in range(100):
            job = json.loads(self.req('GET', f"/api/jobs/{job['id']}")[1])
            if job['state'] in ('done', 'error'):
                break
            time.sleep(0.05)
        self.assertEqual(job['error']['code'], 'unsupported_audio')

    def test_settings_put_and_config(self):
        status, body, _ = self.req('PUT', '/api/settings', {'language': 'bn', 'history_enabled': True})
        self.assertEqual(status, 200)
        cfg = json.loads(self.req('GET', '/api/config')[1])
        self.assertEqual(cfg['settings']['language'], 'bn')
        self.assertEqual({l['key'] for l in cfg['languages']}, {'auto', 'en', 'hi', 'ar', 'hinglish', 'bn'})
        self.assertFalse(any(m['key'].endswith('.en') for m in cfg['models']))


# ── Desktop dictation bridge (Whisper Local hotkey app -> Santa server) ─────
@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        from whisper_key.santa.server import make_server
        from whisper_key.santa import desktop_bridge
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeTranscriber(text='आज की meeting ठीक रही')
        self.svc = make_service(self.tmp.name, self.fake)
        self.svc.settings.update({'language': 'hinglish'})
        self.httpd = make_server('127.0.0.1', 0, self.svc)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.bridge = desktop_bridge
        self._old = desktop_bridge.BASE_URL
        desktop_bridge.BASE_URL = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.addCleanup(setattr, desktop_bridge, 'BASE_URL', self._old)

    def test_numpy_audio_round_trip_uses_santa_language(self):
        engine = self.bridge.SantaHttpEngine()
        t = np.arange(16000 * 2) / 16000
        text = engine.transcribe_audio((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        self.assertEqual(text, 'आज की meeting ठीक रही')
        self.assertEqual(self.fake.calls[-1].language, 'hi')     # Hinglish strategy reached backend

    def test_silence_returns_none(self):
        engine = self.bridge.SantaHttpEngine()
        self.assertIsNone(engine.transcribe_audio(np.zeros(16000, dtype=np.float32)))

    @unittest.skipUnless(sys.platform in ('darwin', 'win32'), 'whisper_key.main needs a desktop platform')
    def test_whisper_local_selects_bridge(self):
        from whisper_key import main as wl_main
        engine = wl_main.setup_whisper_engine({'backend': 'santa', 'language': 'auto'}, None, None)
        self.assertIsInstance(engine, self.bridge.SantaHttpEngine)


if __name__ == '__main__':
    unittest.main()
