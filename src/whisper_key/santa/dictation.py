# santa/dictation.py
# `santa-dictation`: dictate straight into the text field of ANY app.
# Starts (or reuses) the Santa server, points Whisper Local's desktop dictation
# at it (`whisper.backend: santa`), then runs Whisper Local's hotkey app:
#   hold Fn+Ctrl, speak, release -> the text is pasted at the cursor.
# Auto-send (pressing Enter after pasting) stays OFF unless you hold Option
# while releasing, exactly as in Whisper Local.

import sys


def main() -> int:
    from .desktop_bridge import ensure_server
    print('🎅 Santa dictation — starting the speech server…')
    if not ensure_server(wait_seconds=60):
        print('Could not start the Santa server. Open the Santa app once to check the log.')
        return 1

    from ..config_manager import ConfigManager
    cm = ConfigManager(quiet=True)
    if cm.config.get('whisper', {}).get('backend') != 'santa':
        cm.update_user_setting('whisper', 'backend', 'santa')
        print('   ✓ Whisper Local dictation now uses Santa as its engine.')

    from ..main import main as whisper_local_main
    sys.argv = [sys.argv[0]]
    return whisper_local_main() or 0


if __name__ == '__main__':
    sys.exit(main())
