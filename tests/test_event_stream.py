"""Events reach every connected client, and a slow one cannot drown.

The second property is what makes a shared server usable: a panel stuck in
a modal dialog must not accumulate a queue of stale position snapshots that
it renders one by one when it comes back.
"""

import time

import pytest

pytestmark = pytest.mark.timeout(90)


def _collect(client, pattern="motion.positions"):
    got = []
    client.on(pattern, lambda name, data: got.append(data))
    return got


def test_position_events_arrive_without_being_asked_for(beamline_server):
    with beamline_server.client(kind="gui", gui_type="sample_alignment") as c:
        got = _collect(c)
        time.sleep(1.2)
        assert got, "no position events in 1.2s"
        assert "X" in got[-1]["positions"]


def test_every_client_sees_the_same_stream(beamline_server):
    """A second panel can watch what the first one is doing."""
    a = beamline_server.client(kind="gui", gui_type="sample_alignment")
    b = beamline_server.client(kind="gui", gui_type="optics_motors")
    try:
        ga, gb = _collect(a), _collect(b)
        time.sleep(1.2)
        assert ga and gb
        # Both see roughly the same number: one publisher, one fan-out.
        assert abs(len(ga) - len(gb)) <= 2
    finally:
        a.close(); b.close()


def test_the_publisher_runs_at_about_the_configured_rate(beamline_server):
    from ptychosaxs.link import config
    with beamline_server.client(kind="gui", gui_type="sample_alignment") as c:
        got = _collect(c)
        time.sleep(2.0)
        # Generous bounds: this asserts "roughly 10 Hz, not 1 Hz and not a
        # flood", not a precise cadence.
        assert config.PUBLISH_HZ * 0.4 <= len(got) / 2.0 <= config.PUBLISH_HZ * 1.6


def test_clients_changed_is_announced(beamline_server):
    a = beamline_server.client(kind="gui", gui_type="sample_alignment")
    try:
        seen = []
        a.on("clients.changed", lambda n, d: seen.append(d))
        b = beamline_server.client(kind="gui", gui_type="optics_motors")
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        b.close()
        assert seen, "connecting a second client announced nothing"
    finally:
        a.close()


def test_event_sequence_numbers_do_not_go_backwards(beamline_server):
    """A gap tells a client its own queue dropped; a repeat would hide it."""
    import socket
    from ptychosaxs.link import config, wire
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(beamline_server.socket_path)
    try:
        wire.send(s, wire.request("h", "system.hello", {
            "protocol": config.PROTOCOL_VERSION, "kind": "gui",
            "gui_type": "rungui", "label": "seq probe"}))
        seqs = []
        reader = iter(wire.FrameReader(s))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len(seqs) < 8:
            frame = next(reader)
            if frame.get("t") == "evt":
                seqs.append(frame["seq"])
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
    finally:
        s.close()
