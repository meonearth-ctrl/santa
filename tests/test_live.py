# tests/test_live.py
# Live interview mode: the seat tagger port (and its equivalence with Hiring
# Right's JavaScript), cutting the stream at pauses, the rolling .live.json,
# the ?since= feed and the loopback-only HTTP endpoints. Whisper and Silero are
# replaced by fakes (an energy detector stands in for the VAD), so these run in
# seconds; replay a real recording with `santa --replay FILE` to measure latency.

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from whisper_key.santa import live, seat_tagger as st

RATE = 16000
JS_TAGGER = Path(__file__).resolve().parent / 'fixtures' / 'hiring_right_seat_tagger.js'
# Real Hiring Right data, only on the owner's Mac (never committed: it holds candidate names).
HR_NOTES = Path.home() / 'Library/Application Support/Claude/side-session-notes/local_8eae4b36-fb0a-4d37-a3a2-4f4bc2b9edf9'
HR_TRANSCRIPTS = Path.home() / 'Documents/Hiring Right/03_Transcripts'

ROSTER = [{'no': 1, 'name': 'Rose Ancineta'}, {'no': 2, 'name': 'Mark Anthony Cruz'},
          {'no': 3, 'name': 'Ana Santos'}, {'no': 4, 'name': 'Jerome Villanueva'}]


