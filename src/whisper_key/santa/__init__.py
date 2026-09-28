# santa/__init__.py
# "Santa" — a personal multilingual dictation & transcription app built on top of
# Whisper Local. It adds a local browser UI (served only on 127.0.0.1), language
# modes for English / Hindi / Arabic / Hinglish / Bengali, and a reusable
# transcription service that a future phone front-end (Phase 2) can call.
#
# Layering (keep it this way — Phase 2 depends on it):
#   audio_input   decode/validate uploaded or recorded audio     (no UI, no OS hooks)
#   models        model catalogue, download, load, inference     (no UI, no OS hooks)
#   languages     language modes -> Whisper parameters
#   textproc      Unicode-safe post-processing (raw text always preserved)
#   romanize      optional Devanagari -> Hinglish Latin step
#   service       job queue tying the above together            (no UI, no OS hooks)
#   settings / history   local JSON storage outside the repo
#   server        localhost HTTP API + static web UI
#   cli           `santa` command: start server, open browser
# Desktop hotkeys / clipboard / paste-at-cursor stay in the original whisper_key
# app and are never imported here.

__version__ = "0.1.0"
APP_NAME = "Santa"
