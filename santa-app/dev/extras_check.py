# santa-app/dev/extras_check.py
# End-to-end check of Santa's newer features on the Mac, against the RUNNING
# Santa server (http://127.0.0.1:8765) and the real models:
#   1. offline translation (hi/bn/ar -> en, en -> ar, hi -> ar via English)
#   2. speaker separation on a SYNTHETIC two-person conversation made with the
#      macOS `say` voices, through the full transcribe pipeline, then naming a
#      voice and checking it is recognised in a second file
#   3. iPhone access: TLS with the local CA, 401 before pairing, pairing, an
#      authenticated transcription over HTTPS, a Shortcut-style Bearer request
# Test voices/devices it creates are removed again. Prints a short report.

import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))
BASE = 'http://127.0.0.1:8765'


# ── Tiny HTTP helpers ─────────────────────────────────────────────────────────
def call(method, url, body=None, headers=None, ctx=None, timeout=600):
    h = {'X-Santa': '1'}
    h.update(headers or {})
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    if isinstance(body, dict):
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            return resp.status, raw, resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def wait_job(base, job, headers=None, ctx=None, timeout=900):
    end = time.time() + timeout
    while job['state'] not in ('done', 'error', 'cancelled') and time.time() < end:
        time.sleep(0.5)
        _, raw, _ = call('GET', f"{base}/api/jobs/{job['id']}", headers=headers, ctx=ctx)
        job = json.loads(raw)
    return job


# ── 1. Translation ────────────────────────────────────────────────────────────
def check_translation():
    from whisper_key.santa.settings import santa_home
    from whisper_key.santa.translate import Translator
    tr = Translator(santa_home())
    samples = [
        ('hi', 'en', 'मैं कल सुबह दफ़्तर जाऊँगा और बजट के बारे में मीटिंग करूँगा।'),
        ('bn', 'en', 'আমি আগামীকাল অফিসে যাব এবং বাজেট নিয়ে আলোচনা করব।'),
        ('ar', 'en', 'سأذهب إلى المكتب غدًا صباحًا لمناقشة الميزانية.'),
        ('en', 'ar', 'The interview is scheduled for tomorrow at ten in the morning.'),
        ('hi', 'ar', 'उम्मीदवार के पास पाँच साल का अनुभव है।'),
    ]
    print('\n== 1. Translation (offline) ==')
    for src, tgt, text in samples:
        started = time.time()
        try:
            out = tr.translate(text, src, tgt)
            print(f'{src}->{tgt} {time.time() - started:5.1f}s  {out}')
        except Exception as exc:
            print(f'{src}->{tgt} FAILED {type(exc).__name__}: {getattr(exc, "message", exc)}')


# ── 2. Speakers ───────────────────────────────────────────────────────────────
def say_to_wav(voice, text, path):
    aiff = path + '.aiff'
    subprocess.run(['say', '-v', voice, '-o', aiff, text], check=True)
    subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', aiff, path], check=True)
    os.remove(aiff)


def join_wavs(parts, out, gap=0.6):
    import wave
    frames = []
    for p in parts:
        with wave.open(p) as w:
            frames.append(w.readframes(w.getnframes()))
    silence = b'\x00\x00' * int(16000 * gap)
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(silence.join(frames))


def pick_voices():
    listing = subprocess.run(['say', '-v', '?'], capture_output=True, text=True).stdout
    names = [line.split('  ')[0].strip() for line in listing.splitlines() if 'en_' in line]
    preferred = [v for v in ('Samantha', 'Daniel', 'Karen', 'Moira', 'Fred') if v in names]
    return (preferred + names)[:2]


