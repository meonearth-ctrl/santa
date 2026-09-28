# Santa — Phase 2 plan: using Santa from your phone

Status: **design only**. Nothing in this document is built or switched on yet.
Santa today listens only on `127.0.0.1` and the CLI refuses any other address.

## 1. What Phase 2 is (and isn't)

There are three very different things people mean by "dictation on my phone":

| # | What | Where speech recognition runs | Phase 2? |
|---|------|------------------------------|----------|
| 1 | Open a Santa web page on the phone, record there, get the transcript back in that page | **Your Mac** | **Yes — the target** |
| 2 | Dictate into *any* phone app (WhatsApp, Mail…) via a custom keyboard | Your Mac or the phone | No — needs a native iOS keyboard extension / Android IME |
| 3 | Recognition fully on the phone, offline | The phone | No — needs a native app with an on-device model |

A home-screen web app (PWA) gives you an icon and a full-screen page. It does
**not** give system-wide dictation in other apps, and it does **not** run
Whisper on the phone.

With option 1:

* the Mac must be **awake, running Santa, and reachable** from the phone;
* **audio travels from the phone to the Mac** (over your Wi-Fi, encrypted);
* the transcript comes back to the phone page, where you edit, copy or download it.

## 2. What already exists and is reused unchanged

| Component | File | Why it's reusable |
|-----------|------|-------------------|
| Transcription service + job queue (1 worker, max 3 pending, cancel, TTL) | `santa/service.py` | No HTTP, clipboard or hotkey code; takes bytes + language, returns structured result |
| Audio validation/decoding (size & duration limits, temp files deleted) | `santa/audio_input.py` | Decodes WAV, MP3, **M4A/AAC (iOS)**, **WebM/Opus (Android)**, OGG via PyAV |
| Language modes, Hinglish strategies, romanization, Unicode-safe cleanup | `languages.py`, `romanize.py`, `textproc.py` | Pure functions |
| Model manager (one model in memory, background load, progress) | `santa/models.py` | Shared by all clients |
| JSON API: `/api/transcribe`, `/api/jobs/<id>`, `/cancel`, `/api/config`, `/api/settings`, `/api/history` | `santa/server.py` | Phone page can call the same endpoints |
| Browser UI (plain JS, responsive layout, WAV encoding in the browser, RTL-aware textarea) | `santa/web/` | Already works on narrow screens; recorder uses standard Web Audio |

## 3. What has to be added

### 3.1 Network & transport security
1. **Separate "phone mode" listener**, off by default, started explicitly
   (`santa --phone`), binding to the Mac's LAN address on a separate port (e.g. 8766).
   The localhost UI stays unauthenticated-but-loopback-only as today.
2. **HTTPS is mandatory.** Mobile Safari and Chrome only allow microphone access
   (`getUserMedia`) in a *secure context*; `http://192.168.x.x` will not work.
   Plan: create a small **local certificate authority** once (e.g. with `mkcert`
   or Python `cryptography`), issue a certificate for the Mac's `.local` name and
   LAN IP, and install/trust the CA certificate on the phone
   (iOS: Settings › General › VPN & Device Management, then
   Settings › General › About › Certificate Trust Settings; Android: Settings ›
   Security › Install certificate). Document how to remove it again.
3. **Pairing / authentication.** On first use the Mac shows a QR code with a
   one-time pairing code; the phone exchanges it for a long random device token
   (stored as an HttpOnly, Secure, SameSite=Strict cookie). Tokens are listed
   and revocable in Settings. No token → 401. Rate-limit pairing attempts.
4. **Strict origin handling:** accept state-changing requests only from the exact
   phone-mode origin (`https://<mac>.local:8766`); keep the `X-Santa` header
   check; no CORS headers at all.
5. **Request limits:** per-device rate limit, upload cap (default 50 MB for
   phone), the existing 3-job queue shared across devices, 429 with retry-after.
6. **Never** router port-forwarding or exposure to the internet.

### 3.2 Phone recording compatibility
* The current recorder (AudioWorklet → 16 kHz WAV) works in iOS Safari 14.5+
  and Android Chrome; keep it as the primary path because it avoids codec
  differences entirely.
* Fallback: `MediaRecorder` (iOS produces `audio/mp4` AAC, Android
  `audio/webm;codecs=opus`) — both already decode on the server.
* iOS specifics: audio capture must start from a user tap; the page must stay
  in the foreground (screen lock stops recording) — show a warning; use a
  wake-lock where supported.

### 3.3 Reliability on mobile networks
* **Upload resume / retry:** keep the recorded blob in memory (and optionally
  IndexedDB) until the server confirms; retry with exponential back-off; show
  "Waiting for your Mac…" when unreachable.
* **Idempotency key** per recording so a retried upload doesn't create a
  second job.
* **Reconnect-safe polling:** jobs are already addressable by id; the phone
  re-polls after network changes. Add Server-Sent Events later if polling cost
  matters.
* Longer job TTL for phone jobs (e.g. 1 h) so results survive a dropped connection.

### 3.4 PWA (optional)
`manifest.webmanifest` + icons + a minimal service worker that caches only the
static UI (never audio or transcripts). Gives "Add to Home Screen". Clearly
labelled as needing the Mac to be on.

### 3.5 Outside the home network (separate, later option)
Use a **private, authenticated overlay network** such as Tailscale or a
WireGuard VPN to the Mac, with Santa still bound only to that private
interface and still requiring pairing. No public URL, no port forwarding.

## 4. Implementation steps (estimate: 2–4 focused days)
1. `santa --phone`: second `SantaServer` bound to LAN IP with TLS
   (`ssl.SSLContext` wrapping the socket), phone-mode flag on the handler.
2. Local CA + certificate generation command, with install/uninstall guide.
3. Pairing endpoints + QR code in the desktop UI; token store in
   `~/Library/Application Support/Santa/devices.json`; revoke UI.
4. Auth middleware, origin allow-list, per-device rate limiting.
5. Mobile layout pass, wake-lock, foreground warning, retry/idempotency.
6. Tests: TLS handshake, pairing, token revocation, cross-origin rejection,
   iOS `.m4a` and Android `.webm` fixtures, interrupted upload.
7. Real-device checks on one iPhone (Safari) and one Android (Chrome).

## 5. Risks to decide on before building
* Trusting a local CA on the phone is a real security decision — it lets that CA
  vouch for any site on the phone. Mitigation: name-constrained CA or remove it
  when not needed.
* Battery/network: a 1-minute WAV at 16 kHz is ~2 MB; fine on Wi-Fi.
* The Mac going to sleep ends availability; `caffeinate` while Santa runs is an option.
