# santa/live.py
# Live interview mode for Hiring Right group interviews. While the interview
# runs, the Mac microphone is cut at pauses into 5-15 s pieces (Silero VAD, the
# same detector Santa uses to split long files), each piece is transcribed on
# the Metal GPU, tagged to a candidate seat (santa/seat_tagger.py) and appended
# to a rolling <label>.live.json in the export folder, about 5-10 s behind speech.
# On Stop the whole recording (kept on this Mac) goes through the normal
# offline pass, which writes the usual <label>.json + .srt: the record of truth.
#
# Audio never leaves the Mac: the only outputs are local files and the
# loopback-only /api/live feed (santa/server.py). With Live Assist on
# (santa/assist.py), the tagged transcript lines also go to Claude for probe
# suggestions and live rating; still text only. A replay source feeds a
# recorded video through the same path at real or accelerated speed, so the
# pipeline and its latency can be tested without a live room:
#     santa --replay "~/Documents/Hiring Right/02_Batch_Videos/<file>.mov" --speed 4

import json
import logging
import os
import queue
import re
import statistics
import threading
import time
import wave
from datetime import datetime, timezone

import numpy as np

from . import export, seat_tagger
from .audio_input import SAMPLE_RATE

logger = logging.getLogger(__name__)

# ── Tuning ───────────────────────────────────────────────────────────────────
MIN_PIECE_S = 5.0         # don't cut before this much speech, unless the pause is long;
                          # after it, the next >= 300 ms pause ends the piece
MAX_PIECE_S = 15.0        # always cut by this length (at the longest pause inside)
PAUSE_S = 0.4             # silence after Silero's padded speech end that ends a piece (~0.9 s real)
LONG_PAUSE_S = 1.0        # ...and that ends even a short piece ("Number four.")
VAD_EVERY_S = 0.3         # how often the endpointer looks at the buffer
EDGE_PAD_S = 0.2          # audio kept around speech at both ends of a piece
DEFAULT_SEATS = 10        # seats 1..N accepted when no roster is given
MAX_REPLAY_SPEED = 50.0

LABEL_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,79}$')
LIVE_SUFFIX = '.live.json'


class LiveError(Exception):
    """A refused start/stop, with a short code and a user-facing message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ── Inputs: label and roster ─────────────────────────────────────────────────
def check_label(label: str) -> str:
    label = (label or '').strip()
    if not LABEL_PATTERN.match(label):
        raise LiveError('bad_label', 'Give the group a label of letters, numbers, spaces, "_", "-" or "." '
                                     '(for example 2026-11-03_MNL_G4).')
    return label


def parse_roster(value) -> list:
    """Roster from the web page or API: text lines like "1 Rose Ancineta" /
    "2. Mark Cruz" / "3 - Ana", a {seat: name} mapping or a [{no, name}] list.
    Returns [{no, name}] sorted by seat; [] when nothing usable was given."""
    if isinstance(value, str):
        cands = []
        for line in value.splitlines():
            m = re.match(r'^\s*#?(\d{1,2})\s*[.):\-–]?\s*(.*?)\s*$', line)
            if m and int(m.group(1)):
                cands.append({'no': int(m.group(1)), 'name': m.group(2)})
        value = cands
    roster = seat_tagger.normalize_roster(value)
    by_seat = {c['no']: c for c in roster if 0 < c['no'] <= 99}
    return [by_seat[no] for no in sorted(by_seat)]


def roster_or_default(roster: list, seats: int = None) -> list:
    """Without a roster any seat 1..N may be tagged (by number only, no names)."""
    if roster:
        return roster
    count = seats if isinstance(seats, int) and 0 < seats <= 30 else DEFAULT_SEATS
    return [{'no': n, 'name': ''} for n in range(1, count + 1)]


def live_path(folder: str, label: str) -> str:
    return os.path.join(export.resolve_dir(folder), export.export_stem(label + '.wav') + LIVE_SUFFIX)


# ── Endpointing: cut the stream at pauses ────────────────────────────────────
def silero_regions(samples) -> list:
    """Speech regions [{'start', 'end'}] in samples, Silero VAD as bundled with faster-whisper."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    return get_speech_timestamps(samples, VadOptions(min_silence_duration_ms=300, speech_pad_ms=200))