def check_speakers(tmp):
    print('\n== 2. Speaker separation (SYNTHETIC: two macOS voices) ==')
    a, b = pick_voices()
    print('voices:', a, '(A),', b, '(B)')
    lines = [
        (a, 'Good morning, thank you for coming in today. Could you start by telling me about your current role?'),
        (b, 'Of course. I have been working as a kitchen supervisor for five years, mostly managing weekend shifts.'),
        (a, 'That is great. How do you handle a situation where two staff members call in sick at the same time?'),
        (b, 'First I check who is on call, then I rearrange the stations so the busiest section is always covered.'),
        (a, 'Very good. And what would you say is your biggest strength?'),
        (b, 'I stay calm under pressure, and I make sure new team members are trained properly.'),
    ]
    parts = []
    for n, (voice, text) in enumerate(lines):
        path = os.path.join(tmp, f'line{n}.wav')
        say_to_wav(voice, text, path)
        parts.append(path)
    convo = os.path.join(tmp, 'interview.wav')
    join_wavs(parts, convo)
    started = time.time()
    status, raw, _ = call('POST', f'{BASE}/api/transcribe?language=en&filename=interview.wav&source=upload'
                          '&diarize=1&output_language=same', open(convo, 'rb').read(),
                          {'Content-Type': 'audio/wav'})
    job = wait_job(BASE, json.loads(raw))
    if job['state'] != 'done':
        print('FAILED', job.get('error'))
        return
    r = job['result']
    print(f"{r['audio_seconds']:.0f}s audio in {time.time() - started:.1f}s (speakers step {r.get('speaker_seconds')}s)")
    if r.get('speaker_error'):
        print('speaker error:', r['speaker_error'])
    print('speakers:', [(s['name'], s['seconds']) for s in r.get('speakers', [])])
    expected = ['Speaker 1', 'Speaker 2', 'Speaker 1', 'Speaker 2', 'Speaker 1', 'Speaker 2']
    got = [p.split(':')[0] for p in r['text'].split('\n\n')]
    print('turn labels:', got, 'expected', expected, 'MATCH' if got == expected else 'DIFFERENT')
    print(r['text'][:400], '…')

    # Name speaker 1, then a second conversation should say that name by itself.
    status, raw, _ = call('POST', f"{BASE}/api/jobs/{job['id']}/name-speaker",
                          {'speaker_id': 'Speaker 1', 'name': 'Test Interviewer'})
    print('name speaker:', status)
    second = [(a, 'Thanks. Let us talk about availability. Are you free to work on Fridays?'),
              (b, 'Yes, Fridays are fine, and I can also cover Saturday mornings when needed.'),
              (a, 'Perfect, we will be in touch by the end of the week.')]
    parts = []
    for n, (voice, text) in enumerate(second):
        path = os.path.join(tmp, f'second{n}.wav')
        say_to_wav(voice, text, path)
        parts.append(path)
    convo2 = os.path.join(tmp, 'interview2.wav')
    join_wavs(parts, convo2)
    status, raw, _ = call('POST', f'{BASE}/api/transcribe?language=en&filename=interview2.wav&source=upload&diarize=1',
                          open(convo2, 'rb').read(), {'Content-Type': 'audio/wav'})
    job2 = wait_job(BASE, json.loads(raw))
    r2 = job2.get('result') or {}
    print('second file speakers:', [(s['name'], s['known']) for s in r2.get('speakers', [])])
    print(r2.get('text', '')[:300])
    call('DELETE', f'{BASE}/api/voices/Test%20Interviewer')

    # Translation through the whole pipeline, with speaker labels kept.
    status, raw, _ = call('POST', f'{BASE}/api/transcribe?language=en&filename=interview.wav&source=upload'
                          '&diarize=1&output_language=ar', open(convo, 'rb').read(), {'Content-Type': 'audio/wav'})
    job3 = wait_job(BASE, json.loads(raw))
    r3 = job3.get('result') or {}
    print('\nspeakers + Arabic output:', r3.get('translation'), r3.get('translation_error'))
    print(r3.get('text', '')[:300])


