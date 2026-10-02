"""
pytest suite for the goniometer stages' offline EPICS contract.

In scope:
  - GonioAxisController driven against FakeMotorPV: motion, speed, position
    redefinition, multi-axis moves, and connection reporting. The fake sits
    at the PV boundary, so these exercise the production controller logic
    rather than a parallel stub that could drift from it.
  - The compatibility surface the rest of the GUI reaches these stages
    through (motornames/motorunits/channel_names/units, isconnected()
    returning a list, ismoving, a two-element get_speed), which
    motions_ver2.py, rungui.py and scan_handler.py all depend on.

Out of scope (explicitly NOT tested here):
  - Any real Channel Access traffic, IOC, or pyepics behavior - no test here
    constructs an epics.PV, so the suite needs neither a network nor pyepics
    installed.
  - motions_ver2's own dispatch, and any Qt widget behavior.
"""
import math

import pytest

from debug_stubs import FakeMotorPV
from epics_gonio import (
    MOTOR_SLOTS,
    SET_MODE_SET,
    SET_MODE_USE,
    GonioAxisController,
)

_TRANS1 = "12ideMCS1:m1"
_TILT1 = "12ideMCS1:m3"


@pytest.fixture
def gonio():
    """A connected controller backed entirely by in-memory fake records."""
    FakeMotorPV.reset()
    controller = GonioAxisController(pv_class=FakeMotorPV)
    controller.connect()
    return controller


# ---------------------------------------------------------------------------
# Axis naming and units
# ---------------------------------------------------------------------------

def test_axis_names_and_order():
    assert [name for name, _prefix, _unit in MOTOR_SLOTS] == [
        "trans1", "trans2", "tilt1", "tilt2"
    ]


def test_record_prefixes_map_m1_through_m4():
    assert [prefix for _name, prefix, _unit in MOTOR_SLOTS] == [
        "12ideMCS1:m1", "12ideMCS1:m2", "12ideMCS1:m3", "12ideMCS1:m4"
    ]


def test_legacy_name_lists_are_aliases(gonio):
    """rungui reads channel_names/units; scan_handler reads motornames."""
    assert gonio.motornames == ["trans1", "trans2", "tilt1", "tilt2"]
    assert gonio.channel_names == gonio.motornames
    assert gonio.motorunits == ["mm", "mm", "deg", "deg"]
    assert gonio.units == gonio.motorunits


def test_units_come_from_the_records(gonio):
    assert gonio.get_unit("trans1") == "mm"
    assert gonio.get_unit("tilt1") == "deg"


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

def test_starts_at_zero(gonio):
    for name in gonio.motornames:
        assert gonio.get_pos(name) == 0.0


def test_mv_absolute(gonio):
    gonio.mv("trans1", 1.5)
    assert gonio.get_pos("trans1") == 1.5
    gonio.mv("tilt2", -3.0)
    assert gonio.get_pos("tilt2") == -3.0


def test_mvr_relative_accumulates(gonio):
    gonio.mv("trans2", 1.0)
    gonio.mvr("trans2", 0.25)
    gonio.mvr("trans2", -0.1)
    assert gonio.get_pos("trans2") == pytest.approx(1.15)


def test_axes_are_independent(gonio):
    gonio.mv("trans1", 1.0)
    assert gonio.get_pos("trans2") == 0.0


def test_ismoving_false_when_done(gonio):
    assert gonio.ismoving("trans1") is False


def test_stop_leaves_axis_ready_to_move(gonio):
    gonio.stop("trans1")
    assert FakeMotorPV(_TRANS1 + ".SPMG").get() == 3
    gonio.mv("trans1", 1.0)
    assert gonio.get_pos("trans1") == 1.0


# ---------------------------------------------------------------------------
# Coordinated two-axis move (the piezo scan path)
# ---------------------------------------------------------------------------

