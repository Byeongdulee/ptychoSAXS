"""Route SIGTERM through the existing KeyboardInterrupt cleanup.

Same approach as qds_bridge/shutdown.py: the daemon already has a correct
teardown path for Ctrl-C, and a second one for signals would be a second
thing to keep right.
"""

import signal


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def install():
    """Quietly does nothing where the platform or context will not allow it
    (SIGTERM absent, or not on the main thread) -- failing to install a
    shutdown nicety must not stop the server starting."""
    if not hasattr(signal, "SIGTERM"):
        return
    try:
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        pass
