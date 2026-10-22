"""
Linux side: subscribe to the interferometer stream and measure what is lost.

    python qds_subscriber.py
    python qds_subscriber.py --host 192.168.0.42 --quiet

Prints every gap in the sequence as it is noticed, plus a periodic summary of
received / expected / lost. That loss figure, taken at the real USB sample
rate, is what should size the history deque in the next round.

Loss here is end-to-end: it covers samples the publisher dropped at its own
socket (because this subscriber was too slow, or absent) as well as anything
the network lost. The publisher prints its own drop count, so comparing the
two tells you which end gave up.

This module imports nothing from the ptychoSAXS package. Its only dependency
is pyzmq.
"""

import argparse
import os
import struct
import sys
import time

import zmq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import shutdown  # noqa: E402
from wire import SEQ_MODULUS, unpack_sample  # noqa: E402


class SequenceTracker:
    """Counts what arrived, what is missing, and what was never sent.

    The first message after connecting sets the baseline and is not counted
    as loss: the publisher runs whether or not anyone is listening, so every
    sample before we arrived is expected to be absent. Counting those would
    swamp the figure we actually want.
    """

    def __init__(self):
        # Never reset, unlike `received`: rates have to stay meaningful
        # across a publisher restart, and a window counter going backwards
        # produced a negative rate.
        self.total_received = 0
        self.reset(None)
        self.restarts = 0
        self.malformed = 0

    def reset(self, seq):
        self.first_seq = seq
        self.last_seq = seq
        self.received = 0 if seq is None else 1
        self.missing = 0

    def observe(self, seq):
        """Record *seq*. Returns (lo, hi) of a detected gap, or None."""
        self.total_received += 1
        if self.first_seq is None:
            self.reset(seq)
            return None

        if seq == self.last_seq + 1:
            self.last_seq = seq
            self.received += 1
            return None

        if seq > self.last_seq:
            lo, hi = self.last_seq + 1, seq - 1
            self.missing += hi - lo + 1
            self.last_seq = seq
            self.received += 1
            return lo, hi

        # seq went backwards. Either the publisher restarted its counter or
        # the uint64 wrapped; both make the old baseline meaningless, so the
        # window restarts rather than reporting a gap of ~2**64.
        self.restarts += 1
        self.reset(seq)
        return None

    @property
    def expected(self):
        if self.first_seq is None:
            return 0
        span = (self.last_seq - self.first_seq) % SEQ_MODULUS
        return span + 1

    @property
    def lost(self):
        return self.expected - self.received

    @property
    def loss_fraction(self):
        return (self.lost / self.expected) if self.expected else 0.0


def run(endpoint, report_interval, quiet):
    # So `kill` on a backgrounded subscriber still prints the final summary,
    # which is the entire output of a measurement run.
    shutdown.install()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, config.RCV_HWM)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.SUBSCRIBE, config.TOPIC)
    # connect() does not require the publisher to be up, and reconnects on its
    # own if it goes away -- so a publisher restart needs nothing from us
    # beyond noticing the sequence went backwards.
    sock.connect(endpoint)
    print(f"SUB connected to {endpoint} (RCVHWM={config.RCV_HWM}), "
          f"waiting for data... Ctrl-C to stop")

    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)

    tracker = SequenceTracker()
    started = None
    last_report = time.perf_counter()
    total_at_report = 0
    lost_at_report = 0
    last_data = None

    try:
        while True:
            # Timeout rather than a blocking recv, so Ctrl-C lands promptly
            # and a silent feed is distinguishable from a blocked one.
            if not poller.poll(timeout=500):
                now = time.perf_counter()
                if last_data is not None and now - last_data >= report_interval:
                    print(f"[sub] no data for {now - last_data:.1f}s")
                    last_data = now
                continue

            while True:
                try:
                    frames = sock.recv_multipart(zmq.NOBLOCK)
                except zmq.Again:
                    break

                if len(frames) != 2:
                    tracker.malformed += 1
                    continue
                try:
                    seq, ch0, ch1, ch2 = unpack_sample(frames[1])
                except struct.error:
                    tracker.malformed += 1
                    continue

                first = tracker.first_seq is None
                gap = tracker.observe(seq)
                now = time.perf_counter()
                last_data = now
                if started is None:
                    started = now

                if first:
                    print(f"[sub] baseline seq={seq} "
                          f"({ch0:.1f}, {ch1:.1f}, {ch2:.1f}) nm")
                elif gap is not None:
                    lo, hi = gap
                    print(f"[sub] MISSING {lo}-{hi} ({hi - lo + 1})")
                elif not quiet:
                    print(f"[sub] seq={seq} "
                          f"({ch0:.1f}, {ch1:.1f}, {ch2:.1f}) nm")

            now = time.perf_counter()
            elapsed = now - last_report
            if elapsed >= report_interval:
                got = tracker.total_received - total_at_report
                # Clamped because `lost` is a window figure and a restart
                # zeroes the window; without this the line after a restart
                # reports a negative loss.
                lost = max(0, tracker.lost - lost_at_report)
                print(
                    f"[sub] received={tracker.received} "
                    f"expected={tracker.expected} "
                    f"lost={tracker.lost} "
                    f"({tracker.loss_fraction * 100:.3f}%) "
                    f"rate={got / elapsed:,.0f}/s "
                    f"lost-since-last={lost}"
                )
                last_report = now
                total_at_report = tracker.total_received
                lost_at_report = tracker.lost
    except KeyboardInterrupt:
        print()
    finally:
        _summary(tracker, started)
        sock.close()
        ctx.term()


def _summary(tracker, started):
    print("---- final ----")
    if tracker.first_seq is None:
        print("no samples received")
        return
    duration = (time.perf_counter() - started) if started else 0.0
    if tracker.restarts:
        # The loss figures below describe the latest window only, so say so
        # before quoting them rather than after.
        print(f"seq restarts  : {tracker.restarts} "
              f"(publisher restarted or counter wrapped; the loss figures "
              f"below cover only the window since the last restart)")
        print(f"total received: {tracker.total_received} (all windows)")
    print(f"seq range     : {tracker.first_seq} .. {tracker.last_seq}")
    print(f"expected      : {tracker.expected}")
    print(f"received      : {tracker.received}")
    print(f"lost          : {tracker.lost} "
          f"({tracker.loss_fraction * 100:.3f}%)")
    if duration > 0:
        # Averaged over everything received, so the number stays honest when
        # the window was restarted partway through.
        print(f"duration      : {duration:.1f}s "
              f"({tracker.total_received / duration:,.0f} received/s)")
    if tracker.malformed:
        print(f"malformed     : {tracker.malformed}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--host", default=config.QDS_HOST,
                   help=f"publisher hostname (default: {config.QDS_HOST})")
    p.add_argument("--port", type=int, default=config.QDS_PORT,
                   help=f"publisher port (default: {config.QDS_PORT})")
    p.add_argument("--quiet", action="store_true",
                   help="print only gaps and summaries, not every sample "
                        "(use this at any real sample rate)")
    p.add_argument("--report-interval", type=float,
                   default=config.REPORT_INTERVAL_S,
                   help="seconds between summary lines "
                        f"(default: {config.REPORT_INTERVAL_S})")
    args = p.parse_args(argv)

    run(config.endpoint(args.host, args.port), args.report_interval, args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
