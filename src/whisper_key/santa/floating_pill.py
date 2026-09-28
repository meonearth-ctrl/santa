# santa/floating_pill.py
# The floating Santa assistant for macOS: a small pill that stays on top of every
# window (and every Space / full-screen app), like Wispr Flow's bar.
#   * click the mic      -> start recording; click again -> stop, text is pasted
#                           into whatever app/field you were typing in
#   * hold Fn+Ctrl       -> same as before (push-to-talk); the pill shows it
#   * live level bars while recording, "Transcribing…", then a preview of the
#     text that was pasted (or the reason nothing was)
#   * language chip      -> click to cycle Auto / EN / HI / AR / Hinglish / BN
#                           (same setting as the Santa window)
#   * drag anywhere else to move it (position is remembered); right-click for
#     "Open Santa window" / "Hide until restart"
#
# It is a *non-activating* NSPanel: clicking it never steals keyboard focus from
# the app you are writing in, which is what makes paste-at-cursor work.
#
# It replaces Whisper Local's Tk level overlay (disabled on macOS) and exposes
# the same methods (show_recording, show_processing, flash_success,
# flash_failure, hide, set_streaming_text, start, shutdown), so every existing
# state change in state_manager drives it without further changes. Those calls
# come from worker threads, so they only record the desired state; a 20 Hz timer
# on the main thread does all AppKit drawing.

import json
import logging
import threading
import time
import urllib.request
import webbrowser

import objc
from AppKit import (NSApp, NSEvent, NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont, NSMenu, NSMenuItem,
                    NSPanel, NSScreen, NSStatusWindowLevel, NSTextField, NSView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel, NSTextAlignmentLeft, NSTextAlignmentCenter,
                    NSLineBreakByTruncatingTail)
from Foundation import NSMakeRect, NSObject, NSTimer

logger = logging.getLogger(__name__)

SANTA_URL = 'http://127.0.0.1:8765'
LANG_CYCLE = ['auto', 'en', 'hi', 'ar', 'hinglish', 'bn']
LANG_LABEL = {'auto': 'AUTO', 'en': 'EN', 'hi': 'HI', 'ar': 'AR', 'hinglish': 'HING', 'bn': 'BN'}
WIDTH, HEIGHT = 300, 44
RED = (0.78, 0.16, 0.16)


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


