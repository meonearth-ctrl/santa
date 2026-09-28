# santa/translate.py
# Optional, explicit translation of a finished transcript into English or Arabic,
# fully offline. Uses Argos Translate model packages (OpenNMT models converted to
# CTranslate2 + a SentencePiece tokenizer) run directly with ctranslate2, which
# Santa already ships for faster-whisper — no torch, no cloud. A package is
# downloaded once (~100 MB) the first time a language pair is used.
# Translation only happens when the user picks an output language; the original
# transcript is always kept next to it and the result is labelled as translated.

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model packages ───────────────────────────────────────────────────────────
# From the Argos Translate package index
# (https://github.com/argosopentech/argospm-index/blob/main/index.json).
# Models are trained on OPUS data; the argos-translate project is MIT-licensed.
PACKAGE_URLS = {
    ('hi', 'en'): 'https://argos-net.com/v1/translate-hi_en-1_1.argosmodel',
    ('bn', 'en'): 'https://argos-net.com/v1/translate-bn_en-1_9.argosmodel',
    ('ar', 'en'): 'https://argos-net.com/v1/translate-ar_en-1_0.argosmodel',
    ('en', 'ar'): 'https://argos-net.com/v1/translate-en_ar-1_0.argosmodel',
}
OUTPUT_LANGUAGES = {'same': 'As spoken', 'en': 'English', 'ar': 'Arabic'}
LANGUAGE_NAMES = {'en': 'English', 'ar': 'Arabic', 'hi': 'Hindi', 'bn': 'Bengali'}

# Sentence ends for all Santa languages: Latin . ! ? … , Devanagari/Bengali danda
# । ॥, Arabic question mark ؟ and full stop ۔. Newlines always split.
_SENTENCE_END = re.compile(r'(?<=[.!?…।॥؟۔])\s+|\n+')
_MAX_SENTENCE_CHARS = 400


class TranslationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = 'translation_failed'
        self.message = message


# ── Route planning ───────────────────────────────────────────────────────────
def plan_route(source: Optional[str], target: str) -> list:
    """Pairs to run, pivoting through English when there is no direct model.
    [] means nothing to do (already in the target language)."""
    if target not in ('en', 'ar'):
        raise TranslationError(f"Santa can translate to English or Arabic, not {target!r}.")
    source = source or ''
    if source == target:
        return []
    if (source, target) in PACKAGE_URLS:
        return [(source, target)]
    if (source, 'en') in PACKAGE_URLS and ('en', target) in PACKAGE_URLS:
        return [(source, 'en'), ('en', target)]
    name = LANGUAGE_NAMES.get(source, source or 'this language')
    raise TranslationError(f"No offline translation from {name} to {LANGUAGE_NAMES[target]} is available.")


def split_sentences(text: str) -> list:
    """Sentence-sized pieces (the models are trained on sentences); very long
    unpunctuated runs are cut at spaces so nothing is silently truncated."""
    pieces = []
    for sentence in _SENTENCE_END.split(text or ''):
        sentence = sentence.strip()
        while len(sentence) > _MAX_SENTENCE_CHARS:
            cut = sentence.rfind(' ', 0, _MAX_SENTENCE_CHARS)
            cut = cut if cut > 50 else _MAX_SENTENCE_CHARS
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    return pieces


