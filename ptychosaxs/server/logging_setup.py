"""Logging for the daemon, including the device layer's print() output.

That second part is not incidental. motions_ver2.build_controller announces
every driver fallback with print(), interferometers prints its USB-to-
softglue fallback, and motions_ver2.mv prints each hexapod servo recovery.
Those lines are the first thing anyone asks for when a stage comes up
simulated, and a daemon with stdout on /dev/null throws them away. Rather
than rewrite the device layer's output, we capture fd-level stdout.
"""

import logging
import logging.handlers
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 2 << 20
BACKUPS = 5


class _LineLogger:
    """A file-like that turns each written line into a log record."""

    def __init__(self, log, level):
        self._log = log
        self._level = level
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            if line.strip():
                self._log.log(self._level, "%s", line.rstrip())
        return len(text)

    def flush(self):
        if self._buf.strip():
            self._log.log(self._level, "%s", self._buf.rstrip())
        self._buf = ""

    def isatty(self):
        return False


def configure(path, level=logging.INFO, foreground=False):
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUPS)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)

    if foreground:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(stream)

    return logging.getLogger("ptychosaxs.server")


def capture_prints():
    """Send the device layer's print() into the log.

    Only when stdout is not a terminal: running --foreground in a shell, the
    operator wants to see those lines where they are.
    """
    if sys.stdout is not None and sys.stdout.isatty():
        return
    sys.stdout = _LineLogger(logging.getLogger("ptychosaxs.device"),
                             logging.INFO)
    sys.stderr = _LineLogger(logging.getLogger("ptychosaxs.device"),
                             logging.WARNING)
