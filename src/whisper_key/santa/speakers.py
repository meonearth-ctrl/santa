# santa/speakers.py
# Offline "who spoke when" for long recordings (e.g. HR interviews), plus an
# optional book of known voices. Uses sherpa-onnx (ONNX Runtime, no torch, no
# Hugging Face token): pyannote segmentation-3.0 finds speech turns, a CAM++
# speaker-embedding model tells voices apart, and the turns are then matched to
# Whisper's transcript segments. Everything runs on this computer; the only
# network use is the one-time model download from GitHub (no telemetry).

import bisect
import hashlib
import json
import logging
import os
import shutil
import ssl
import tarfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

import numpy as np

from .settings import santa_home

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# ── Models (downloaded once, on first use) ───────────────────────────────────
# Segmentation: pyannote segmentation-3.0 converted to ONNX by the sherpa-onnx
# project. Licence: MIT (CNRS, see LICENSE inside the archive). The archive is
# 6.9 MB; we keep only model.onnx (6.0 MB) and LICENSE.
SEGMENTATION_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/'
                    'speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2')
SEGMENTATION_ARCHIVE_BYTES = 6_958_444
SEGMENTATION_ARCHIVE_SHA256 = '24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488'
SEGMENTATION_MEMBER = 'sherpa-onnx-pyannote-segmentation-3-0/model.onnx'
SEGMENTATION_LICENSE_MEMBER = 'sherpa-onnx-pyannote-segmentation-3-0/LICENSE'
SEGMENTATION_FILE = 'pyannote-segmentation-3-0.onnx'
SEGMENTATION_LICENSE_FILE = 'pyannote-segmentation-3-0.LICENSE'

# Speaker embedding: 3D-Speaker CAM++ "zh_en common advanced" (iic on
# ModelScope; trained on ~200k Chinese + English speakers), 28.3 MB, 192-dim.
# Licence: Apache-2.0. Measured against five other sherpa-onnx embedding models
# (CAM++ VoxCeleb, ERes2Net base, NeMo TitaNet-small, WeSpeaker ResNet34 and
# CAM++) on three real/synthetic test files, it was the only one that found the
# right speaker count on all three, with the best agreement (99% on the
# 2-speaker English sample). Speaker identity is largely language-independent,
# which suits en/hi/ar/bn interviews (not yet measured on those languages).
# (The release tag really is spelled "speaker-recongition-models".)
EMBEDDING_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/'
                 'speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx')
EMBEDDING_BYTES = 28_281_164
EMBEDDING_SHA256 = 'aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2'
EMBEDDING_FILE = '3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx'
EMBEDDING_MODEL_ID = 'campplus_zh_en_advanced_16k'   # stored with every voiceprint

DOWNLOAD_MB = round((SEGMENTATION_ARCHIVE_BYTES + EMBEDDING_BYTES) / 1e6)

# ── Tuning (measured with santa-app/dev/diarize_check.py) ────────────────────
# Clustering threshold for automatic speaker count (num_speakers=0). sherpa-onnx
# merges clusters whose embeddings are closer than this; smaller → more
# speakers, larger → fewer. With this embedding model 0.6–0.75 found the right
# count on the three short test files (0.5 split the 2-speaker English sample
# into 3; 0.8 merged two of the 4 Chinese speakers), but on 10-minute audio
# 0.65 over-split 2 speakers into 4 while 0.7 stayed right. 0.7 was the only
# value correct on all five files. A known num_speakers is NOT always better:
# forcing 4 on the 10-minute 4-speaker file returned 3, while automatic found 4.
DEFAULT_CLUSTER_THRESHOLD = 0.7
MIN_DURATION_ON = 0.3     # drop speech turns shorter than this (seconds)
MIN_DURATION_OFF = 0.5    # join same-speaker turns separated by less than this

# Voiceprints: at most this much audio (the longest turns) per speaker, and
# turns shorter than MIN_TURN_SECONDS are only used if nothing longer exists.
VOICEPRINT_MAX_SECONDS = 30.0
MIN_TURN_SECONDS = 1.0

# Known-voice matching: cosine similarity between L2-normalised CAM++
# embeddings. Measured on real voices (5 people, 16 clips): with averaged
# voiceprints (several clips / diarized turns, as the app uses) the same person
# scored 0.61–0.87 and different people at most 0.48 (two people recorded in
# the same room; people on different recordings stayed below 0.39). Single
# 1.5–4 s clips overlap around 0.5, which is why voiceprints use up to 30 s.
# 0.55 sits in the gap and prefers "Speaker 2" over a wrong name when unsure.
DEFAULT_MATCH_THRESHOLD = 0.55

