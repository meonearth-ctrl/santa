# santa-app/dev/e2e_service.py
# End-to-end check of the REAL Santa service on this Mac (no fakes): settings ->
# job queue -> decode -> GPU fast path or CPU engine -> post-processing.
# Measures cold start (process start to both engines ready) and per-clip job
# latency (submit -> result), for engine=auto and engine=cpu, and verifies:
#   * the model is loaded once and reused (repeated jobs, no reload)
#   * language choice reaches the backend (detected language)
#   * silent / garbage input give clear errors
#   * Hinglish romanized output keeps the raw transcript
# Uses a throw-away SANTA_HOME so your real settings/history are untouched.

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))
os.environ['SANTA_HOME'] = tempfile.mkdtemp(prefix='santa-e2e-')

from bench import load_samples, cer                                 # noqa: E402
from whisper_key.santa.accel_cpp import MetalAccelerator            # noqa: E402
from whisper_key.santa.service import TranscriptionService          # noqa: E402
from whisper_key.santa.settings import SettingsStore                # noqa: E402

T0 = time.time()
settings = SettingsStore()
svc = TranscriptionService(settings=settings, accelerator=MetalAccelerator())
svc.preload()
while True:
    st = svc.status()
    m_ok = st['model']['state'] in ('ready', 'error')
    a_ok = st['accelerator'] is None or st['accelerator']['state'] in ('ready', 'error', 'unavailable')
    if m_ok and a_ok:
        break
    time.sleep(0.1)
report = {'cold_start_seconds': round(time.time() - T0, 2), 'status_after_start': svc.status(), 'runs': [], 'checks': {}}
print('cold start', report['cold_start_seconds'], json.dumps(svc.status(), ensure_ascii=False))

load_calls_before = svc.models.get_status().get('load_seconds')
for engine in ('auto', 'cpu'):
    settings.update({'engine': engine})
    for sample, path in load_samples('synthetic'):
        data = open(path, 'rb').read()
        t = time.time()
        job = svc.transcribe_sync(data, os.path.basename(path), sample['mode'], timeout=600)
        latency = round(time.time() - t, 2)
        r = job.get('result') or {}
        run = {'engine_setting': engine, 'sample': sample['id'], 'mode': sample['mode'],
               'state': job['state'], 'job_latency_s': latency, 'audio_s': r.get('audio_seconds'),
               'engine_used': r.get('engine'), 'detected': r.get('detected_language'),
               'cer': cer(sample['text'], r.get('raw_text', '')), 'text': r.get('text'),
               'script_warning': r.get('script_warning')}
        report['runs'].append(run)
        print(json.dumps({k: run[k] for k in ('engine_setting', 'sample', 'job_latency_s', 'engine_used', 'cer')},
                         ensure_ascii=False), flush=True)

# Same model object reused: load_seconds unchanged => no reload happened.
report['checks']['model_not_reloaded'] = svc.models.get_status().get('load_seconds') == load_calls_before

# Hinglish romanized output keeps raw.
settings.update({'engine': 'auto', 'hinglish_output': 'roman'})
s, p = next((s, p) for s, p in load_samples('synthetic') if s['id'] == 'hinglish_meeting')
r = svc.transcribe_sync(open(p, 'rb').read(), 'h.wav', 'hinglish', timeout=300)['result']
report['checks']['hinglish_roman'] = {'text': r['text'], 'raw_text': r['raw_text']}
settings.update({'hinglish_output': 'native'})

# Silent and garbage inputs.
import io, wave  # noqa: E401,E402
buf = io.BytesIO()
with wave.open(buf, 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\x00\x00' * 32000)
report['checks']['silent'] = svc.transcribe_sync(buf.getvalue(), 'silence.wav', 'en', timeout=60).get('error')
report['checks']['garbage'] = svc.transcribe_sync(b'not audio' * 300, 'x.mp3', 'en', timeout=60).get('error')
try:
    svc.submit(b'abc', 'notes.docx', 'en')
except Exception as exc:
    report['checks']['bad_extension'] = getattr(exc, 'message', str(exc))

out = os.path.join(HERE, 'out', 'e2e.json')
with open(out, 'w', encoding='utf-8') as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)
print(json.dumps(report['checks'], ensure_ascii=False, indent=1))
import shutil  # noqa: E402
shutil.rmtree(os.environ['SANTA_HOME'], ignore_errors=True)
print('WROTE', out)
