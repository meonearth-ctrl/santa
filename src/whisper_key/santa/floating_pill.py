# santa/floating_pill.py
# The floating Santa assistant for macOS: a small "liquid glass" capsule that
# stays above every window (and every Space / full-screen app), like Wispr Flow.
# Designed to stay out of the way: no words, dimmed until you hover or dictate.
#   * mic glyph        click -> start; click again -> stop, text is pasted into the
#                      app/field you were typing in (Fn+Ctrl hold also works)
#   * tiny tag         input language › output script, e.g. "HI › अ", "HG › Aa";
#                      click to cycle the input language
#   * while recording  the glyph turns into a red dot and the tag into level bars
#   * afterwards       a brief green check (pasted) or amber mark (nothing / error);
#                      hover the capsule for the reason
#   * right-click      language, Hinglish output, Open Santa window, Hide
#   * drag             move it; the position is remembered
#
# It is a *non-activating* NSPanel: clicking it never takes keyboard focus away
# from the app you are writing in, which is what makes paste-at-cursor work.
# The glass is Apple's NSGlassEffectView (macOS 26 "Liquid Glass") when the
# installed PyObjC exposes it, otherwise the HUD NSVisualEffectView blur.
#
# It replaces Whisper Local's Tk level overlay (disabled on macOS) and keeps the
# same methods (show_recording, show_processing, flash_success, flash_failure,
# hide, set_streaming_text, start, shutdown), so state_manager drives it as-is.
# Those calls come from worker threads, so they only record the desired state;
# a 20 Hz timer on the main thread does all AppKit work.

import json
import logging
import threading
import time
import urllib.request
import webbrowser

import objc
import AppKit
from AppKit import (NSBackingStoreBuffered, NSBezierPath, NSColor, NSEvent, NSFont, NSMenu,
                    NSMenuItem, NSPanel, NSScreen, NSStatusWindowLevel, NSView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSTrackingArea)
from Foundation import NSAttributedString, NSMakeRect, NSObject, NSTimer

logger = logging.getLogger(__name__)

SANTA_URL = 'http://127.0.0.1:8765'
LANG_CYCLE = ['auto', 'en', 'hi', 'ar', 'hinglish', 'bn']
LANG_CODE = {'auto': 'AUTO', 'en': 'EN', 'hi': 'HI', 'ar': 'AR', 'hinglish': 'HG', 'bn': 'BN'}
LANG_NAME = {'auto': 'Auto-detect', 'en': 'English', 'hi': 'Hindi', 'ar': 'Arabic',
             'hinglish': 'Hinglish', 'bn': 'Bengali'}
# Output script shown after the arrow: what the text will be written in.
OUT_SCRIPT = {'en': 'Aa', 'hi': 'अ', 'ar': 'ع', 'bn': 'অ'}

WIDTH, HEIGHT = 84, 26
RADIUS = HEIGHT / 2
IDLE_ALPHA, ACTIVE_ALPHA = 0.55, 1.0
FLASH_SECONDS = 1.4

# NSTrackingArea options: mouseEnteredAndExited | activeAlways | inVisibleRect
_TRACKING = 0x01 | 0x80 | 0x200


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