def _santa_request(method, path, body=None, timeout=2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SANTA_URL + path, data=data, method=method,
                                 headers={'X-Santa': '1', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


# ── Views ────────────────────────────────────────────────────────────────────
class SantaPillView(NSView):
    """Background, mic button, level bars and language chip, all drawn here."""

    def initWithFrame_(self, frame):
        self = objc.super(SantaPillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.pill = None
        return self

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True                     # one click works even when the pill isn't focused

    def mouseDownCanMoveWindow(self):
        return False                    # we move the window ourselves so clicks stay clicks

    def hitTest_(self, point):
        # Labels are drawn on top; route every click to this view.
        hit = objc.super(SantaPillView, self).hitTest_(point)
        return self if hit is not None else None

    def mouseDown_(self, event):
        if self.pill is not None:
            self.pill.dragged = False
            self.pill.drag_origin = NSEvent.mouseLocation()
            self.pill.window_origin = self.window().frame().origin

    # Hit areas (view coordinates)
    def micRect(self):
        return NSMakeRect(6, 5, 34, 34)

    def chipRect(self):
        return NSMakeRect(WIDTH - 58, 11, 50, 22)

    def drawRect_(self, rect):
        p = self.pill
        if p is None:
            return
        bounds = self.bounds()
        _rgb(0.09, 0.09, 0.10, 0.92).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, HEIGHT / 2, HEIGHT / 2).fill()
        _rgb(1, 1, 1, 0.12).setStroke()
        border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1), HEIGHT / 2 - 0.5, HEIGHT / 2 - 0.5)
        border.setLineWidth_(1.0)
        border.stroke()

        # Mic button
        mode = p.mode
        mic = self.micRect()
        base = {'recording': (0.90, 0.22, 0.21), 'processing': (0.85, 0.55, 0.10)}.get(mode, RED)
        pulse = 0.75 + 0.25 * abs(((time.time() * 1.6) % 2) - 1) if mode == 'recording' else 1.0
        _rgb(*base, pulse).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(mic).fill()
        NSColor.whiteColor().setFill()
        cx, cy = mic.origin.x + 17, mic.origin.y + 17
        if mode == 'recording':        # stop square
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(cx - 6, cy - 6, 12, 12), 2, 2).fill()
        elif mode == 'processing':     # three dots
            for i in range(3):
                phase = (time.time() * 3 - i * 0.4) % 3
                alpha = 1.0 if phase < 1 else 0.35
                _rgb(1, 1, 1, alpha).setFill()
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 9 + i * 7, cy - 2, 4.5, 4.5)).fill()
        else:                           # microphone glyph
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(cx - 3.5, cy - 2, 7, 12), 3.5, 3.5).fill()
            stand = NSBezierPath.bezierPath()
            stand.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((cx, cy + 1), 6.5, 200, 340, False)
            stand.setLineWidth_(1.8)
            NSColor.whiteColor().setStroke()
            stand.stroke()
            NSBezierPath.fillRect_(NSMakeRect(cx - 0.9, cy - 9, 1.8, 4))

        # Level bars while recording (the text label is hidden then)
        if mode == 'recording':
            level = p.level_smoothed
            n, x0, gap, w = 16, 50, 3, 4
            for i in range(n):
                shape = 0.45 + 0.55 * abs((i - n / 2) / (n / 2) - 0) ** 0.1
                wobble = 0.6 + 0.4 * abs(((time.time() * 4 + i * 0.37) % 2) - 1)
                h = max(3.0, min(26.0, 4 + level * 260 * shape * wobble))
                _rgb(0.35, 0.85, 0.45, 0.95).setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x0 + i * (w + gap), HEIGHT / 2 - h / 2, w, h), 2, 2).fill()

        # Language chip
        chip = self.chipRect()
        _rgb(1, 1, 1, 0.14).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(chip, 11, 11).fill()

    def mouseUp_(self, event):
        p = self.pill
        if p is None:
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if p.dragged:
            p.dragged = False
            return
        if _inside(point, self.micRect()):
            p.toggle_recording()
        elif _inside(point, self.chipRect()):
            p.cycle_language()

    def mouseDragged_(self, event):
        p = self.pill
        if p is None or p.drag_origin is None:
            return
        now = NSEvent.mouseLocation()
        dx, dy = now.x - p.drag_origin.x, now.y - p.drag_origin.y
        if abs(dx) + abs(dy) > 3:
            p.dragged = True
        self.window().setFrameOrigin_((p.window_origin.x + dx, p.window_origin.y + dy))

    def rightMouseDown_(self, event):
        if self.pill is not None:
            NSMenu.popUpContextMenu_withEvent_forView_(self.pill.menu, event, self)


def _inside(point, rect):
    return (rect.origin.x <= point.x <= rect.origin.x + rect.size.width and
            rect.origin.y <= point.y <= rect.origin.y + rect.size.height)


class _PillTarget(NSObject):
    """Receives the timer tick and menu actions on the main thread."""

    def tick_(self, timer):
        if self.pill is not None:
            self.pill._tick()

    def openSanta_(self, sender):
        webbrowser.open(SANTA_URL + '/')

    def hidePill_(self, sender):
        if self.pill is not None:
            self.pill.panel.orderOut_(None)


