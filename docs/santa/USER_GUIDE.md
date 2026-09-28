# Santa — user guide

Santa is your private, offline dictation and transcription app. It runs
entirely on your Mac: no internet is needed once the models are downloaded,
nothing is sent to any cloud service, and no audio is stored.

It has two ways to use it:

| | **Santa window** | **Santa Assistant** (floating pill) |
|---|---|---|
| What | A page in your browser at `http://127.0.0.1:8765` | A small bar that floats above every window and types into *any* app |
| How | Start / Stop button, or upload a file | Click the red mic (click again to finish) — or hold **Fn + Ctrl**, speak, release |
| Best for | Long recordings, audio/video files, editing, export | Messages, emails, forms, chat — Wispr Flow–style |

Both use the same engine and the **same language setting**. A third way, your
**iPhone**, is described in section 9.

### The floating assistant
* Start: Spotlight → **Santa Assistant** (or `santa-app/Santa Dictation (hotkey).command`).
* A small liquid-glass capsule sits at the bottom of the screen, above all windows
  and full-screen apps. It is dimmed until you hover over it or dictate. Drag it
  anywhere; it remembers the position.
* **Mic** (left): click to start, click to stop → the text is pasted where your
  cursor is. The capsule never takes focus away from the app you're typing in.
  **Fn + Ctrl**: hold while you speak and release to paste — or just *tap* it once
  to dictate hands-free, then tap **Fn** (or click the capsule) to finish.
* **Tag** (right): input language › output — `EN › Aa`, `HI › अ`,
  `AR › ع`, `BN › অ`, `HG › अa` (Hinglish mixed) or `HG › Aa` (Hinglish
  romanized), `AUTO`; `HI › EN` / `AUTO › AR` when a translation is switched on.
  Click it to cycle the language.
* While recording the mic becomes a red dot and the tag becomes level bars; then
  three dots while transcribing; then a brief green ✓ (pasted) or amber ! (nothing
  recognised). Hover the capsule to read the last result or the reason.
* Right-click: language, **Output** (as spoken / English / Arabic), Hinglish
  output (mixed / romanized), **Add words Santa should know…**, Open Santa window,
  **Start at login**, **Restart Santa Assistant**, Hide, **Quit Santa Assistant**.
  To start it again later: Spotlight (⌘ Space) → "Santa Assistant".
* First use: macOS asks for **Microphone** and **Accessibility** (to paste). For
  the Fn+Ctrl hotkey also allow **Input Monitoring**. If Accessibility is off,
  the capsule shows an amber **!**, the text is left on the clipboard (press ⌘V),
  and the Accessibility settings page opens so you can switch Santa Assistant on
  (then quit and reopen Santa Assistant).

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
* Names or special terms keep coming out wrong? Add them at the top of
  Settings › **Words Santa should know** (e.g. `Kout Food Group, Salmiya, ATOSS`),
  or right-click the capsule › *Add words Santa should know…*. It is a hint to the
  model, so it makes those spellings more likely, not certain.

### Output: as spoken, English or Arabic
Next to the language there is an **Output** choice (also in the capsule menu):
* **As spoken** (default): no translation, ever.
* **English** or **Arabic**: after recognition, Santa translates on your Mac with
  offline Argos Translate models (about 100 MB per language pair, downloaded the
  first time). Hindi and Bengali reach Arabic through English.
* The result says *Translated from … to …*, and the original wording is kept under
  *Last result details › Original*. Machine translation can be wrong, so check it
  before sending anything important. The Hindi → English model is the weakest.
* Watch-folder transcripts (Hiring Right) are never translated.

## 4. Files, paths and links
The **Files & links** panel in the Santa window takes:
* **Files**: drag one or several audio/video files onto the dashed box (or anywhere
  on the page), or press **Choose files…** (⌥U). mp4, mov, m4v, mkv, m4a, mp3,
  wav, aac, ogg/opus, webm, flac, caf, aiff.
* **A path**: paste it in the box under the drop zone and press *Transcribe*
  (in Finder: right-click the file, hold ⌥ Option, *Copy … as Pathname*). The file
  is read where it is and never moved or deleted.
* **A link**: YouTube, Loom, Vimeo, a public Google Drive file, or a direct
  .mp4/.mp3 link. Santa downloads only the audio to a temporary file and deletes
  it after. Private links that need a sign-in won't work, so download those and
  drop the file in. Links to your own network are refused.
* Each file gets its own line with progress and *Cancel*. When it finishes, the
  text is added to the transcript under a `── file name ──` heading.
* Limits (Settings): 200 MB / 60 minutes by default; you raised them to
  2000 MB / 120 minutes for interview videos.

## 4a. Speakers (interviews)
Tick **Separate speakers** in the Files & links panel (or Settings › Speakers ›
*Separate speakers in files by default*, which also covers the watch folder).
Pick the number of people if you know it, otherwise *auto*.
* The transcript becomes `Speaker 1: …` / `Speaker 2: …` paragraphs. The .json
  gets a `speaker` on every segment plus a `speakers` summary, and the .srt lines
  start with `[Speaker 1]`.
