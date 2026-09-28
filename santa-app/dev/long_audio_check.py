# santa-app/dev/long_audio_check.py
# Long-audio check: GPU path with pause-based splitting vs the CPU engine, same
# service code as the app, on en_long (36 s) and long_en_x5 (~3 min, synthetic).
import json, os, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))
os.environ['SANTA_HOME'] = tempfile.mkdtemp(prefix='santa-long-')
from bench import cer                                                 # noqa: E402
from whisper_key.santa.accel_cpp import MetalAccelerator              # noqa: E402
from whisper_key.santa.service import TranscriptionService            # noqa: E402

ref_one = json.load(open(os.path.join(HERE, '..', 'eval', 'samples.json'), encoding='utf-8'))
ref_one = next(s['text'] for s in ref_one['samples'] if s['id'] == 'en_long')
cases = [('en_long.wav', ref_one), ('long_en_x5.wav', ' '.join([ref_one] * 5))]
svc = TranscriptionService(accelerator=MetalAccelerator())
svc.preload()
while svc.status()['model']['state'] != 'ready' or (svc.status()['accelerator'] or {}).get('state') != 'ready':
    time.sleep(0.2)
out = []
for engine in ('auto', 'cpu'):
    svc.settings.update({'engine': engine})
    for name, ref in cases:
        data = open(os.path.join(HERE, '..', 'eval', 'synthetic', name), 'rb').read()
        t = time.time()
        r = svc.transcribe_sync(data, name, 'en', timeout=900)['result']
        row = {'engine_setting': engine, 'file': name, 'audio_s': r['audio_seconds'],
               'job_latency_s': round(time.time() - t, 2), 'engine_used': r['engine'], 'cer': cer(ref, r['raw_text'])}
        out.append(row)
        print(json.dumps(row), flush=True)
json.dump(out, open(os.path.join(HERE, 'out', 'long_audio.json'), 'w'), indent=1)