# ── Controller ───────────────────────────────────────────────────────────────
class SantaPill:
    def __init__(self, state_manager=None, level_provider=None):
        self.state_manager = state_manager
        self.level_provider = level_provider or (lambda: 0.0)
        self.mode = 'idle'                      # idle | recording | processing | success | failure
        self.message = ''
        self.message_until = 0.0
        self.level_smoothed = 0.0
        self.language = 'auto'
        self.dragged = False
        self.drag_origin = None
        self.window_origin = None
        self._lang_checked = 0.0
        self._lock = threading.Lock()
        self.panel = None

    # ── Build (main thread) ──────────────────────────────────────────────
    def start(self):
        screen = NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - WIDTH) / 2
        y = screen.origin.y + 24
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                                     NSWindowCollectionBehaviorFullScreenAuxiliary |
                                     NSWindowCollectionBehaviorStationary)
        panel.setFrameAutosaveName_('SantaFloatingPill')     # remembers where you dragged it

        view = SantaPillView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        view.pill = self
        panel.setContentView_(view)

        self.label = self._label(NSMakeRect(48, 12, WIDTH - 48 - 64, 20), 12.5, NSTextAlignmentLeft)
        self.chip_label = self._label(NSMakeRect(WIDTH - 58, 14, 50, 16), 10.5, NSTextAlignmentCenter)
        self.chip_label.setFont_(NSFont.boldSystemFontOfSize_(10.5))
        view.addSubview_(self.label)
        view.addSubview_(self.chip_label)

        self.target = _PillTarget.alloc().init()
        self.target.pill = self
        self.menu = NSMenu.alloc().initWithTitle_('Santa')
        for title, sel in (('Open Santa window', 'openSanta:'), ('Hide until restart', 'hidePill:')):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
            item.setTarget_(self.target)
            self.menu.addItem_(item)

        self.panel, self.view = panel, view
        panel.orderFrontRegardless()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self.target, 'tick:', None, True)
        threading.Thread(target=self._refresh_language, daemon=True).start()

    @staticmethod
    def _label(frame, size, align):
        label = NSTextField.labelWithString_('')
        label.setFrame_(frame)
        label.setFont_(NSFont.systemFontOfSize_(size))
        label.setTextColor_(_rgb(0.92, 0.92, 0.92))
        label.setAlignment_(align)
        label.setLineBreakMode_(NSLineBreakByTruncatingTail)
        return label

    # ── Whisper Local overlay interface (any thread) ─────────────────────
    def show_recording(self):
        self._set('recording', '')

    def show_processing(self):
        self._set('processing', 'Transcribing…')

    def flash_success(self):
        from . import desktop_bridge
        preview = (desktop_bridge.LAST_RESULT or {}).get('text') or 'Done'
        self._set('success', '✓ ' + preview, hold=4.0)

    def flash_failure(self, reason: str = None):
        self._set('failure', reason or 'Nothing recognised', hold=4.0)

    def hide(self):
        self._set('idle', '')

    def set_streaming_text(self, text: str):
        pass                                    # Santa has no live preview (yet)

    def shutdown(self):
        try:
            self.timer.invalidate()
            self.panel.orderOut_(None)
        except Exception:
            pass

    def _set(self, mode, message, hold=0.0):
        with self._lock:
            self.mode = mode if mode in ('recording', 'processing') else 'idle'
            self.message = message
            self.message_until = time.time() + hold if hold else 0.0

    # ── Actions (main thread) ────────────────────────────────────────────
    def toggle_recording(self):
        sm = self.state_manager
        if sm is None:
            return
        state = sm.get_current_state()
        if state == 'recording':
            threading.Thread(target=sm.stop_recording, daemon=True).start()
        elif state == 'idle':
            threading.Thread(target=sm.start_recording, daemon=True).start()
            self._set('recording', '')

    def cycle_language(self):
        nxt = LANG_CYCLE[(LANG_CYCLE.index(self.language) + 1) % len(LANG_CYCLE)] \
            if self.language in LANG_CYCLE else 'en'
        self.language = nxt
        self._set('idle', f'Language: {_long_name(nxt)}', hold=2.0)

        def save():
            try:
                _santa_request('PUT', '/api/settings', {'language': nxt})
            except Exception as exc:
                self._set('idle', f'Could not reach Santa ({type(exc).__name__})', hold=3.0)
        threading.Thread(target=save, daemon=True).start()

    def _refresh_language(self):
        try:
            self.language = _santa_request('GET', '/api/settings')['settings']['language']
        except Exception:
            pass

    # ── Main-thread redraw ───────────────────────────────────────────────
    def _tick(self):
        now = time.time()
        if now - self._lang_checked > 5:        # pick up changes made in the Santa window
            self._lang_checked = now
            threading.Thread(target=self._refresh_language, daemon=True).start()
        with self._lock:
            mode, message, until = self.mode, self.message, self.message_until
        if mode == 'recording':
            try:
                raw = float(self.level_provider() or 0.0)
            except Exception:
                raw = 0.0
            self.level_smoothed = 0.6 * self.level_smoothed + 0.4 * raw
        else:
            self.level_smoothed = 0.0
        if until and now > until:
            message = ''
        if mode == 'recording':
            text = ''
        elif message:
            text = message
        else:
            text = 'Click or hold fn⌃ to dictate'
        if self.label.stringValue() != text:
            self.label.setStringValue_(text)
        self.label.setHidden_(mode == 'recording')
        chip = LANG_LABEL.get(self.language, self.language.upper()[:4])
        if self.chip_label.stringValue() != chip:
            self.chip_label.setStringValue_(chip)
        self.view.setNeedsDisplay_(True)


def _long_name(code):
    return {'auto': 'Auto-detect', 'en': 'English', 'hi': 'Hindi', 'ar': 'Arabic',
            'hinglish': 'Hinglish', 'bn': 'Bengali'}.get(code, code)
