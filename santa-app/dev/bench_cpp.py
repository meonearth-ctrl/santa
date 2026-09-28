# santa-app/dev/bench_cpp.py
# Experiment: whisper.cpp (pywhispercpp, Apple Metal GPU) vs faster-whisper (CPU)
# on the same synthetic samples, CER metric and decode parameters as bench.py.
# Text is rebuilt from the raw UTF-8 bytes of all segments (pywhispercpp decodes
# each segment separately, which can split a Devanagari/Arabic character that
# straddles a segment boundary). Results go to santa-app/dev/out/bench.jsonl.
#   python bench_cpp.py --model large-v3-turbo-q5_0 --beam 5

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))

from bench import load_samples, cer, latin_words_kept          # noqa: E402
from whisper_key.santa.audio_input import decode_bytes          # noqa: E402
from whisper_key.santa.languages import resolve_decode_params   # noqa: E402
from whisper_key.santa.romanize import romanize_hinglish        # noqa: E402


def joined_text(model) -> tuple:
    import _pywhispercpp as pw
    ctx = model._ctx
    n = pw.whisper_full_n_segments(ctx)
    raw = b''.join(pw.whisper_full_get_segment_text(ctx, i) for i in range(n))
    per_segment = ''.join(pw.whisper_full_get_segment_text(ctx, i).decode('utf-8', 'replace') for i in range(n))
    return raw.decode('utf-8', 'replace').strip(), per_segment.strip(), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='large-v3-turbo-q5_0')
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--beam', type=int, default=1, help='1 = greedy, >1 = beam search')
    ap.add_argument('--single-segment', action='store_true')
    ap.add_argument('--no-timestamps', action='store_true')
    args = ap.parse_args()

    from pywhispercpp.model import Model
    record = {'backend': 'whisper.cpp', 'model': args.model, 'threads': args.threads, 'beam': args.beam,
              'single_segment': args.single_segment, 'no_timestamps': args.no_timestamps,
              'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'runs': []}
    extra = {}
    if args.beam > 1:
        extra = {'params_sampling_strategy': 1, 'beam_search': {'beam_size': args.beam, 'patience': -1.0}}
    extra['single_segment'] = args.single_segment
    extra['no_timestamps'] = args.no_timestamps
    t0 = time.time()
    model = Model(args.model, n_threads=args.threads, print_progress=False, print_realtime=False,
                  redirect_whispercpp_logs_to=os.path.join(HERE, 'out', 'whispercpp-native.log'), **extra)
    record['load_seconds'] = round(time.time() - t0, 2)
    record['system_info'] = Model.system_info()

    for sample, path in load_samples('synthetic'):
        with open(path, 'rb') as fh:
            decoded = decode_bytes(fh.read(), '.wav', 3600)
        params = resolve_decode_params(sample['mode'], 'hindi_mixed')
        kwargs = {'language': params.language or 'auto', 'initial_prompt': params.initial_prompt or ''}
        start = time.time()
        model.transcribe(decoded.samples, **kwargs)
        secs = round(time.time() - start, 2)
        text, per_seg, nseg = joined_text(model)
        run = {'sample': sample['id'], 'mode': sample['mode'],
               'audio_s': round(decoded.duration_s, 2), 'seconds': secs,
               'rtf': round(secs / decoded.duration_s, 3), 'cer': cer(sample['text'], text),
               'segments': nseg, 'replacement_chars_per_segment_decode': per_seg.count('�'),
               'replacement_chars_joined': text.count('�'),
               'text': text, 'reference': sample['text']}
        if sample['mode'] == 'hinglish':
            run['cer_romanized'] = cer(romanize_hinglish(sample['text']), romanize_hinglish(text))
            run['english_kept_latin'] = latin_words_kept(sample['text'], text)
        record['runs'].append(run)
        print(json.dumps({k: run[k] for k in ('sample', 'seconds', 'cer', 'segments',
                                              'replacement_chars_joined')}, ensure_ascii=False), flush=True)

    # Cancellation check: abort after ~0.3 s on the longest sample.
    longest = max(load_samples('synthetic'), key=lambda sp: os.path.getsize(sp[1]))
    with open(longest[1], 'rb') as fh:
        decoded = decode_bytes(fh.read(), '.wav', 3600)
    deadline = time.time() + 0.3
    start = time.time()
    model.transcribe(decoded.samples, language='en', abort_callback=lambda: time.time() > deadline)
    record['abort_test_seconds'] = round(time.time() - start, 2)
    print('abort test returned after', record['abort_test_seconds'], 's')

    with open(os.path.join(HERE, 'out', 'bench.jsonl'), 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    print('WROTE')


if __name__ == '__main__':
    main()
