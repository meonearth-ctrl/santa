# tests/test_speakers.py
# Tests for speaker separation (santa/speakers.py). The pure-logic tests need no
# models: matching turns to segments, labels, dialogue text, the known-voice
# book. One integration test runs real diarization, only when sherpa-onnx is
# installed and $SANTA_SPEAKER_MODELS points at already-downloaded models.

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from whisper_key.santa import speakers
from whisper_key.santa.speakers import (VoiceBook, assign_speakers, format_dialogue, label_speakers,
                                        diarize_transcript)


def seg(start, end, text, **extra):
    return dict({'start': start, 'end': end, 'text': text}, **extra)


def turn(start, end, speaker):
    return {'start': start, 'end': end, 'speaker': speaker}


def unit(*values):
    vector = np.asarray(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


# ── assign_speakers ──────────────────────────────────────────────────────────
class AssignSpeakersTests(unittest.TestCase):
    def test_largest_overlap_wins(self):
        turns = [turn(0.0, 5.0, 0), turn(5.0, 12.0, 1)]
        out = assign_speakers([seg(0.5, 4.0, ' a'), seg(4.5, 11.0, ' b'), seg(3.0, 6.0, ' c')], turns)
        self.assertEqual([s['speaker'] for s in out], [0, 1, 0])

    def test_sums_overlap_across_turns_of_same_speaker(self):
        turns = [turn(0.0, 1.0, 1), turn(1.0, 2.2, 0), turn(2.2, 3.0, 1), turn(3.0, 4.0, 1)]
        out = assign_speakers([seg(0.0, 4.0, 'x')], turns)
        self.assertEqual(out[0]['speaker'], 1)

    def test_nearest_turn_when_no_overlap(self):
        turns = [turn(0.0, 2.0, 0), turn(10.0, 12.0, 1)]
        out = assign_speakers([seg(2.5, 3.0, 'near 0'), seg(8.5, 9.5, 'near 1')], turns)
        self.assertEqual([s['speaker'] for s in out], [0, 1])

    def test_copies_not_mutates_and_keeps_order(self):
        segments = [seg(5.0, 6.0, 'later'), seg(0.0, 1.0, 'first')]
        out = assign_speakers(segments, [turn(0.0, 10.0, 3)])
        self.assertNotIn('speaker', segments[0])
        self.assertEqual([s['text'] for s in out], ['later', 'first'])

    def test_no_turns_means_one_speaker(self):
        out = assign_speakers([seg(0, 1, 'a')], [])
        self.assertEqual(out[0]['speaker'], 0)

    def test_segment_spanning_two_speakers_keeps_majority_and_is_flagged(self):
        turns = [turn(0.0, 3.0, 0), turn(3.0, 5.0, 1)]      # speaker 1: 2 s of 5 s (40%)
        out = assign_speakers([seg(0.0, 5.0, 'both')], turns)
        self.assertEqual(out[0]['speaker'], 0)
        self.assertNotIn('mixed_speakers', out[0])           # exactly 40% is not "more than"
        turns = [turn(0.0, 2.8, 0), turn(2.8, 5.0, 1)]      # speaker 1: 2.2 s (44%)
        out = assign_speakers([seg(0.0, 5.0, 'both')], turns)
        self.assertEqual(out[0]['speaker'], 0)
        self.assertTrue(out[0]['mixed_speakers'])
        turns = [turn(0.0, 1.6, 0), turn(1.6, 3.0, 1)]      # 47% but only 1.4 s
        out = assign_speakers([seg(0.0, 3.0, 'short')], turns)
        self.assertNotIn('mixed_speakers', out[0])

    def test_long_turn_starting_well_before_segment_is_found(self):
        turns = [turn(0.0, 100.0, 0), turn(50.0, 51.0, 1), turn(60.0, 61.0, 1)]
        out = assign_speakers([seg(80.0, 82.0, 'deep inside turn 0')], turns)
        self.assertEqual(out[0]['speaker'], 0)


# ── label_speakers / format_dialogue ─────────────────────────────────────────
class LabelTests(unittest.TestCase):
    def test_numbered_by_first_appearance(self):
        segments = [seg(0, 2, 'a', speaker=3), seg(2, 3, 'b', speaker=0), seg(3, 5, 'c', speaker=3)]
        out, people = label_speakers(segments)
        self.assertEqual([s['speaker'] for s in out], ['Speaker 1', 'Speaker 2', 'Speaker 1'])
        self.assertEqual([p['id'] for p in people], ['Speaker 1', 'Speaker 2'])
        self.assertEqual(people[0], {'id': 'Speaker 1', 'name': 'Speaker 1', 'known': False, 'seconds': 4.0})
        self.assertEqual(people[1]['seconds'], 1.0)

    def test_known_names_replace_labels(self):
        segments = [seg(0, 1, 'a', speaker=1), seg(1, 2, 'b', speaker=0)]
        out, people = label_speakers(segments, {0: 'Amin'})
        self.assertEqual([s['speaker'] for s in out], ['Speaker 1', 'Amin'])
        self.assertEqual(out[1]['speaker_id'], 'Speaker 2')
        self.assertEqual(people[1], {'id': 'Speaker 2', 'name': 'Amin', 'known': True, 'seconds': 1.0})

    def test_dialogue_merges_consecutive_turns(self):
        segments = [seg(0, 1, ' Hello there.', speaker='Speaker 1'),
                    seg(1, 2, ' How are you?  ', speaker='Speaker 1'),
                    seg(2, 3, ' Fine.', speaker='Speaker 2'),
                    seg(3, 4, '   ', speaker='Speaker 1'),
                    seg(4, 5, ' Thanks.', speaker='Speaker 2')]
        self.assertEqual(format_dialogue(segments),
                         'Speaker 1: Hello there. How are you?\n\nSpeaker 2: Fine. Thanks.')

    def test_dialogue_keeps_unicode_byte_identical(self):
        hindi = 'आपका नाम क्या है?'
        arabic = 'ما هو اسمك؟'
        bengali = 'আমার নাম শৌভিক।'
        segments = [seg(0, 1, ' ' + hindi, speaker='Speaker 1'),
                    seg(1, 2, ' ' + arabic, speaker='Speaker 2'),
                    seg(2, 3, bengali + ' ', speaker='Speaker 2')]
        text = format_dialogue(segments)
        self.assertEqual(text, f'Speaker 1: {hindi}\n\nSpeaker 2: {arabic} {bengali}')
        for original in (hindi, arabic, bengali):
            self.assertIn(original.encode('utf-8'), text.encode('utf-8'))


# ── VoiceBook ────────────────────────────────────────────────────────────────
class VoiceBookTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='santa-voices-')
        self.book = VoiceBook(self.home)

    def test_enroll_list_hides_vectors(self):
        self.book.enroll('  Amin  Mohammed ', unit(1, 0, 0), 20)
        listing = self.book.list()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['name'], 'Amin Mohammed')
        self.assertEqual(set(listing[0]), {'name', 'seconds', 'created'})

    def test_name_validation(self):
        for bad in ('', '   ', 'x' * 41):
            with self.assertRaises(ValueError):
                self.book.enroll(bad, unit(1, 0), 5)
        self.book.enroll('x' * 40, unit(1, 0), 5)

    def test_reenroll_averages_by_seconds(self):
        self.book.enroll('Amin', unit(1, 0), 10)
        self.book.enroll('amin', unit(0, 1), 10)
        listing = self.book.list()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['seconds'], 20.0)
        stored = json.load(open(os.path.join(self.home, 'voices.json')))['voices'][0]['embedding']
        np.testing.assert_allclose(stored, unit(1, 1), atol=1e-5)

    def test_delete_and_clear(self):
        self.book.enroll('A', unit(1, 0), 5)
        self.book.enroll('B', unit(0, 1), 5)
        self.assertTrue(self.book.delete('a'))
        self.assertFalse(self.book.delete('nobody'))
        self.assertEqual([v['name'] for v in self.book.list()], ['B'])
        self.book.clear()
        self.assertEqual(self.book.list(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, 'voices.json')))

    def test_file_is_owner_only(self):
        self.book.enroll('A', unit(1, 0), 5)
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, 'voices.json')).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'voices.json.tmp')))

    def test_match_threshold(self):
        self.book.enroll('A', unit(1, 0, 0), 5)
        close = unit(1, 0.3, 0)       # cos ≈ 0.96
        far = unit(0.3, 1, 0)         # cos ≈ 0.29
        self.assertEqual(self.book.match({0: close, 1: far}), {0: 'A'})
        self.assertEqual(self.book.match({0: close}, threshold=0.99), {})

    def test_match_each_name_once_best_score_first(self):
        self.book.enroll('A', unit(1, 0, 0), 5)
        self.book.enroll('B', unit(0, 1, 0), 5)
        embeddings = {0: unit(1, 0.2, 0),     # A 0.98
                      1: unit(1, 0.5, 0),     # A 0.89, B 0.45
                      2: unit(0.1, 1, 0)}     # B 0.99
        self.assertEqual(self.book.match(embeddings), {0: 'A', 2: 'B'})

    def test_ignores_voiceprints_from_another_model_or_size(self):
        self.book.enroll('A', unit(1, 0, 0), 5)
        self.assertEqual(self.book.match({0: unit(1, 0)}), {})
        path = os.path.join(self.home, 'voices.json')
        data = json.load(open(path))
        data['voices'][0]['model'] = 'old-model'
        json.dump(data, open(path, 'w'))
        self.assertEqual(self.book.match({0: unit(1, 0, 0)}), {})

    def test_corrupt_file_reads_as_empty(self):
        with open(os.path.join(self.home, 'voices.json'), 'w') as fh:
            fh.write('{not json')
        self.assertEqual(self.book.list(), [])