# A Whisper segment "clearly spans two speakers" when the runner-up speaker
# covers more than this share of it AND at least MIXED_MIN_SECONDS.
MIXED_SHARE = 0.40
MIXED_MIN_SECONDS = 1.5

# Share of diarization time spent in segmentation, which reports no progress
# (measured: 12 s of 35 s for 3 minutes of audio).
SEGMENTATION_SHARE = 0.35


def default_threads() -> int:
    """Light by default: half the cores, at most 4 (the Mac also runs Whisper)."""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


class DiarizationCancelled(Exception):
    pass


class SpeakerModelError(Exception):
    pass


# ── Download helpers ─────────────────────────────────────────────────────────
def _ssl_context():
    """Honour an explicit CA bundle, else certifi (python.org Mac builds ship no
    system certificates), else the platform default."""
    cafile = os.environ.get('SSL_CERT_FILE') or os.environ.get('REQUESTS_CA_BUNDLE')
    if not cafile:
        try:
            import certifi
            cafile = certifi.where()
        except Exception:
            cafile = None
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def _download(url: str, dest: str, expected_bytes: int, expected_sha256: str, on_bytes=None) -> None:
    """Download url to dest atomically (temp file, verify size + SHA-256, rename)."""
    tmp = dest + '.part'
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(url, headers={'User-Agent': 'Santa-dictation'})
    try:
        with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as response, \
                open(tmp, 'wb') as out:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                done += len(block)
                if on_bytes:
                    on_bytes(len(block))
        if done <= 0 or (expected_bytes and done != expected_bytes):
            raise SpeakerModelError(f'{os.path.basename(dest)}: got {done} bytes, expected {expected_bytes}')
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            raise SpeakerModelError(f'{os.path.basename(dest)}: checksum mismatch')
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _extract_member(archive: str, member: str, dest: str) -> None:
    """Copy one named file out of a .tar.bz2 (never extracts arbitrary paths)."""
    tmp = dest + '.part'
    with tarfile.open(archive, 'r:bz2') as tar:
        source = tar.extractfile(tar.getmember(member))
        if source is None:
            raise SpeakerModelError(f'{member} missing from archive')
        with source, open(tmp, 'wb') as out:
            shutil.copyfileobj(source, out)
    if os.path.getsize(tmp) <= 0:
        os.remove(tmp)
        raise SpeakerModelError(f'{member} is empty')
    os.replace(tmp, dest)


