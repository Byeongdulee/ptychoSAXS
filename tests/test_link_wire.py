"""The on-the-wire format.

These are the tests the old UDP protocol could not have: it had no framing
to get wrong, because one datagram was one message and an oversized one
simply vanished.
"""

import socket

import pytest

from ptychosaxs.link import config, wire
from ptychosaxs.link.errors import ProtocolError


def test_a_request_round_trips():
    frame = wire.request("c-1", "motion.mvr", {"axis": "X", "delta": 0.01})
    assert wire.decode(wire.encode(frame).strip()) == frame


def test_every_frame_ends_with_exactly_one_newline():
    for frame in (wire.request("1", "system.ping"),
                  wire.ok("1", {"a": 1}),
                  wire.error("1", "ERR_BUSY", "no"),
                  wire.event(7, "motion.positions", {}),
                  wire.ask("p1", "confirm", "T", "txt", ["Yes"], "Yes"),
                  wire.answer("p1", "Yes")):
        raw = wire.encode(frame)
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1


def test_content_cannot_break_framing():
    """A newline inside a string must be escaped, not emitted literally.

    If it were not, a scan name with a line break in it would split one
    frame into two and desynchronise the stream for good.
    """
    frame = wire.request("c-1", "system.status",
                         {"note": "line one\nline two\r\n"})
    raw = wire.encode(frame)
    assert raw.count(b"\n") == 1
    assert wire.decode(raw.strip())["params"]["note"] == "line one\nline two\r\n"


def test_non_ascii_survives():
    frame = wire.ok("1", {"msg": "µm ± 0.5 °"})
    assert wire.decode(wire.encode(frame).strip())["result"]["msg"] == "µm ± 0.5 °"


def test_an_oversized_frame_is_refused_rather_than_sent():
    frame = wire.ok("1", {"blob": "x" * (config.MAX_FRAME_BYTES + 10)})
    with pytest.raises(ProtocolError, match="over the"):
        wire.encode(frame)


@pytest.mark.parametrize("bad", [b"not json", b"[1,2,3]", b'"a string"',
                                 b"123", b"{}"])
def test_unusable_frames_raise(bad):
    with pytest.raises(ProtocolError):
        wire.decode(bad)


def test_an_unknown_frame_type_is_refused():
    with pytest.raises(ProtocolError, match="unknown frame type"):
        wire.decode(b'{"v":1,"t":"wat"}')


def _pair():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return a, b


def test_reader_splits_frames_that_arrive_coalesced():
    a, b = _pair()
    with a, b:
        a.sendall(wire.encode(wire.request("1", "system.ping"))
                  + wire.encode(wire.request("2", "system.status")))
        a.shutdown(socket.SHUT_WR)
        got = list(wire.FrameReader(b))
    assert [f["id"] for f in got] == ["1", "2"]


def test_reader_rejoins_a_frame_split_across_packets():
    a, b = _pair()
    with a, b:
        raw = wire.encode(wire.request("1", "motion.position", {"axis": "X"}))
        a.sendall(raw[:10])
        a.sendall(raw[10:])
        a.shutdown(socket.SHUT_WR)
        got = list(wire.FrameReader(b))
    assert got[0]["params"]["axis"] == "X"


def test_a_clean_close_ends_iteration():
    a, b = _pair()
    with a, b:
        a.shutdown(socket.SHUT_WR)
        assert list(wire.FrameReader(b)) == []


def test_a_truncated_frame_is_an_error_not_a_silent_drop():
    """A peer dying mid-write must not turn a half-sent command into a no-op."""
    a, b = _pair()
    with a, b:
        a.sendall(b'{"v":1,"t":"req","id":"1","method":"mo')
        a.shutdown(socket.SHUT_WR)
        with pytest.raises(ProtocolError, match="mid-frame"):
            list(wire.FrameReader(b))


def test_a_peer_that_never_terminates_a_frame_cannot_exhaust_memory():
    a, b = _pair()
    with a, b:
        reader = wire.FrameReader(b, max_bytes=4096)
        a.sendall(b"x" * 9000)
        with pytest.raises(ProtocolError, match="no newline"):
            list(reader)
