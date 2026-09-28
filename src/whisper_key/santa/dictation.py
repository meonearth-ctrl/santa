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

    _main_thread_garbage_collection()
    _install_floating_pill()
    _headless_permission_handling()
    _tap_to_latch()
    _paste_permission_guard()

    from ..main import main as whisper_local_main
    sys.argv = [sys.argv[0]]
    return whisper_local_main() or 0


def _main_thread_garbage_collection() -> None:
    """Tk objects (Whisper Local's welcome/fallback windows) must be destroyed on
    the main thread; if Python's cycle collector happens to free one on a worker
    thread (e.g. the recorder starting), Tcl aborts the whole app
    ("Tcl_AsyncDelete: async handler deleted by the wrong thread"). So automatic
    cycle collection is switched off and the floating pill's main-thread timer
    runs gc.collect() every few seconds instead (see floating_pill._tick).
    Ordinary reference-counted objects are still freed immediately."""
    import gc
    gc.collect()
    gc.disable()


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
            import gc
            gc.enable()        # no main-thread timer to collect for us

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


# A tap shorter than this (press + release) starts hands-free recording instead
# of producing a useless half-second clip; tap fn (or click the capsule) to stop.
TAP_SECONDS = 0.45


def _tap_to_latch() -> None:
    """Hold Fn+Ctrl = push-to-talk (as before). A quick *tap* of Fn+Ctrl now
    keeps recording hands-free until you tap Fn (or click the capsule)."""
    import time
    from ..hotkey_listener import HotkeyListener

    original = HotkeyListener._push_to_talk_released

    def released(self):
        sm = self.state_manager
        started = getattr(getattr(sm, 'audio_recorder', None), 'recording_start_time', None)
        if (sm.get_current_state() == 'recording' and started
                and time.time() - started < TAP_SECONDS):
            self.keys_armed = True          # the next Fn press is the stop key
            print('   ⏺ Hands-free: tap fn (or click the capsule) to stop')
            return
        original(self)

    HotkeyListener._push_to_talk_released = released


def _paste_permission_guard() -> None:
    """macOS silently drops the simulated ⌘V when the app has no Accessibility
    permission, so the text never appears. Detect that, leave the text on the
    clipboard, tell the user via the capsule, and open the right Settings pane
    once so they can allow it."""
    if sys.platform != 'darwin':
        return
    from .. import clipboard_manager as cm_module
    from . import desktop_bridge
    opened = {'done': False}
    original = cm_module.ClipboardManager._clipboard_paste

    def guarded(self, text):
        try:
            from ApplicationServices import AXIsProcessTrusted
            trusted = bool(AXIsProcessTrusted())
        except Exception:
            trusted = True
        if trusted:
            desktop_bridge.PASTE_BLOCKED = False
            return original(self, text)
        self.copy_text(text)
        desktop_bridge.PASTE_BLOCKED = True
        print('   ⚠ Not allowed to paste (Accessibility off) — text is on the clipboard, press ⌘V')
        if not opened['done']:
            opened['done'] = True
            import subprocess
            subprocess.Popen(['open', 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'])
        return True

    cm_module.ClipboardManager._clipboard_paste = guarded


if __name__ == '__main__':
    sys.exit(main())
