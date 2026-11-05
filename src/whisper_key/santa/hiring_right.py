# santa/hiring_right.py
# The Hiring Right set-up, shared by the Mac and Windows installers: where the
# interview folders live, and the settings an interviewer's Santa needs
# (watch 02_Batch_Videos, export .json/.srt into 03_Transcripts, English,
# files up to 120 min). `santa --setup-hiring-right` applies it.
#
# On Windows the Documents folder can be moved (e.g. into OneDrive), so we ask
# Windows where it really is instead of assuming %USERPROFILE%\Documents.

import os
import sys

from .models import total_ram_gb

FOLDER_NAME = 'Hiring Right'
VIDEOS_SUBDIR = '02_Batch_Videos'
TRANSCRIPTS_SUBDIR = '03_Transcripts'

# Below this much RAM the Large-v3 Turbo model (≈2.5 GB resident) crowds out the
# browser and video player, so smaller laptops get Small instead.
TURBO_MIN_RAM_GB = 8.0

# FOLDERID_Documents: https://learn.microsoft.com/windows/win32/shell/knownfolderid
_FOLDERID_DOCUMENTS = '{FDD39AD0-238F-46AF-ADB4-6C85480369C7}'


# ── Folder locations ─────────────────────────────────────────────────────────
def _windows_documents_dir():
    """The user's real Documents folder from the Windows shell, or None."""
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                        ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

        raw = uuid.UUID(_FOLDERID_DOCUMENTS).bytes_le
        guid = GUID.from_buffer_copy(raw)
        out = ctypes.c_wchar_p()
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return None


def documents_dir(platform: str = None) -> str:
    """Documents folder: the shell's answer on Windows, else ~/Documents."""
    platform = platform or sys.platform
    if platform == 'win32':
        found = _windows_documents_dir()
        if found:
            return found
    return os.path.join(os.path.expanduser('~'), 'Documents')


def hiring_right_dirs(documents: str = None) -> dict:
    """Absolute paths of the Hiring Right root, videos and transcripts folders."""
    root = os.path.join(documents or documents_dir(), FOLDER_NAME)
    return {'root': root,
            'videos': os.path.join(root, VIDEOS_SUBDIR),
            'transcripts': os.path.join(root, TRANSCRIPTS_SUBDIR)}


# ── Settings preset ──────────────────────────────────────────────────────────
def recommended_model(ram_gb: float) -> str:
    """Large-v3 Turbo (the same model as the Mac) unless the laptop is small.
    ram_gb == 0 means "unknown", which keeps the default."""
    return 'small' if 0 < ram_gb < TURBO_MIN_RAM_GB else 'large-v3-turbo'


def preset_settings(dirs: dict, ram_gb: float) -> dict:
    """Settings an interviewer's Santa needs for Hiring Right (mirrors the Mac set-up)."""
    return {
        'model': recommended_model(ram_gb),
        'language': 'en',
        'watch_language': 'en',
        'export_enabled': True,
        'export_dir': dirs['transcripts'],
        'watch_enabled': True,
        'watch_dir': dirs['videos'],
        'max_upload_mb': 2000,       # batch videos run 45–90 min
        'max_duration_min': 120,
        'diarize_files': True,       # adds "speaker" per segment, as on the Mac
        'output_language': 'same',
        'preload_model': True,
    }


def apply(store, documents: str = None, ram_gb: float = None) -> dict:
    """Create the folders and save the preset. Returns {'dirs', 'settings'}."""
    dirs = hiring_right_dirs(documents)
    for key in ('videos', 'transcripts'):
        os.makedirs(dirs[key], exist_ok=True)
    ram = total_ram_gb() if ram_gb is None else ram_gb
    saved = store.update(preset_settings(dirs, ram))
    return {'dirs': dirs, 'settings': saved}
