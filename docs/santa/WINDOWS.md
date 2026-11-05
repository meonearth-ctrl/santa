# Santa for Windows: build, design and test plan

Phase 2 of the Hiring Right rollout: interviewers install Santa on their own
Windows 10/11 laptops and get the same transcripts as the Mac. Scope is offline
file transcription, the watch folder and the local web page. Live mode, Live Assist,
dictation polish, iPhone access and hotkey dictation are not included.

## What the interviewer gets
| | |
|---|---|
| Installer | `Santa-Setup-<version>.exe` (NSIS, 83 MB). Per user, **no admin rights**, installs to `%LOCALAPPDATA%\Programs\Santa`. Fallback: `Santa-for-Windows-<version>.zip` with `Install Santa.bat` |
| Python | Bundled CPython 3.12 (python-build-standalone) + wheels + the Microsoft C++ runtime DLLs next to `python.exe` (no VC++ redistributable install needed) |
| Shortcuts | Start menu and desktop "Santa", plus a sign-in shortcut (`--no-browser`) so the watch folder works without opening Santa |
| UI | The same local web page as the Mac (`http://127.0.0.1:8765`), wording adapted ("this PC", Ctrl/Alt), plus a tray icon: Open Santa · Videos folder · Transcripts folder · Quit |
| Folders | `Documents\Hiring Right\02_Batch_Videos` (watched) → `Documents\Hiring Right\03_Transcripts` (`<video>.json` + `.srt`). Documents is resolved with the Windows known-folder API, so a OneDrive-redirected Documents works |
| Model | Large-v3 Turbo, the same as the Mac, downloaded during set-up (1.6 GB). PCs with less than 8 GB RAM get Small (485 MB) |
| GPU | CPU (int8) by default. With an NVIDIA card the installer adds the "GPU pack" (cuBLAS + cuDNN wheels, ~1.2 GB) and Santa runs on CUDA; any GPU failure falls back to the CPU |
| Uninstall | Settings › Apps › "Santa (offline transcription)", or `Uninstall Santa.bat`. Asks before deleting settings/logs and models; never touches `Documents\Hiring Right` |

## Code changes (all OS-neutral, Mac behaviour unchanged)
| File | Change |
|---|---|
| `santa/hiring_right.py` | Documents known folder, Hiring Right folders, preset (watch + export on, English, 120 min / 2000 MB, speakers on, model by RAM) |
| `santa/setup_tasks.py` | `santa --setup hiring-right / model / gpu / stop`: the installer's steps |
| `santa/cuda.py` | NVIDIA detection: GPU count + all cuBLAS/cuDNN DLLs loadable (else CTranslate2 can kill the process at the first transcription), GPU-pack DLL folders added to the search path |
| `santa/models.py` | `allow_gpu` (Settings › Engine "auto"), CUDA load with CPU fallback, `engine_label()`, RAM on Windows via `GlobalMemoryStatusEx` |
| `santa/watch.py` | On Windows a file still held open for writing is not taken. Explorer pre-sizes copied files, so size/date alone can look settled mid-copy |
| `santa/keep_awake.py` + `service.py` | Windows does not go to sleep while a job runs (`SetThreadExecutionState`) |
| `santa/tray.py`, `cli.py` | Tray icon (default on Windows), safe under `pythonw.exe` (no console), no crash on non-UTF-8 consoles, iPhone access only on macOS |
| `santa/server.py`, `web/app.js` | `/api/config` reports `host_os` and GPU status; the page says "this PC", shows Ctrl/Alt, and offers the NVIDIA engine choice |

The export code (`export.py`) is untouched, so it already wrote the same bytes on every OS
(UTF-8, no BOM, `\n` line endings, `indent=1`, same key order).