* **Known voices**: in *Last result details › Speakers*, type a name next to a
  speaker and press *Remember voice* (e.g. yourself as interviewer). Or record a
  20-second sample in Settings › Speakers. From then on Santa writes that name
  instead of "Speaker N" in every file.
* Voiceprints are numbers describing a voice, not recordings. They are stored
  only on your Mac (`voices.json`), can be deleted in Settings, and are only
  created when you save one. Ask people before saving theirs. For candidates,
  your usual interview-recording consent should mention it.
* Never used for dictation. The first use downloads about 35 MB of speaker models.
* Limits: overlapping speech gets one label; a Whisper segment that spans two
  people keeps the majority speaker; it was tested on synthetic voices, not yet
  on your real interviews.

## 5. Copy, read aloud, share, export, clear
* **Copy** (⌥C) copies the whole transcript.
* **Read aloud** (⌥R) reads the transcript, or just the selected part, only when
  you press it. Press again to stop. It uses the voices installed on the device
  (on the Mac: System Settings › Accessibility › Spoken Content › System voice ›
  Manage Voices, where you can add Hindi, Arabic or Bengali voices), and never an
  online voice.
* **Share…** (iPhone) sends the text to WhatsApp, Mail, Notes and other apps.
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

## 7. Hiring Right (batch interview videos)
Settings › *Transcript files & watch folder*:
* **Save transcripts to** `~/Documents/Hiring Right/03_Transcripts` → every
  uploaded or watched file produces `<video name>.json` (source, model,
  language, audio length, segments with start/end seconds and text) and
  `<video name>.srt`. A failed file produces `<video name>.error.txt`.
* **Watch folder** `~/Documents/Hiring Right/02_Batch_Videos`, language
  English → drop a video there and it's transcribed automatically once it has
  finished copying. Santa never moves or deletes the videos; a video that
  already has an up-to-date `.json` is skipped.
* Limits are set to 120 minutes / 2000 MB for these videos.
* Long videos run in a separate lane, so the floating assistant keeps working
  while a batch is being transcribed.
* Speaker labels: switch on Settings › Speakers › *Separate speakers in files by
  default* and every watched video's .json/.srt carries speakers (see 4a).

## 8. Troubleshooting
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
| iPhone: "Safari can't open the page" | Mac asleep / Santa closed / not on the same Wi-Fi; or iPhone access is off |
| iPhone: certificate warning or no microphone | Step 2 not finished: install the profile **and** switch it on in Certificate Trust Settings |
| iPhone: "This phone is not paired" | Press *Show pairing code* on the Mac and type the new code |
| Translation says it couldn't download | Internet is needed once per language pair |
| Read aloud says no voice installed | Add a voice for that language (see section 5) |

Logs (no transcript text or audio is logged): `~/Library/Application Support/Santa/logs/`.

## 9. iPhone
Santa on your iPhone is the same page as the Santa window, served by your Mac
over your home Wi-Fi. Your speech is sent to the Mac, encrypted, and recognised
there. Nothing goes to the internet, and nothing is reachable from outside your
home network (no router settings are changed).

One-time setup (5 minutes):
1. Mac: Santa window › Settings › **iPhone access** › tick *Let my paired iPhone
   use Santa…* (the first time, macOS may ask whether Python may accept incoming
   connections: choose Allow). Press **Set up iPhone…**.
2. **Trust this Mac**: press *Show download QR code* and scan it with the iPhone
   camera (or *AirDrop the certificate*). On the iPhone: Settings › *Profile
   Downloaded* › Install; then Settings › General › About › **Certificate Trust
   Settings** › switch on *Santa local CA*. This certificate can only vouch for
   this Mac on your home network, never for real websites. To remove it later:
   Settings › General › VPN & Device Management.
3. **Pair**: press *Show pairing code* and scan the QR code, or open the address
   shown in Safari and type the 6-digit code. Codes last 5 minutes and work once.
4. **Home Screen**: in Safari, Share › *Add to Home Screen*. The Home Screen app
   asks for its own code the first time (press *Show pairing code* again).

Using it: tap **Start recording**, speak, tap again. Then **Share…** into any
app, or **Copy**. Language, Output, Read aloud, links and Settings › Words work
the same as on the Mac.
* **From any app (optional)**: Set up iPhone › step 4 creates a *Shortcut key*.
  An iOS Shortcut (Record Audio → Get Contents of URL → Copy to Clipboard) then
  dictates to the clipboard from anywhere; assign it to Back Tap (Settings ›
  Accessibility › Touch › Back Tap › Double Tap).
* The Mac must be on, awake and running Santa. While iPhone access is on, Santa
  stops the Mac from idle-sleeping (the screen still sleeps). With the lid closed
  on battery it will still sleep.
* Paired phones and Shortcut keys are listed in Set up iPhone › *Paired devices*.
  Remove one to cut it off at once.
* Outside home (mobile data): not supported yet; see PHASE2.md (Tailscale option).

## 10. Uninstall
Double-click `santa-app/Uninstall Santa.command`. It stops Santa and asks
before removing each item: the app launcher, Santa's private Python
environment/settings/history/logs, and the downloaded models. It also removes the capsule's *Start at login* item if you switched it on. Unrelated files are never touched; the code
folder `~/Documents/Santa` is yours to delete in Finder afterwards.
