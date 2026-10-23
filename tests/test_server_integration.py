"""The shared server, end to end, against a real daemon subprocess.

What these cover is the whole point of the refactor: one process owns the
hardware, several panels share it, the same panel cannot be opened twice,
and the last one out turns the lights off.
"""

import time

import pytest

from ptychosaxs.link import errors

pytestmark = pytest.mark.timeout(90)


def test_a_client_can_connect_and_the_server_reports_itself(beamline_server):
    with beamline_server.client(label="probe") as c:
        assert c.ready
        assert c.simulated, "the test daemon must not touch real hardware"
        assert c.hello["server_pid"] > 0


def test_two_different_guis_share_one_server(beamline_server):
    a = beamline_server.client(kind="gui", gui_type="sample_alignment")
    b = beamline_server.client(kind="gui", gui_type="optics_motors")
    try:
        assert a.hello["server_pid"] == b.hello["server_pid"]
        labels = {c["gui_type"] for c in b.system.clients()}
        assert labels == {"sample_alignment", "optics_motors"}
    finally:
        a.close(); b.close()


def test_the_same_gui_cannot_be_opened_twice(beamline_server):
    a = beamline_server.client(kind="gui", gui_type="sample_alignment")
    try:
        with pytest.raises(errors.DuplicateGuiError) as exc:
            beamline_server.client(kind="gui", gui_type="sample_alignment")
        assert exc.value.pid, "the refusal must name the holder's pid"
    finally:
        a.close()


def test_closing_a_gui_frees_its_slot(beamline_server):
    a = beamline_server.client(kind="gui", gui_type="sample_alignment")
    a.close()
    time.sleep(0.4)
    b = beamline_server.client(kind="gui", gui_type="sample_alignment")
    b.close()


def test_a_refused_launch_can_raise_the_existing_window(beamline_server):
    """The second process asks, the first one's window comes forward."""
    held = beamline_server.client(kind="gui", gui_type="sample_alignment")
    raised = []
    held.on("client.focus_requested", lambda n, d: raised.append(d))
    relauncher = beamline_server.client(kind="cli", label="relaunch")
    try:
        assert relauncher.system.request_focus(
            "sample_alignment")["raised"] is True
        deadline = time.monotonic() + 5
        while not raised and time.monotonic() < deadline:
            time.sleep(0.05)
        assert raised == [{"gui_type": "sample_alignment"}]
    finally:
        relauncher.close(); held.close()


def test_asking_to_focus_a_gui_that_is_not_open_says_so(beamline_server):
    with beamline_server.client() as c:
        assert c.system.request_focus("optics_motors")["raised"] is False


def test_device_groups_open_only_for_a_client_that_needs_them(beamline_server):
    """A CLI client must not cause the hexapod to be opened."""
    with beamline_server.client(kind="cli") as c:
        assert [g for g in c.system.status()["groups"] if g["open"]] == []

    with beamline_server.client(kind="gui", gui_type="sample_alignment") as c:
        open_now = {g["name"] for g in c.system.status()["groups"] if g["open"]}
        assert "motion" in open_now


def test_device_groups_close_when_the_last_client_needing_them_leaves(
        beamline_server):
    # Two *different* gui types: one type may only be open once, and the
    # point here is two clients sharing a single device group.
    a = beamline_server.client(kind="gui", gui_type="sample_alignment")
    b = beamline_server.client(kind="gui", gui_type="sample_alignment2")
    try:
        a.close()
        time.sleep(0.5)
        # b still needs motion, so it must stay open
        open_now = {g["name"] for g in b.system.status()["groups"] if g["open"]}
        assert "motion" in open_now
    finally:
        b.close()


def test_motion_is_readable_once_a_client_has_asked_for_it(beamline_server):
    with beamline_server.client(kind="gui", gui_type="sample_alignment") as c:
        names = [a["name"] for a in c.motion.axes()]
        assert {"X", "Y", "Z"} <= set(names)
        assert isinstance(c.motion.position("X"), float)
        assert "X" in c.motion.positions()


def test_motion_is_refused_when_no_client_has_opened_it(beamline_server):
    """Honest refusal rather than a confusing empty answer."""
    with beamline_server.client(kind="cli") as c:
        with pytest.raises(errors.ServerError) as exc:
            c.motion.axes()
        assert exc.value.code == errors.ERR_NOT_READY


def test_an_unknown_method_is_refused_by_name(beamline_server):
    with beamline_server.client() as c:
        with pytest.raises(errors.ServerError) as exc:
            c.call("motion.teleport", {})
        assert exc.value.code == errors.ERR_UNKNOWN_METHOD


def test_the_first_frame_must_be_a_handshake(beamline_server):
    """Otherwise a client could drive hardware without identifying itself."""
    import socket
    from ptychosaxs.link import wire
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(beamline_server.socket_path)
    try:
        wire.send(s, wire.request("x", "system.status"))
        reply = next(iter(wire.FrameReader(s)))
        assert reply["ok"] is False
        assert reply["error"]["code"] == errors.ERR_PROTOCOL
    finally:
        s.close()