# ── Engine ───────────────────────────────────────────────────────────────────
class SpeakerEngine:
    """Diarization + speaker embeddings. sherpa objects are created once, on
    first use, and reused; a lock serialises all model calls."""

    def __init__(self, home=None, num_threads: int = 0, model_dir=None):
        self.home = home or santa_home()
        self.model_dir = model_dir or os.path.join(self.home, 'models', 'speakers')
        self.num_threads = num_threads or default_threads()
        self._lock = threading.Lock()            # model use
        self._download_lock = threading.Lock()   # one download at a time
        self._diarizer = None
        self._extractor = None
        self._clustering = None                  # (num_clusters, threshold) currently set
        if not self.available():
            self._status = {'state': 'off', 'message': 'Speaker separation needs the sherpa-onnx package.'}
        elif self.models_present():
            self._status = {'state': 'ready', 'message': 'Speaker separation ready.'}
        else:
            self._status = {'state': 'off',
                            'message': f'Speaker models not downloaded yet (~{DOWNLOAD_MB} MB, one time).'}

    # ── Public API ───────────────────────────────────────────────────────
    @staticmethod
    def available() -> bool:
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def status(self) -> dict:
        return dict(self._status)

    @property
    def segmentation_path(self) -> str:
        return os.path.join(self.model_dir, SEGMENTATION_FILE)

    @property
    def embedding_path(self) -> str:
        return os.path.join(self.model_dir, EMBEDDING_FILE)

    def models_present(self) -> bool:
        return all(os.path.isfile(p) and os.path.getsize(p) > 0
                   for p in (self.segmentation_path, self.embedding_path))

    def ensure_models(self) -> None:
        """Download the two models if missing (blocking). Raises on failure."""
        with self._download_lock:
            if self.models_present():
                self._status = {'state': 'ready', 'message': 'Speaker separation ready.'}
                return
            os.makedirs(self.model_dir, exist_ok=True)
            total = SEGMENTATION_ARCHIVE_BYTES + EMBEDDING_BYTES
            received = [0]

            def on_bytes(n):
                received[0] += n
                pct = min(100, int(100 * received[0] / total))
                self._status = {'state': 'downloading',
                                'message': f'Downloading speaker models… {pct}% of {DOWNLOAD_MB} MB'}

            self._status = {'state': 'downloading', 'message': f'Downloading speaker models (~{DOWNLOAD_MB} MB)…'}
            archive = os.path.join(self.model_dir, 'segmentation.tar.bz2')
            try:
                if not os.path.isfile(self.segmentation_path):
                    _download(SEGMENTATION_URL, archive, SEGMENTATION_ARCHIVE_BYTES,
                              SEGMENTATION_ARCHIVE_SHA256, on_bytes)
                    _extract_member(archive, SEGMENTATION_LICENSE_MEMBER,
                                    os.path.join(self.model_dir, SEGMENTATION_LICENSE_FILE))
                    _extract_member(archive, SEGMENTATION_MEMBER, self.segmentation_path)
                else:
                    received[0] += SEGMENTATION_ARCHIVE_BYTES
                if not os.path.isfile(self.embedding_path):
                    _download(EMBEDDING_URL, self.embedding_path, EMBEDDING_BYTES, EMBEDDING_SHA256, on_bytes)
            except Exception as exc:
                self._status = {'state': 'error',
                                'message': f'Speaker model download failed ({type(exc).__name__}: {exc}).'}
                raise
            finally:
                if os.path.exists(archive):
                    os.remove(archive)
            self._status = {'state': 'ready', 'message': 'Speaker separation ready.'}

    def diarize(self, samples, num_speakers: int = 0, cancel_event=None, on_progress=None) -> list:
        """Who spoke when. samples: float32 mono 16 kHz. num_speakers=0 means
        automatic (clustering threshold). Returns [{'start','end','speaker'}]
        sorted by start; speaker ids are ints starting at 0.

        Measured limits of sherpa-onnx 1.13.8: its progress callback only runs
        in the embedding phase (after segmentation, ~1/3 of the time), and a
        non-zero return does NOT stop the run. So cancel_event is honoured
        before starting and the result is discarded afterwards, but the
        native run itself finishes (it does not block other Python threads)."""
        audio = _as_audio(samples)
        if audio.size < SAMPLE_RATE // 2:
            return []
        if cancel_event is not None and cancel_event.is_set():
            raise DiarizationCancelled()

        # Progress: segmentation reports nothing, so map the embedding phase
        # onto the remaining SEGMENTATION_SHARE..1 of the bar.
        def callback(done_chunks, total_chunks):
            if cancel_event is not None and cancel_event.is_set():
                return 1    # asks sherpa-onnx to stop (ignored by 1.13.8, harmless)
            if on_progress and total_chunks:
                try:
                    on_progress(SEGMENTATION_SHARE + (1 - SEGMENTATION_SHARE) * done_chunks / total_chunks)
                except Exception:
                    logger.exception('diarize progress callback failed')
            return 0

        if on_progress:
            try:
                on_progress(0.0)
            except Exception:
                logger.exception('diarize progress callback failed')

        with self._lock:
            diarizer = self._get_diarizer()
            self._set_clustering(diarizer, int(num_speakers or 0))
            result = diarizer.process(audio, callback=callback).sort_by_start_time()
        if cancel_event is not None and cancel_event.is_set():
            raise DiarizationCancelled()
        turns = [{'start': round(float(t.start), 3), 'end': round(float(t.end), 3), 'speaker': int(t.speaker)}
                 for t in result if t.end > t.start]
        turns.sort(key=lambda t: (t['start'], t['end']))
        return turns

    def embedding(self, samples) -> np.ndarray:
        """L2-normalised voice embedding of one stretch of audio."""
        audio = _as_audio(samples)
        with self._lock:
            extractor = self._get_extractor()
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio)
            stream.input_finished()
            if not extractor.is_ready(stream):
                raise ValueError('audio too short for a voice embedding')
            vector = np.asarray(extractor.compute(stream), dtype=np.float32)
        return _normalise(vector)

    def speaker_embeddings(self, samples, turns) -> dict:
        """{speaker_id: embedding} averaged (by duration) over each speaker's
        longest turns, using at most VOICEPRINT_MAX_SECONDS of audio each."""
        audio = _as_audio(samples)
        out = {}
        for speaker, picked in _voiceprint_turns(turns).items():
            total = np.zeros(0, dtype=np.float32)
            weight = 0.0
            for start, end in picked:
                clip = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
                try:
                    vector = self.embedding(clip)
                except ValueError:
                    continue
                total = vector * (end - start) if total.size == 0 else total + vector * (end - start)
                weight += end - start
            if weight > 0:
                out[speaker] = _normalise(total)
        return out

    # ── sherpa-onnx objects ──────────────────────────────────────────────
    def _get_diarizer(self):
        if self._diarizer is None:
            if not self.models_present():
                self.ensure_models()
            import sherpa_onnx
            config = self._diarization_config(sherpa_onnx, 0)
            if not config.validate():
                raise SpeakerModelError('speaker model files are missing or invalid')
            self._diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
            self._clustering = (-1, DEFAULT_CLUSTER_THRESHOLD)
        return self._diarizer

    def _set_clustering(self, diarizer, num_speakers: int) -> None:
        # Only the clustering part of a new config is applied by set_config,
        # so switching the speaker count does not reload the models.
        wanted = (num_speakers if num_speakers > 0 else -1, DEFAULT_CLUSTER_THRESHOLD)
        if wanted != self._clustering:
            import sherpa_onnx
            diarizer.set_config(self._diarization_config(sherpa_onnx, num_speakers))
            self._clustering = wanted

    def _diarization_config(self, sherpa_onnx, num_speakers: int):
        return sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=self.segmentation_path),
                num_threads=self.num_threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self.embedding_path, num_threads=self.num_threads),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers if num_speakers > 0 else -1,
                threshold=DEFAULT_CLUSTER_THRESHOLD),
            min_duration_on=MIN_DURATION_ON,
            min_duration_off=MIN_DURATION_OFF)

    def _get_extractor(self):
        if self._extractor is None:
            if not self.models_present():
                self.ensure_models()
            import sherpa_onnx
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self.embedding_path, num_threads=self.num_threads)
            if not config.validate():
                raise SpeakerModelError('speaker embedding model is missing or invalid')
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return self._extractor