def test_mv_many_moves_both_axes(gonio):
    assert gonio.mv_many([("trans1", 1.0), ("trans2", -2.0)]) is True
    assert gonio.get_pos("trans1") == 1.0
    assert gonio.get_pos("trans2") == -2.0


def test_mv_many_without_wait_still_commands_the_moves(gonio):
    gonio.mv_many([("trans1", 0.5), ("trans2", 0.25)], wait=False)
    assert gonio.get_pos("trans1") == 0.5
    assert gonio.get_pos("trans2") == 0.25


def test_mv_many_reports_timeout_rather_than_raising(gonio):
    """A scan tolerates a late move and carries on, so this returns False
    instead of propagating out of the worker thread."""

    class _NeverCompletes:
        def put(self, *args, **kwargs):
            return 1  # accepted, but the completion callback never fires

    gonio._pvs[("trans1", "drive")] = _NeverCompletes()
    assert gonio.mv_many([("trans1", 1.0)], timeout=0.05) is False


def test_mv_many_does_not_inherit_a_previous_move_s_completion(gonio):
    """Completion must be judged per call. A move whose callback never
    arrives has to time out even though the same axis completed a move a
    moment earlier - otherwise a scan measures mid-travel."""
    assert gonio.mv_many([("trans1", 1.0)]) is True

    class _NeverCompletes:
        put_complete = True  # left over from the move above

        def put(self, *args, **kwargs):
            return 1

    gonio._pvs[("trans1", "drive")] = _NeverCompletes()
    assert gonio.mv_many([("trans1", 2.0)], timeout=0.05) is False


def test_mv_many_fails_when_a_put_is_dropped(gonio):
    """A disconnected record silently drops the put, so reporting success
    would have the scan measure at the previous point's position."""
    for (name, field), pv in gonio._pvs.items():
        if name == "trans2" and field == "drive":
            pv.connected = False
    assert gonio.mv_many([("trans1", 1.0), ("trans2", 2.0)]) is False


# ---------------------------------------------------------------------------
# Position redefinition (.SET), used by the "Set to 0" context menu
# ---------------------------------------------------------------------------

def test_set_pos_redefines_position(gonio):
    gonio.mv("trans1", 4.0)
    assert gonio.set_pos("trans1", 0.0) == 0.0
    assert gonio.get_pos("trans1") == 0.0


def test_set_pos_restores_use_mode(gonio):
    """If .SET stuck at 1, every later move would silently redefine the
    position instead of moving the stage."""
    gonio.set_pos("trans1", 0.0)
    assert FakeMotorPV(_TRANS1 + ".SET").get() == 0


def test_set_pos_restores_use_mode_even_if_the_write_fails(gonio):
    class _Failing:
        def put(self, *args, **kwargs):
            raise RuntimeError("write rejected")

    gonio._pvs[("trans1", "drive")] = _Failing()
    with pytest.raises(RuntimeError):
        gonio.set_pos("trans1", 0.0)
    assert FakeMotorPV(_TRANS1 + ".SET").get() == 0


def test_set_pos_restores_use_mode_if_entering_set_mode_fails(gonio):
    """A .SET write that reaches the record and then errors must still be
    undone, or the axis is left silently redefining every later move."""
    real = gonio._pvs[("trans1", "set_mode")]

    class _FailsOnSet:
        def __init__(self):
            self.writes = []

        def put(self, val, *args, **kwargs):
            self.writes.append(val)
            real.put(val, *args, **kwargs)
            if val == SET_MODE_SET:
                raise RuntimeError("write rejected")

    probe = _FailsOnSet()
    gonio._pvs[("trans1", "set_mode")] = probe
    with pytest.raises(RuntimeError):
        gonio.set_pos("trans1", 0.0)
    assert probe.writes == [SET_MODE_SET, SET_MODE_USE]
    assert FakeMotorPV(_TRANS1 + ".SET").get() == SET_MODE_USE


