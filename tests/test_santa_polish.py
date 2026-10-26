# tests/test_santa_polish.py
# Tests for Santa's Wispr-Flow-style features: rule-based clean-up in every
# script, the local Ollama grammar step (against a fake Ollama HTTP server, so
# no model or network is needed), its safety guards, the service wiring, every
# Whisper language, and "0 = no limit" settings.

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from whisper_key.santa import languages, polish, textproc  # noqa: E402
from whisper_key.santa.settings import (validate, DEFAULTS, upload_limit_bytes,  # noqa: E402
                                        duration_limit_seconds, NO_LIMIT)
from test_santa import make_service, make_wav, FakeTranscriber, FakePolisher, HAVE_FW  # noqa: E402


# ── Level "clean": rules, every script ──────────────────────────────────────
class CleanTextTests(unittest.TestCase):
    def test_english_fillers_stutters_and_self_correction(self):
        self.assertEqual(polish.clean_text('Um, so I think we should, uh, meet at 2, actually 3.', 'en'),
                         'So I think we should meet at 3.')
        self.assertEqual(polish.clean_text('I I think the the plan works.', 'en'), 'I think the plan works.')

    def test_filler_as_own_sentence_leaves_no_stray_punctuation(self):
        self.assertEqual(polish.clean_text('I was, um, thinking about the umbrella. Hmm.', 'en'),
                         'I was thinking about the umbrella.')
        self.assertEqual(polish.clean_text('Wait... what? Um, okay.', 'en'), 'Wait... what? Okay.')
        self.assertEqual(polish.clean_text('Version 2.5 is out. Uh. Really.', 'en'), 'Version 2.5 is out. Really.')

    def test_words_containing_filler_letters_are_untouched(self):
        for text in ('The umbrella is here.', 'Hum log kal milenge.', 'Uhuru Park.', 'Ermine fur.'):
            self.assertEqual(polish.clean_text(text, None), text)

    def test_hindi_filler_removed_but_reduplication_kept(self):
        self.assertEqual(polish.clean_text('उम्म, मैं कल धीरे धीरे आऊँगा। हम बार बार मिलेंगे। उम्म।', 'hi'),
                         'मैं कल धीरे धीरे आऊँगा। हम बार बार मिलेंगे।')

    def test_arabic_and_bengali_fillers(self):
        self.assertEqual(polish.clean_text('امم، أنا أفكر في ذلك', 'ar'), 'أنا أفكر في ذلك')
        self.assertEqual(polish.clean_text('উম, আমি আস্তে আস্তে যাব', 'bn'), 'আমি আস্তে আস্তে যাব')

    def test_stutter_rules_are_english_only(self):
        # French "nous nous" is grammatical, not a stutter.
        self.assertEqual(polish.clean_text('Euh, je pense que nous nous sommes trompés.', 'fr'),
                         'Je pense que nous nous sommes trompés.')

    def test_only_a_filler_keeps_what_whisper_heard(self):
        self.assertEqual(polish.clean_text('Um.', 'en'), 'Um.')

    def test_every_combining_mark_survives(self):
        text = 'नमस्ते, आप कैसे हैं? সুপ্রভাত। مَرْحَبًا'
        self.assertEqual(polish.clean_text(text, None), text)


# ── Splitting long text (what makes length unlimited) ────────────────────────
class SplitTests(unittest.TestCase):
    def test_long_text_round_trips_in_bounded_pieces(self):
        text = ('This is a sentence. ' * 400).strip() + '\n\nNew paragraph here. And more.'
        pieces = polish.split_for_model(text, max_chars=500)
        self.assertGreater(len(pieces), 10)
        self.assertTrue(all(len(p) <= 500 for p, _ in pieces))
        self.assertEqual(polish.join_pieces(pieces), text)

    def test_unpunctuated_and_cjk_text_is_hard_split(self):
        for text in ('word ' * 1000, '我们今天开会' * 500):
            pieces = polish.split_for_model(text.strip(), max_chars=300)
            self.assertTrue(all(len(p) <= 300 for p, _ in pieces))
            self.assertEqual(''.join(p for p, _ in pieces).replace(' ', ''), text.replace(' ', ''))

    def test_danda_and_arabic_question_mark_are_sentence_ends(self):
        pieces = polish.split_for_model('मैं घर गया। फिर सो गया। ' * 60 + 'هل أنت هنا؟ نعم.', max_chars=200)
        self.assertTrue(all(p.endswith(('।', '؟', '.')) for p, _ in pieces[:-1]))