# ── Test helpers ─────────────────────────────────────────────────────────────
def energy_vad(samples, frame=512, threshold=0.01):
    """Stand-in for Silero: frames louder than `threshold` are speech; regions
    are merged across < 300 ms gaps and padded by 200 ms, like the real options."""
    n = len(samples) // frame
    loud = [float(np.sqrt(np.mean(samples[i * frame:(i + 1) * frame] ** 2))) > threshold for i in range(n)]
    regions, start = [], None
    for i, on in enumerate(loud + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            regions.append([start * frame, i * frame])
            start = None
    merged = []
    for a, b in regions:
        if merged and a - merged[-1][1] < 0.3 * RATE:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    pad = int(0.2 * RATE)
    return [{'start': max(0, a - pad), 'end': min(len(samples), b + pad)} for a, b in merged]


def speechy(pattern):
    """[(seconds, loud?)] -> float32 audio: a tone for 'speech', silence otherwise."""
    parts = []
    for seconds, loud in pattern:
        t = np.arange(int(seconds * RATE)) / RATE
        parts.append((0.3 * np.sin(2 * np.pi * 220 * t) if loud else np.zeros_like(t)).astype(np.float32))
    return np.concatenate(parts)


def segs_of(lines):
    """[(start, end, text, speaker)] -> tagger lines."""
    return [{'a': a, 'z': z, 't': t, 's': s} for a, z, t, s in lines]


# A small made-up group interview exercising every cue (no real candidate data).
SYNTHETIC = [
    (0.0, 4.0, 'Good afternoon everyone, welcome to the group interview.', 'Speaker 1'),
    (4.5, 8.0, 'Candidate number two, please start with your introduction.', 'Speaker 1'),
    (8.5, 12.0, 'Thank you sir. I graduated in hospitality management.', 'Speaker 2'),
    (12.5, 16.0, 'I have worked two years in a fast food restaurant.', 'Speaker 2'),
    (17.0, 20.0, 'Number four.', 'Speaker 3'),
    (20.5, 25.0, 'My name is Jerome Villanova, twenty four years old.', 'Speaker 3'),
    (60.0, 62.0, 'Okay, thank you.', 'Speaker 1'),
    (63.0, 66.0, 'Hello, my name is Rose Ansineta from Quezon City.', 'Speaker 4'),
    (66.5, 70.0, 'I love serving guests.', 'Speaker 4'),
    (71.0, 74.0, 'Number three and number one, please discuss together.', 'Speaker 1'),
    (75.0, 78.0, "I'm candidate number three, I think we should plan first.", 'Speaker 5'),
    (78.5, 82.0, 'Yes, I agree with her.', ''),
    (82.5, 85.0, 'Hi, I am Ana Santos.', 'Speaker 5'),
    (86.0, 89.0, 'Candidate 1, what would you add?', 'Speaker 1'),
    (89.5, 93.0, 'I would add a timeline for each task.', 'Speaker 4'),
    (130.0, 133.0, 'This is number seven speaking.', 'Speaker 6'),
    (134.0, 137.0, 'My name is Marc Cruz.', 'Speaker 2'),
    (137.5, 140.0, 'Number five here.', ''),
]


# ── Seat tagger: single-line cues ────────────────────────────────────────────
class SeatCueTests(unittest.TestCase):
    def test_self_cues(self):
        self.assertEqual(st.self_cue("I'm candidate number three, I think"), 3)
        self.assertEqual(st.self_cue('This is number 6 speaking.'), 6)
        self.assertEqual(st.self_cue('Good morning po, number four.'), 4)
        self.assertEqual(st.self_cue('“Number twelve!”'), 12)
        self.assertIsNone(st.self_cue('There were number four tables.'))
        self.assertIsNone(st.self_cue('Number 0.'))

    def test_call_cue_needs_exactly_one_number(self):
        self.assertEqual(st.call_cue('Candidate number two, why this job?'), 2)
        self.assertIsNone(st.call_cue('Number three and number one, discuss.'))
        self.assertIsNone(st.call_cue("I'm number five here"))          # that is a self cue

    def test_name_cue_tolerates_whisper_spelling(self):
        self.assertEqual(st.name_cue('Hello, my name is Rose Ansineta.', ROSTER), 1)
        self.assertEqual(st.name_cue("I'm Jerome, 24 years old", ROSTER), 4)
        self.assertIsNone(st.name_cue('My name is Peter Parker.', ROSTER))
        self.assertIsNone(st.name_cue('I am from Manila', ROSTER))

    def test_numbers_and_ascii_word_boundaries(self):
        self.assertEqual(st.number_of('04'), 4)
        self.assertIsNone(st.number_of('zero'))
        # JS \\b is ASCII-only: a digit glued to an Arabic letter still counts as a boundary.
        self.assertEqual(st.call_cue('candidate number 3ب'), 3)

    def test_parse_roster_forms(self):
        text = '1 Rose Ancineta\n2. Mark Cruz\n#3 - Ana\n\nnotes\n10) Jo'
        self.assertEqual(live.parse_roster(text), [{'no': 1, 'name': 'Rose Ancineta'}, {'no': 2, 'name': 'Mark Cruz'},
                                                   {'no': 3, 'name': 'Ana'}, {'no': 10, 'name': 'Jo'}])
        self.assertEqual(live.parse_roster({'2': 'B', 1: 'A'}), [{'no': 1, 'name': 'A'}, {'no': 2, 'name': 'B'}])
        self.assertEqual(live.parse_roster(None), [])
        self.assertEqual(len(live.roster_or_default([], None)), live.DEFAULT_SEATS)


# ── Seat tagger: whole transcripts ───────────────────────────────────────────
class SeatTaggingTests(unittest.TestCase):
    def test_live_tags_are_forward_only(self):
        """Tagging line by line (live) == tagging the finished transcript."""
        segs = segs_of(SYNTHETIC)
        batch = st.tag_segments(segs, ROSTER)
        tagger = st.SeatTagger(ROSTER)
        one_by_one = [dict(zip(('n', 'how'), tagger.tag(g['a'], g['z'], g['t'], g['s']))) for g in segs]
        self.assertEqual(batch, one_by_one)

    def test_expected_tags_without_speakers(self):
        tags = st.tag_segments([dict(g, s='') for g in segs_of(SYNTHETIC)], ROSTER)
        got = [(t['n'], t['how']) for t in tags]
        self.assertEqual(got[1], ('panel', 'call'))
        self.assertEqual(got[2], (2, 'called'))
        self.assertEqual(got[3], (2, 'carry'))
        self.assertEqual(got[4], (4, 'num'))
        self.assertEqual(got[5], (4, 'name'))
        self.assertEqual(got[6], (None, ''))                  # > 20 s later: no carry
        self.assertEqual(got[7], (1, 'name'))
        self.assertEqual(got[15], (None, ''))                 # seat 7 is not on the roster
        self.assertEqual(got[16], (2, 'name'))
        self.assertEqual(got[17], (2, 'carry'))               # seat 5 isn't either, so the voice carries on

    def test_speaker_map(self):
        segs = segs_of(SYNTHETIC)
        mapping = st.auto_map(segs, ROSTER)
        self.assertEqual(mapping['Speaker 1'], 'panel')
        self.assertEqual(mapping['Speaker 3'], 4)
        self.assertEqual(mapping['Speaker 4'], 1)

    @unittest.skipUnless(shutil.which('node'), 'Node.js not installed')
    def test_matches_hiring_right_javascript_on_synthetic_group(self):
        js = _run_js({'synthetic': {'cands': ROSTER}}, {'synthetic': {'segments': [
            {'start': a, 'end': z, 'text': t, **({'speaker': s} if s else {})} for a, z, t, s in SYNTHETIC]}})
        self._assert_same(js['synthetic'], {'segments': [
            {'start': a, 'end': z, 'text': t, **({'speaker': s} if s else {})} for a, z, t, s in SYNTHETIC]}, ROSTER)

    @unittest.skipUnless(shutil.which('node') and (HR_NOTES / 'transcript-group-rosters.json').exists()
                         and HR_TRANSCRIPTS.exists(), 'real Hiring Right transcripts are only on the owner Mac')
    def test_matches_hiring_right_javascript_on_real_groups(self):
        rosters = json.loads((HR_NOTES / 'transcript-group-rosters.json').read_text(encoding='utf-8'))
        transcripts = {stem: json.loads((HR_TRANSCRIPTS / f'{stem}.json').read_text(encoding='utf-8'))
                       for stem in rosters if (HR_TRANSCRIPTS / f'{stem}.json').exists()}
        js = _run_js(rosters, transcripts)
        self.assertGreater(len(js), 50)
        for stem, transcript in transcripts.items():
            with self.subTest(group=stem):
                self._assert_same(js[stem], transcript, rosters[stem]['cands'])

    def _assert_same(self, js: dict, transcript: dict, cands):
        segs = st.santa_segments(transcript)
        mapping = st.auto_map(segs, cands)
        self.assertEqual(mapping, {k: (v if v == 'panel' else int(v)) for k, v in js['map'].items()})
        self.assertEqual(st.tag_segments(segs, cands, mapping), js['tags'])
        self.assertEqual(st.tag_segments([dict(g, s='') for g in segs], cands), js['live'])


def _run_js(rosters: dict, transcripts: dict) -> dict:
    """Run the JavaScript tagger on the same transcripts: speaker map, tags with
    speakers, and tags with speakers removed (what live mode sees)."""
    script = """
const T0 = require(process.argv[1]); const input = JSON.parse(require('fs').readFileSync(0, 'utf8')); const out = {};
for (const [stem, tr] of Object.entries(input.transcripts)) {
  const cands = input.rosters[stem].cands; const T = T0.parseSanta(stem + '.json', JSON.stringify(tr));
  T.map = T0.autoMap(T.segs, cands);
  out[stem] = { map: T.map, tags: T0.tagSegs(T, cands), live: T0.tagSegs({ segs: T.segs.map((g) => ({ ...g, s: '' })) }, cands) };
}
process.stdout.write(JSON.stringify(out));
"""
    done = subprocess.run(['node', '-e', script, str(JS_TAGGER)], input=json.dumps(
        {'rosters': rosters, 'transcripts': transcripts}), capture_output=True, text=True, timeout=120, check=True)
    return json.loads(done.stdout)


# ── Endpointer: cutting at pauses ────────────────────────────────────────────
class EndpointerTests(unittest.TestCase):
    def _cut(self, audio, block=1600):
        ep = live.Endpointer(vad=energy_vad)
        pieces = []
        for pos in range(0, len(audio), block):
            pieces += ep.feed(audio[pos:pos + block])
        return pieces, ep.flush()

    def test_cuts_at_pauses_after_min_length(self):
        # 3 s speech, short gap, 3 s speech, 1.2 s pause, 4 s speech, end.
        audio = speechy([(3, True), (0.6, False), (3, True), (1.2, False), (4, True), (0.5, False)])
        pieces, rest = self._cut(audio)
        self.assertEqual(len(pieces), 1)                       # the 0.6 s gap is inside piece 1
        start, samples = pieces[0]
        self.assertLess(start, 0.3 * RATE)
        self.assertAlmostEqual(len(samples) / RATE, 6.6 + 0.2 + 0.2, delta=0.4)
        self.assertEqual(len(rest), 1)                         # the last 4 s come out on flush

    def test_short_answer_after_long_pause(self):
        audio = speechy([(1.0, True), (2.0, False)])           # "Number four." then silence
        pieces, _ = self._cut(audio)
        self.assertEqual(len(pieces), 1)

    def test_long_speech_is_cut_by_max_length(self):
        pattern = [(2.5, True), (0.35, False)] * 12             # 34 s of talk with tiny gaps
        pieces, rest = self._cut(speechy(pattern))
        lengths = [len(s) / RATE for _, s in pieces + rest]
        self.assertTrue(all(n <= live.MAX_PIECE_S + 0.5 for n in lengths), lengths)
        self.assertGreaterEqual(len(lengths), 3)
        # Pieces follow each other without overlap.
        ends = [a + len(s) for a, s in pieces + rest]
        starts = [a for a, _ in pieces + rest]
        self.assertTrue(all(b >= e for e, b in zip(ends, starts[1:])))

    def test_silence_only_produces_nothing(self):
        pieces, rest = self._cut(np.zeros(RATE * 20, dtype=np.float32))
        self.assertEqual(pieces + rest, [])


# ── Live session: rolling file and feed ──────────────────────────────────────
class ListSource:
    """Pushes prepared audio as fast as possible, like a replay at high speed."""
    kind, speed = 'replay', 1.0

    def __init__(self, samples):
        self.samples = samples

    def start(self, on_audio, on_end):
        def run():
            for pos in range(0, len(self.samples), 1600):
                on_audio(self.samples[pos:pos + 1600])
            on_end()
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        pass


def scripted_transcriber(lines):
    """Each call returns the next line, as one segment spanning the piece."""
    queue_ = list(lines)

    def transcribe(samples, language):
        text = queue_.pop(0) if queue_ else ''
        return {'segments': [{'start': 0.0, 'end': round(len(samples) / RATE, 2), 'text': ' ' + text}],
                'detected_language': 'en', 'engine': 'fake engine'}
    return transcribe


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_rolling_file_schema_seats_and_feed(self):
        audio = speechy([(6, True), (1.2, False), (1.5, True), (1.5, False), (6, True), (1, False)])
        session = live.LiveSession(
            '2026-11-03_MNL_G4', ListSource(audio),
            scripted_transcriber(['Candidate number two, introduce yourself.', 'Hello, number two.',
                                  'I worked in a cafe for two years.']),
            self.tmp.name, 'en', ROSTER, audio_path=os.path.join(self.tmp.name, 'audio', 'g4.wav'), vad=energy_vad)
        session.start()
        self.assertTrue(session.wait(timeout=10))
        path = os.path.join(self.tmp.name, '2026-11-03_MNL_G4.live.json')
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        for key in ('source', 'model', 'engine', 'language', 'audio_seconds', 'segments'):
            self.assertIn(key, data)
        self.assertTrue(data['live'])
        self.assertEqual(data['state'], 'stopped')
        self.assertEqual([s.get('seat') for s in data['segments']], ['panel', 2, 2])
        self.assertEqual([s['seat_how'] for s in data['segments']], ['call', 'num', 'carry'])
        self.assertAlmostEqual(data['audio_seconds'], len(audio) / RATE, delta=0.1)
        self.assertGreater(data['segments'][1]['start'], data['segments'][0]['end'])   # file times, not piece times
        self.assertEqual(data['latency_seconds']['segment']['count'], 3)
        self.assertFalse(any(name.endswith('.part') for name in os.listdir(self.tmp.name)))
        with wave.open(os.path.join(self.tmp.name, 'audio', 'g4.wav')) as w:   # audio kept locally
            self.assertAlmostEqual(w.getnframes() / RATE, len(audio) / RATE, delta=0.1)
        # The feed returns only lines ending after `since`, and where to continue.
        first = live.feed(data)
        self.assertEqual(len(first['segments']), 3)
        later = live.feed(data, data['segments'][0]['end'])
        self.assertEqual([s['text'] for s in later['segments']], [s['text'] for s in data['segments'][1:]])
        self.assertEqual(live.feed(data, later['next_since'])['segments'], [])

    def test_bad_labels_rejected(self):
        for label in ('', '../x', '.hidden', 'a/b', 'x' * 81):
            with self.assertRaises(live.LiveError):
                live.check_label(label)


# ── HTTP: start, feed, stop, replay ──────────────────────────────────────────
try:
    import faster_whisper  # noqa: F401
    HAVE_FW = True
except ImportError:
    HAVE_FW = False


@unittest.skipUnless(HAVE_FW, 'faster-whisper not installed (needed to decode audio)')
class LiveServerTests(unittest.TestCase):
    def setUp(self):
        from test_santa import make_service, FakeTranscriber, make_wav  # noqa: F401
        from whisper_key.santa.server import make_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = make_service(self.tmp.name, FakeTranscriber(text='Number three here.'))
        self.svc.settings.update({'export_dir': os.path.join(self.tmp.name, 'out')})
        self.svc.live = live.LiveManager(self.svc, os.path.join(self.tmp.name, 'live'))
        self.svc.live.vad = energy_vad
        self.httpd = make_server('127.0.0.1', 0, self.svc)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None, headers=None):
        h = {'X-Santa': '1'}
        h.update(headers or {})
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_replay_feed_and_offline_pass(self):
        wav = os.path.join(self.tmp.name, 'G7.wav')
        audio = speechy([(2, True), (1.5, False), (6, True), (1.2, False)])
        with wave.open(wav, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes((audio * 32767).astype('<i2').tobytes())
        status, body = self.req('POST', '/api/live/replay', {'path': wav, 'label': 'G7_live', 'speed': 50,
                                                             'roster': '3 Ana Santos', 'offline': True})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.req('POST', '/api/live/replay', {'path': wav, 'label': 'G8'})[0], 409)   # one at a time
        deadline = time.time() + 15
        while time.time() < deadline:
            status, feed = self.req('GET', '/api/live/G7_live?since=0')
            if feed['state'] == 'stopped' and feed['offline_job']:
                break
            time.sleep(0.1)
        self.assertEqual(feed['state'], 'stopped')
        self.assertEqual([s['seat'] for s in feed['segments']], [3, 3])
        self.assertEqual(self.req('GET', f'/api/live/G7_live?since={feed["next_since"]}')[1]['segments'], [])
        job = self.svc.get_job(feed['offline_job'])
        while job.state not in ('done', 'error'):
            time.sleep(0.05)
        self.assertEqual(job.state, 'done', job.error)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, 'out', 'G7_live.json')))
        self.assertEqual(self.req('GET', '/api/live/nope')[0], 404)
        self.assertEqual(self.req('POST', '/api/live/replay', {'path': wav, 'label': 'G7_live'})[0], 409)  # exists

    def test_feed_reads_old_sessions_from_disk_and_guards(self):
        out = os.path.join(self.tmp.name, 'out')
        os.makedirs(out)
        Path(out, 'OLD.live.json').write_text(json.dumps({'source': 'OLD', 'live': True, 'segments': [
            {'start': 0, 'end': 2, 'text': 'a'}, {'start': 3, 'end': 4, 'text': 'b', 'seat': 1}]}))
        status, feed = self.req('GET', '/api/live/OLD?since=2')
        self.assertEqual((status, [s['text'] for s in feed['segments']]), (200, ['b']))
        self.assertEqual(self.req('GET', '/api/live/OLD?since=x')[0], 400)
        self.assertEqual(self.req('POST', '/api/live/stop', {})[0], 409)
        self.assertEqual(self.req('POST', '/api/live/start', {'label': '../x'})[0], 400)
        # State changes need the X-Santa header; another host name is refused (DNS rebinding).
        self.assertEqual(self.req('POST', '/api/live/stop', {}, headers={'X-Santa': ''})[0], 403)
        self.assertEqual(self.req('GET', '/api/live', headers={'Host': 'evil.example'})[0], 421)


if __name__ == '__main__':
    unittest.main()
