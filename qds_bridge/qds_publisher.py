"""
Windows side: read the quDIS interferometer over USB and publish every sample.

Run this on the machine with the USB connection (sec12mic). It polls the
device in a tight loop, stamps each reading with a monotonically increasing
sequence number, and publishes it immediately as one ZMQ message.

    python qds_publisher.py                 # real hardware
    python qds_publisher.py --simulate      # no hardware, synthetic samples
    python qds_publisher.py --simulate --rate 20000

Acquisition does not depend on anyone listening. The PUB socket is bound, not
connected, and sends are non-blocking: with no subscriber the messages go
nowhere and the loop keeps its rate. That is the behaviour being measured --
see config.SND_HWM.

Startup loss is expected and fine. A subscriber that connects mid-run joins at
whatever sequence number is current; it reports that as its baseline rather
than as loss.

This module imports nothing from the ptychoSAXS package. Its only
dependencies are pyzmq and (unless --simulate) pyqudis.
"""

import argparse
import os
import sys
import time

import zmq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import shutdown  # noqa: E402
from wire import pack_sample  # noqa: E402


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class HardwareSource:
    """The real quDIS over USB, via the pyqudis ctypes wrapper.

    Mirrors what ptycho_qudis.connect() does: discover over Ethernet+USB3,
    then connect every device found. We publish the *relative* positions of
    one device, which is what the GUI's get_qds_pos consumes.
    """

    IF_ETHERNET = 0x02
    IF_USB3 = 0x10

    def __init__(self, device=0):
        self.device = device
        self._qudis = None
        self._poll = None
        self.device_count = 0

    def open(self):
        import ctypes

        # Imported here, not at module scope, so --simulate needs neither the
        # DLL nor pyqudis's own matplotlib/IPython imports.
        from qds.qds import QuDIS, poll_position

        self._poll = poll_position
        self._qudis = QuDIS()

        dev_count = ctypes.c_uint()
        rc = self._qudis.qdslib.QDS_discover(
            self.IF_ETHERNET | self.IF_USB3, ctypes.byref(dev_count)
        )
        if rc != 0:
            raise RuntimeError(f"QDS_discover failed (code {rc}); no quDIS found")
        self.device_count = dev_count.value
        if self.device_count == 0:
            raise RuntimeError("QDS_discover found no devices")
        if self.device >= self.device_count:
            raise RuntimeError(
                f"--device {self.device} requested but only "
                f"{self.device_count} device(s) found"
            )
        for dev in range(self.device_count):
            self._qudis.qdslib.QDS_connect(dev)
        print(f"quDIS: {self.device_count} device(s), publishing device "
              f"{self.device} relative positions (nm)")

    def read(self):
        rel, _abs = self._poll(self._qudis, self.device)
        return rel[0], rel[1], rel[2]

    def close(self):
        if self._qudis is None:
            return
        for dev in range(self.device_count):
            try:
                self._qudis.qdslib.QDS_disconnect(dev)
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                print(f"QDS_disconnect({dev}) warning: {exc}")


class SimulatedSource:
    """Synthetic samples at a target rate, so both ends can be exercised on
    Linux before anyone touches the hardware.

    Paced against a deadline rather than by sleeping a fixed interval, so the
    average rate holds even though each sleep overshoots. `rate=0` means free
    running: generate as fast as the loop allows, which is the useful setting
    for finding where the transport starts dropping.
    """

    def __init__(self, rate=1000.0):
        self.period = (1.0 / rate) if rate > 0 else 0.0
        self._next = None
        self._t0 = None

    def open(self):
        self._t0 = time.perf_counter()
        self._next = self._t0
        rate = "free-running" if self.period == 0 else f"{1.0 / self.period:.0f} Hz"
        print(f"simulated source: {rate}")

    def read(self):
        import math

        if self.period:
            now = time.perf_counter()
            if now < self._next:
                time.sleep(self._next - now)
            self._next += self.period
            # Re-anchor if we have fallen far behind, rather than trying to
            # claw back a backlog we will never make up.
            if self._next < now - 1.0:
                self._next = now
        t = time.perf_counter() - self._t0
        return (
            1000.0 * math.sin(t),
            1000.0 * math.cos(t),
            100.0 * math.sin(7.0 * t),
        )

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Publish loop
# ---------------------------------------------------------------------------

def run(source, port, report_interval):
    # Before the device is opened, so a SIGTERM during startup still unwinds
    # through the finally block below.
    shutdown.install()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.PUB)
    sock.setsockopt(zmq.SNDHWM, config.SND_HWM)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind(config.bind_endpoint(port))
    print(f"PUB bound on {config.bind_endpoint(port)} "
          f"(SNDHWM={config.SND_HWM})")

    source.open()

    seq = 0
    dropped = 0
    last_report = time.perf_counter()
    seq_at_report = 0
    dropped_at_report = 0

    try:
        while True:
            ch0, ch1, ch2 = source.read()
            try:
                sock.send_multipart(
                    [config.TOPIC, pack_sample(seq, ch0, ch1, ch2)],
                    zmq.NOBLOCK,
                )
            except zmq.Again:
                # Send queue full. Counted, never fatal: dropping the sample
                # is the correct trade here, because stalling the poll loop
                # would corrupt the rate measurement this tool exists for.
                dropped += 1
            seq += 1

            # ---------------------------------------------------------------
            # EXTENSION POINT - history for gap recovery.
            #
            # A deque(maxlen=N) of recent (seq, ch0, ch1, ch2) tuples goes
            # here, appended on every iteration, and published periodically
            # under a second topic (config.TOPIC_HISTORY) so a subscriber can
            # backfill the gaps it detects. Deliberately not implemented yet:
            # the loss numbers this script produces are what should size N.
            # Nothing above needs to change to add it.
            # ---------------------------------------------------------------

            now = time.perf_counter()
            elapsed = now - last_report
            if elapsed >= report_interval:
                sent = seq - seq_at_report
                drops = dropped - dropped_at_report
                print(
                    f"[pub] seq={seq} "
                    f"rate={sent / elapsed:,.0f}/s "
                    f"dropped={drops} (total {dropped})"
                )
                last_report = now
                seq_at_report = seq
                dropped_at_report = dropped
    except KeyboardInterrupt:
        print()
    finally:
        print(f"[pub] final: {seq} samples produced, {dropped} dropped at the socket")
        source.close()
        sock.close()
        ctx.term()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--simulate", action="store_true",
                   help="generate synthetic samples instead of reading USB")
    p.add_argument("--rate", type=float, default=1000.0,
                   help="simulated samples per second; 0 = free-running "
                        "(default: 1000, ignored without --simulate)")
    p.add_argument("--device", type=int, default=0,
                   help="quDIS device index to publish (default: 0)")
    p.add_argument("--port", type=int, default=config.QDS_PORT,
                   help=f"TCP port to bind (default: {config.QDS_PORT})")
    p.add_argument("--report-interval", type=float,
                   default=config.REPORT_INTERVAL_S,
                   help="seconds between rate reports "
                        f"(default: {config.REPORT_INTERVAL_S})")
    args = p.parse_args(argv)

    source = (SimulatedSource(args.rate) if args.simulate
              else HardwareSource(args.device))
    run(source, args.port, args.report_interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
