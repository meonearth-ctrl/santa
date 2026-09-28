# Real recordings for Santa's accuracy check

Synthetic voices can't tell us how Santa handles *your* voice, a Kuwaiti accent
or natural Hindi–English switching. To run the real check:

1. Record short clips (5–30 s each) with Santa's own recorder or Voice Memos.
   Suggested set: English with names and numbers, Hindi, Arabic (formal),
   Arabic (Kuwaiti/Gulf, conversational), Bengali, two or three Hinglish
   sentences, and one longer (1–2 min) recording.
2. Save them here (any supported format: .m4a, .wav, .mp3 …).
3. Copy `manifest.example.json` to `manifest.json` and fill in, for each file,
   the language mode and exactly what you said (`text`).
4. Run `python santa-app/dev/bench.py --model large-v3-turbo --set human`.

Everything in this folder except this README and the example is git-ignored,
so your recordings never end up in version control.