# ── 3. iPhone access ──────────────────────────────────────────────────────────
def check_phone(tmp):
    print('\n== 3. iPhone access (HTTPS + pairing) ==')
    call('PUT', f'{BASE}/api/settings', {'phone_enabled': True})
    time.sleep(2)
    _, raw, _ = call('GET', f'{BASE}/api/phone')
    st = json.loads(raw)
    print('running:', st.get('running'), st.get('url'), 'ips', st.get('ips'), 'error', st.get('error'))
    if not st.get('running'):
        return
    from whisper_key.santa.settings import santa_home
    from whisper_key.santa.phone import PhoneCertificates
    ca = PhoneCertificates(santa_home()).ca_cert_path
    ctx = ssl.create_default_context(cafile=ca)       # trust ONLY Santa's CA, like the iPhone will
    base = st['url'].rstrip('/')
    origin = {'Origin': base}
    status, _, _ = call('GET', base + '/', ctx=ctx)
    print('page over TLS:', status)
    status, raw, _ = call('GET', base + '/api/config', ctx=ctx)
    print('API before pairing:', status, '(expect 401)')
    _, raw, _ = call('POST', f'{BASE}/api/phone/pairing', {})
    pairing = json.loads(raw)
    status, raw, headers = call('POST', base + '/api/pair', {'code': pairing['code'], 'device_name': 'Test phone'},
                                origin, ctx=ctx)
    print('pair:', status)
    cookie = {'Cookie': headers['Set-Cookie'].split(';')[0], **origin}
    status, raw, _ = call('GET', base + '/api/config', headers=cookie, ctx=ctx)
    print('API after pairing:', status, json.loads(raw).get('client'))
    wav = os.path.join(tmp, 'phone.wav')
    say_to_wav(pick_voices()[0], 'This sentence was sent from the phone test over the encrypted connection.', wav)
    started = time.time()
    status, raw, _ = call('POST', base + '/api/transcribe?language=en&filename=phone.wav&source=recording',
                          open(wav, 'rb').read(), dict(cookie, **{'Content-Type': 'audio/wav'}), ctx=ctx)
    job = wait_job(base, json.loads(raw), headers=cookie, ctx=ctx)
    print(f'phone transcription: {job["state"]} in {time.time() - started:.1f}s:', (job.get('result') or {}).get('text'))
    status, raw, _ = call('POST', base + '/api/shutdown', {}, cookie, ctx=ctx)
    print('phone tries to quit Santa:', status, '(expect 403)')
    # Shortcut key
    _, raw, _ = call('POST', f'{BASE}/api/phone/shortcut-token', {'name': 'Test shortcut'})
    token = json.loads(raw)['token']
    started = time.time()
    status, raw, _ = call('POST', base + '/api/transcribe?filename=s.wav&source=recording&wait=60&format=text',
                          open(wav, 'rb').read(), {'Authorization': 'Bearer ' + token, 'X-Santa': ''}, ctx=ctx)
    print(f'Shortcut request: {status} in {time.time() - started:.1f}s:', raw.decode('utf-8'))
    # A client that doesn't trust Santa's CA must fail the handshake.
    try:
        urllib.request.urlopen(base + '/', timeout=10, context=ssl.create_default_context())
        print('untrusted client: UNEXPECTEDLY CONNECTED')
    except (ssl.SSLError, urllib.error.URLError) as exc:
        print('untrusted client refused:', type(getattr(exc, 'reason', exc)).__name__)
    # Remove the test devices again.
    _, raw, _ = call('GET', f'{BASE}/api/phone')
    for d in json.loads(raw)['devices']:
        if d['name'] in ('Test phone', 'Test shortcut'):
            call('DELETE', f"{BASE}/api/phone/devices/{d['id']}")
    print('fingerprint:', st.get('fingerprint'))


if __name__ == '__main__':
    only = sys.argv[1:] or ['translation', 'speakers', 'phone']
    with tempfile.TemporaryDirectory() as tmp:
        if 'translation' in only:
            check_translation()
        if 'speakers' in only:
            check_speakers(tmp)
        if 'phone' in only:
            check_phone(tmp)
