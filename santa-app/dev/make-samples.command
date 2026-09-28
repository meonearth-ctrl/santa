#!/bin/bash
# Renders the SYNTHETIC evaluation set with macOS text-to-speech (offline).
# Output: santa-app/eval/synthetic/<id>.wav (16 kHz mono). Human recordings go
# in santa-app/eval/human/ instead.
cd "$(dirname "$0")/.." || exit 1
PY="$HOME/Library/Application Support/Santa/venv/bin/python"
mkdir -p dev/out eval/synthetic
"$PY" - <<'PYEOF' > dev/out/make-samples.txt 2>&1
import json, subprocess, os
data = json.load(open('eval/samples.json', encoding='utf-8'))
for s in data['samples']:
    aiff = f"eval/synthetic/{s['id']}.aiff"; wav = f"eval/synthetic/{s['id']}.wav"
    subprocess.run(['say', '-v', s['voice'], '-o', aiff, s['text']], check=True)
    subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', aiff, wav], check=True)
    os.remove(aiff)
    print('ok', s['id'], os.path.getsize(wav))
print('DONE')
PYEOF
echo "finished: see santa-app/dev/out/make-samples.txt"