# ── Guards on the model's answer ─────────────────────────────────────────────
class GuardTests(unittest.TestCase):
    def test_good_edit_passes(self):
        self.assertIsNone(polish.guard_reason('so i think we should um go', 'So I think we should go.'))
        self.assertIsNone(polish.guard_reason('मैं आज office जा रहा हूँ', 'मैं आज office जा रहा हूँ।'))

    def test_translation_is_caught(self):
        self.assertIn('Devanagari', polish.guard_reason('मैं कल आऊँगा।', 'I will come tomorrow.'))
        self.assertIn('Latin', polish.guard_reason('see you tomorrow at the office', 'कल ऑफिस में मिलते हैं'))

    def test_hinglish_must_stay_mixed(self):
        original = 'आज की meeting में हमने project के बारे में बात की'
        self.assertIsNotNone(polish.guard_reason(original, 'आज की मीटिंग में हमने प्रोजेक्ट के बारे में बात की और फिर चले गए'))

    def test_chat_replies_and_length_changes_are_caught(self):
        self.assertIn('replied', polish.guard_reason('what time is it', "Sure! It's 3 PM."))
        self.assertIn('added', polish.guard_reason('call mom', 'Call mom. Also remember to buy milk, eggs and bread today.'))
        self.assertIn('removed', polish.guard_reason('one two three four five six seven eight nine ten ' * 3, 'One.'))
        self.assertIn('nothing', polish.guard_reason('hello', '  '))

    def test_answer_wrappers_are_stripped(self):
        self.assertEqual(polish._tidy_answer('<think>hmm</think>\n<transcript>Hello there.</transcript>', 'hello there'),
                         'Hello there.')
        self.assertEqual(polish._tidy_answer('"Hello there."', 'hello there'), 'Hello there.')
        self.assertEqual(polish._tidy_answer('"Quoted," he said.', '"quoted" he said'), '"Quoted," he said.')