# ── diarize_transcript with a fake engine ────────────────────────────────────
class FakeEngine:
    def __init__(self, turns, vectors):
        self.turns, self.vectors = turns, vectors

    def diarize(self, samples, num_speakers=0, cancel_event=None, on_progress=None):
        return list(self.turns)

    def speaker_embeddings(self, samples, turns):
        return dict(self.vectors)


class DiarizeTranscriptTests(unittest.TestCase):
    def test_end_to_end_with_known_voice(self):
        home = tempfile.mkdtemp(prefix='santa-voices-')
        book = VoiceBook(home)
        book.enroll('Shouvik', unit(0, 1), 30)
        engine = FakeEngine([turn(0, 4, 0), turn(4, 9, 1), turn(9, 12, 0)], {0: unit(1, 0), 1: unit(0.05, 1)})
        segments = [seg(0.0, 3.8, ' Welcome.'), seg(4.1, 8.7, ' Thank you.'), seg(9.2, 11.5, ' Shall we start?')]
        result = diarize_transcript(engine, np.zeros(16000 * 12, np.float32), segments, voicebook=book)
        self.assertEqual([s['speaker'] for s in result['segments']], ['Speaker 1', 'Shouvik', 'Speaker 1'])
        self.assertEqual(result['text'], 'Speaker 1: Welcome.\n\nShouvik: Thank you.\n\nSpeaker 1: Shall we start?')
        self.assertEqual([p['name'] for p in result['speakers']], ['Speaker 1', 'Shouvik'])
        self.assertEqual([t['speaker'] for t in result['turns']], ['Speaker 1', 'Speaker 2', 'Speaker 1'])
        self.assertIn('diarize_seconds', result)
        json.dumps(result)     # must be JSON-safe for the local API


