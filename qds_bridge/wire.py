"""
The one and only definition of what a sample looks like on the wire.

One sample is 40 bytes:

    offset  type      field
    0       uint64    seq     monotonically increasing, starts at 0
    8       float64   ch0     interferometer channel 0
    16      float64   ch1     interferometer channel 1
    24      float64   ch2     interferometer channel 2
    32      float64   t       publisher wall clock, Unix-epoch seconds

Little-endian and fixed-width, so the struct module can pack it in one call
with no allocation beyond the bytes object. This is on the acquisition hot
path at whatever rate the USB link sustains, which is why it is not JSON.

The legacy 32-byte form (the same fields without `t`) is still read, so an
updated subscriber keeps working against an old publisher. The reverse does
not hold: an old subscriber rejects every 40-byte frame as malformed. Update
the Linux side first, then the publisher on sec12mic.

UNITS: nanometres, exactly as the quDIS DLL reports them. The GUI divides by
1000 for micrometres (see gui/handlers/status_handler.py get_qds_pos). The
scaling is left to the consumer so the publisher ships what it measured.

`seq` is the join key for loss tracking. It identifies a sample independently
of any clock, so gap detection never depends on the two machines agreeing on
the time. uint64 at any achievable sample rate will not roll over in any
realistic run (100 kHz for ~5.8 million years), but the subscriber still
handles a decreasing seq, because a publisher restart looks exactly like one.

`t` is for matching samples against events stamped by some other clock --
EPICS timestamps in particular. It uses the same epoch as a PVA timeStamp
(secondsPastEpoch + nanoseconds * 1e-9), so the two compare directly, but only
as well as both machines' clocks are NTP-synced; any residual offset is the
consumer's to correct. float64 resolves ~0.2 us at present-day epoch values.
"""

import struct

_PACKER = struct.Struct("<Qdddd")
_LEGACY_PACKER = struct.Struct("<Qddd")

SAMPLE_BYTES = _PACKER.size  # 40
LEGACY_SAMPLE_BYTES = _LEGACY_PACKER.size  # 32
SEQ_MODULUS = 1 << 64


def pack_sample(seq, ch0, ch1, ch2, t=None):
    """Encode one sample. `seq` is masked into range rather than raising, so
    an overflowing counter degrades to a wrap the subscriber can see.

    With `t` omitted the legacy 32-byte form is written, which only tests and
    old tooling should want; the publisher always passes `t`."""
    if t is None:
        return _LEGACY_PACKER.pack(seq % SEQ_MODULUS, ch0, ch1, ch2)
    return _PACKER.pack(seq % SEQ_MODULUS, ch0, ch1, ch2, t)


def unpack_sample_ts(payload):
    """Decode one sample into (seq, ch0, ch1, ch2, t). `t` is None for a
    legacy 32-byte sample, which carries no timestamp.

    Raises struct.error on any other payload size; the subscriber reports
    that as a malformed frame rather than letting it corrupt the sequence
    tracking.
    """
    if len(payload) == LEGACY_SAMPLE_BYTES:
        return _LEGACY_PACKER.unpack(payload) + (None,)
    return _PACKER.unpack(payload)


def unpack_sample(payload):
    """Decode one sample into (seq, ch0, ch1, ch2), dropping any timestamp.

    Accepts both the current and the legacy payload size, so callers written
    before the timestamp existed are unaffected. Raises struct.error on any
    other size.
    """
    return unpack_sample_ts(payload)[:4]
