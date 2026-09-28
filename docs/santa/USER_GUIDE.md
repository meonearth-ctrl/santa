# Santa — user guide

Santa is your private, offline dictation and transcription app. It runs
entirely on your Mac: no internet is needed once the models are downloaded,
nothing is sent to any cloud service, and no audio is stored.

It has two ways to use it:

| | **Santa window** | **Santa Dictation** (hotkey) |
|---|---|---|
| What | A page in your browser at `http://127.0.0.1:8765` | Types straight into *any* app |
| How | Start / Stop button, or upload a file | Hold **Fn + Ctrl**, speak, release |
| Best for | Long recordings, audio files, editing, export | Messages, emails, forms, chat |

Both use the same engine and the **same language setting** (chosen in the Santa window).

---

## 1. Starting and stopping

**Santa window**
* Start: open **Santa** from Spotlight (⌘ Space, type "Santa") or from
  `~/Applications`. Your browser opens the Santa page. (Or double-click
  `santa-app/Santa.command` to run it in a Terminal window.)
* The pill at the top shows the model status. First launch after installing
  loads the models (~5–10 s); "GPU fast path on" means everything is ready.
* Stop: click **Quit Santa** (top right). Closing the browser tab alone keeps
  Santa running in the background.

**Santa Dictation (type into any app)**
* Start: double-click `santa-app/Santa Dictation (hotkey).command`. Keep that
  Terminal window open while you dictate (you can minimise it).
* First time only, macOS needs three permissions for **Terminal**, in
  System Settings › Privacy & Security: **Microphone**, **Accessibility**
  and **Input Monitoring**. Then close and reopen the dictation window.
* Use: click into any text box → **hold Fn + Ctrl** → speak → **release**.
  The text is pasted at the cursor. Nothing is sent automatically. If you
  *want* it sent (Enter pressed after pasting), press **Option** to stop
  instead of releasing. Press **Shift** while recording to cancel.
* Stop: close that Terminal window (or press Ctrl+C in it).
* If the Santa window isn't open, Santa Dictation starts the engine itself.

## 2. Recording in the Santa window
1. Pick the **Language** and **Microphone**.
2. Press **Start recording** (or **⌥S**). The timer runs and the green bar moves
   while you speak.
3. Press **Stop recording** (⌥S again). Status goes *Processing… → Complete*.
4. The text is inserted **where your cursor is** in the transcript box, so you
   can dictate, edit, and dictate more.
* **Cancel**: press **Esc** or the Cancel button (while recording or processing).
* The browser asks for microphone access the first time — allow it.

## 3. Languages

| Choice | Output |
|---|---|
| English | Latin script |
| Hindi | Devanagari (हिन्दी) |
| Arabic | Arabic script, right-to-left |
| Bengali | Bengali script (বাংলা) |
| Hinglish | Mixed: Hindi in Devanagari, English words in Latin — or **Romanized** (all Latin: "aaj ki meeting mein…") |
| Auto-detect | Whatever Whisper hears |

Tips
* **Choose the language when you know it.** It is more reliable than
  Auto-detect — especially for Bengali (often mistaken for Hindi) and Hinglish.
* Santa never translates. Hindi stays Hindi, Arabic stays Arabic.
* **Hinglish › Romanized** is a separate step applied after recognition; the
  original mixed-script text is always shown under *Last result details* and
  can be inserted instead.
* Names or special terms keep coming out wrong? Add them under
  Settings › *Names & vocabulary hint* (e.g. `Kout Food Group, Salmiya, ATOSS`).

## 4. Files
**Transcribe a file…** (⌥U) accepts m4a, mp3, wav, aac, ogg/opus, webm, flac,
caf, aiff. Limits (changeable in Settings): 200 MB, 60 minutes. Uploaded audio
is deleted as soon as it has been read.

## 5. Copy, export, clear
* **Copy** (⌥C) copies the whole transcript.
* **Export .txt** (⌥E) saves a UTF-8 text file to your Downloads folder
  (Hindi, Bengali and Arabic characters are preserved exactly).
* **Clear** asks for a second click to confirm.

## 6. Settings
* **Model** — default *Large-v3 Turbo* (best balance). *Small* is lighter but
  much weaker for Hindi/Arabic/Bengali. *Large-v3* is the most accurate but a
  3 GB download and slower; the app shows size and memory before downloading.
* **Engine** — *Automatic* uses the Apple GPU (fast); *CPU only* is the
  fallback if you ever see odd output.
* **Hinglish strategy** — leave on *Hindi-biased, mixed-script prompt*
  (it measured best); the others are there for experiments.
* **Light cleanup** — only tidies spaces (and capitalises English); never
  changes Hindi, Arabic or Bengali text. The raw text is always available.
* **History** — off by default. If you switch it on, transcripts (never audio)
  are kept on this Mac; a *History* button appears where you can insert or
  delete entries, or *Delete all*.

## 7. Troubleshooting
| Problem | Fix |
|---|---|
| "Microphone access was denied" | Browser address bar › site settings › Microphone: Allow; and System Settings › Privacy & Security › Microphone: enable your browser |
| "No microphone was found" | Plug one in / check System Settings › Sound › Input |
| "The recording is silent" | Wrong or muted microphone selected |
| "Cannot reach the Santa server" | Santa was quit — open Santa again |
| Hotkey does nothing | Terminal needs Accessibility + Input Monitoring; reopen the dictation window after granting |
| Text isn't pasted, only copied | Terminal needs Accessibility |
| Output in the wrong script (e.g. Urdu letters for Hindi) | Select the language explicitly instead of Auto-detect |
| Very slow | Check the status pill says "GPU fast path on"; Settings › Engine = Automatic |

Logs (no transcript text or audio is logged): `~/Library/Application Support/Santa/logs/`.

## 8. Uninstall
Double-click `santa-app/Uninstall Santa.command`. It stops Santa and asks
before removing each item: the app launcher, Santa's private Python
environment/settings/history/logs, and the downloaded models. Santa doesn't
enable any start-at-login item. Unrelated files are never touched; the code
folder `~/Documents/Santa` is yours to delete in Finder afterwards.
