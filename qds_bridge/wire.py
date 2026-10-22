"""
The one and only definition of what a sample looks like on the wire.

One sample is 32 bytes:

    offset  type      field
    0       uint64    seq     monotonically increasing, starts at 0
    8       float64   ch0     interferometer channel 0
    16      float64   ch1     interferometer channel 1
    24      float64   ch2     interferometer channel 2

Little-endian and fixed-width, so the struct module can pack it in one call
with no allocation beyond the bytes object. This is on the acquisition hot
path at whatever rate the USB link sustains, which is why it is not JSON.

UNITS: nanometres, exactly as the quDIS DLL reports them. The GUI divides by
1000 for micrometres (see gui/handlers/status_handler.py get_qds_pos). The
scaling is left to the consumer so the publisher ships what it measured.

`seq` is the join key. It identifies a sample independently of any clock, so
the two machines never have to agree on the time -- which is the whole reason
it exists. uint64 at any achievable sample rate will not roll over in any
realistic run (100 kHz for ~5.8 million years), but the subscriber still
handles a decreasing seq, because a publisher restart looks exactly like one.
"""

import struct

_FORMAT = "<Qddd"
_PACKER = struct.Struct(_FORMAT)

SAMPLE_BYTES = _PACKER.size  # 32
SEQ_MODULUS = 1 << 64


def pack_sample(seq, ch0, ch1, ch2):
    """Encode one sample. `seq` is masked into range rather than raising, so
    an overflowing counter degrades to a wrap the subscriber can see."""
    return _PACKER.pack(seq % SEQ_MODULUS, ch0, ch1, ch2)


def unpack_sample(payload):
    """Decode one sample into (seq, ch0, ch1, ch2).

    Raises struct.error on a short or oversized payload; the subscriber
    reports that as a malformed frame rather than letting it corrupt the
    sequence tracking.
    """
    return _PACKER.unpack(payload)
