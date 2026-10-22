"""
Make SIGTERM shut down as cleanly as Ctrl-C does.

Both ends do real work on the way out: the publisher disconnects the quDIS
devices, and both print a final summary. All of that hangs off the
KeyboardInterrupt handler, so an unhandled SIGTERM would skip it -- leaving
the USB devices connected and the run's numbers unreported. Anything that
supervises these processes (a service wrapper, a `kill` in a script, a
container stop) sends SIGTERM, not SIGINT.

Turning the signal into the exception the cleanup already catches is enough;
no second shutdown path to keep in step.
"""

import signal


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def install():
    """Route SIGTERM through the existing KeyboardInterrupt cleanup.

    Quietly does nothing where the platform or context will not allow it
    (SIGTERM is absent, or we are not on the main thread) -- failing to
    install a shutdown nicety must not stop the program starting.
    """
    if not hasattr(signal, "SIGTERM"):
        return
    try:
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        pass