class Endpointer:
    """Collects incoming audio and returns finished pieces, each cut at a pause.
    Pieces are (absolute start sample, float32 samples); silence between them is
    dropped. `vad` is injectable so tests can use a simple energy detector."""

    def __init__(self, vad=silero_regions, rate: int = SAMPLE_RATE):
        self.vad, self.rate = vad, rate
        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_start = 0                     # absolute sample index of _buffer[0]
        self._unchecked = 0                        # samples added since the last VAD look

    def feed(self, samples) -> list:
        self._buffer = np.concatenate([self._buffer, np.asarray(samples, dtype=np.float32)])
        self._unchecked += len(samples)
        if self._unchecked < VAD_EVERY_S * self.rate:
            return []
        self._unchecked = 0
        pieces = []
        while True:                                 # a long buffer can hold more than one piece
            piece = self._next_piece(final=False)
            if piece is None:
                return pieces
            pieces.append(piece)

    def flush(self) -> list:
        """End of the stream: whatever speech is left becomes the last piece(s)."""
        pieces = []
        while True:
            piece = self._next_piece(final=True)
            if piece is None:
                return pieces
            pieces.append(piece)

    def _next_piece(self, final: bool):
        rate, n = self.rate, len(self._buffer)
        regions = self.vad(self._buffer) if n else []
        if not regions:
            keep = int(EDGE_PAD_S * rate)          # silence only: drop all but a short tail
            if n > keep:
                self._drop(n - keep)
            return None
        first, last_end = regions[0]['start'], regions[-1]['end']
        trailing = (n - last_end) / rate
        speech = (last_end - first) / rate
        # A pause Silero already closed (>= 300 ms, speech resumed after it) once
        # MIN_PIECE_S of speech has built up: cut there now rather than waiting
        # for a long pause, so a fluent speaker doesn't hold text back for 15 s.
        closed_pauses = [(a['end'] + b['start']) // 2 for a, b in zip(regions, regions[1:])
                         if a['end'] - first >= MIN_PIECE_S * rate]
        if closed_pauses:
            cut = closed_pauses[0]
        elif final or (trailing >= PAUSE_S and speech >= MIN_PIECE_S) or trailing >= LONG_PAUSE_S:
            cut = min(n, last_end + int(EDGE_PAD_S * rate))
        elif (n - first) / rate >= MAX_PIECE_S:
            cut = self._cut_inside(regions, first)
        else:
            return None
        start = max(0, first - int(EDGE_PAD_S * rate))
        piece = (self._buffer_start + start, self._buffer[start:cut].copy())
        self._drop(cut)
        return piece if len(piece[1]) else None

    def _cut_inside(self, regions: list, first: int) -> int:
        """A long stretch of speech with no pause after MIN_PIECE_S: cut in the
        middle of its longest earlier pause that leaves a piece of reasonable
        length, or hard at MAX_PIECE_S if there is none."""
        rate = self.rate
        limit = first + int(MAX_PIECE_S * rate)
        gaps = [(b['start'] - a['end'], (a['end'] + b['start']) // 2)
                for a, b in zip(regions, regions[1:])
                if a['end'] - first >= 0.4 * MIN_PIECE_S * rate and (a['end'] + b['start']) // 2 <= limit]
        return max(gaps)[1] if gaps else limit

    def _drop(self, count: int) -> None:
        self._buffer = self._buffer[count:]
        self._buffer_start += count


# ── Audio sources: the Mac microphone, or a recorded file for replay ─────────
class MicSource:
    """Records the Mac's default (or chosen) microphone at 16 kHz mono."""
    kind = 'microphone'
    speed = 1.0

    def __init__(self, device=None):
        self.device = device
        self._stream = None

    def start(self, on_audio, on_end) -> None:
        import sounddevice as sd

        def callback(indata, frames, time_info, status):
            on_audio(indata[:, 0].copy())

        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32',
                                      device=self.device or None, callback=callback, blocksize=1600)
        self._stream.start()
        self._on_end = on_end

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._on_end()


class FileSource:
    """Plays a recorded audio/video file into the session in 0.1 s blocks, paced
    at `speed` x real time, as if it were the microphone."""
    kind = 'replay'

    def __init__(self, path: str, speed: float = 1.0, max_duration_s: float = 4 * 3600):
        from .audio_input import decode_path
        self.path = path
        self.speed = min(max(float(speed or 1.0), 0.1), MAX_REPLAY_SPEED)
        self.samples = decode_path(path, max_duration_s).samples
        self._stop = threading.Event()
        self._thread = None

    def start(self, on_audio, on_end) -> None:
        def play():
            block, started = SAMPLE_RATE // 10, time.time()
            for pos in range(0, len(self.samples), block):
                if self._stop.is_set():
                    break
                delay = started + pos / SAMPLE_RATE / self.speed - time.time()
                if delay > 0:
                    time.sleep(delay)
                on_audio(self.samples[pos:pos + block])
            on_end()
        self._thread = threading.Thread(target=play, daemon=True, name='santa-live-replay')
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ── One live session ─────────────────────────────────────────────────────────
class LiveSession:
    """Source -> endpointer -> transcriber -> seat tags -> <label>.live.json.
    Three threads keep the stages apart, so cutting the next piece never waits
    for the GPU. `transcribe(samples, language)` returns Santa's raw result
    ({'segments': [...piece-relative...], 'detected_language', 'engine'})."""

    def __init__(self, label: str, source, transcribe, export_dir: str, language_mode: str = 'en',
                 roster: list = None, seats: int = None, audio_path: str = None, model: str = '',
                 vad=silero_regions, on_stopped=None):
        self.label = check_label(label)
        self.source, self.transcribe = source, transcribe
        self.language_mode, self.model = language_mode, model
        self.roster = roster or []
        self.cands = roster_or_default(self.roster, seats)
        self.tagger = seat_tagger.SeatTagger(self.cands)
        self.export_dir = export.resolve_dir(export_dir)
        self.path = live_path(self.export_dir, self.label)
        self.audio_path = audio_path              # None: don't keep the audio (replays)
        self.on_stopped = on_stopped
        self.endpointer = Endpointer(vad)
        self.state = 'starting'                   # starting | recording | finishing | stopped | error
        self.error = None
        self.segments = []                        # what the live file holds
        self.latencies, self.piece_latencies = [], []
        self.transcribe_times = []                # GPU/CPU seconds per piece (diagnostics)
        self.cpu_pieces = 0                       # pieces the GPU output checks sent to the CPU engine
        self.engine, self.language = None, None
        self.offline_job_id = None
        self.assist = None                        # AssistSession when Live Assist is on
        self.wants_offline = False                # run the full offline pass after Stop
        self.replay_source = None                 # the replayed file (offline pass input), if any
        self.started_at = time.time()
        self._samples_in = 0
        self._clock_start = None                  # wall time of audio sample 0
        self._lock = threading.Lock()
        self._audio = queue.Queue()
        self._pieces = queue.Queue()
        self._wav = None
        self._threads = []

    # ── Control ──────────────────────────────────────────────────────────
    def start(self) -> None:
        os.makedirs(self.export_dir, exist_ok=True)
        if self.audio_path:
            os.makedirs(os.path.dirname(self.audio_path), exist_ok=True)
            self._wav = wave.open(self.audio_path, 'wb')
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SAMPLE_RATE)
        for target, name in ((self._cut_loop, 'santa-live-cut'), (self._transcribe_loop, 'santa-live-asr')):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)
        self.state = 'recording'
        self._write()
        try:
            self.source.start(self._audio.put, lambda: self._audio.put(None))
        except Exception as exc:
            self._fail(f'Could not open the audio source ({type(exc).__name__}: {exc}).')
            self._audio.put(None)
            raise LiveError('no_audio', self.error) from exc

    def stop(self) -> None:
        """Stop listening; the remaining speech is still transcribed (state
        'finishing') before the session is 'stopped'."""
        if self.state == 'recording':
            self.state = 'finishing'
            self._write()
        self.source.stop()

    def wait(self, timeout: float = None) -> bool:
        deadline = None if timeout is None else time.time() + timeout
        for thread in self._threads:
            thread.join(None if deadline is None else max(0.0, deadline - time.time()))
        return self.state in ('stopped', 'error')

    @property
    def active(self) -> bool:
        return self.state in ('starting', 'recording', 'finishing')

    # ── Stage 1: audio in, pieces out ────────────────────────────────────
    def _cut_loop(self) -> None:
        try:
            while True:
                block = self._audio.get()
                if block is None:
                    break
                if self._clock_start is None:      # wall time of the first sample of this block
                    self._clock_start = time.time() - len(block) / SAMPLE_RATE / self.source.speed
                before = self._samples_in
                self._samples_in += len(block)
                if self.assist is not None and before // SAMPLE_RATE != self._samples_in // SAMPLE_RATE:
                    self.assist.tick(self._samples_in / SAMPLE_RATE)     # once per second of audio
                if self._wav is not None:
                    self._wav.writeframes((np.clip(block, -1, 1) * 32767).astype('<i2').tobytes())
                for piece in self.endpointer.feed(block):
                    self._pieces.put(piece)
            if self.state == 'recording':          # the source ended by itself (replay finished)
                self.state = 'finishing'
            for piece in self.endpointer.flush():
                self._pieces.put(piece)
        except Exception as exc:
            logger.exception('live %s: audio stage failed', self.label)
            self._fail(f'Audio processing failed ({type(exc).__name__}).')
        finally:
            if self._wav is not None:
                self._wav.close()
            self._pieces.put(None)

    # ── Stage 2: pieces in, tagged segments out ──────────────────────────
    def _transcribe_loop(self) -> None:
        try:
            while True:
                piece = self._pieces.get()
                if piece is None:
                    break
                self._transcribe_piece(*piece)
        except Exception as exc:
            logger.exception('live %s: transcription failed', self.label)
            self._fail(f'Live transcription failed ({type(exc).__name__}).')
        if self.state != 'error':
            self.state = 'stopped'
        self._write()
        logger.info('live %s: %s, %.0fs audio, %d segments, latency %s', self.label, self.state,
                    self._samples_in / SAMPLE_RATE, len(self.segments), _stats(self.latencies))
        if self.on_stopped:
            self.on_stopped(self)

    def _transcribe_piece(self, start_sample: int, samples) -> None:
        offset = start_sample / SAMPLE_RATE
        began = time.time()
        raw = self.transcribe(samples, self.language)
        transcribe_seconds = time.time() - began
        engine = raw.get('engine') or ''
        if 'CPU' in engine:
            self.cpu_pieces += 1
        if engine and (self.engine is None or 'CPU' not in engine):   # name the main engine, not the fallback
            self.engine = engine
        self.language = self.language or raw.get('detected_language')
        new = []
        for seg in raw.get('segments') or []:
            text = (seg.get('text') or '').strip()
            if not text:
                continue
            start, end = round(seg['start'] + offset, 2), round(seg['end'] + offset, 2)
            seat, how = self.tagger.tag(start, end, text)
            item = {'start': start, 'end': end, 'text': text}
            if seat is not None:
                item['seat'], item['seat_how'] = seat, how
            new.append(item)
        written = time.time()
        with self._lock:
            self.segments.extend(new)
            # Latency = when the line was written minus when its last word was spoken.
            spoken_at = self._wall_time
            self.latencies.extend(round(written - spoken_at(seg['end']), 2) for seg in new)
            self.piece_latencies.append(round(written - spoken_at(offset + len(samples) / SAMPLE_RATE), 2))
            self.transcribe_times.append(round(transcribe_seconds, 2))
        self._write()
        if self.assist is not None and new:
            self.assist.add_lines(new, self._samples_in / SAMPLE_RATE)

    def _wall_time(self, audio_seconds: float) -> float:
        return (self._clock_start or self.started_at) + audio_seconds / self.source.speed

    def _fail(self, message: str) -> None:
        self.state, self.error = 'error', message
        self._write()

    # ── Output ───────────────────────────────────────────────────────────
    def payload(self) -> dict:
        """The live file: Santa's export schema plus "live": true and seat tags."""
        with self._lock:
            segments = list(self.segments)
            latency = {'segment': _stats(self.latencies), 'piece': _stats(self.piece_latencies),
                       'transcribe': _stats(self.transcribe_times)}
        return {
            'source': self.label,
            'model': self.model,
            'engine': (self.engine or 'pending') + ' · live',
            'language': self.language,
            'audio_seconds': round(self._samples_in / SAMPLE_RATE, 2),
            'live': True,
            'state': self.state,
            'error': self.error,
            'input': self.source.kind,
            'speed': self.source.speed,
            'started_at': _iso(self.started_at),
            'updated_at': _iso(time.time()),
            'seats': [{'no': c['no'], 'name': c['name']} for c in self.roster],
            'latency_seconds': latency,
            'pieces': {'total': len(self.transcribe_times), 'redone_on_cpu': self.cpu_pieces},
            'offline_job': self.offline_job_id,
            'assist': self.assist.path if self.assist is not None else None,
            'segments': segments,
        }

    def _write(self) -> None:
        try:
            export._atomic_write(self.path, json.dumps(self.payload(), ensure_ascii=False, indent=1))
        except OSError as exc:
            logger.warning('live %s: could not write the live file: %s', self.label, exc)

    def public(self) -> dict:
        p = self.payload()
        p.pop('segments')
        p.update(label=self.label, segments=len(self.segments), path=self.path, audio_path=self.audio_path)
        return p


