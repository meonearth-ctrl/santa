# Santa — technical summary

Base: [`drajb/whisper-local`](https://github.com/drajb/whisper-local) at commit
`651c1dddfcad5b05706413bc2dfeaecf6532a90c` (v0.20.0, MIT, © Rohit Burani —
licence and attribution unchanged). All Santa work is on the local branch
`santa`; nothing in the original app was rewritten.

## What the repository already had (reused)
* faster-whisper engine, model registry and cache checks, whisper.cpp backend stub
* macOS/Windows desktop dictation: global push-to-talk hotkey (Fn+Ctrl),
  recording, paste-at-cursor, auto-send off by default, menu-bar icon,
  per-app rules, voice commands, settings/history windows
* a localhost OpenAI-compatible server (`--serve`), smoke-test suite (≈260 tests)

## What Santa adds
```
src/whisper_key/santa/
  languages.py      UI language modes -> Whisper params; Hinglish strategies (no invented codes)
  romanize.py       optional Devanagari -> Hinglish Latin heuristic (raw text kept)
  textproc.py       NFC, script detection/warnings, script-aware light cleanup
  audio_input.py    upload validation, PyAV decode (m4a/mp3/webm/ogg/…), limits, temp cleanup
  models.py         model catalogue (multilingual only), background download/load, one model reused
  accel_cpp.py      Metal fast path (whisper.cpp large-v3-turbo q5_0), pause-based splitting,
                    byte-level UTF-8 reassembly, reject-and-fallback on damaged output
  service.py        transcription service: FIFO queue, 1 worker, max 3 pending, cancel, TTL,
                    engine routing (GPU -> CPU fallback), result building, optional history
  settings.py / history.py   JSON in ~/Library/Application Support/Santa (outside the repo)
  server.py         stdlib HTTP server on 127.0.0.1:8765 + JSON API + static UI
  cli.py            `santa` command (single instance, opens browser, logging)
  desktop_bridge.py Whisper Local hotkey app -> Santa server (backend "santa")
  dictation.py      `santa-dictation` command
  web/              index.html, app.js, style.css, recorder-worklet.js (no framework)
src/whisper_key/main.py      +5 lines: `whisper.backend: santa` selects the bridge
tests/test_santa.py          40 focused tests (fake model; plumbing, not accuracy)
santa-app/                   launchers, installer, uninstaller, dev/eval scripts
docs/santa/                  this document, user guide, evaluation, Phase 2 plan
pyproject.toml               `santa`, `santa-dictation` scripts; `santa-gpu` extra (pywhispercpp)
```

### Separation of concerns
* **Audio capture**: browser (AudioWorklet → 16 kHz WAV) for the window;
  whisper_key's sounddevice recorder for the hotkey. Both deliver bytes to the
  same service. **Audio-file ingestion**: `audio_input.py`.
* **Recognition & model management**: `models.py`, `accel_cpp.py`.
* **Language & output-script preferences**: `languages.py`, `romanize.py`.
* **Transcript processing**: `textproc.py` (optional, never destructive).
* **Desktop/OS integration** (hotkeys, clipboard, paste): stays in whisper_key;
  the service never imports it — so phone requests (Phase 2) can use
  transcription without being able to control the computer.
* **Storage**: `settings.py`, `history.py` (history off by default, audio never stored).
* **Service interface for Phase 2**: `service.py` + the JSON API in `server.py`.

### Engine routing (per job)
1. Decode + validate (size ≤ 200 MB, duration ≤ 60 min, silence → clear error).
2. If *Engine = Automatic*, model = Large-v3 Turbo and the Metal path is ready:
   clips ≤ 28 s go straight to whisper.cpp; longer audio is split with Silero
   VAD at pauses into ≤ 28 s pieces (language detected once on the first piece
   then fixed). Output is rebuilt from raw UTF-8 bytes; any U+FFFD → whole job
   redone on the CPU.
3. Otherwise faster-whisper (CTranslate2 int8, 8 threads) with VAD filter.
4. `task` is always `transcribe` (never translate). Raw text, cleaned text and
   (Hinglish) romanized text are all returned.

### Security / privacy
* Binds 127.0.0.1 only; CLI refuses other hosts. Host-header allow-list
  (DNS-rebinding), `X-Santa` header + same-origin check on every write,
  body-size caps, CSP `default-src 'self'`, `nosniff`, no-referrer.
* No telemetry (HF telemetry disabled), no cloud calls after model download.
* Logs contain sizes/timings, never transcript text or audio.
* Settings, history, logs, venv live outside the repo; `santa-app/.gitignore`
  excludes recordings and eval outputs.

## Hardware findings (Apple M5, 16 GB, macOS 26.6)
* CTranslate2 has no Metal backend → faster-whisper runs on CPU (int8).
  CT2's default uses 4 threads; 8 measured ~20 % faster → Santa uses cores−2 (max 8).
* whisper.cpp (pywhispercpp 1.5.1 wheel) reports `MTL: EMBED_LIBRARY = 1`, i.e. Metal.
  It is ~4× faster for short clips but in timestamp mode produced a duplicated
  phrase + split character on a 15 s Hindi clip, and in timestamp-less mode
  dropped text beyond 30 s. Hence: timestamp-less, ≤ 28 s pieces, byte
  reassembly, and CPU fallback. Details and numbers: `EVALUATION.md`.

## Commands
| Command | Purpose |
|---|---|
| `santa` | start server + open browser (`--port`, `--no-browser`, `--model`) |
| `santa-dictation` | hotkey dictation into any app via Santa |
| `python -m pytest tests/test_santa.py tests/test_smoke.py` | tests |
| `python santa-app/dev/bench.py --model large-v3-turbo` | CPU benchmark |
| `python santa-app/dev/bench_cpp.py --beam 5 --no-timestamps` | Metal benchmark |
| `python santa-app/dev/e2e_service.py` | end-to-end service check |