## Building (on the Mac, no Windows needed)
```bash
"$HOME/Library/Application Support/Santa/venv/bin/python" santa-app/windows/build_windows.py
```
Needs uv, Docker (NSIS runs in a small Debian image) and Pillow. Output goes to
`~/Documents/Hiring Right/09_Santa_Install/Windows/`. Takes ~3 minutes once wheels are cached.
Pinned versions: `santa-app/windows/requirements-windows.txt` (same as the Mac's constraints).

## Verified, and how
| Check | Where | Result |
|---|---|---|
| Unit tests `tests/test_santa_windows.py` (33): export parity against golden files written by the Mac export code, Windows file names, OneDrive Documents, preset, watch "still copying" guard, CUDA decision + CPU fallback (faked), keep-awake, tray menu, set-up steps | Mac (full venv and numpy-only venv) | pass |
| Existing Santa tests (`test_santa.py`, `test_santa_extras.py`) | Mac | 99 pass in total |
| Re-export of all 71 real Mac transcripts in `03_Transcripts` from their own data | Mac | 71/71 byte-identical `.json` and `.srt` (live-mode `.live.json` files excluded) |
| End to end on the code path Windows uses: `--setup hiring-right`, CPU engine, spoken `.m4a` dropped in the watch folder, speakers on | Mac | `.json` + `.srt` written; text correct; same keys/order as the Mac files |
| Installer is a valid NSIS PE32 and contains `pythonw.exe`, Santa, the web page, `msvcp140*.dll` and the uninstaller; every bundled `.pyd/.dll`'s C++ runtime imports are satisfied | Mac (7-Zip listing, PE import scan) | ok |

## Not verified on Windows (no Windows machine, VM or runner was available)
Nothing has been run on Windows yet. In particular:
* the installer UI, the shortcuts, the Apps & features entry and the uninstaller;
* the bundled Python starting and every wheel loading (ctranslate2, av, onnxruntime, sherpa-onnx);
  the possible clash between onnxruntime's and sherpa-onnx's `onnxruntime.dll` (same file name, different folders);
* the tray icon (pystray), `os.startfile`, the known-folder lookup, the sharing-violation check
  during an Explorer copy, `SetThreadExecutionState`;
* the NVIDIA GPU pack (pip into the bundled Python) and CUDA transcription;
* SmartScreen and antivirus behaviour with an unsigned installer;
* speed: how long a 90-minute video takes on a typical interviewer laptop CPU.

The GitHub Actions workflow `.github/workflows/santa-windows.yml` covers most of this on a
real Windows runner. It builds the installer, installs it silently without admin rights,
transcribes a spoken `.wav` and `.mp4` through the watch folder, checks the export, and
uninstalls. It runs once the branch is pushed. The GPU, tray, SmartScreen and speed checks still need a real laptop.

## Test plan on a real Windows laptop (≈ 45 min)
Use a laptop like the interviewers' (Windows 10 or 11, standard user, 8–16 GB RAM).
1. **Install**: download `Santa-Setup-*.exe`, run it. Expect SmartScreen "Windows protected your PC"
   → More info → Run anyway; **no admin prompt**. Keep all ticks. The model download shows a console
   window with progress. At the end Santa opens in the browser and a Santa icon appears in the tray.
2. **Folders**: `Documents\Hiring Right\02_Batch_Videos` and `03_Transcripts` exist (check a PC with
   OneDrive "Documents backup" on too).
3. **Watch folder, short file**: copy a 2–5 min interview `.mp4` into `02_Batch_Videos`. Within a minute
   the page shows it in progress; `03_Transcripts\<name>.json` and `.srt` appear. Upload the `.json`
   into Hiring Right Kout and check it parses and tags speakers.
4. **Long file**: copy a 60–90 min group interview (copying takes a while, so check it does **not** start before the
   copy finishes). Note the time taken; the laptop must not sleep during it (leave it idle, on battery).
5. **Parity**: transcribe the same video on a Mac and on the PC. `source`, `model`, `language`,
   segment structure and SRT format must match; the text may differ slightly (CPU vs Metal decoding).
6. **Web page**: open Santa from the desktop icon; drop a `.mov` and an `.m4a` on the page; Settings
   shows "this PC" wording and Ctrl/Alt shortcuts. Tray menu items open the right folders; Quit stops it.
7. **Restart**: sign out and in. Santa starts in the tray without opening the browser and still watches.
8. **NVIDIA laptop** (if any): the installer adds the GPU pack; Settings shows the GPU as ready; the result
   `engine` says "NVIDIA GPU"; then rename the `nvidia` folder in `site-packages` and check Santa falls back to the CPU.
9. **Zip fallback**: on a second PC use `Santa-for-Windows-*.zip` → `Install Santa.bat`.
10. **Uninstall**: Settings › Apps › Santa › Uninstall. Program files and shortcuts are gone, the
    `Hiring Right` folder is untouched, and the settings/models prompts behave as described.
Collect `%APPDATA%\Santa\logs\santa.log` for any failure.

## Known limits / follow-ups
* The installer is unsigned. SmartScreen will warn until it is code-signed (needs a certificate).
* The `languages` field that the newer `claude/santa-live-assist` export writes is not on this
  branch. If the Mac build ships from that branch, merge it here before building Windows so the
  `.json` keys match exactly. `export.py` was not touched here, so the merge is clean.
* x64 build. A Windows-on-ARM laptop runs it under Windows' x64 emulation (slower, untested).