def feed(payload: dict, since: float = None) -> dict:
    """What the probe service polls: segments that END after `since` (seconds
    of interview audio), plus `next_since` to pass on the next poll. Pieces are
    cut at pauses and never overlap, so nothing is skipped or repeated."""
    segments = payload.get('segments') or []
    if since is not None:
        segments = [seg for seg in segments if seg['end'] > since]
    out = {k: v for k, v in payload.items() if k != 'segments'}
    out['label'] = payload.get('source')
    out['segments'] = segments
    out['next_since'] = segments[-1]['end'] if segments else since
    return out


def _stats(values: list) -> dict:
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(ordered), 'median': round(statistics.median(ordered), 2),
            'p90': ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 'max': ordered[-1]}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds')


# ── Manager: one session at a time, owned by the transcription service ───────
class LiveManager:
    """Starts and stops sessions for the server. One live session (or replay)
    runs at a time; finished ones stay readable from memory, older ones from
    their .live.json file."""

    def __init__(self, service, audio_dir: str):
        self.service = service
        self.audio_dir = audio_dir
        self.current = None
        self.vad = silero_regions                 # replaceable in tests
        self.make_client = None                   # Claude client factory (tests pass a fake)
        self.rating_viewers = set()
        self._recent = {}
        self._lock = threading.Lock()

    def start(self, label: str, roster=None, seats: int = None, language: str = None,
              replay_path: str = None, speed: float = 1.0, offline: bool = None,
              assist: bool = False, track: str = 'team') -> LiveSession:
        s = self.service.settings.get()
        label = check_label(label)
        language = language or s['watch_language']
        self.service._check_language(language)
        export_dir = s['export_dir'] or '~/Documents/Hiring Right/03_Transcripts'
        offline = (replay_path is None) if offline is None else bool(offline)
        with self._lock:
            if self.current is not None and self.current.active:
                raise LiveError('busy', f'Live session "{self.current.label}" is still running; stop it first.')
            final_json = os.path.join(export.resolve_dir(export_dir), export.export_stem(label + '.wav') + '.json')
            if os.path.exists(live_path(export_dir, label)) or (offline and os.path.exists(final_json)):
                raise LiveError('exists', f'"{label}" already has a transcript; choose another label.')
            if replay_path:
                from .settings import duration_limit_seconds
                source = FileSource(os.path.expanduser(replay_path), speed, duration_limit_seconds(s))
            else:
                source = MicSource(s.get('microphone_id') or None)
            audio_path = os.path.join(self.audio_dir, export.export_stem(label + '.wav') + '.wav') \
                if replay_path is None else None
            session = LiveSession(label, source, self._transcriber(language), export_dir, language,
                                  parse_roster(roster), seats, audio_path=audio_path, model=s['model'],
                                  vad=self.vad, on_stopped=self._finished)
            session.replay_source = replay_path
            session.wants_offline = offline
            if assist:
                source_name = label + (os.path.splitext(replay_path)[1] if replay_path else '.wav')
                session.assist = self._assist_for(session, track, s, export_dir, source_name)
            self.rating_viewers = set()           # who may see live ratings: 'mac' and lead phone ids
            self.current = session
            self._recent[label] = session
        session.start()
        logger.info('live %s: started (%s, %d seats named)', label, session.source.kind, len(session.roster))
        return session

    def stop(self) -> LiveSession:
        session = self.current
        if session is None or not session.active:
            raise LiveError('not_running', 'No live session is running.')
        session.stop()
        return session

    def get(self, label: str):
        return self._recent.get(label)

    def feed(self, label: str, since: float = None):
        """Feed for a label from memory, or from its .live.json on disk."""
        session = self.get(label)
        if session is not None:
            return feed(session.payload(), since)
        path = live_path(self.service.settings.get()['export_dir'] or '~/Documents/Hiring Right/03_Transcripts',
                         label)
        try:
            with open(path, encoding='utf-8') as fh:
                return feed(json.load(fh), since)
        except (OSError, ValueError):
            return None

    def status(self) -> dict:
        current = self.current.public() if self.current is not None else None
        return {'current': current, 'recent': [s.label for s in self._recent.values()]}

    def _transcriber(self, language_mode: str):
        def transcribe(samples, language):
            return self.service.transcribe_clip(samples, language_mode, language)
        return transcribe

    def _assist_for(self, session: LiveSession, track: str, s: dict, export_dir: str, source_name: str):
        """Live Assist for a session, or LiveError saying what is missing."""
        from . import assist, assist_prompts
        key = assist.api_key()
        if self.make_client is None and not key:
            raise LiveError('no_key', 'Live Assist needs the Anthropic API key in ~/.config/hiring-right/anthropic-key.')
        try:
            cfg = assist_prompts.load_config(s['assist_config_dir'])
        except (OSError, ValueError, KeyError) as exc:
            raise LiveError('no_config', f'Live Assist could not read the Hiring Right config in '
                                         f'{s["assist_config_dir"]} ({type(exc).__name__}).') from exc
        try:
            client = self.make_client() if self.make_client else assist.make_client(key)
        except ImportError as exc:
            raise LiveError('no_sdk', 'Live Assist needs the anthropic package (run the Santa installer again).') from exc
        try:
            return assist.AssistSession(session.label, track, session.roster, cfg, client, s,
                                        assist.SpendLedger(self.service.settings.home), export_dir, source_name)
        except assist.AssistError as exc:
            raise LiveError(exc.code, exc.message) from exc

    def assist(self):
        """The Live Assist of the current (or just finished) session, if any."""
        return self.current.assist if self.current is not None else None

    def _finished(self, session: LiveSession) -> None:
        """After Stop: Live Assist's final rating and output file (in the
        background, it takes a few seconds), then the offline pass."""
        if session.assist is not None:
            threading.Thread(target=session.assist.finish, args=(session._samples_in / SAMPLE_RATE,),
                             daemon=True, name='santa-assist-finish').start()
        if session.wants_offline:
            self._offline_pass(session)

    def _offline_pass(self, session: LiveSession) -> None:
        """The kept recording (or the replayed file) goes through the normal
        offline pass (speaker separation per Settings) -> <label>.json + .srt."""
        recording = session.audio_path or session.replay_source
        if session.state != 'stopped' or not recording or not session._samples_in:
            return
        try:
            name = session.label + os.path.splitext(recording)[1]
            job = self.service.submit_file(recording, name, session.language_mode,
                                           source='live', delete_after=False)
            session.offline_job_id = job.id
            session._write()
        except Exception as exc:
            logger.warning('live %s: offline pass not queued: %s', session.label, exc)
            session.error = f'Offline pass not queued ({type(exc).__name__}); transcribe {session.audio_path} by hand.'
            session._write()