def _santa_request(method, path, body=None, timeout=2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SANTA_URL + path, data=data, method=method,
                                 headers={'X-Santa': '1', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def language_tag(language: str, hinglish_output: str) -> str:
    """'HI › अ', 'HG › अa' (mixed), 'HG › Aa' (romanized), 'AUTO'."""
    if language == 'auto':
        return 'AUTO'
    if language == 'hinglish':
        return 'HG › ' + ('Aa' if hinglish_output == 'roman' else 'अa')
    return f"{LANG_CODE.get(language, language.upper()[:3])} › {OUT_SCRIPT.get(language, '')}".strip()


# ── Drawing view (sits on top of the glass) ─────────────────────────────────
class SantaPillView(NSView):

    def initWithFrame_(self, frame):
        self = objc.super(SantaPillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.pill = None
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(frame, _TRACKING, self, None)
        self.addTrackingArea_(area)
        return self

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True                     # one click works even when the pill isn't focused

    def mouseDownCanMoveWindow(self):
        return False                    # we move the window ourselves so clicks stay clicks

    def micRect(self):
        return NSMakeRect(3, 3, HEIGHT - 6, HEIGHT - 6)

    def tagRect(self):
        return NSMakeRect(HEIGHT, 0, WIDTH - HEIGHT - 4, HEIGHT)

    # ── Drawing ─────────────────────────────────────────────────────────
    def drawRect_(self, rect):
        p = self.pill
        if p is None:
            return
        b = self.bounds()
        # Glass edge: hairline rim plus a faint top highlight.
        rim = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, b.size.width - 1, b.size.height - 1), RADIUS - 0.5, RADIUS - 0.5)
        rim.setLineWidth_(0.8)
        _rgb(1, 1, 1, 0.28).setStroke()
        rim.stroke()
        _rgb(1, 1, 1, 0.07).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(3, b.size.height / 2, b.size.width - 6, b.size.height / 2 - 2), RADIUS - 3, RADIUS - 3).fill()

        mode, flash = p.mode, p.current_flash()
        mic = self.micRect()
        cx, cy = mic.origin.x + mic.size.width / 2, mic.origin.y + mic.size.height / 2
        ink = _rgb(1, 1, 1, 0.9)

        if mode == 'recording':
            pulse = 0.65 + 0.35 * abs(((time.time() * 1.6) % 2) - 1)
            _rgb(1.0, 0.27, 0.23, pulse).setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 5, cy - 5, 10, 10)).fill()
            self._draw_bars(p.level_smoothed)
            return
        if mode == 'processing':
            for i in range(3):
                phase = (time.time() * 3 - i * 0.4) % 3
                _rgb(1, 1, 1, 0.95 if phase < 1 else 0.3).setFill()
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 7 + i * 5.5, cy - 1.6, 3.2, 3.2)).fill()
        elif flash == 'success':
            _rgb(0.30, 0.85, 0.45).setStroke()
            tick = NSBezierPath.bezierPath()
            tick.moveToPoint_((cx - 4.5, cy))
            tick.lineToPoint_((cx - 1.2, cy - 3.4))
            tick.lineToPoint_((cx + 4.8, cy + 3.8))
            tick.setLineWidth_(1.8)
            tick.setLineCapStyle_(1)
            tick.stroke()
        elif flash == 'failure':
            _rgb(1.0, 0.72, 0.25).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(cx - 0.9, cy - 1, 1.8, 6.5), 0.9, 0.9).fill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 1.1, cy - 5, 2.2, 2.2)).fill()
        else:
            self._draw_mic(cx, cy, ink)
        self._draw_tag(p.tag)

    def _draw_mic(self, cx, cy, color):
        color.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(cx - 2.4, cy - 1, 4.8, 7.5), 2.4, 2.4).fill()
        arc = NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((cx, cy + 1.5), 4.3, 200, 340, False)
        arc.setLineWidth_(1.2)
        color.setStroke()
        arc.stroke()
        NSBezierPath.fillRect_(NSMakeRect(cx - 0.6, cy - 5.2, 1.2, 2.6))

    def _draw_tag(self, text):
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_weight_(9.5, 0.3),
                 NSForegroundColorAttributeName: _rgb(1, 1, 1, 0.72)}
        s = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        size = s.size()
        area = self.tagRect()
        s.drawAtPoint_((area.origin.x + (area.size.width - size.width) / 2,
                        (HEIGHT - size.height) / 2))

    def _draw_bars(self, level):
        area = self.tagRect()
        n, w, gap = 9, 2.2, 2.4
        x0 = area.origin.x + (area.size.width - (n * w + (n - 1) * gap)) / 2
        for i in range(n):
            centre = 1.0 - abs(i - (n - 1) / 2) / ((n - 1) / 2) * 0.55
            wobble = 0.55 + 0.45 * abs(((time.time() * 4 + i * 0.37) % 2) - 1)
            h = max(2.0, min(15.0, 2 + level * 180 * centre * wobble))
            _rgb(1, 1, 1, 0.85).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x0 + i * (w + gap), HEIGHT / 2 - h / 2, w, h), 1.1, 1.1).fill()

    # ── Mouse ───────────────────────────────────────────────────────────
    def mouseEntered_(self, event):
        if self.pill is not None:
            self.pill.hovered = True

    def mouseExited_(self, event):
        if self.pill is not None:
            self.pill.hovered = False

    def mouseDown_(self, event):
        p = self.pill
        if p is not None:
            p.dragged = False
            p.drag_origin = NSEvent.mouseLocation()
            p.window_origin = self.window().frame().origin

    def mouseDragged_(self, event):
        p = self.pill
        if p is None or p.drag_origin is None:
            return
        now = NSEvent.mouseLocation()
        dx, dy = now.x - p.drag_origin.x, now.y - p.drag_origin.y
        if abs(dx) + abs(dy) > 3:
            p.dragged = True
        self.window().setFrameOrigin_((p.window_origin.x + dx, p.window_origin.y + dy))

    def mouseUp_(self, event):
        p = self.pill
        if p is None:
            return
        if p.dragged:
            p.dragged = False
            p.panel.saveFrameUsingName_('SantaGlassPill')
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if point.x < HEIGHT:
            p.toggle_recording()
        else:
            p.cycle_language()

    def rightMouseDown_(self, event):
        if self.pill is not None:
            self.pill.rebuild_menu()
            NSMenu.popUpContextMenu_withEvent_forView_(self.pill.menu, event, self)


