# santa-app/dev/diarize_check.py
# Real-audio check for speaker separation (santa/speakers.py): runs diarization
# on a recording and prints the turns, the number of speakers found and the
# real-time factor. Optional: --rttm compares with a reference, --voices prints
# same-person vs different-person similarity scores (to tune the match threshold).
#
#   diarize_check.py <audio file> [--speakers N] [--threads N] [--rttm ref.rttm]
#   diarize_check.py --voices alice-1.wav alice-2.wav bob-1.wav …   (person = name before first '-')
#
# Models come from $SANTA_SPEAKER_MODELS if set, else Santa's model folder
# (downloaded there on first use).
import argparse
import itertools
import os
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))

import numpy as np                                                    # noqa: E402
from whisper_key.santa import speakers                                # noqa: E402


def load_audio(path: str) -> np.ndarray:
    """16 kHz mono float32: faster-whisper's decoder if installed, else 16 kHz wav."""
    try:
        from faster_whisper.audio import decode_audio
        return decode_audio(path, sampling_rate=speakers.SAMPLE_RATE).astype(np.float32)
    except ImportError:
        with wave.open(path, 'rb') as w:
            if w.getframerate() != speakers.SAMPLE_RATE or w.getsampwidth() != 2:
                sys.exit('without faster-whisper only 16 kHz 16-bit wav is supported')
            data = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
            return data.reshape(-1, w.getnchannels()).mean(axis=1)


def make_engine(threads: int) -> speakers.SpeakerEngine:
    model_dir = os.environ.get('SANTA_SPEAKER_MODELS') or None
    engine = speakers.SpeakerEngine(num_threads=threads, model_dir=model_dir)
    if not engine.available():
        sys.exit('sherpa-onnx is not installed')
    if not engine.models_present():
        print('downloading speaker models…', flush=True)
        t = time.time()
        engine.ensure_models()
        print(f'  done in {time.time() - t:.1f}s -> {engine.model_dir}')
    return engine


# ── Reference comparison (RTTM) ──────────────────────────────────────────────
def read_rttm(path: str) -> list:
    turns = []
    for line in open(path, encoding='utf-8'):
        parts = line.split()
        if len(parts) >= 8 and parts[0] == 'SPEAKER':
            start, dur = float(parts[3]), float(parts[4])
            turns.append({'start': start, 'end': start + dur, 'speaker': parts[7]})
    return turns


def frame_agreement(reference: list, hypothesis: list, step: float = 0.01) -> float:
    """Share of reference speech frames (single-speaker only) given the right
    speaker, under the best one-to-one mapping of hypothesis to reference ids."""
    end = max([t['end'] for t in reference + hypothesis] + [0.0])
    n = int(end / step) + 1

    def frames(turns):
        ids = sorted({t['speaker'] for t in turns}, key=str)
        grid = np.zeros((len(ids), n), dtype=bool)
        for t in turns:
            grid[ids.index(t['speaker']), int(t['start'] / step):int(t['end'] / step)] = True
        return ids, grid

    ref_ids, ref = frames(reference)
    hyp_ids, hyp = frames(hypothesis)
    single = ref.sum(axis=0) == 1
    total = int(single.sum()) or 1
    best = 0
    for perm in itertools.permutations(range(len(hyp_ids)), min(len(hyp_ids), len(ref_ids))):
        hits = sum(int((ref[r] & hyp[h] & single).sum()) for r, h in enumerate(perm))
        best = max(best, hits)
    return best / total


# ── Modes ────────────────────────────────────────────────────────────────────
def run_diarization(args) -> None:
    engine = make_engine(args.threads)
    audio = load_audio(args.audio)
    seconds = len(audio) / speakers.SAMPLE_RATE
    t = time.time()
    turns = engine.diarize(audio, num_speakers=args.speakers)
    elapsed = time.time() - t
    for turn in turns:
        print(f"{turn['start']:8.2f} – {turn['end']:8.2f}  speaker {turn['speaker']}")
    found = len({turn['speaker'] for turn in turns})
    print(f'audio {seconds:.1f}s | speakers found {found} | turns {len(turns)} | '
          f'diarize {elapsed:.2f}s | RTF {elapsed / max(seconds, 1e-9):.3f} | threads {engine.num_threads}')
    if args.rttm:
        reference = read_rttm(args.rttm)
        print(f"reference speakers {len({t['speaker'] for t in reference})} | "
              f"frame agreement {frame_agreement(reference, turns):.1%}")
    if turns:
        t = time.time()
        vectors = engine.speaker_embeddings(audio, turns)
        print(f'voiceprints for {len(vectors)} speakers in {time.time() - t:.2f}s')


def run_voices(args) -> None:
    engine = make_engine(args.threads)
    vectors = {}
    for path in args.voices:
        vectors[path] = engine.embedding(load_audio(path))
    same, different = [], []
    for a, b in itertools.combinations(vectors, 2):
        person_a = os.path.basename(a).split('-')[0]
        person_b = os.path.basename(b).split('-')[0]
        score = float(np.dot(vectors[a], vectors[b]))
        (same if person_a == person_b else different).append(score)
        print(f'{score:6.3f}  {"same" if person_a == person_b else "diff"}  '
              f'{os.path.basename(a)}  {os.path.basename(b)}')
    if same:
        print(f'same person:      min {min(same):.3f}  mean {np.mean(same):.3f}')
    if different:
        print(f'different people: max {max(different):.3f}  mean {np.mean(different):.3f}')
    print(f'match threshold in speakers.py: {speakers.DEFAULT_MATCH_THRESHOLD}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', nargs='?')
    parser.add_argument('--speakers', type=int, default=0, help='known speaker count (0 = automatic)')
    parser.add_argument('--threads', type=int, default=0)
    parser.add_argument('--rttm', help='reference turns to compare with')
    parser.add_argument('--voices', nargs='+', help='single-speaker clips; person = file name before "-"')
    args = parser.parse_args()
    if args.voices:
        run_voices(args)
    elif args.audio:
        run_diarization(args)
    else:
        parser.error('give an audio file or --voices')


if __name__ == '__main__':
    main()