# ── `santa --replay FILE`: test live mode without a live room ────────────────
def load_roster_file(path: str, stem: str):
    """A roster JSON: {seat: name}, [{no, name}], or Hiring Right's
    transcript-group-rosters.json ({stem: {cands: [...]}}, picked by file stem)."""
    with open(os.path.expanduser(path), encoding='utf-8') as fh:
        data = json.load(fh)
    if isinstance(data, dict) and stem in data and isinstance(data[stem], dict):
        data = data[stem].get('cands') or []
    elif isinstance(data, dict) and isinstance(data.get('cands'), list):
        data = data['cands']
    return parse_roster(data)


def run_replay(path: str, speed: float = 1.0, label: str = None, roster_path: str = None,
               language: str = None, out_dir: str = None, offline: bool = False,
               assist_track: str = None) -> int:
    """Feed a recorded file through live mode in this process (no server needed,
    so it also works while the installed Santa is running) and print the
    latency from speech end to segment written."""
    from .accel_cpp import MetalAccelerator
    from .service import TranscriptionService
    from .settings import SettingsStore, santa_home

    home = santa_home()
    settings = SettingsStore(home)
    s = settings.get()
    stem = export.export_stem(path)
    label = check_label(label or f'{stem}.replay')
    roster = load_roster_file(roster_path, stem) if roster_path else []
    service = TranscriptionService(settings=settings, accelerator=MetalAccelerator(os.path.join(home, 'logs')))
    service.preload()
    print(f'Loading models for {s["model"]}…', flush=True)
    deadline = time.time() + 600
    while service._accel_wanted(s) and not service.accel.ready() and time.time() < deadline:
        if service.accel.get_status()['state'] in ('error', 'unavailable'):
            break
        time.sleep(0.5)
    language = language or s['watch_language']
    out_dir = out_dir or s['export_dir'] or '~/Documents/Hiring Right/03_Transcripts'
    source = FileSource(os.path.expanduser(path), speed)
    session = LiveSession(label, source, lambda samples, lang: service.transcribe_clip(samples, language, lang),
                          out_dir, language, roster, audio_path=None, model=s['model'])
    session.replay_source = os.path.expanduser(path)
    if assist_track:
        session.assist = _replay_assist(session, assist_track, s, settings.home, out_dir,
                                        label + os.path.splitext(path)[1], stem)
    print(f'Replaying {os.path.basename(path)} ({len(source.samples) / SAMPLE_RATE / 60:.1f} min) at {source.speed:g}x '
          f'-> {session.path}  ({len(roster)} named seats)', flush=True)
    session.start()
    shown = 0
    try:
        while session.active:
            time.sleep(1.0)
            for seg in session.segments[shown:]:
                seat = seg.get('seat')
                tag = f'#{seat}' if isinstance(seat, int) else ('panel' if seat else '?')
                print(f'  [{seg["start"]:7.1f}] {tag:>6}  {seg["text"][:90]}', flush=True)
            shown = len(session.segments)
    except KeyboardInterrupt:
        session.stop()
    session.wait(timeout=120)
    if session.assist is not None:
        print('Live Assist: final rating…', flush=True)
        session.assist.finish(session._samples_in / SAMPLE_RATE)
        _print_assist(session.assist)
    p = session.payload()
    tagged = sum(isinstance(seg.get('seat'), int) for seg in p['segments'])
    print(f'\nDone: {p["state"]}, {p["audio_seconds"] / 60:.1f} min audio, {len(p["segments"])} segments, '
          f'{tagged} tagged to a seat; engine {p["engine"]}')
    print(f'Latency (speech end -> segment written, wall seconds at {source.speed:g}x): '
          f'segment {p["latency_seconds"]["segment"]}, piece end {p["latency_seconds"]["piece"]}')
    print(f'Transcription per piece: {p["latency_seconds"]["transcribe"]}; pieces {p["pieces"]}')
    if offline and session.state == 'stopped':
        job = service.submit_file(session.replay_source, label + os.path.splitext(path)[1], language,
                                  source='live', delete_after=False)
        print('Offline pass running…', flush=True)
        while job.state not in ('done', 'error', 'cancelled'):
            time.sleep(1)
        print(f'Offline pass: {job.state} {(job.result or {}).get("exported") or job.error}')
    return 0 if session.state == 'stopped' else 1


