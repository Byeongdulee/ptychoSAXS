"""
pytest suite for motions_ver2's per-axis dispatch.

motors is the layer that turns an axis name into a controller call, and it
used to special-case the hexapod at every turn because the hexapod's
interface was the odd one out. Now that all three rigs present the same
shape, the dispatch is uniform - these tests pin the shapes the scan engine
unpacks, and the one piece of real logic left: choosing between a
coordinated move and two sequential ones.

In scope:
  - mv_xy picking mv_many for a same-controller pair and sequential moves
    for a mixed one.
  - The return shapes scan_handler destructures.

Out of scope:
  - Hardware, Channel Access and Qt event loops. Every controller here is
    simulated: the debug flags are set before motions_ver2 is imported, so
    it never imports pihexapod or connects to the ACS controller.
"""
import os

# Must precede the import below: motions_ver2 reads these at module scope and
# connects to the ACS controller at import time when they are not set.
os.environ["PTYCHOSAXS_DEBUG_HEXAPOD"] = "1"
os.environ["PTYCHOSAXS_DEBUG_ACS"] = "1"
os.environ["PTYCHOSAXS_DEBUG_GONIO"] = "1"

import pytest

from ptychosaxs.debug.debug_stubs import FakeMotorPV
from ptychosaxs import motions_ver2


@pytest.fixture
def pts():
    FakeMotorPV.reset()
    return motions_ver2.motors()


# ---------------------------------------------------------------------------
# Axis table
# ---------------------------------------------------------------------------

def test_every_axis_is_listed_once_with_its_controller(pts):
    assert pts.motornames[:6] == ["X", "Y", "Z", "U", "V", "W"]
    assert pts.controller[:6] == ["hexapod"] * 6
    assert pts.motornames[6] == "phi"
    assert pts.controller[6] == "phi"
    assert set(pts.controller[7:]) == {"gonio"}


def test_connected_reports_reachability_not_servo_state(pts):
    """Seeding this from is_servo_on() meant an axis with its servo off
    vanished from the motor panel instead of showing as idle."""
    assert pts.connected[:6] == [True] * 6


def test_controller_for_resolves_the_owning_object(pts):
    assert pts.controller_for("X") is pts.hexapod
    assert pts.controller_for("trans1") is pts.gonio
    assert pts.controller_for("phi") is pts.phi


# ---------------------------------------------------------------------------
# Return shapes the scan engine unpacks
# ---------------------------------------------------------------------------

def test_get_pos_returns_a_rounded_float_for_every_controller(pts):
    for axis in ("X", "trans1", "phi"):
        assert isinstance(pts.get_pos(axis), float)


def test_get_speed_returns_two_elements(pts):
    """scan_handler does `vel, acc = pts.get_speed(axis)`."""
    for axis in ("X", "trans1"):
        vel, acc = pts.get_speed(axis)
        assert isinstance(vel, float)
        assert acc is None


def test_set_speed_takes_the_same_three_arguments_everywhere(pts):
    """motor_handler calls pts.set_speed(axis, 5, None) for the hexapod and
    pts.set_speed(name) for the gonio; the dispatch no longer special-cases
    either. Only the hexapod's round trip is asserted here - the gonio is
    _GonioDebug in this configuration, whose set_speed is a no-op."""
    pts.set_speed("X", 5, None)
    assert pts.get_speed("X")[0] == pytest.approx(5)
    pts.set_speed("trans1", 2.5, None)  # must be accepted, not raise
    assert pts.get_speed("trans1")[1] is None


# ---------------------------------------------------------------------------
# mv_xy - the seam that makes the scan axes swappable
# ---------------------------------------------------------------------------

def test_mv_xy_uses_one_coordinated_move_for_a_same_controller_pair(pts):
    calls = []
    pts.hexapod.mv_many = lambda targets, wait=True: calls.append(list(targets)) or True
    assert pts.mv_xy("X", 1.0, "Z", 2.0) is True
    assert calls == [[("X", 1.0), ("Z", 2.0)]]


def test_mv_xy_passes_the_scan_timeout_to_the_coordinated_move(pts):
    """The scan engine bounds a coordinated move at 10 s so one stuck point
    is skipped rather than stalling the run for the controller's 300 s
    default."""
    seen = {}

    def _mv_many(targets, wait=True, timeout=None):
        seen["timeout"] = timeout
        return True

    pts.hexapod.mv_many = _mv_many
    pts.mv_xy("X", 1.0, "Z", 2.0, timeout=10)
    assert seen["timeout"] == 10


def test_mv_xy_moves_sequentially_across_controllers(pts):
    coordinated = []
    pts.hexapod.mv_many = lambda targets, wait=True: coordinated.append(targets) or True
    pts.gonio.mv_many = lambda targets, wait=True: coordinated.append(targets) or True
    assert pts.mv_xy("X", 1.0, "trans1", 2.0) is True
    assert coordinated == []
    assert pts.get_pos("X") == pytest.approx(1.0)
    assert pts.get_pos("trans1") == pytest.approx(2.0)


def test_can_coordinate_matches_what_mv_xy_will_do(pts):
    """The scan engine asks this before choosing between one coordinated
    move and two independently-handled ones, so it has to agree with mv_xy."""
    assert pts.can_coordinate("X", "Z") is True
    assert pts.can_coordinate("X", "trans1") is False
    assert pts.can_coordinate("phi", "phi") is False  # no mv_many on phi


def test_mv_xy_falls_back_when_the_controller_cannot_coordinate(pts):
    """phi has no mv_many, so a pair on it must not try one."""
    assert pts.mv_xy("phi", 1.0, "phi", 2.0) is True


def test_mv_xy_reaches_the_hexapod_deferred_move(pts):
    """End to end through the real controller: the coordinated path has to
    arrive at the IOC's deferred-moves handshake, not at two plain puts."""
    from ptychosaxs.epics_hexapod import DEFER_OFF, DEFER_ON, DEFER_PV

    FakeMotorPV.reset()
    assert pts.mv_xy("X", 1.0, "Z", 2.0) is True
    assert FakeMotorPV.history(DEFER_PV) == [DEFER_ON, DEFER_OFF]


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

def test_connect_rebuilds_the_connected_list(pts):
    """It used to append to self.isconnected - a method - so it raised
    AttributeError on its first iteration and left self.connected empty."""
    pts.connected = []
    result = pts.connect()
    assert len(result) == len(pts.motornames)
    assert pts.connected == result
    assert pts.isconnected("X") is True
