# santa/tray.py
# Windows notification-area (tray) icon for the Santa server. On Windows Santa
# runs without a console window, so the tray is how people see it is running and
# how they stop it. Menu: Open Santa · Videos folder · Transcripts folder · Quit.
# Optional: if pystray/Pillow are missing, Santa just runs without a tray.

import logging
import os
import threading
import webbrowser

from .settings import SettingsStore

logger = logging.getLogger(__name__)


def _open_folder(path: str) -> None:
    path = os.path.abspath(os.path.expanduser(path or ''))
    if not path:
        return
    os.makedirs(path, exist_ok=True)
    if hasattr(os, 'startfile'):
        os.startfile(path)          # Explorer on Windows
    else:
        webbrowser.open('file://' + path)


def _icon_image():
    from importlib import resources
    from PIL import Image
    with resources.files('whisper_key.santa').joinpath('web', 'icon-180.png').open('rb') as fh:
        return Image.open(fh).convert('RGBA').resize((64, 64))


def start_tray(url: str, settings: SettingsStore, on_quit) -> bool:
    """Show the tray icon on a background thread. Returns False if unavailable."""
    try:
        import pystray
        image = _icon_image()
    except Exception as exc:
        logger.info('tray icon unavailable (%s)', type(exc).__name__)
        return False

    def folder_item(label, key):
        return pystray.MenuItem(label, lambda *_: _open_folder(settings.get()[key]),
                                visible=lambda _item: bool(settings.get()[key]))

    def quit_santa(icon, _item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem('Open Santa', lambda *_: webbrowser.open(url), default=True),
        folder_item('Videos folder (watched)', 'watch_dir'),
        folder_item('Transcripts folder', 'export_dir'),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit Santa', quit_santa),
    )
    icon = pystray.Icon('Santa', image, 'Santa: offline transcription', menu)
    threading.Thread(target=icon.run, daemon=True, name='santa-tray').start()
    return True
