"""The qds_bridge sample format, with and without the publisher timestamp.

The publisher on sec12mic and the subscribers on Linux run separate copies of
wire.py, so the reader has to keep accepting the legacy 32-byte sample, and
callers of unpack_sample written before the timestamp existed must still get
exactly four values back.
"""
import struct

import pytest

from qds_bridge import wire


def test_timestamped_round_trip():
    payload = wire.pack_sample(7, 1.5, -2.25, 3.0, 1793728349.415187)
    assert len(payload) == wire.SAMPLE_BYTES == 40
    seq, ch0, ch1, ch2, t = wire.unpack_sample_ts(payload)
    assert (seq, ch0, ch1, ch2) == (7, 1.5, -2.25, 3.0)
    assert t == pytest.approx(1793728349.415187, abs=1e-6)


def test_legacy_round_trip_has_no_timestamp():
    payload = wire.pack_sample(7, 1.5, -2.25, 3.0)
    assert len(payload) == wire.LEGACY_SAMPLE_BYTES == 32
    assert wire.unpack_sample_ts(payload) == (7, 1.5, -2.25, 3.0, None)


@pytest.mark.parametrize("t", [None, 1793728349.0])
def test_unpack_sample_still_returns_four_values(t):
    payload = wire.pack_sample(3, 1.0, 2.0, 3.0, t)
    assert wire.unpack_sample(payload) == (3, 1.0, 2.0, 3.0)


def test_seq_wraps_rather_than_raising():
    payload = wire.pack_sample(wire.SEQ_MODULUS + 5, 0.0, 0.0, 0.0, 1.0)
    assert wire.unpack_sample(payload)[0] == 5


@pytest.mark.parametrize("size", [0, 31, 33, 39, 41, 64])
def test_wrong_size_is_malformed(size):
    with pytest.raises(struct.error):
        wire.unpack_sample_ts(b"\0" * size)
    with pytest.raises(struct.error):
        wire.unpack_sample(b"\0" * size)