def _as_audio(samples) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim != 1:
        audio = audio.reshape(-1)
    return np.ascontiguousarray(audio)


def _normalise(vector) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def _voiceprint_turns(turns) -> dict:
    """{speaker: [(start, end)]}: longest turns first, capped at
    VOICEPRINT_MAX_SECONDS; short turns only if nothing longer exists."""
    by_speaker = {}
    for turn in turns:
        by_speaker.setdefault(turn['speaker'], []).append((float(turn['start']), float(turn['end'])))
    picked = {}
    for speaker, spans in by_speaker.items():
        spans.sort(key=lambda s: s[1] - s[0], reverse=True)
        long_enough = [s for s in spans if s[1] - s[0] >= MIN_TURN_SECONDS] or spans[:1]
        chosen, used = [], 0.0
        for start, end in long_enough:
            if used >= VOICEPRINT_MAX_SECONDS:
                break
            end = min(end, start + (VOICEPRINT_MAX_SECONDS - used))
            chosen.append((start, end))
            used += end - start
        picked[speaker] = chosen
    return picked


# ── Matching turns to transcript segments ────────────────────────────────────
def assign_speakers(segments, turns) -> list:
    """Copies of the Whisper segments, each with 'speaker' = the diarization
    speaker overlapping it most (nearest turn if none overlaps).

    Limitation: a segment is never split. When one Whisper segment clearly spans
    two speakers (runner-up > 40% of it and >= 1.5 s) it keeps the majority
    speaker and is flagged 'mixed_speakers': True so the UI can show it."""
    ordered = sorted(turns, key=lambda t: t['start'])
    starts = [t['start'] for t in ordered]
    longest = max((t['end'] - t['start'] for t in ordered), default=0.0)
    out = []
    for seg in segments:
        copy = dict(seg)
        copy.pop('mixed_speakers', None)
        if not ordered:
            copy['speaker'] = 0
            out.append(copy)
            continue
        start, end = float(seg['start']), float(seg['end'])
        overlap = _overlap_by_speaker(ordered, starts, longest, start, end)
        if overlap:
            ranked = sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0]))
            copy['speaker'] = ranked[0][0]
            if len(ranked) > 1:
                runner_up = ranked[1][1]
                length = max(end - start, 1e-6)
                if runner_up / length > MIXED_SHARE and runner_up >= MIXED_MIN_SECONDS:
                    copy['mixed_speakers'] = True
        else:
            copy['speaker'] = _nearest_speaker(ordered, start, end)
        out.append(copy)
    return out


