# santa/keep_awake.py
# Stops Windows from going to sleep while Santa is transcribing. A 90-minute
# interview can take 30+ minutes on a laptop CPU, longer than the usual 15–30
# minute sleep timer, and sleep would freeze the job halfway. The screen may still
# turn off. Elsewhere this does nothing: the Mac build has its own rules.

import contextlib
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(platform: str = None):
    """While the block runs, the PC stays awake (Windows only; per thread)."""
    kernel32 = None
    if (platform or sys.platform) == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            kernel32 = None
    try:
        yield
    finally:
        if kernel32 is not None:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)   # back to normal sleep rules
