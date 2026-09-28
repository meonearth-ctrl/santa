# santa/history.py
# Optional transcript history (OFF by default). When enabled, each finished
# transcript is appended as one JSON line to <santa_home>/history.jsonl. No audio
# is ever stored. Entries can be deleted one by one or all at once.

import json
import os
import threading
import time
import uuid

MAX_ENTRIES = 500


class HistoryStore:
    def __init__(self, home: str):
        self.path = os.path.join(home, 'history.jsonl')
        self._lock = threading.Lock()

    def add(self, text: str, language_mode: str, detected_language: str,
            model: str, duration_s: float, raw_text: str = '') -> dict:
        entry = {
            'id': uuid.uuid4().hex[:12], 'created': time.time(),
            'language_mode': language_mode, 'detected_language': detected_language,
            'model': model, 'duration_s': round(duration_s, 2),
            'text': text, 'raw_text': raw_text,
        }
        with self._lock:
            entries = self._read()
            entries.append(entry)
            self._write(entries[-MAX_ENTRIES:])
        return entry

    def list(self) -> list:
        with self._lock:
            return list(reversed(self._read()))

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            entries = self._read()
            kept = [e for e in entries if e.get('id') != entry_id]
            self._write(kept)
            return len(kept) != len(entries)

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass

    def _read(self) -> list:
        entries = []
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        return entries

    def _write(self, entries: list) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + '\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