class _PillTarget(NSObject):
    """Receives the timer tick and menu actions on the main thread."""

    def tick_(self, timer):
        if self.pill is not None:
            self.pill._tick()

    def chooseLanguage_(self, sender):
        if self.pill is not None:
            self.pill.set_language(sender.representedObject())

    def chooseHinglishOutput_(self, sender):
        if self.pill is not None:
            self.pill.set_hinglish_output(sender.representedObject())

    def openSanta_(self, sender):
        webbrowser.open(SANTA_URL + '/')

    def hidePill_(self, sender):
        if self.pill is not None:
            self.pill.panel.orderOut_(None)


# ── Glass background ────────────────────────────────────────────────────────
def _make_glass(frame, content):
    """Liquid Glass on macOS 26 when available; blurred HUD material otherwise."""
    glass_cls = getattr(AppKit, 'NSGlassEffectView', None)
    if glass_cls is not None:
        try:
            glass = glass_cls.alloc().initWithFrame_(frame)
            glass.setCornerRadius_(RADIUS)
            glass.setContentView_(content)
            return glass, 'liquid-glass'
        except Exception as exc:                      # API shape differs: fall back
            logger.info('NSGlassEffectView unavailable (%s); using blur', exc)
    blur = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame)
    blur.setMaterial_(13)                             # NSVisualEffectMaterialHUDWindow
    blur.setBlendingMode_(0)                          # behind window
    blur.setState_(1)                                 # always active
    blur.setWantsLayer_(True)
    blur.layer().setCornerRadius_(RADIUS)
    blur.layer().setMasksToBounds_(True)
    blur.addSubview_(content)
    return blur, 'blur'


