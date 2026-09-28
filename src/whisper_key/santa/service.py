# santa/service.py
# The reusable transcription service. Accepts audio (a file or bytes) + options,
# runs it through FIFO job queues and returns structured results. Two lanes:
#   interactive  dictation, recordings, small uploads — max 3 pending
#   batch        watch-folder files and big uploads (long interview videos)
# Each lane has one worker. The GPU path works in <= 28 s pieces and takes a
# lock per piece, so a dictation clip waits at most one piece behind a batch job. It has no knowledge of HTTP, browsers, hotkeys or the
# clipboard, so the desktop UI today and a phone front-end in Phase 2 can both
# call it.

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Optional

import os

from . import audio_input, textproc, accel_cpp, export
from .history import HistoryStore
from .languages import (LANGUAGE_MODES, WHISPER_LANGUAGE_NAMES, resolve_decode_params,
                        UnknownLanguageMode)
from .models import ModelManager, ModelUnavailable, TranscriptionCancelled, run_transcription
from .romanize import contains_devanagari, romanize_hinglish
from .settings import SettingsStore

logger = logging.getLogger(__name__)

MAX_PENDING_JOBS = 3          # interactive lane: queued + running; more is refused (HTTP 429)
MAX_BATCH_JOBS = 50           # batch lane
BATCH_BYTES = 25_000_000      # uploads bigger than this go to the batch lane
FILE_SOURCES = ('upload', 'watch')   # jobs that are exported when export is on
JOB_TTL_SECONDS = 15 * 60     # finished jobs are forgotten after this


class QueueFull(Exception):
    code = 'busy'
    message = 'Santa is already busy with other transcriptions. Try again in a moment.'


@dataclass
class Job:
    id: str
    language_mode: str
    source: str                      # 'recording' | 'upload' | 'watch' | 'api'
    audio_path: Optional[str]        # file to decode
    ext: str
    source_name: str = ''            # original file name (for exports)
    delete_after: bool = True        # temp files are deleted; watch-folder videos are not
    lane: str = 'interactive'
    created: float = field(default_factory=time.time)
    state: str = 'queued'            # queued | decoding | waiting_model | transcribing | done | error | cancelled
    progress: Optional[float] = None
    result: Optional[dict] = None
    error: Optional[dict] = None
    finished: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {'id': self.id, 'state': self.state, 'progress': self.progress,
                'language_mode': self.language_mode, 'source': self.source,
                'source_name': self.source_name, 'lane': self.lane,
                'result': self.result, 'error': self.error, 'created': self.created}


