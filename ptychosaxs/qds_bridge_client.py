"""
A quiet QDS reader for the GUI process, fed by qds_bridge/qds_publisher.py.

Unlike qds_bridge/qds_subscriber.py (a diagnostic CLI that prints every
sample and a periodic loss summary), this is meant to run silently inside
rungui.py as a background thread: no output at all while samples keep
arriving, and only the connectivity transitions below printed to terminal.

Watchdog behaviour:
  - 10.0s grace period after connect() before the first "no data" check,
    since the publisher may simply not have sent anything yet.
  - The first time the gap since the last sample reaches 5.0s, print that
    once.
  - While silent, re-check every 5.0s rather than on every poll - no repeat
    prints for a connection that is still down.
  - The moment a sample arrives while silent, print that the connection is
    back and resume silence.

get_position() matches the (rel, abs) shape ptycho_qudis/_QDSDebug return
(interferometers.py), so this is a drop-in `qds` object: `rel` is a
one-device list `[[ch0, ch1, ch2]]`, `abs` is always None (the bridge only
ever carries relative positions - see qds_bridge/README.md).

This is the one place in ptychosaxs/ that imports from qds_bridge/: the wire
format and host/port must not drift between the two copies that run on
separate machines. qds_bridge/ never imports back.
"""

import os
import struct
import sys
import threading
import time

import zmq

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from qds_bridge import config as _bridge_config  # noqa: E402
from qds_bridge import wire as _bridge_wire  # noqa: E402

STARTUP_GRACE_S = 10.0
SILENCE_THRESHOLD_S = 5.0
RECHECK_INTERVAL_S = 5.0
_POLL_TIMEOUT_MS = 500

# Printed whenever the bridge looks unreachable (no data, or a synchronous
# connect failure), so whoever is staring at the GUI terminal has the actual
# fix in front of them instead of having to go find this README.
_CANNOT_CONNECT_MSG = (
    "Cannot connect to qds interferometer. As of Oct 2026:\n"
    "Login to s12ide@sec12mic, in the Anaconda terminal run:\n"
    "conda activate ptychoSAXS\n"
    "cd C:\\Users\\s12ide\\Documents\\GitHub\\ptychoSAXS\\qds_bridge\n"
    "python qds_publisher.py\n"
    "Or simply double-click qds_publisher on the desktop."
)


class QDSBridgeClient:
    """Subscribes to one qds_bridge publisher and reports its latest sample."""

    def __init__(self, host=_bridge_config.QDS_HOST, port=_bridge_config.QDS_PORT):
        self._endpoint = _bridge_config.endpoint(host, port)
        self._lock = threading.Lock()
        self._latest = None
        self._have_data = False
        self._connect_error = None
        self._thread = None
        self._stop = threading.Event()

    def connect(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="qds-bridge-client", daemon=True,
        )
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def get_position(self):
        with self._lock:
            if self._connect_error is not None:
                raise RuntimeError(
                    f"QDS bridge: connect failed on {self._endpoint}: "
                    f"{self._connect_error}"
                )
            if not self._have_data:
                raise RuntimeError(
                    f"QDS bridge: no samples received yet from {self._endpoint}"
                )
            ch0, ch1, ch2 = self._latest
        return [[ch0, ch1, ch2]], None

    def _run(self):
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        try:
            sock.setsockopt(zmq.RCVHWM, _bridge_config.RCV_HWM)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.SUBSCRIBE, _bridge_config.TOPIC)
            sock.connect(self._endpoint)
        except zmq.ZMQError as exc:
            print(f"[qds-client] connect failed: {exc}")
            print(_CANNOT_CONNECT_MSG)
            with self._lock:
                self._connect_error = str(exc)
            sock.close()
            ctx.term()
            return

        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)

        start = time.monotonic()
        last_data = None
        silent = False
        next_check = start + STARTUP_GRACE_S + SILENCE_THRESHOLD_S

        try:
            while not self._stop.is_set():
                if not poller.poll(timeout=_POLL_TIMEOUT_MS):
                    now = time.monotonic()
                    if now - start < STARTUP_GRACE_S or now < next_check:
                        continue
                    gap = now - (last_data if last_data is not None else start)
                    if not silent and gap >= SILENCE_THRESHOLD_S:
                        print(f"[qds-client] no data for {gap:.1f}s "
                              f"from {self._endpoint}")
                        print(_CANNOT_CONNECT_MSG)
                        silent = True
                    next_check = now + RECHECK_INTERVAL_S
                    continue

                while True:
                    try:
                        frames = sock.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                    if len(frames) != 2:
                        continue
                    try:
                        _seq, ch0, ch1, ch2 = _bridge_wire.unpack_sample(frames[1])
                    except struct.error:
                        continue

                    last_data = time.monotonic()
                    if silent:
                        print(f"[qds-client] connection restored "
                              f"({self._endpoint})")
                        silent = False
                    with self._lock:
                        self._latest = (ch0, ch1, ch2)
                        self._have_data = True
        finally:
            sock.close()
            ctx.term()