def _overlap_by_speaker(ordered, starts, longest, start, end) -> dict:
    """Seconds of overlap per speaker between [start, end] and the turns."""
    totals = {}
    index = bisect.bisect_left(starts, end) - 1
    while index >= 0 and ordered[index]['start'] >= start - longest:
        turn = ordered[index]
        shared = min(end, turn['end']) - max(start, turn['start'])
        if shared > 0:
            totals[turn['speaker']] = totals.get(turn['speaker'], 0.0) + shared
        index -= 1
    return totals


def _nearest_speaker(ordered, start, end) -> int:
    def gap(turn):
        return max(turn['start'] - end, start - turn['end'], 0.0)
    return min(ordered, key=gap)['speaker']


# ── Labels and dialogue text ─────────────────────────────────────────────────
def speaker_labels(segments) -> dict:
    """{raw speaker id: 'Speaker N'} numbered by first appearance."""
    labels = {}
    for seg in sorted(segments, key=lambda s: float(s['start'])):
        raw = seg.get('speaker')
        if raw is not None and raw not in labels:
            labels[raw] = f'Speaker {len(labels) + 1}'
    return labels


def label_speakers(segments, names: dict = None):
    """Replace raw ids with display labels ('Speaker 1'… by first appearance,
    or the enrolled name). Each segment also keeps 'speaker_id' ('Speaker N')
    so the UI can refer to a speaker stably. Returns (segments, speakers)."""
    names = names or {}
    labels = speaker_labels(segments)
    talk = {}
    out = []
    for seg in segments:
        copy = dict(seg)
        raw = seg.get('speaker')
        if raw in labels:
            copy['speaker_id'] = labels[raw]
            copy['speaker'] = names.get(raw) or labels[raw]
            talk[raw] = talk.get(raw, 0.0) + max(0.0, float(seg['end']) - float(seg['start']))
        out.append(copy)
    speakers = [{'id': label, 'name': names.get(raw) or label, 'known': bool(names.get(raw)),
                 'seconds': round(talk.get(raw, 0.0), 1)}
                for raw, label in labels.items()]
    return out, speakers


def format_dialogue(segments) -> str:
    """'Speaker 1: text…' paragraphs, consecutive same-speaker segments merged.
    Text is only stripped and joined with single spaces (no other changes)."""
    paragraphs = []
    current, parts = None, []
    for seg in segments:
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        speaker = seg.get('speaker')
        speaker = 'Unknown' if speaker is None else str(speaker)
        if speaker != current and parts:
            paragraphs.append(f"{current}: {' '.join(parts)}")
            parts = []
        current = speaker
        parts.append(text)
    if parts:
        paragraphs.append(f"{current}: {' '.join(parts)}")
    return '\n\n'.join(paragraphs)


