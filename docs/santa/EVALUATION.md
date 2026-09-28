# Santa — evaluation, benchmarks and limitations

Machine: MacBook Pro, **Apple M5** (10 CPU cores, 10-core GPU, Metal 4),
16 GB RAM, macOS 26.6.2. Python 3.12.14, faster-whisper 1.2.1,
CTranslate2 4.7.1, pywhispercpp 1.5.1. Measured 28 Sep 2026.

> **Important:** every accuracy number below comes from a **synthetic**
> evaluation set rendered with macOS text-to-speech voices (Rishi/Aman en-IN,
> Lekha hi-IN, Majed ar, Piya bn-IN). It is a smoke test and a fair way to
> compare settings *against each other*. It does **not** show accuracy on real
> human speech, accents, background noise or natural Hindi–English switching.
> Real-speech accuracy is **awaiting your recordings** (`santa-app/eval/human/README.md`).

CER = character error rate after lower-casing and removing punctuation
(lower is better). Arabic CER is inflated because the reference includes
tanween/hamza marks (e.g. "مرحباً") that Whisper usually omits ("مرحبا").

## 1. Models and engines (Large-v3 Turbo unless noted)

| Sample (synthetic) | Audio | CPU faster-whisper int8, 8 thr | | GPU whisper.cpp Metal (Santa default) | |
|---|---|---|---|---|---|
| | s | time s | CER | time s | CER |
| English, names + numbers | 8.3 | 3.70 | 0.000 | 1.05 | 0.000 |
| English, long | 36.6 | 8.74 | 0.082 | 3.09 ¹ | 0.067 |
| Hindi, short | 5.3 | 4.17 | 0.048 | 1.13 | 0.048 |
| Hindi, long | 15.5 | 7.49 | 0.086 | 4.24 | 0.108 |
| Arabic MSA | 10.2 | 3.90 | 0.077 | 1.07 | 0.077 |
| Arabic, Kuwaiti wording ² | 7.2 | 3.67 | 0.061 | 1.00 | 0.076 |
| Bengali | 4.9 | 4.91 | 0.317 | 1.31 | 0.283 |
| Hinglish "meeting" | 4.7 | 3.82 | 0.014 | 1.07 | 0.014 |
| Hinglish "payroll" | 4.8 | 3.99 | 0.171 | 1.03 | 0.145 |
| Hinglish loan-words | 3.9 | 4.09 | 0.050 | 1.05 | 0.050 |
| English ×5 (3.1 min) | 187.8 | 38.47 | 0.070 | 10.06 ¹ | 0.067 |

¹ split at pauses into ≤ 28 s pieces (2 and 8 pieces).
² Kuwaiti wording read by an MSA synthetic voice — not real Gulf pronunciation.

Other measurements
* **Small** (CPU): faster on English/Arabic, but Hindi-long CER 0.158 and
  Bengali unusable (CER 0.88, looped for 35 s). → not recommended.
* **CPU threads** (Turbo): CTranslate2 default (4) ≈ 4.4–5.5 s per short clip;
  8 threads ≈ 3.7–4.9 s (~20 % faster) → Santa uses 8.
* **Auto-detect** costs a second encoder pass on the CPU engine (+~4 s per
  clip) and **mis-detected Bengali as Hindi** (p = 0.44, CER 0.90). With the
  GPU path detection is part of the single pass. → pick the language when you know it.
* **whisper.cpp variants tried** on Hindi-long: timestamps on → duplicated phrase
  and a split Devanagari character (U+FFFD); timestamps off + greedy → CER 0.297;
  timestamps off + beam 5 → CER 0.108 (chosen). Timestamps-off loses text past
  30 s, which is why long audio is split first.
* **Cold start** (models already downloaded): CPU model 5.3–7.0 s, GPU model
  0.6–1.6 s, both load in parallel in the background; the UI is usable at once
  and shows the status. **Warm** requests reuse the loaded models (verified:
  no reload across 20 consecutive jobs).
* **First-time downloads** on your connection: Small 31 s (485 MB), Turbo CPU
  193 s (1.6 GB), Turbo GPU 100 s (547 MB).