class TranscriptionService:
    def __init__(self, settings: SettingsStore = None, model_manager: ModelManager = None,
                 history: HistoryStore = None, transcribe_fn=None, accelerator=None):
        self.settings = settings or SettingsStore()
        s = self.settings.get()
        self.models = model_manager or ModelManager(s['compute_type'], s['cpu_threads'])
        # Optional Metal fast path for short clips (None in tests / other platforms).
        self.accel = accelerator
        self.history = history or HistoryStore(self.settings.home)
        self._transcribe_fn = transcribe_fn or run_transcription   # injectable for tests
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._queues = {'interactive': queue.Queue(), 'batch': queue.Queue()}
        for lane, q in self._queues.items():
            threading.Thread(target=self._work, args=(q,), daemon=True, name=f'santa-{lane}').start()

    # ── Public API ───────────────────────────────────────────────────────
    def preload(self) -> None:
        s = self.settings.get()
        self.models.ensure_loading(s['model'], s['compute_type'], s['cpu_threads'])
        if self._accel_wanted(s):
            self.accel.ensure_loading()

    def _accel_wanted(self, s: dict) -> bool:
        return (self.accel is not None and s['engine'] == 'auto'
                and s['model'] == accel_cpp.SERVES_MODEL and self.accel.available())

    def status(self) -> dict:
        with self._jobs_lock:
            active = [j.id for j in self._jobs.values() if j.state not in ('done', 'error', 'cancelled')]
        accel = self.accel.get_status() if self._accel_wanted(self.settings.get()) else None
        return {'model': self.models.get_status(), 'accelerator': accel, 'active_jobs': active}

    def submit(self, audio_bytes: bytes, filename: str, language_mode: str,
               source: str = 'upload') -> Job:
        """Bytes in memory (recordings, API). Validates, stores a private temp file, enqueues."""
        s = self.settings.get()
        ext = audio_input.check_upload(filename, len(audio_bytes or b''), s['max_upload_mb'] * 1_000_000)
        self._check_language(language_mode)
        path = audio_input.write_temp(audio_bytes, ext)
        try:
            return self.submit_file(path, filename, language_mode, source=source, delete_after=True)
        except Exception:
            audio_input.remove_temp(path)
            raise

    def submit_file(self, path: str, filename: str, language_mode: str, source: str = 'upload',
                    delete_after: bool = True) -> Job:
        """A file already on disk (streamed upload or watch folder). Enqueues it."""
        self._check_language(language_mode)
        ext = os.path.splitext(filename or path)[1].lower() or '.wav'
        if ext not in audio_input.SUPPORTED_EXTENSIONS:
            audio_input.check_upload(filename, 1, 1)          # raises the friendly error
        size = os.path.getsize(path)
        lane = 'batch' if source == 'watch' or size > BATCH_BYTES else 'interactive'
        self._forget_old_jobs()
        with self._jobs_lock:
            active = sum(1 for j in self._jobs.values()
                         if j.lane == lane and j.state not in ('done', 'error', 'cancelled'))
            if active >= (MAX_PENDING_JOBS if lane == 'interactive' else MAX_BATCH_JOBS):
                raise QueueFull()
            job = Job(id=uuid.uuid4().hex[:16], language_mode=language_mode, source=source,
                      audio_path=path, ext=ext, source_name=os.path.basename(filename or path),
                      delete_after=delete_after, lane=lane)
            self._jobs[job.id] = job
        self._queues[lane].put(job.id)
        return job

    @staticmethod
    def _check_language(language_mode: str) -> None:
        if language_mode not in LANGUAGE_MODES:
            raise UnknownLanguageMode(f"Unknown language '{language_mode}'.")

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job or job.state in ('done', 'error', 'cancelled'):
            return False
        job.cancel_event.set()
        if job.state == 'queued':
            self._finish(job, 'cancelled')
        return True

    def transcribe_sync(self, audio_bytes: bytes, filename: str, language_mode: str,
                        timeout: float = None) -> dict:
        """Convenience for scripts/benchmarks: submit and wait."""
        job = self.submit(audio_bytes, filename, language_mode, source='api')
        deadline = None if timeout is None else time.time() + timeout
        while job.state not in ('done', 'error', 'cancelled'):
            if deadline and time.time() > deadline:
                self.cancel(job.id)
                raise TimeoutError('transcription timed out')
            time.sleep(0.05)
        return job.public()

    # ── Worker ───────────────────────────────────────────────────────────
    def _work(self, jobs_queue) -> None:
        while True:
            job_id = jobs_queue.get()
            job = self.get_job(job_id)
            if job is None or job.state == 'cancelled':
                continue
            try:
                self._run(job)
            except TranscriptionCancelled:
                self._finish(job, 'cancelled')
            except (audio_input.AudioInputError, ModelUnavailable) as exc:
                self._finish(job, 'error', error={'code': exc.code, 'message': exc.message})
            except MemoryError:
                self._finish(job, 'error', error={'code': 'out_of_memory', 'message':
                             'Ran out of memory. Close other apps or use a smaller model.'})
            except Exception as exc:
                logger.exception('Unexpected transcription failure')
                self._finish(job, 'error', error={'code': 'internal', 'message':
                             f'Unexpected error: {type(exc).__name__}. See the Santa log.'})
            finally:
                self._release_audio(job)
                if job.state == 'error' and job.source == 'watch':
                    self._export_error(job)

    def _run(self, job: Job) -> None:
        s = self.settings.get()
        job.state = 'decoding'
        decoded = audio_input.decode_path(job.audio_path, s['max_duration_min'] * 60)
        self._release_audio(job)   # decoded samples are in memory; drop the temp file now
        if job.cancel_event.is_set():
            raise TranscriptionCancelled()

        params = resolve_decode_params(job.language_mode, s['hinglish_strategy'], s['user_prompt'])

        def progress(p):
            job.progress = p

        raw = None
        # Fast path: Metal accelerator ready. Long audio is split at pauses into
        # <= 28 s pieces. Any doubt about the output -> CPU engine for the whole job.
        if self._accel_wanted(s) and self.accel.ready() and not params.multilingual:
            job.state, job.progress = 'transcribing', None
            try:
                raw = self._gpu_transcribe(decoded.samples, params, job, timestamps=self._wants_export(job, s))
            except accel_cpp.AcceleratorError as exc:
                logger.info('job %s: GPU fast path rejected (%s); using CPU engine', job.id, exc)
                raw = None

        if raw is None:
            job.state = 'waiting_model'
            model = self.models.wait_ready(s['model'], cancel_event=job.cancel_event)
            job.state, job.progress = 'transcribing', 0.0
            raw = self._transcribe_fn(model, decoded.samples, params, beam_size=s['beam_size'],
                                      vad_filter=s['vad_filter'], cancel_event=job.cancel_event,
                                      on_progress=progress)
            raw.setdefault('engine', 'faster-whisper · CPU')
        model_key = s['model']
        result = self._build_result(job, raw, decoded.duration_s, model_key, params, s)
        if self._wants_export(job, s) and result['segments']:
            try:
                result['exported'] = export.write_transcript(s['export_dir'], job.source_name, result)
            except OSError as exc:
                result['export_error'] = f'Could not save transcript files: {exc}'
                logger.warning('job %s: export failed: %s', job.id, exc)
        if s['history_enabled'] and result['text']:
            self.history.add(result['text'], job.language_mode, result['detected_language'],
                             model_key, decoded.duration_s, raw_text=result['raw_text'])
        # Log sizes and timings only — never transcript content.
        logger.info('job %s: %.1fs audio -> %d chars in %.2fs (%s, %s)', job.id,
                    decoded.duration_s, len(result['text']), raw['transcribe_seconds'],
                    model_key, job.language_mode)
        self._finish(job, 'done', result=result)

    def _gpu_transcribe(self, samples, params, job: Job, timestamps: bool = False) -> dict:
        if len(samples) / 16000.0 <= accel_cpp.MAX_CLIP_SECONDS:
            return self.accel.transcribe(samples, params, cancel_event=job.cancel_event, timestamps=timestamps)
        chunks = accel_cpp.plan_chunks(samples)
        if not chunks:
            raise accel_cpp.AcceleratorError('no speech regions found for chunking')
        segments, seconds, detected = [], 0.0, None
        chunk_params = replace(params)
        for n, (start, end) in enumerate(chunks, 1):
            part = self.accel.transcribe(samples[start:end], chunk_params, cancel_event=job.cancel_event,
                                         timestamps=timestamps)
            if chunk_params.language is None and part.get('detected_language'):
                # Auto-detect: decide the language once, on the first piece, so
                # later pieces can't flip language mid-document.
                chunk_params = replace(chunk_params, language=part['detected_language'])
            detected = detected or part.get('detected_language')
            offset = start / 16000.0
            for seg in part['segments']:                 # piece-relative -> file times
                segments.append({'start': round(seg['start'] + offset, 2),
                                 'end': round(seg['end'] + offset, 2), 'text': seg['text']})
            seconds += part['transcribe_seconds']
            job.progress = n / len(chunks)
        return {'segments': segments, 'detected_language': detected, 'language_probability': None,
                'transcribe_seconds': round(seconds, 2),
                'engine': f'whisper.cpp · Metal GPU ({len(chunks)} pieces split at pauses)'}

    @staticmethod
    def _wants_export(job: Job, s: dict) -> bool:
        return bool(s['export_enabled'] and s['export_dir'] and job.source in FILE_SOURCES)

    def _export_error(self, job: Job) -> None:
        s = self.settings.get()
        if self._wants_export(job, s) and job.error:
            try:
                export.write_error(s['export_dir'], job.source_name, job.error['message'])
            except OSError:
                pass

    @staticmethod
    def _release_audio(job: Job) -> None:
        if job.audio_path and job.delete_after:
            audio_input.remove_temp(job.audio_path)
        job.audio_path = None

    @staticmethod
    def _build_result(job: Job, raw: dict, duration_s: float, model_key: str, params, s: dict) -> dict:
        raw_text = textproc.join_segments(seg['text'] for seg in raw['segments'])
        text = textproc.light_cleanup(raw_text) if s['cleanup'] else raw_text
        romanized = None
        if job.language_mode == 'hinglish' and contains_devanagari(text):
            romanized = romanize_hinglish(text)
            if s['hinglish_output'] == 'roman':
                text = romanized
        mode = LANGUAGE_MODES[job.language_mode]
        detected = raw.get('detected_language')
        return {
            'text': text,
            'raw_text': raw_text,
            'romanized_text': romanized,
            'segments': raw['segments'],
            'language_mode': job.language_mode,
            'whisper_language': params.language,
            'detected_language': detected,
            'detected_language_name': WHISPER_LANGUAGE_NAMES.get(detected, detected),
            'language_probability': raw.get('language_probability'),
            'rtl': textproc.is_rtl_dominant(text),
            'script_warning': textproc.script_warning(raw_text, mode.script),
            'empty': not raw_text,
            'audio_seconds': round(duration_s, 2),
            'transcribe_seconds': raw.get('transcribe_seconds'),
            'model': model_key,
            'engine': raw.get('engine'),
            'notes': params.notes,
            'source_name': job.source_name,
        }

    def _finish(self, job: Job, state: str, result: dict = None, error: dict = None) -> None:
        job.state, job.result, job.error = state, result, error
        self._release_audio(job)
        job.progress = 1.0 if state == 'done' else job.progress
        job.finished = time.time()

    def _forget_old_jobs(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        with self._jobs_lock:
            for jid in [j.id for j in self._jobs.values() if j.finished and j.finished < cutoff]:
                del self._jobs[jid]