class EngineWithoutModelsTests(unittest.TestCase):
    def test_status_off_before_download(self):
        engine = speakers.SpeakerEngine(home=tempfile.mkdtemp(prefix='santa-spk-'))
        self.assertEqual(engine.status['state'], 'off')
        self.assertFalse(engine.models_present())

    def test_voiceprint_turn_selection_caps_audio(self):
        turns = [turn(0, 20, 0), turn(20, 45, 0), turn(45, 45.5, 0), turn(50, 50.4, 1)]
        picked = speakers._voiceprint_turns(turns)
        self.assertEqual(picked[0], [(20.0, 45.0), (0.0, 5.0)])     # longest first, 30 s cap
        self.assertEqual(picked[1], [(50.0, 50.4)])                 # only short turn: still used


# ── Real models (opt-in) ─────────────────────────────────────────────────────
MODELS = os.environ.get('SANTA_SPEAKER_MODELS', '')


@unittest.skipUnless(speakers.SpeakerEngine.available() and MODELS and os.path.isfile(
    os.path.join(MODELS, speakers.EMBEDDING_FILE)), 'needs sherpa-onnx and $SANTA_SPEAKER_MODELS')
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = speakers.SpeakerEngine(home=tempfile.mkdtemp(prefix='santa-spk-'), model_dir=MODELS)

    def test_embedding_is_normalised_and_diarize_runs(self):
        rng = np.random.default_rng(0)
        t = np.arange(16000 * 3) / 16000
        voice = (0.2 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
        voice += 0.01 * rng.standard_normal(voice.size).astype(np.float32)
        vector = self.engine.embedding(voice)
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=4)
        turns = self.engine.diarize(np.tile(voice, 3))
        self.assertEqual(turns, sorted(turns, key=lambda x: x['start']))
        self.assertEqual(self.engine.status['state'], 'ready')

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(speakers.DiarizationCancelled):
            self.engine.diarize(np.zeros(16000 * 5, np.float32), cancel_event=cancel)

    def test_cancel_during_run_discards_result(self):
        cancel = threading.Event()
        progress = []

        def on_progress(fraction):
            progress.append(fraction)
            cancel.set()          # user presses cancel once the run has started
        with self.assertRaises(speakers.DiarizationCancelled):
            self.engine.diarize(np.zeros(16000 * 5, np.float32), cancel_event=cancel, on_progress=on_progress)
        self.assertEqual(progress[0], 0.0)


if __name__ == '__main__':
    unittest.main()
