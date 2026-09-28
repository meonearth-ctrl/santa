# santa-app/dev/hiring_right_check.py
# End-to-end check of the Hiring Right hand-off on this Mac, using throw-away
# folders (your real Hiring Right folders are not touched):
#   1. builds a SYNTHETIC "batch interview" (interviewer + candidates announcing
#      their numbers, macOS voices), as .mp4 (AAC audio), ~2.5 min, and a ~75 min
#      version (the same conversation looped) to exercise long files;
#   2. runs the real service + watch folder with export on;
#   3. checks the .json/.srt files: format, timestamps increasing, candidate
#      numbers present, and how far the GPU timestamps are from the CPU ones.
import json, logging, os, re, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))
BASE = tempfile.mkdtemp(prefix='santa-hr-')
os.environ['SANTA_HOME'] = os.path.join(BASE, 'home')
from whisper_key.santa.accel_cpp import MetalAccelerator     # noqa: E402
from whisper_key.santa.service import TranscriptionService   # noqa: E402
from whisper_key.santa.watch import WatchFolder              # noqa: E402
logging.basicConfig(filename=os.path.join(HERE, 'out', 'hiring_right_check.log'), level=logging.INFO, filemode='w')

# Interviewer = Rishi (en-IN); candidates = Tara (en-IN) and Lekha (hi-IN voice speaking English).
TURNS = [
    ('Rishi', 'Good morning everyone, and welcome to the group interview for the restaurant crew positions. Please say your candidate number before you answer.'),
    ('Lekha', 'Good morning. I am candidate number seven. I worked for three years as a barista in Manila, and I enjoy working in a busy team.'),
    ('Rishi', 'Thank you. Next, please.'),
    ('Tara', 'Hello, I am candidate number twelve. I have two years of experience as a cashier, and I can work night shifts and weekends.'),
    ('Rishi', 'Candidate twelve, how would you handle an angry customer?'),
    ('Tara', 'Candidate twelve. I would listen first, apologise, and then offer a quick solution, for example a replacement meal.'),
    ('Lekha', 'Candidate number fifteen here. In my last job in Cebu I trained four new staff members on food safety.'),
    ('Rishi', 'Very good. Candidate seven, why do you want to work in Kuwait?'),
    ('Lekha', 'Candidate seven. I want to grow my career in a large company and support my family.'),
]


def build_audio(folder):
    parts = []
    for n, (voice, text) in enumerate(TURNS):
        aiff = os.path.join(folder, f't{n}.aiff')
        subprocess.run(['say', '-v', voice, '-o', aiff, text], check=True)
        parts.append(aiff)
    import numpy as np, wave
    from whisper_key.santa.audio_input import decode_path
    pcm = [decode_path(p, 600).samples for p in parts]
    gap = np.zeros(int(16000 * 0.8), dtype=np.float32)
    one = np.concatenate([np.concatenate([p, gap]) for p in pcm])
    def write(name, samples):
        wav = os.path.join(folder, name + '.wav')
        with wave.open(wav, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes((samples * 32767).astype('<i2').tobytes())
        mp4 = os.path.join(folder, name + '.mp4')
        subprocess.run(['afconvert', '-f', 'mp4f', '-d', 'aac', wav, mp4], check=True)
        os.remove(wav)
        return mp4, len(samples) / 16000
    short = write('2026-10-12_MNL_G1', one)
    loops = int(75 * 60 / (len(one) / 16000)) + 1
    long_ = write('2026-10-12_MNL_G2_long', np.tile(one, loops))
    for p in parts:
        os.remove(p)
    return short, long_


def main():
    src = os.path.join(BASE, 'build'); os.makedirs(src)
    videos = os.path.join(BASE, '02_Batch_Videos'); os.makedirs(videos)
    out = os.path.join(BASE, '03_Transcripts')
    (short, short_s), (long_, long_s) = build_audio(src)
    print(f'built {os.path.basename(short)} {short_s:.0f}s and {os.path.basename(long_)} {long_s/60:.1f} min', flush=True)

    svc = TranscriptionService(accelerator=MetalAccelerator())
    svc.settings.update({'export_enabled': True, 'export_dir': out, 'watch_enabled': True, 'watch_dir': videos,
                         'watch_language': 'en', 'max_duration_min': 120, 'max_upload_mb': 2000})
    svc.preload()
    while svc.status()['model']['state'] != 'ready' or (svc.status()['accelerator'] or {}).get('state') != 'ready':
        time.sleep(0.2)
    w = WatchFolder(svc, settle_seconds=2)
    report = {'files': {}}
    for path, secs in ((short, short_s), (long_, long_s)):
        shutil.copy(path, videos)
        t0 = time.time()
        stem = os.path.splitext(os.path.basename(path))[0]
        json_path = os.path.join(out, stem + '.json')
        while not os.path.exists(json_path) and not os.path.exists(os.path.join(out, stem + '.error.txt')):
            w.scan_once(); time.sleep(1)
            if time.time() - t0 > 3600:
                break
        elapsed = time.time() - t0
        data = json.load(open(json_path, encoding='utf-8'))
        segs = data['segments']
        text = ' '.join(s['text'] for s in segs).lower()
        report['files'][stem] = {
            'audio_min': round(secs / 60, 1), 'wall_seconds_incl_detection': round(elapsed, 1),
            'segments': len(segs), 'engine': data.get('engine'),
            'times_increasing': all(segs[i]['start'] >= segs[i - 1]['start'] for i in range(1, len(segs))),
            'max_segment_s': round(max(s['end'] - s['start'] for s in segs), 1),
            'mentions': {n: len(re.findall(rf'(?:candidate|number|no\.)\s*(?:number\s*)?(?:{n}|{w})\b', text))
                         for n, w in (('7', 'seven'), ('12', 'twelve'), ('15', 'fifteen'))},
            'srt_ok': open(os.path.join(out, stem + '.srt'), encoding='utf-8').read().startswith('1\n00:00:0'),
            'json_keys': sorted(data.keys()),
        }
        print(json.dumps({stem: report['files'][stem]}, ensure_ascii=False), flush=True)
        if stem.endswith('G1'):
            report['short_segments_gpu'] = segs
    # Timestamp agreement: same short file on the CPU engine.
    svc.settings.update({'engine': 'cpu'})
    job = svc.submit_file(short, os.path.basename(short), 'en', source='upload', delete_after=False)
    while job.state not in ('done', 'error'):
        time.sleep(0.2)
    cpu = job.result['segments']
    report['short_segments_cpu'] = [{k: s[k] for k in ('start', 'end', 'text')} for s in cpu]
    def first_time(segs, n, w):
        pat = re.compile(rf'(?:candidate|number|no\.)\s*(?:number\s*)?(?:{n}|{w})\b')
        return next((s['start'] for s in segs if pat.search(s['text'].lower())), None)
    report['timestamp_check'] = {n: (first_time(report['short_segments_gpu'], n, w), first_time(cpu, n, w))
                                 for n, w in (('7', 'seven'), ('12', 'twelve'), ('15', 'fifteen'))}
    print('timestamps gpu vs cpu:', json.dumps(report['timestamp_check']))
    json.dump(report, open(os.path.join(HERE, 'out', 'hiring_right_check.json'), 'w'), ensure_ascii=False, indent=1)
    shutil.rmtree(BASE, ignore_errors=True)
    print('DONE')


if __name__ == '__main__':
    main()
