# santa/links.py
# Turns what the user pasted — a local file path or a web link — into a file
# Santa can transcribe. Paths are checked (exists, supported type) and read in
# place, never moved. Links are downloaded to a private temp file that is
# deleted after decoding: direct media links with urllib, pages (YouTube, Loom,
# Vimeo, public Google Drive…) with yt-dlp when it is installed (audio only).
# Links may only point at public internet addresses, so a pasted link can't be
# used to poke at routers or other devices on the home network.

import ipaddress
import os
import shutil
import socket
import tempfile
import urllib.parse
import urllib.request

from .audio_input import SUPPORTED_EXTENSIONS, AudioInputError


class SourceError(AudioInputError):
    """A pasted path/link that can't be used; carries its own short code."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── Classifying what was pasted ──────────────────────────────────────────────
def classify(source: str) -> str:
    """'link' for http(s) URLs, 'path' for everything else."""
    text = (source or '').strip()
    return 'link' if text.lower().startswith(('http://', 'https://')) else 'path'


def resolve_path(source: str) -> str:
    """Validate a local file path; returns the absolute path."""
    text = (source or '').strip().strip('"').strip("'")
    if text.startswith('file://'):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    text = text.replace('\\ ', ' ')                      # paths copied from Terminal
    path = os.path.abspath(os.path.expanduser(text))
    if not text or not os.path.exists(path):
        raise SourceError('not_found', 'There is no file at that path. Tip: right-click the file in '
                          'Finder, hold ⌥ Option and choose "Copy … as Pathname".')
    if os.path.isdir(path):
        raise SourceError('is_folder', 'That is a folder. Paste the path of one audio or video file '
                          '(or use the watch folder for whole folders).')
    if os.path.splitext(path)[1].lower() not in SUPPORTED_EXTENSIONS:
        raise SourceError('unsupported_format', 'That file type is not supported. Use an audio or '
                          'video file (mp4, mov, m4a, mp3, wav…).')
    if not os.access(path, os.R_OK):
        raise SourceError('not_readable', 'Santa is not allowed to read that file.')
    return path


# ── Link safety ──────────────────────────────────────────────────────────────
def check_public_url(url: str) -> None:
    """Refuse links that resolve to loopback, private, link-local or other
    non-public addresses (SSRF protection)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise SourceError('bad_link', 'Only http:// and https:// links can be transcribed.')
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    except socket.gaierror:
        raise SourceError('bad_link', f'Cannot find the website {parsed.hostname}. Check the link '
                          'and the internet connection.')
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split('%')[0])
        if not address.is_global:
            raise SourceError('bad_link', 'Links to this computer or your local network are not allowed.')


class _PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)                        # every hop must stay public
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _looks_like_media(url: str) -> bool:
    return os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() in SUPPORTED_EXTENSIONS


# ── Downloading ──────────────────────────────────────────────────────────────
def download_link(url: str, max_bytes: int, cancel_event=None, on_progress=None) -> tuple:
    """Download the audio behind `url` into a private temp dir.
    Returns (file_path, display_name). The caller deletes the file."""
    check_public_url(url)
    work = tempfile.mkdtemp(prefix='santa-link-')
    try:
        if _looks_like_media(url):
            return _download_direct(url, work, max_bytes, cancel_event, on_progress)
        try:
            return _download_page(url, work, max_bytes, cancel_event, on_progress)
        except ImportError:
            # No yt-dlp: the link may still be a plain media file without an extension.
            return _download_direct(url, work, max_bytes, cancel_event, on_progress)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _download_direct(url, work, max_bytes, cancel_event, on_progress) -> tuple:
    opener = urllib.request.build_opener(_PublicOnlyRedirects)
    req = urllib.request.Request(url, headers={'User-Agent': 'Santa'})
    with opener.open(req, timeout=30) as resp:
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        name = os.path.basename(urllib.parse.urlparse(resp.geturl()).path) or 'link'
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            ext = _EXT_FOR_TYPE.get(ctype)
            if not ext:
                raise SourceError('unsupported_link', 'That link is a web page, not an audio/video file. '
                                  'Page links (YouTube, Loom, Vimeo…) need yt-dlp — run the Santa installer again.')
            name += ext
        total = int(resp.headers.get('Content-Length') or 0)
        if total and total > max_bytes:
            raise SourceError('audio_too_large', f'The file is {total / 1e6:.0f} MB; the limit is {max_bytes / 1e6:.0f} MB.')
        path = os.path.join(work, 'input' + ext)
        got = 0
        with open(path, 'wb') as fh:
            os.chmod(path, 0o600)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    from .models import TranscriptionCancelled
                    raise TranscriptionCancelled()
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > max_bytes:
                    raise SourceError('audio_too_large', f'The file is larger than the {max_bytes / 1e6:.0f} MB limit.')
                fh.write(chunk)
                if on_progress and total:
                    on_progress(got / total)
    return path, name


_EXT_FOR_TYPE = {
    'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a', 'audio/wav': '.wav',
    'audio/x-wav': '.wav', 'audio/ogg': '.ogg', 'audio/webm': '.webm', 'audio/flac': '.flac',
    'audio/aac': '.aac', 'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm',
    'video/x-matroska': '.mkv', 'video/x-m4v': '.m4v',
}


def _download_page(url, work, max_bytes, cancel_event, on_progress) -> tuple:
    """yt-dlp, audio only, one video (no playlists)."""
    import yt_dlp                                       # ImportError handled by caller

    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled('cancelled')
        if on_progress and d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total:
                on_progress(min(0.99, d.get('downloaded_bytes', 0) / total))

    options = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': os.path.join(work, 'input.%(ext)s'),
        'noplaylist': True, 'quiet': True, 'no_warnings': True, 'noprogress': True,
        'max_filesize': max_bytes, 'progress_hooks': [hook],
        'restrictfilenames': True, 'cachedir': False,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadCancelled:
        from .models import TranscriptionCancelled
        raise TranscriptionCancelled()
    except yt_dlp.utils.DownloadError as exc:
        raise SourceError('link_failed', 'Could not get the audio from that link. It may be private '
                          'or need a sign-in — download the file and drop it in instead. '
                          f'({str(exc)[:160]})')
    files = [f for f in os.listdir(work) if f.startswith('input.') and not f.endswith('.part')]
    if not files:
        raise SourceError('link_failed', 'That link did not contain audio Santa could download '
                          f'(it may be larger than the {max_bytes / 1e6:.0f} MB limit).')
    path = os.path.join(work, files[0])
    os.chmod(path, 0o600)
    title = (info or {}).get('title') or 'link'
    return path, title[:120] + os.path.splitext(path)[1]
