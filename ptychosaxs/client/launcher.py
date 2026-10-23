"""Connect to the shared server, starting it if nobody has yet.

The race this has to survive: three panels launched together all find no
socket and all spawn a daemon. That is fine, and deliberately so -- the
daemon takes an exclusive flock before importing anything, so the losers
exit 0 having touched no hardware. A child exiting 0 is therefore the
*expected* outcome for all but one of them, and the launcher keeps polling
rather than treating it as a failure.

What it must not do is give up with a bare timeout. Building the hexapod
over GCS, the goniometer and the QDS takes seconds and can fall back, so on
failure we read the tail of the server log and put that in the exception
instead of "timed out".
"""

import os
import socket
import subprocess
import sys
import time

from ptychosaxs.link import config


class ServerUnavailable(RuntimeError):
    """No server, and we were told not to start one."""


class ServerStartFailed(RuntimeError):
    """We started one and it did not come up."""


def try_connect(path, timeout=0.5):
    """Connect, or return None. Never raises for the ordinary cases."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        sock.close()
        return None
    sock.settimeout(None)
    return sock


def spawn_server(extra_args=()):
    """Start a detached daemon. Its stdio goes nowhere; it logs to a file."""
    return subprocess.Popen(
        [sys.executable, "-m", "ptychosaxs.server", *extra_args],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/",
        env=os.environ.copy(),
    )


def _log_tail(lines=25):
    try:
        with open(config.log_path()) as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return "(no server log)"


def ensure_server(path=None, *, start_server=True, timeout_s=20.0,
                  extra_args=()):
    """Return a connected socket, starting the daemon if necessary."""
    path = path or config.socket_path()

    sock = try_connect(path)
    if sock is not None:
        return sock

    if not start_server:
        raise ServerUnavailable(
            f"no ptychoSAXS server on {path} and start_server is False")

    proc = spawn_server(extra_args)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sock = try_connect(path)
        if sock is not None:
            return sock
        rc = proc.poll()
        if rc is not None and rc != 0:
            raise ServerStartFailed(
                f"the server exited with status {rc} and no socket "
                f"appeared.\n--- {config.log_path()} ---\n{_log_tail()}")
        # rc == 0 means another daemon won the flock; keep waiting for its
        # socket rather than reporting a failure.
        time.sleep(0.1)

    raise ServerStartFailed(
        f"no socket at {path} after {timeout_s}s.\n"
        f"--- {config.log_path()} ---\n{_log_tail()}")