# ── Translator ───────────────────────────────────────────────────────────────
class Translator:
    def __init__(self, home: str):
        self.dir = os.path.join(home, 'models', 'translate')
        self._lock = threading.Lock()
        self._loaded = {}                     # (src, tgt) -> (ct2 translator, sp processor, prefix)
        self.status = {'state': 'idle', 'message': ''}

    @staticmethod
    def available() -> bool:
        try:
            import ctranslate2  # noqa: F401
            import sentencepiece  # noqa: F401
            return True
        except ImportError:
            return False

    def installed_pairs(self) -> list:
        return [pair for pair in PACKAGE_URLS if os.path.isdir(self._package_dir(pair))]

    def translate(self, text: str, source: Optional[str], target: str, cancel_event=None) -> str:
        """Translate text (keeps paragraph breaks). Raises TranslationError."""
        route = plan_route(source, target)
        if not route or not text.strip():
            return text
        if not self.available():
            raise TranslationError('Translation needs the "sentencepiece" package; run the Santa installer again.')
        paragraphs = text.split('\n\n')
        for pair in route:
            paragraphs = [self._translate_paragraph(p, pair, cancel_event) for p in paragraphs]
        return '\n\n'.join(paragraphs)

    # Internals ------------------------------------------------------------
    def _translate_paragraph(self, paragraph: str, pair, cancel_event) -> str:
        if not paragraph.strip():
            return paragraph
        sentences = split_sentences(paragraph)
        translator, sp, prefix = self._load(pair)
        if cancel_event is not None and cancel_event.is_set():
            from .models import TranscriptionCancelled
            raise TranscriptionCancelled()
        batch = [sp.encode(s, out_type=str) for s in sentences]
        options = dict(beam_size=4, max_batch_size=16, replace_unknowns=True, length_penalty=0.2)
        if prefix:
            options['target_prefix'] = [[prefix]] * len(batch)
        with self._lock:                       # one translation at a time keeps memory flat
            results = translator.translate_batch(batch, **options)
        out = []
        for res in results:
            tokens = list(res.hypotheses[0])
            if prefix and tokens and tokens[0] == prefix:
                tokens = tokens[1:]
            # Standard SentencePiece detokenising: "▁" marks a word start. Done by
            # hand because some packages' target pieces aren't in the source
            # tokenizer's vocabulary, and sp.decode would leave the marks in.
            out.append(''.join(tokens).replace('▁', ' ').strip())
        return ' '.join(s for s in out if s)

    def _package_dir(self, pair) -> str:
        return os.path.join(self.dir, f'{pair[0]}_{pair[1]}')

    def _load(self, pair):
        with self._lock:
            if pair in self._loaded:
                return self._loaded[pair]
        folder = self._package_dir(pair)
        if not os.path.isdir(folder):
            self._download(pair, folder)
        import ctranslate2
        import sentencepiece
        sp_path = os.path.join(folder, 'sentencepiece.model')
        if not os.path.exists(sp_path):
            raise TranslationError('This translation package uses an unsupported tokenizer.')
        prefix = None
        try:
            with open(os.path.join(folder, 'metadata.json'), encoding='utf-8') as fh:
                prefix = json.load(fh).get('target_prefix') or None
        except (OSError, ValueError):
            pass
        translator = ctranslate2.Translator(os.path.join(folder, 'model'), device='cpu',
                                            compute_type='int8', inter_threads=1, intra_threads=4)
        sp = sentencepiece.SentencePieceProcessor(model_file=sp_path)
        with self._lock:
            self._loaded[pair] = (translator, sp, prefix)
        self.status = {'state': 'ready', 'message': 'Translation ready'}
        return translator, sp, prefix

    def _remove_stale_downloads(self) -> None:
        # Leftovers of an interrupted download (older than an hour, so a
        # download running in another Santa process is never touched).
        import time
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if name.startswith('dl-') and time.time() - os.path.getmtime(path) > 3600:
                shutil.rmtree(path, ignore_errors=True)

    def _download(self, pair, folder: str) -> None:
        """Fetch and unpack one package (a zip with a single top folder)."""
        url = PACKAGE_URLS[pair]
        label = f'{LANGUAGE_NAMES[pair[0]]} → {LANGUAGE_NAMES[pair[1]]}'
        self.status = {'state': 'downloading', 'message': f'Downloading translation model {label} (first time only)…'}
        os.makedirs(self.dir, exist_ok=True)
        self._remove_stale_downloads()
        work = tempfile.mkdtemp(prefix='dl-', dir=self.dir)
        try:
            archive = os.path.join(work, 'package.zip')
            req = urllib.request.Request(url, headers={'User-Agent': 'Santa'})
            with urllib.request.urlopen(req, timeout=60) as resp, open(archive, 'wb') as fh:
                shutil.copyfileobj(resp, fh, 1 << 20)
            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():           # refuse path tricks in the archive
                    if member.startswith('/') or '..' in member.split('/'):
                        raise TranslationError('The translation package looks damaged.')
                zf.extractall(work)
            tops = [d for d in os.listdir(work) if os.path.isdir(os.path.join(work, d))]
            if len(tops) != 1 or not os.path.isdir(os.path.join(work, tops[0], 'model')):
                raise TranslationError('The translation package has an unexpected layout.')
            os.replace(os.path.join(work, tops[0]), folder)
        except TranslationError:
            self.status = {'state': 'error', 'message': 'Translation model could not be installed.'}
            raise
        except Exception as exc:
            self.status = {'state': 'error', 'message': 'Translation model download failed.'}
            raise TranslationError(f'Could not download the {label} translation model '
                                   f'(internet needed once). {type(exc).__name__}') from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
        logger.info('translation package %s_%s installed', *pair)