def _replay_assist(session: LiveSession, track: str, s: dict, home: str, out_dir: str, source_name: str,
                   recording_stem: str):
    """Live Assist for a command-line replay (stops with a message when it can't start)."""
    from . import assist, assist_prompts
    key = assist.api_key()
    if not key:
        raise SystemExit('Live Assist needs the Anthropic API key in ~/.config/hiring-right/anthropic-key.')
    cfg = assist_prompts.load_config(s['assist_config_dir'])
    helper = assist.AssistSession(session.label, track, session.roster, cfg, assist.make_client(key), s,
                                  assist.SpendLedger(home), out_dir, source_name)
    # The recording's real activities (if Hiring Right has them) stand in for the panel's taps.
    try:
        with open(os.path.join(os.path.expanduser(s['assist_config_dir']), 'phases.json'), encoding='utf-8') as fh:
            phases = json.load(fh).get(recording_stem) or []
    except (OSError, ValueError):
        phases = []
    helper.play_phases(phases)
    print(f'Live Assist ({track}): {len(phases)} recorded activities will be marked as the replay passes them.')
    return helper


def _print_assist(a) -> None:
    p = a.payload()
    probes = [c for c in p['calls'] if c['kind'] == 'probes']
    ratings = [c for c in p['calls'] if c['kind'] == 'rating']
    median = lambda xs: statistics.median(xs) if xs else 0
    print(f"Live Assist: {len(probes)} probe calls (median {median([c['ms'] for c in probes]) / 1000:.1f} s), "
          f"{len(p['suggestions'])} cards; {len(ratings)} rating calls "
          f"(median {median([c['ms'] for c in ratings]) / 1000:.1f} s); cost ${p['cost']['total']:.3f} "
          f"(probes ${p['cost']['probes']:.3f}, rating ${p['cost']['rating']:.3f}) -> {a.path}")