# ── Known voices ─────────────────────────────────────────────────────────────
# These are biometric voiceprints. They are created ONLY when the user
# explicitly enrolls a voice, stored ONLY in voices.json on this computer
# (owner-readable, 0600), never uploaded, and can be deleted one by one or all
# at once. A voiceprint is a 192-number summary of a voice, not a recording.
class VoiceBook:
    MAX_NAME = 40

    def __init__(self, home=None):
        self.home = home or santa_home()
        self.path = os.path.join(self.home, 'voices.json')
        self._lock = threading.Lock()

    def enroll(self, name: str, embedding, seconds: float) -> dict:
        """Save (or refine) a named voice. Re-enrolling a name averages the
        voiceprints, weighted by the seconds of audio behind each."""
        name = self._clean_name(name)
        vector = _normalise(embedding)
        seconds = max(0.0, float(seconds))
        with self._lock:
            voices = self._load()
            now = _now()
            existing = next((v for v in voices if v['name'].casefold() == name.casefold()), None)
            if existing and existing.get('model') == EMBEDDING_MODEL_ID and \
                    len(existing['embedding']) == vector.size:
                old_seconds = float(existing.get('seconds', 0.0)) or 1.0
                new_seconds = seconds or 1.0
                merged = np.asarray(existing['embedding'], dtype=np.float32) * old_seconds + vector * new_seconds
                existing.update(embedding=_normalise(merged).tolist(), updated=now,
                                seconds=round(float(existing.get('seconds', 0.0)) + seconds, 1))
                entry = existing
            else:
                if existing:
                    voices.remove(existing)   # made with another model: start again
                entry = {'name': name, 'embedding': vector.tolist(), 'seconds': round(seconds, 1),
                         'model': EMBEDDING_MODEL_ID, 'created': now, 'updated': now}
                voices.append(entry)
            self._save(voices)
            return {'name': entry['name'], 'seconds': entry['seconds'], 'created': entry['created']}

    def list(self) -> list:
        """Names only (never the voiceprints)."""
        with self._lock:
            return [{'name': v['name'], 'seconds': v.get('seconds', 0.0), 'created': v.get('created', '')}
                    for v in self._load()]

    def delete(self, name: str) -> bool:
        with self._lock:
            voices = self._load()
            keep = [v for v in voices if v['name'].casefold() != (name or '').strip().casefold()]
            if len(keep) == len(voices):
                return False
            self._save(keep)
            return True

    def clear(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                os.remove(self.path)

    def match(self, embeddings: dict, threshold: float = DEFAULT_MATCH_THRESHOLD) -> dict:
        """{speaker_id: name} for speakers that sound like an enrolled voice.
        Cosine similarity; greedy by best score so each speaker gets at most
        one name and each name is used at most once."""
        with self._lock:
            voices = [v for v in self._load() if v.get('model') == EMBEDDING_MODEL_ID]
        pairs = []
        for speaker, vector in (embeddings or {}).items():
            vector = _normalise(vector)
            for voice in voices:
                known = np.asarray(voice['embedding'], dtype=np.float32)
                if known.size != vector.size:
                    continue
                score = float(np.dot(vector, _normalise(known)))
                if score >= threshold:
                    pairs.append((score, speaker, voice['name']))
        pairs.sort(key=lambda p: -p[0])
        result, used = {}, set()
        for score, speaker, name in pairs:
            if speaker not in result and name not in used:
                result[speaker] = name
                used.add(name)
        return result

    # ── Storage ──────────────────────────────────────────────────────────
    def _clean_name(self, name) -> str:
        name = ' '.join(str(name or '').split())
        if not 1 <= len(name) <= self.MAX_NAME:
            raise ValueError(f'name must be 1–{self.MAX_NAME} characters')
        return name

    def _load(self) -> list:
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            voices = data.get('voices', []) if isinstance(data, dict) else []
            return [v for v in voices if isinstance(v, dict) and v.get('name') and v.get('embedding')]
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning('voices.json unreadable; treating as empty')
            return []

    def _save(self, voices: list) -> None:
        # Owner-only file, written to a temp name and renamed (never half-written).
        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump({'version': 1, 'voices': voices}, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── One-call convenience ─────────────────────────────────────────────────────
def diarize_transcript(engine, samples, segments, num_speakers=0, voicebook=None, cancel_event=None) -> dict:
    """Diarize the audio, attach speakers to the transcript segments, name
    known voices (if a VoiceBook is given) and build the dialogue text.
    'turns' carries the raw turns with 'Speaker N' ids, for later enrollment."""
    started = time.time()
    turns = engine.diarize(samples, num_speakers=num_speakers, cancel_event=cancel_event)
    assigned = assign_speakers(segments, turns)
    names = {}
    if voicebook is not None and turns and voicebook.list():
        names = voicebook.match(engine.speaker_embeddings(samples, turns))
    labelled, speakers = label_speakers(assigned, names)
    labels = speaker_labels(assigned)
    for turn in turns:     # speakers heard only between transcript segments
        labels.setdefault(turn['speaker'], f'Speaker {len(labels) + 1}')
    return {
        'segments': labelled,
        'speakers': speakers,
        'text': format_dialogue(labelled),
        'turns': [{'start': t['start'], 'end': t['end'], 'speaker': labels[t['speaker']]} for t in turns],
        'diarize_seconds': round(time.time() - started, 2),
    }


def voiceprint_for(engine, samples, turns, speaker_id: str):
    """(embedding, seconds) for one 'Speaker N' from diarize_transcript's
    'turns' — what VoiceBook.enroll needs when the user names that speaker."""
    own = [dict(t, speaker=0) for t in turns if t['speaker'] == speaker_id]
    if not own:
        raise ValueError(f'no audio for {speaker_id}')
    seconds = sum(e - s for s, e in _voiceprint_turns(own)[0])
    vectors = engine.speaker_embeddings(samples, own)
    if 0 not in vectors:
        raise ValueError(f'not enough audio for {speaker_id}')
    return vectors[0], round(seconds, 1)