# ── Controller ───────────────────────────────────────────────────────────────
class SantaPill:
    def __init__(self, state_manager=None, level_provider=None):
        self.state_manager = state_manager
        self.level_provider = level_provider or (lambda: 0.0)
        self.mode = 'idle'                  # idle | recording | processing
        self.flash = None                   # 'success' | 'failure' (brief)
        self.flash_until = 0.0
        self.reason = ''
        self.level_smoothed = 0.0
        self.language = 'auto'
        self.hinglish_output = 'native'
        self.hovered = False
        self.dragged = False
        self.drag_origin = None
        self.window_origin = None
        self.glass_kind = None
        self._settings_checked = 0.0
        self._lock = threading.Lock()
        self.panel = None

    @property
    def tag(self) -> str:
        return language_tag(self.language, self.hinglish_output)

    def current_flash(self):
        return self.flash if self.flash and time.time() < self.flash_until else None

    # ── Build (main thread) ──────────────────────────────────────────────
    def start(self):
        screen = NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - WIDTH) / 2
        y = screen.origin.y + 18
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
        panel.setFrameUsingName_('SantaGlassPill')        # last dragged position, if any
        panel.setAlphaValue_(IDLE_ALPHA)

        frame = NSMakeRect(0, 0, WIDTH, HEIGHT)
        view = SantaPillView.alloc().initWithFrame_(frame)
        view.pill = self
        glass, self.glass_kind = _make_glass(frame, view)
        panel.setContentView_(glass)

        self.target = _PillTarget.alloc().init()
        self.target.pill = self
        self.menu = NSMenu.alloc().initWithTitle_('Santa')
        self.panel, self.view = panel, view
        panel.orderFrontRegardless()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self.target, 'tick:', None, True)
        threading.Thread(target=self._refresh_settings, daemon=True).start()

    def rebuild_menu(self):
        menu = self.menu
        menu.removeAllItems()

        def add(title, sel, obj=None, checked=False, parent=menu):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
            item.setTarget_(self.target)
            if obj is not None:
                item.setRepresentedObject_(obj)
            item.setState_(1 if checked else 0)
            parent.addItem_(item)

        for code in LANG_CYCLE:
            add(LANG_NAME[code], 'chooseLanguage:', code, checked=(code == self.language))
        menu.addItem_(NSMenuItem.separatorItem())
        add('Hinglish: mixed script (हिन्दी + English)', 'chooseHinglishOutput:', 'native',
            checked=(self.hinglish_output == 'native'))
        add('Hinglish: romanized (Latin)', 'chooseHinglishOutput:', 'roman',
            checked=(self.hinglish_output == 'roman'))
        menu.addItem_(NSMenuItem.separatorItem())
        add('Open Santa window', 'openSanta:')
        add('Hide until restart', 'hidePill:')

    # ── Whisper Local overlay interface (any thread) ─────────────────────
    def show_recording(self):
        self._set('recording')

    def show_processing(self):
        self._set('processing')

    def flash_success(self):
        from . import desktop_bridge
        self._set('idle', flash='success',
                  reason=(desktop_bridge.LAST_RESULT or {}).get('text') or 'Pasted')

    def flash_failure(self, reason: str = None):
        self._set('idle', flash='failure', reason=reason or 'Nothing recognised')

    def hide(self):
        self._set('idle')

    def set_streaming_text(self, text: str):
        pass                                    # Santa has no live preview (yet)

    def shutdown(self):
        try:
            self.timer.invalidate()
            self.panel.orderOut_(None)
        except Exception:
            pass

    def _set(self, mode, flash=None, reason=None):
        with self._lock:
            self.mode = mode
            if flash:
                self.flash, self.flash_until = flash, time.time() + FLASH_SECONDS
            if reason is not None:
                self.reason = reason

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
            self._set('recording')

    def cycle_language(self):
        i = LANG_CYCLE.index(self.language) if self.language in LANG_CYCLE else 0
        self.set_language(LANG_CYCLE[(i + 1) % len(LANG_CYCLE)])

    def set_language(self, code):
        self.language = code
        self._save({'language': code})

    def set_hinglish_output(self, value):
        self.hinglish_output = value
        self._save({'hinglish_output': value})

    def _save(self, changes):
        def run():
            try:
                _santa_request('PUT', '/api/settings', changes)
            except Exception as exc:
                self._set(self.mode, flash='failure', reason=f'Santa not reachable ({type(exc).__name__})')
        threading.Thread(target=run, daemon=True).start()

    def _refresh_settings(self):
        try:
            s = _santa_request('GET', '/api/settings')['settings']
            self.language, self.hinglish_output = s['language'], s['hinglish_output']
        except Exception:
            pass

    # ── Main-thread redraw ───────────────────────────────────────────────
    def _tick(self):
        now = time.time()
        if now - self._settings_checked > 5:    # follow changes made in the Santa window
            self._settings_checked = now
            threading.Thread(target=self._refresh_settings, daemon=True).start()
        if self.mode == 'recording':
            try:
                raw = float(self.level_provider() or 0.0)
            except Exception:
                raw = 0.0
            self.level_smoothed = 0.6 * self.level_smoothed + 0.4 * raw
        else:
            self.level_smoothed = 0.0
        busy = self.mode in ('recording', 'processing') or self.current_flash() is not None
        target_alpha = ACTIVE_ALPHA if (busy or self.hovered) else IDLE_ALPHA
        alpha = self.panel.alphaValue()
        if abs(alpha - target_alpha) > 0.01:
            self.panel.setAlphaValue_(alpha + (target_alpha - alpha) * 0.3)
        tip = self.reason if self.reason else f'Santa — {LANG_NAME.get(self.language, self.language)}. ' \
                                              'Click mic or hold fn⌃ to dictate.'
        if self.view.toolTip() != tip:
            self.view.setToolTip_(tip)
        self.view.setNeedsDisplay_(True)
