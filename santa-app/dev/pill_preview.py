# santa-app/dev/pill_preview.py
# Renders the floating pill in each state to PNGs (offscreen, no permissions
# needed) so its look can be checked: santa-app/dev/out/pill_<state>.png
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'src'))
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSBitmapImageFileTypePNG  # noqa: E402
from Foundation import NSDate, NSRunLoop  # noqa: E402

app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
from whisper_key.santa.floating_pill import SantaPill  # noqa: E402
from whisper_key.santa import desktop_bridge  # noqa: E402


class FakeSM:
    def get_current_state(self):
        return 'idle'


level = {'v': 0.0}
pill = SantaPill(state_manager=FakeSM(), level_provider=lambda: level['v'])
pill.start()


def pump(seconds):
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def snap(name):
    pill._tick()
    view = pill.view
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    data.writeToFile_atomically_(os.path.join(HERE, 'out', f'pill_{name}.png'), True)
    print('wrote', name)


pump(1.0); snap('idle')
pill.show_recording(); level['v'] = 0.06; pump(0.6); snap('recording')
pill.show_processing(); pump(0.5); snap('processing')
desktop_bridge.LAST_RESULT = {'text': 'आज की meeting में budget finalize करना है'}
pill.flash_success(); pump(0.3); snap('success')
pill.flash_failure('No speech detected'); pump(0.3); snap('failure')
pill.language = 'hinglish'; pump(0.2); snap('hinglish')
print('frame', pill.panel.frame(), 'level', pill.panel.level(), 'visible', pill.panel.isVisible())
pill.shutdown()