* **Your own test** (real speech, before the GPU/long-audio change): a 206.6 s
  recording on Auto-detect took **59.0 s** on the CPU. The same length of
  synthetic audio now takes ~10–11 s. Accuracy of your recording was not
  scored (I don't have the reference text).

Nothing here is real-time or sub-second: short clips return in about **1–1.5 s**
after you stop speaking; long recordings take roughly **5–6 %** of their length.

## 2. Hinglish strategy (evidence for the default)

Large-v3 Turbo, CPU engine, 3 synthetic Hinglish clips (Lekha voice reading mixed script).

| Strategy | CER meeting / payroll / loan | English words kept in Latin | Verdict |
|---|---|---|---|
| **Hindi-biased + mixed-script prompt** (default) | **0.014 / 0.171 / 0.050** | **100 % / 71 % / 100 %** | best overall |
| Plain auto-detect | 0.408 / 0.145 / 0.350 | 0 % / 71 % / 0 % | English words transliterated to Devanagari |
| Auto-detect per segment | same as auto | same | no benefit on short clips |
| Forced Hindi, no prompt | same as auto | same | — |
| English-biased + romanized prompt | 0.577 / 0.474 / 0.667 (romanized CER 0.10 / 0.20 / 0.08) | 100 % / 71 % / 100 % | usable only if you want Latin-only output; spelling unstable |

"Romanized" output is therefore produced by Santa's own post-processing step
(`romanize.py`) from the mixed-script transcript, and the raw transcript is kept.

## 3. Functional checks

| Check | Result | How |
|---|---|---|
| Install from source on this Mac (branch `santa`, Python 3.12 via uv, no admin) | ✅ | `Install Santa.command` |
| Repository smoke tests + Santa tests on the Mac | ✅ 301 passed, 6 skipped | pytest |
| Launch (`Santa.app`), bound to 127.0.0.1 only | ✅ | `lsof` shows `127.0.0.1:8765` |
| Foreign Host header / missing X-Santa header rejected | ✅ 421 / 403 | curl |
| File transcription via the UI: Hindi, Arabic, Hinglish, Bengali | ✅ | Chrome, uploaded synthetic WAVs |
| Language choice reaches the backend | ✅ | detected language matches; tests assert params |
| Arabic RTL paragraph with English + digits; Hindi/Bengali matras | ✅ visually | screenshot |
| Export .txt is UTF-8 and byte-identical after decode | ✅ | in-page check of the exported Blob |
| Copy button | ⚠ not verifiable by automation (browser clipboard access needs a real, focused user click); code now reports failure instead of claiming success | **awaiting your check** |
| Settings persistence | ✅ | tests + restart |
| Silent audio / garbage file / unsupported type | ✅ clear messages | e2e script |
| Model reused across requests | ✅ | e2e script |
| Offline after download | ✅ by design (local files only, HF telemetry off); not tested with Wi-Fi switched off | **awaiting your check** |
| Microphone recording in the Santa window | ✅ **tested by you** | — |
| Hotkey dictation into other apps (`santa-dictation`) | starts ✅; needs your macOS permissions; bridge tested with unit tests | **awaiting your check** |
| Real-speech accuracy (all languages, Kuwaiti Arabic, natural Hinglish) | not measured | **awaiting your recordings** |

## 4. Hiring Right hand-off (synthetic batch interview)

A synthetic group interview (interviewer + two candidates announcing their
numbers; macOS voices Rishi, Tara, Lekha), saved as `.mp4` (AAC), was dropped
into a throw-away watch folder with export on (your real Hiring Right folders
were not used for testing).

| File | Audio | Wall time (incl. waiting for the copy to settle) | Engine | Segments | Candidate numbers found |
|---|---|---|---|---|---|
| `2026-10-12_MNL_G1.mp4` | 1.0 min | 6 s | GPU, 3 pieces | 11 | 7 ×3, 12 ×3, 15 ×1 (all) |
| `2026-10-12_MNL_G2_long.mp4` (same talk ×76) | 76 min | 213 s | GPU, 178 pieces, 0 redone | 824 | 7: 228/228, 12: 198/228, 15: 66/76 |

* `.json` has exactly the keys Hiring Right expects (`source`, `model`,
  `audio_seconds`, `segments[start,end,text]`) plus `language` and `engine`;
  `.srt` is valid; segment times always increase; longest segment 9.2 s.
* **Timestamps**: GPU vs CPU start time of each candidate's first announcement:
  9.32 / 9.42 s, 21.48 / 21.52 s, 40.66 / 40.68 s (≤ 0.1 s apart).
* Numbers come out as digits ("candidate number 12") — match on digits and words.
* ~13 % of "twelve/fifteen" mentions in the long file were missed or merged —
  plan for Hiring Right to tolerate an occasional missed announcement.
* Before per-piece fallback, an earlier synthetic build (with a garbled voice)
  tripped the repeated-phrase check and pushed the whole 75-min file to the CPU
  (14 min). Now only the failing piece is redone on the CPU.
* Silent / unreadable video → `<name>.error.txt`; tested with unit tests.
* Real batch videos (Filipino/Indian English, room noise, overlapping voices)
  are **not yet tested** — please drop one real video in when you have it.

## 5. Floating assistant

Rendered offscreen on your Mac in every state (idle, recording with level
bars, transcribing, success preview in Devanagari, failure, language chip) —
see `santa-app/dev/pill_preview.py`. It is a non-activating, always-on-top
panel on all Spaces. Clicking it to start/stop and pasting into another app
need your macOS permissions and are **awaiting your check**.

## 6. Known limitations
* **Hinglish**: Whisper has no code-switching mode. With the default strategy
  English words usually stay in Latin, but a common English word said with a
  strong accent can still come out in Devanagari (e.g. "सालरी" for "salary"),
  and fast switching mid-phrase can drop or merge words ("otherwise" →
  "और दवाइस" in one clip). Romanization is a rule-based heuristic: readable, not
  standardised spelling (e.g. "achchha", "zindgi").
* **Arabic dialects**: Whisper is trained mostly on MSA. Kuwaiti/Gulf words
  are recognised when common ("شلونك", "الدوام"), but dialect-specific words
  (e.g. "باچر") were misrecognised even in the synthetic test. Expect more
  errors on fast conversational Kuwaiti speech; this is untested on real speech.
* **Bengali** is the weakest of the five (CER ≈ 0.3 synthetic); choose Bengali
  explicitly — Auto-detect confuses it with Hindi.
* Numbers: Whisper may write spoken numbers as digits ("six" → "6", "पच्चीस" → "25").
* The GPU path is used only with Large-v3 Turbo; other models run on the CPU.
* Very long continuous speech without any pause > 0.3 s is cut at 27 s.
* Browser dictation requires the Santa page to stay open; hotkey dictation
  requires its Terminal window to stay open.


## 7. New features (measured 2026-09-28 on the Mac, M5)

All audio below is **synthetic** (macOS `say` voices), not real speech.

**Translation** (Argos Translate models, offline, CPU; times after the one-time download):

| Pair | Input | Output | Time |
|---|---|---|---|
| hi → en | मैं कल सुबह दफ़्तर जाऊँगा और बजट के बारे में मीटिंग करूँगा। | "I would like to meet the budget in the morning." — **wrong meaning** | 0.4 s |
| bn → en | আমি আগামীকাল অফিসে যাব এবং বাজেট নিয়ে আলোচনা করব। | "I'll go to the office tomorrow and discuss the budget." | 0.2 s |
| ar → en | سأذهب إلى المكتب غدًا صباحًا لمناقشة الميزانية. | "I'm going to the office tomorrow morning to discuss the budget." | 0.2 s |
| en → ar | The interview is scheduled for tomorrow at ten in the morning. | موعد المقابلة غداً في العاشرة صباحاً | 0.2 s |
| hi → ar (via en) | उम्मीदवार के पास पाँच साल का अनुभव है। | وللمرشح خبرة خمس سنوات. | 0.1 s |

The Hindi → English model is weak (1 of 2 Hindi sentences lost its meaning); the
UI labels every translation and keeps the original. A stronger free model with a
licence that allows business use was not found yet (NLLB is non-commercial).

**Speaker separation** (sherpa-onnx: pyannote segmentation 3.0 + 3D-Speaker CAM++):
* 34 s synthetic interview, 2 voices (Samantha/Daniel), 6 turns, through the full
  pipeline: **all 6 turns labelled correctly**; 16.8 s total including the one-time
  35 MB model download.
* After naming Speaker 1 "Test Interviewer", a second synthetic conversation
  labelled that voice by name automatically and left the other as "Speaker 2".
* Same file with Arabic output: speaker labels kept, each turn translated.
* In the Linux container: pyannote's real 2-speaker English sample 99.3 % frame
  agreement, sherpa's real 4-speaker Chinese sample 4 of 4 found; RTF ≈ 0.18 on
  2 cores. A 30-minute tiled file found 4 speakers instead of 2 at an older
  threshold (0.65); 0.7 is now used. **Not yet tested on a real interview**,
  or on Hindi/Arabic/Bengali conversations.

**iPhone access** (from the Mac to itself over its LAN address, trusting only
Santa's CA as the iPhone will): page over TLS 200; API before pairing 401;
pairing 200; phone-mode transcription of a 4.5 s clip in 1.5 s; the phone was
refused when it tried to quit Santa (403); Shortcut request answered with the
text in 2.0 s; a client that does not trust the CA failed the TLS handshake.
**Not yet tried on the real iPhone.**
