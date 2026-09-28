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

    _install_floating_pill()
    _headless_permission_handling()

    from ..main import main as whisper_local_main
    sys.argv = [sys.argv[0]]
    return whisper_local_main() or 0


def _install_floating_pill() -> None:
    """On macOS, swap Whisper Local's (disabled-on-Mac) Tk overlay for Santa's
    always-on-top pill. Done by wrapping StateManager.attach_components, which
    runs on the main thread right after the recorder exists."""
    if sys.platform != 'darwin':
        return
    from .. import state_manager as sm_module

    original = sm_module.StateManager.attach_components

    def attach_with_pill(self, audio_recorder, system_tray):
        original(self, audio_recorder, system_tray)
        try:
            from .floating_pill import SantaPill
            pill = SantaPill(state_manager=self, level_provider=audio_recorder.get_current_level)
            pill.start()
            self.level_overlay = pill
            print('   ✓ Santa floating assistant is on screen (drag to move, right-click for options)')
        except Exception as exc:
            print(f'   ⚠ Floating assistant unavailable: {type(exc).__name__}: {exc}')

    sm_module.StateManager.attach_components = attach_with_pill


def _headless_permission_handling() -> None:
    """Launched as 'Santa Assistant.app' there is no terminal to answer Whisper
    Local's permission question. Ask macOS for Accessibility instead (system
    dialog) and keep running: the pill and hotkeys stay up, and pasting works as
    soon as the permission is granted."""
    if sys.platform != 'darwin' or (sys.stdin is not None and sys.stdin.isatty()):
        return
    from ..platform import permissions

    def ask_system(config_manager):
        permissions.request_accessibility_permission()
        print('Accessibility permission requested from macOS; paste works once it is granted.')
        return False                       # main() then just runs the event loop

    permissions.handle_missing_permission = ask_system


if __name__ == '__main__':
    sys.exit(main())
