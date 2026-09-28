# santa/export.py
# Writes finished transcripts to a folder for other tools to pick up (used by
# Hiring Right: ~/Documents/Hiring Right/03_Transcripts). For every file job:
#   <name>.json  {"source", "model", "language", "audio_seconds", "segments": [{start, end, text, speaker?}],
#                 "speakers"?: [...], "translation"?: {language, text}}
#   <name>.srt   the same segments as subtitles ("[Speaker 1] …" when speakers were separated)
# <name> is the source file name without its extension. Files are written to a
# temporary name first and then renamed, so a reader never sees half a file.
# A failed job writes <name>.error.txt instead, so nothing fails silently.

import json
import os
import unicodedata
from pathlib import Path


def export_stem(source_name: str) -> str:
    stem = Path(source_name or 'transcript').stem.strip() or 'transcript'
    return stem.replace('/', '_').replace('\\', '_')


def resolve_dir(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path or ''))


def _clean_segments(segments: list) -> list:
    out = []
    for seg in segments:
        text = unicodedata.normalize('NFC', seg.get('text', '')).strip()
        if text:
            item = {'start': round(float(seg['start']), 2), 'end': round(float(seg['end']), 2),
                    'text': text}
            if seg.get('speaker') is not None:          # present when speakers were separated
                item['speaker'] = str(seg['speaker'])
            out.append(item)
    return out


def srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def to_srt(segments: list) -> str:
    blocks = []
    for n, seg in enumerate(segments, 1):
        text = f"[{seg['speaker']}] {seg['text']}" if seg.get('speaker') else seg['text']
        blocks.append(f"{n}\n{srt_time(seg['start'])} --> {srt_time(seg['end'])}\n{text}\n")
    return '\n'.join(blocks)


def _atomic_write(path: str, text: str) -> None:
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_transcript(folder: str, source_name: str, result: dict) -> list:
    """Write <stem>.json and <stem>.srt; returns the paths written."""
    folder = resolve_dir(folder)
    os.makedirs(folder, exist_ok=True)
    stem = export_stem(source_name)
    segments = _clean_segments(result.get('segments') or [])
    payload = {
        'source': os.path.basename(source_name),
        'model': result.get('model'),
        'language': result.get('detected_language') or result.get('whisper_language'),
        'engine': result.get('engine'),
        'audio_seconds': result.get('audio_seconds'),
        'segments': segments,
    }
    if result.get('speakers'):
        payload['speakers'] = [{'label': sp['name'], 'known_voice': sp['known'], 'talk_seconds': sp['seconds']}
                               for sp in result['speakers']]
    if result.get('translation'):
        # Segments above stay in the spoken language; the translation is extra.
        payload['translation'] = {'language': result['translation']['to'], 'text': result['text']}
    json_path = os.path.join(folder, stem + '.json')
    srt_path = os.path.join(folder, stem + '.srt')
    _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=1))
    _atomic_write(srt_path, to_srt(segments))
    _remove_quietly(os.path.join(folder, stem + '.error.txt'))   # a retry succeeded
    return [json_path, srt_path]


def write_error(folder: str, source_name: str, message: str) -> str:
    folder = resolve_dir(folder)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, export_stem(source_name) + '.error.txt')
    _atomic_write(path, f'Santa could not transcribe {os.path.basename(source_name)}:\n{message}\n')
    return path


def already_exported(folder: str, source_path: str) -> bool:
    """True when <stem>.json exists and is newer than the source file."""
    json_path = os.path.join(resolve_dir(folder), export_stem(source_path) + '.json')
    try:
        return os.path.getmtime(json_path) >= os.path.getmtime(source_path)
    except OSError:
        return False


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