@pytest.mark.parametrize("suffix", sorted(GonioAxisController.FIELDS.values()))
def test_fake_motor_pv_splits_every_gonio_field_suffix(suffix):
    """This rig adds .SET to the base field map; an unrecognised suffix would
    be treated as part of the record name and route the write elsewhere."""
    FakeMotorPV.reset()
    pv = FakeMotorPV(_TRANS1 + suffix)
    assert (pv._prefix, pv._field) == (_TRANS1, suffix)


# ---------------------------------------------------------------------------
# Speed - the two-element shape motions_ver2/scan_handler unpack
# ---------------------------------------------------------------------------

def test_get_speed_returns_velocity_and_none(gonio):
    """scan_handler does `vel, acc = pts.get_speed(axis)`; this IOC has no
    acceleration field, which motions_ver2 already reports as None for the
    hexapod."""
    assert gonio.get_speed("trans1") == (1.0, None)


def test_set_speed_roundtrip(gonio):
    gonio.set_speed("trans1", 2.5)
    assert FakeMotorPV(_TRANS1 + "_vCh.A").get() == pytest.approx(2.5)
    assert gonio.get_speed("trans1") == (pytest.approx(2.5), None)


def test_set_speed_accepts_and_ignores_acceleration(gonio):
    """Existing call sites pass three arguments."""
    gonio.set_speed("trans1", 2.5, 25.0)
    assert gonio.get_speed("trans1") == (pytest.approx(2.5), None)


def test_set_speed_defaults_to_the_rig_default(gonio):
    gonio.set_speed("trans1", 3.0)
    gonio.set_speed("trans1")
    assert gonio.get_speed("trans1") == (1.0, None)


# ---------------------------------------------------------------------------
# Connection reporting - motions_ver2 indexes the list this returns
# ---------------------------------------------------------------------------

def test_isconnected_returns_a_list_by_default(gonio):
    assert gonio.isconnected() == [True, True, True, True]


def test_isconnected_with_an_index_returns_a_bool(gonio):
    assert gonio.isconnected(0) is True


def test_disconnected_axis_is_reported_everywhere(gonio):
    for (name, _field), pv in gonio._pvs.items():
        if name == "tilt1":
            pv.connected = False
    assert gonio.isconnected() == [True, True, False, True]
    assert gonio.isconnected(2) is False
    assert gonio.is_connected() is False
    assert gonio.disconnected_axes() == ["tilt1"]
    assert gonio.get_pos("trans1") == 0.0  # other axes unaffected


def test_disconnected_axis_reads_nan_not_none(gonio):
    """motions_ver2 rounds this value and the GUI formats it with %f, so a
    None here would raise rather than show the axis as unavailable."""
    for (name, _field), pv in gonio._pvs.items():
        if name == "tilt1":
            pv.connected = False
    pos = gonio.get_pos("tilt1")
    assert isinstance(pos, float) and math.isnan(pos)
    assert round(pos, 6) is not None  # the call motions_ver2.get_pos makes


def test_disconnected_axis_falls_back_to_configured_unit(gonio):
    for (name, _field), pv in gonio._pvs.items():
        if name == "tilt1":
            pv.connected = False
    assert gonio.get_unit("tilt1") == "deg"


# ---------------------------------------------------------------------------
# Integer axis indices - ptychosaxs.scan() addresses axes by slot number
# ---------------------------------------------------------------------------

def test_axes_are_addressable_by_index(gonio):
    gonio.mv(0, 1.25)
    assert gonio.get_pos(0) == 1.25
    assert gonio.get_pos("trans1") == 1.25
    assert gonio.ismoving(0) is False
    assert gonio.get_unit(2) == "deg"
    assert gonio.is_axis_connected(0) is True


def test_index_and_name_reach_the_same_record(gonio):
    gonio.mv(2, 0.75)
    assert FakeMotorPV(_TILT1 + ".RBV").get() == pytest.approx(0.75)
