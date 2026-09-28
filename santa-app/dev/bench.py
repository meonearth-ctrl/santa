# santa-app/dev/bench.py
# Benchmark + recognition check for Santa on this computer. Uses the same code
# path as the app (languages.resolve_decode_params -> models.run_transcription
# -> textproc/romanize). One model per process so "cold start" is honest.
#
#   python bench.py --model large-v3-turbo --set synthetic [--threads 0]
#
# Results are appended to santa-app/dev/out/bench.jsonl. CER = character error
# rate after light normalisation (case, punctuation, spacing). For Hinglish we
# also compare both sides after romanization, so a correct word written in the
# "other" script is not counted as wrong twice.

import argparse
import json
import os
import re
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(os.path.dirname(APP), 'src'))

from whisper_key.santa import textproc                                   # noqa: E402
from whisper_key.santa.audio_input import decode_bytes                   # noqa: E402
from whisper_key.santa.languages import resolve_decode_params, HINGLISH_STRATEGIES  # noqa: E402
from whisper_key.santa.models import ModelManager, run_transcription, cached_model_path, CATALOG  # noqa: E402
from whisper_key.santa.romanize import romanize_hinglish                 # noqa: E402

_PUNCT = re.compile(r"[\s\.,!?;:\"'“”‘’()\-–—،؛؟।॥]+")


def norm(s: str) -> str:
    s = unicodedata.normalize('NFC', s or '').lower()
    s = re.sub(r'(?<=\d),(?=\d{3})', '', s)          # 12,500 -> 12500
    return _PUNCT.sub(' ', s).strip()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    return round(edit_distance(r, h) / max(1, len(r)), 3)


def latin_words_kept(ref: str, hyp: str) -> float:
    """Share of the reference's Latin-script (English) words that appear in Latin in the output."""
    words = [w.lower() for w in re.findall(r'[A-Za-z]+', ref)]
    if not words:
        return None
    hyp_words = {w.lower() for w in re.findall(r'[A-Za-z]+', hyp)}
    return round(sum(w in hyp_words for w in words) / len(words), 2)


def load_samples(which: str):
    if which == 'synthetic':
        meta = json.load(open(os.path.join(APP, 'eval', 'samples.json'), encoding='utf-8'))['samples']
        folder = os.path.join(APP, 'eval', 'synthetic')
    else:
        folder = os.path.join(APP, 'eval', 'human')
        meta = json.load(open(os.path.join(folder, 'manifest.json'), encoding='utf-8'))['samples']
    for s in meta:
        path = os.path.join(folder, s.get('file') or s['id'] + '.wav')
        if os.path.exists(path):
            yield s, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', required=True, choices=list(CATALOG))
    ap.add_argument('--set', default='synthetic', choices=['synthetic', 'human'])
    ap.add_argument('--threads', type=int, default=0)
    ap.add_argument('--beam', type=int, default=5)
    ap.add_argument('--speed-only', action='store_true', help='only each sample in its own mode')
    args = ap.parse_args()

    out_path = os.path.join(HERE, 'out', 'bench.jsonl')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    record = {'model': args.model, 'set': args.set, 'threads': args.threads, 'beam': args.beam,
              'compute_type': 'int8', 'device': 'cpu', 'started': time.strftime('%Y-%m-%d %H:%M:%S'),
              'was_cached': cached_model_path(args.model) is not None, 'runs': []}

    mm = ModelManager('int8', args.threads)
    t0 = time.time()
    mm.ensure_loading(args.model)
    model = mm.wait_ready(args.model, timeout=3600)
    record['load_seconds_incl_download'] = round(time.time() - t0, 2)
    record['load_seconds'] = mm.get_status().get('load_seconds')

    for sample, path in load_samples(args.set):
        with open(path, 'rb') as fh:
            decoded = decode_bytes(fh.read(), os.path.splitext(path)[1], 3600)
        own = sample['mode']
        configs = [(own, None)]
        if not args.speed_only:
            if own == 'hinglish':
                configs = [('hinglish', k) for k in HINGLISH_STRATEGIES] + [('hi', None)]
            else:
                configs.append(('auto', None))
        first = True
        for mode, strategy in configs:
            params = resolve_decode_params(mode, strategy or 'hindi_mixed')
            raw = run_transcription(model, decoded.samples, params, beam_size=args.beam, vad_filter=True)
            text = textproc.join_segments(s['text'] for s in raw['segments'])
            ref = sample['text']
            run = {
                'sample': sample['id'], 'mode': mode, 'strategy': strategy,
                'audio_s': round(decoded.duration_s, 2), 'seconds': raw['transcribe_seconds'],
                'rtf': round(raw['transcribe_seconds'] / decoded.duration_s, 3),
                'first_on_sample': first,
                'detected': raw['detected_language'], 'prob': raw['language_probability'],
                'cer': cer(ref, text), 'text': text, 'reference': ref,
            }
            if own == 'hinglish':
                run['cer_romanized'] = cer(romanize_hinglish(ref), romanize_hinglish(text))
                run['english_kept_latin'] = latin_words_kept(ref, text)
            first = False
            record['runs'].append(run)
            print(json.dumps({k: run[k] for k in ('sample', 'mode', 'strategy', 'seconds', 'cer')},
                             ensure_ascii=False), flush=True)
    record['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(out_path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    print('WROTE', out_path)


if __name__ == '__main__':
    main()
