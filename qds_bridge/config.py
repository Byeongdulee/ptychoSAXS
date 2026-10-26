"""
Everything you are likely to want to change, in one place.

This module is deliberately plain constants with no imports. Both ends read
it, so the wire settings cannot drift apart between the Windows publisher and
the Linux subscriber -- as long as both machines are running the same copy.
"""

# The Windows box with the quDIS interferometer on USB. The publisher binds
# here and the subscriber connects to it; if the machine is renamed or you
# want to test against a different host, this is the only line to edit.
QDS_HOST = "sec12mic"
QDS_PORT = 5561

# Frame 0 of every message. A topic frame costs almost nothing and is how a
# second stream gets added later (see the extension note in qds_publisher.py)
# without the subscriber having to guess what it just received.
TOPIC = b"qds"

# Bounded queues, on purpose.
#
# A PUB socket that has hit its send HWM *drops* messages rather than blocking
# the caller. That is the property that keeps acquisition independent of the
# client: if the subscriber stalls, dies, or was never started, the poll loop
# keeps running at full rate and we lose samples instead of time. Raising
# SND_HWM buys tolerance for brief subscriber hiccups at the cost of delivering
# staler data after one.
SND_HWM = 10_000
RCV_HWM = 100_000

# How often each end prints a summary line, in seconds.
REPORT_INTERVAL_S = 5.0

# Target rate for polling the real quDIS hardware, in Hz. The device can be
# polled far faster than any consumer needs, and nothing downstream (the
# bridge client in ptychosaxs/, or a human watching the GUI) benefits from
# more than this. One line to change.
#
# This is separate from --simulate --rate, which stays uncapped: that flag
# exists to stress-test the transport at rates above what the real device
# would ever produce (see README), and this constant would defeat that if it
# applied there too.
HARDWARE_RATE_HZ = 100.0


def endpoint(host=QDS_HOST, port=QDS_PORT):
    """Connect-side address, e.g. "tcp://sec12mic:5561"."""
    return f"tcp://{host}:{port}"


def bind_endpoint(port=QDS_PORT):
    """Bind-side address. Binds on all interfaces, not just the one that
    resolves from QDS_HOST, so the publisher works regardless of which NIC
    the subscriber arrives on."""
    return f"tcp://*:{port}"
