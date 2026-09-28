# santa/watch.py
# Watch-folder mode: every few seconds, look for new audio/video files in the
# configured folder and queue them on the service's batch lane. A file is taken
# only once its size and modification time have stopped changing (so a video
# that is still being copied is left alone) and only if no up-to-date
# <name>.json transcript exists yet. Source files are never moved or deleted —
# Hiring Right removes videos itself once rating is finished.

import logging
import os
import threading
import time

from . import export
from .audio_input import SUPPORTED_EXTENSIONS, AudioInputError

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
SETTLE_SECONDS = 5.0      # file must be unchanged for this long before we start


class WatchFolder:
    def __init__(self, service, poll_seconds: float = POLL_SECONDS, settle_seconds: float = SETTLE_SECONDS):
        self.service = service
        self.poll_seconds = poll_seconds
        self.settle_seconds = settle_seconds
        self._seen = {}          # path -> (size, mtime, first_seen_stable_at)
        self._queued = {}        # path -> (mtime, job_id)
        self._stop = threading.Event()
        self._thread = None
        self.status = {'state': 'off', 'folder': None, 'queued': 0, 'message': 'Watch folder off.'}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name='santa-watch')
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.scan_once()
            except Exception:
                logger.exception('watch folder scan failed')

    # One pass; returns the jobs queued in this pass (also used by tests).
    def scan_once(self) -> list:
        s = self.service.settings.get()
        folder = export.resolve_dir(s['watch_dir'])
        if not (s['watch_enabled'] and s['watch_dir']):
            self.status = {'state': 'off', 'folder': None, 'queued': 0, 'message': 'Watch folder off.'}
            return []
        if not os.path.isdir(folder):
            self.status = {'state': 'error', 'folder': folder, 'queued': 0,
                           'message': f'Watch folder not found: {folder}'}
            return []
        now, queued = time.time(), []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.startswith(('.', '~')) or not os.path.isfile(path):
                continue
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if self._queued.get(path, (None,))[0] == st.st_mtime:
                continue                                   # already handled this version
            if s['export_enabled'] and s['export_dir'] and export.already_exported(s['export_dir'], path):
                continue
            prev = self._seen.get(path)
            if not prev or prev[0] != st.st_size or prev[1] != st.st_mtime:
                self._seen[path] = (st.st_size, st.st_mtime, now)   # new or still changing
                continue
            if now - prev[2] < self.settle_seconds or now - st.st_mtime < self.settle_seconds:
                continue
            try:
                job = self.service.submit_file(path, name, s['watch_language'], source='watch',
                                               delete_after=False)
            except AudioInputError as exc:
                if s['export_enabled'] and s['export_dir']:
                    export.write_error(s['export_dir'], name, exc.message)
                self._queued[path] = (st.st_mtime, None)
                continue
            except Exception as exc:          # queue full etc.: try again next pass
                logger.info('watch: will retry %s later (%s)', name, type(exc).__name__)
                continue
            self._queued[path] = (st.st_mtime, job.id)
            self._seen.pop(path, None)
            queued.append(job)
            logger.info('watch: queued %s as job %s', name, job.id)
        waiting = sum(1 for _, jid in self._queued.values()
                      if jid and (j := self.service.get_job(jid)) and j.state not in ('done', 'error', 'cancelled'))
        self.status = {'state': 'on', 'folder': folder, 'queued': waiting,
                       'message': f'Watching {os.path.basename(folder)} · {waiting} in progress'}
        return queued