# ── Level "grammar" against a fake Ollama server ────────────────────────────
class FakeOllama:
    """Minimal stand-in for Ollama's /api/tags and /api/chat."""
    def __init__(self, models=('gemma3:4b',), reply=None):
        self.models, self.requests = list(models), []
        self.reply = reply or (lambda transcript: transcript[:1].upper() + transcript[1:] + '.')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {'models': [{'name': m} for m in owner.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(body)
                if body['model'] not in owner.models:
                    return self._send(404, {'error': f"model '{body['model']}' not found"})
                user = body['messages'][-1]['content']
                transcript = user.split('<transcript>\n', 1)[1].rsplit('\n</transcript>', 1)[0]
                self._send(200, {'message': {'role': 'assistant', 'content': owner.reply(transcript)}})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OllamaPolisherTests(unittest.TestCase):
    def setUp(self):
        self.ollama = FakeOllama()
        self.addCleanup(self.ollama.close)
        self.polisher = polish.OllamaPolisher(self.ollama.url, timeout=5)

    def test_request_is_local_deterministic_and_wrapped(self):
        out = self.polisher.polish('so we meet at three', 'gemma3:4b', 'English', 'Santa, Hiring Right')
        self.assertEqual(out, 'So we meet at three.')
        req = self.ollama.requests[0]
        self.assertEqual(req['options']['temperature'], 0)
        self.assertFalse(req['stream'])
        self.assertIn('Never translate', req['messages'][0]['content'])
        user = req['messages'][1]['content']
        self.assertIn('Language: English', user)
        self.assertIn('Spell these exactly as given: Santa, Hiring Right', user)
        self.assertIn('<transcript>\nso we meet at three\n</transcript>', user)

    def test_long_text_goes_in_pieces_and_comes_back_whole(self):
        text = ' '.join(f'this is sentence number {n}.' for n in range(400))
        out = self.polisher.polish(text, 'gemma3:4b')
        self.assertGreater(len(self.ollama.requests), 3)
        self.assertTrue(all(len(r['messages'][1]['content']) < polish.MAX_CHUNK_CHARS + 100
                            for r in self.ollama.requests))
        self.assertEqual(out.count('his is sentence number'), 400)     # nothing lost or doubled
        self.assertTrue(out.startswith('This is sentence number 0.'))

    def test_status_reports_running_and_model(self):
        st = self.polisher.status('gemma3:4b')
        self.assertTrue(st['running'] and st['model_installed'])
        self.assertFalse(polish.OllamaPolisher(self.ollama.url).status('llama3.1')['model_installed'])

    def test_missing_model_and_stopped_ollama_are_unavailable(self):
        with self.assertRaisesRegex(polish.PolishUnavailable, 'ollama pull qwen2.5:3b'):
            self.polisher.polish('hello', 'qwen2.5:3b')
        stopped = polish.OllamaPolisher('http://127.0.0.1:9', timeout=2)
        with self.assertRaises(polish.PolishUnavailable):
            stopped.polish('hello', 'gemma3:4b')
        self.assertFalse(stopped.status('gemma3:4b')['running'])

    def test_translating_answer_is_rejected(self):
        self.ollama.reply = lambda transcript: 'I will come tomorrow.'
        with self.assertRaises(polish.PolishRejected):
            self.polisher.polish('मैं कल आऊँगा', 'gemma3:4b')

    def test_polish_text_falls_back_to_clean_with_a_note(self):
        self.ollama.reply = lambda transcript: "Sure! Here's your text."
        outcome = polish.polish_text('um, hello there', 'grammar', 'en', polisher=self.polisher)
        self.assertEqual((outcome.text, outcome.level, outcome.requested), ('Hello there', 'clean', 'grammar'))
        self.assertIn('not used', outcome.note)
        self.ollama.reply = lambda transcript: transcript + '.'
        good = polish.polish_text('um, hello there', 'grammar', 'en', polisher=polish.OllamaPolisher(self.ollama.url))
        self.assertEqual((good.text, good.level), ('Hello there.', 'grammar'))

    def test_off_returns_text_untouched(self):
        outcome = polish.polish_text('um  hello', 'off', 'en', polisher=self.polisher)
        self.assertEqual((outcome.text, outcome.level), ('um  hello', 'off'))
        self.assertEqual(self.ollama.requests, [])


# ── Service wiring ───────────────────────────────────────────────────────────
@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class ServicePolishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_job(self, svc, mode='en', source='recording'):
        job = svc.submit(make_wav(), 'r.wav', mode, source=source)
        deadline = time.time() + 30
        while job.state not in ('done', 'error', 'cancelled') and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(job.state, 'done', job.error)
        return job.result

    def test_dictation_is_cleaned_then_corrected_and_raw_is_kept(self):
        polisher = FakePolisher(answer='So we meet at three.')
        svc = make_service(self.tmp.name, FakeTranscriber(text='Um, so we meet at three'), polisher)
        r = self.run_job(svc)
        self.assertEqual(r['text'], 'So we meet at three.')
        self.assertEqual(r['raw_text'], 'Um, so we meet at three')
        self.assertEqual(polisher.calls[0]['text'], 'So we meet at three')     # fillers gone first
        self.assertEqual(polisher.calls[0]['language_name'], 'English')
        self.assertEqual(r['polish']['level'], 'grammar')

    def test_auto_detect_tells_the_model_the_detected_language(self):
        polisher = FakePolisher()
        svc = make_service(self.tmp.name, FakeTranscriber(text='bonjour à tous', detected='fr'), polisher)
        self.run_job(svc, mode='auto')
        self.assertEqual(polisher.calls[0]['language_name'], 'French')

    def test_unavailable_model_falls_back_with_a_note(self):
        svc = make_service(self.tmp.name, FakeTranscriber(text='um, hello there'),
                           FakePolisher(error=polish.PolishUnavailable('Ollama is not running')))
        r = self.run_job(svc)
        self.assertEqual(r['text'], 'Hello there')
        self.assertEqual(r['polish']['level'], 'clean')
        self.assertIn('Ollama is not running', r['polish']['note'])

    def test_files_only_when_asked_and_watch_folder_never(self):
        polisher = FakePolisher(answer='Edited.')
        svc = make_service(self.tmp.name, FakeTranscriber(text='an interview answer'), polisher)
        self.assertEqual(self.run_job(svc, source='upload')['text'], 'An interview answer')
        svc.settings.update({'polish_files': True})
        self.assertEqual(self.run_job(svc, source='upload')['text'], 'Edited.')
        self.assertEqual(self.run_job(svc, source='watch')['text'], 'An interview answer')
        self.assertEqual(len(polisher.calls), 1)

    def test_level_off_and_clean(self):
        polisher = FakePolisher(answer='never used')
        svc = make_service(self.tmp.name, FakeTranscriber(text='um, hello  there'), polisher)
        svc.settings.update({'polish': 'off'})
        self.assertEqual(self.run_job(svc)['text'], 'um, hello  there')
        svc.settings.update({'polish': 'clean'})
        self.assertEqual(self.run_job(svc)['text'], 'Hello there')
        self.assertEqual(polisher.calls, [])

    def test_hinglish_romanization_uses_the_polished_wording(self):
        polisher = FakePolisher(answer='मैं आज office जा रहा हूँ।')
        svc = make_service(self.tmp.name, FakeTranscriber(text='उम्म, मैं आज office जा रहा हूँ'), polisher)
        svc.settings.update({'hinglish_output': 'roman'})
        r = self.run_job(svc, mode='hinglish')
        self.assertEqual(r['text'], 'main aaj office ja raha hoon.')
        self.assertEqual(r['native_text'], 'मैं आज office जा रहा हूँ।')
        self.assertTrue(polisher.calls[0]['language_name'].startswith('Hinglish'))

    def test_status_reports_grammar_model(self):
        svc = make_service(self.tmp.name, FakeTranscriber())
        st = svc.status()['polish']
        self.assertEqual((st['level'], st['model']), ('grammar', 'gemma3:4b'))
        self.assertTrue(st['ollama']['model_installed'])


# ── Every Whisper language ───────────────────────────────────────────────────
class LanguageTests(unittest.TestCase):
    def test_every_whisper_language_is_offered(self):
        codes = set(languages.LANGUAGE_MODES) - {'auto', 'hinglish'}
        try:
            from faster_whisper.tokenizer import _LANGUAGE_CODES
        except ImportError:
            self.skipTest('faster-whisper not installed')
        self.assertEqual(codes, set(_LANGUAGE_CODES))

    def test_each_language_decodes_with_its_own_code(self):
        for code, mode in languages.LANGUAGE_MODES.items():
            if code in ('auto', 'hinglish'):
                continue
            params = languages.resolve_decode_params(code)
            self.assertEqual(params.language, code)
            self.assertEqual(params.task, 'transcribe')

    def test_rtl_and_groups(self):
        modes = languages.LANGUAGE_MODES
        for code in ('ar', 'fa', 'ur', 'he', 'yi', 'ps', 'sd'):
            self.assertTrue(modes[code].rtl, code)
        self.assertFalse(modes['fr'].rtl)
        self.assertEqual([m.key for m in modes.values() if m.group == 'santa'],
                         ['auto', 'en', 'hi', 'ar', 'hinglish', 'bn'])
        self.assertTrue(textproc.is_rtl_dominant('שלום לכולם'))

    def test_script_warnings_for_new_scripts(self):
        self.assertEqual(textproc.script_warning('Привет, как дела?', 'Cyrillic'), '')
        self.assertIn('Expected Cyrillic', textproc.script_warning('Privet, kak dela?', 'Cyrillic'))
        self.assertEqual(textproc.script_warning('Tôi đang ở nhà.', 'Latin'), '')
        self.assertEqual(textproc.dominant_script('வணக்கம் நண்பர்களே'), 'Tamil')

    def test_language_name_for_the_model(self):
        self.assertEqual(languages.language_name('ta'), 'Tamil')
        self.assertEqual(languages.language_name('auto', 'de'), 'German')
        self.assertEqual(languages.language_name('auto'), '')
        self.assertIn('Hinglish', languages.language_name('hinglish'))


# ── Settings ─────────────────────────────────────────────────────────────────
class SettingsTests(unittest.TestCase):
    def test_polish_defaults_and_validation(self):
        self.assertEqual((DEFAULTS['polish'], DEFAULTS['polish_model']), ('grammar', 'gemma3:4b'))
        self.assertEqual(validate({'polish': 'clean'})['polish'], 'clean')
        self.assertEqual(validate({'polish': 'rewrite-everything'})['polish'], 'grammar')
        self.assertEqual(validate({'polish_model': 'qwen2.5:7b-instruct'})['polish_model'], 'qwen2.5:7b-instruct')
        self.assertEqual(validate({'polish_model': 'x; rm -rf ~'})['polish_model'], 'gemma3:4b')
        self.assertEqual(validate({'language': 'ta'})['language'], 'ta')

    def test_zero_means_no_limit(self):
        s = validate({'max_upload_mb': 0, 'max_duration_min': 0})
        self.assertEqual((upload_limit_bytes(s), duration_limit_seconds(s)), (NO_LIMIT, NO_LIMIT))
        s = validate({'max_upload_mb': 5000, 'max_duration_min': 600})
        self.assertEqual((upload_limit_bytes(s), duration_limit_seconds(s)), (5e9, 36000))

    def test_no_limit_accepts_any_size_and_length(self):
        import numpy as np
        from whisper_key.santa import audio_input
        self.assertEqual(audio_input.check_upload('big.mp4', 50_000_000_000, NO_LIMIT), '.mp4')
        decoded = audio_input.check_samples(np.zeros(16000 * 5, dtype=np.float32) + 0.1, NO_LIMIT)
        self.assertAlmostEqual(decoded.duration_s, 5.0, places=1)


if __name__ == '__main__':
    unittest.main()
