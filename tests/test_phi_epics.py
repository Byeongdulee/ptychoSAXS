"""
pytest suite for the tomography phi stage's offline EPICS contract.

In scope:
  - PhiAxisController driven against FakeMotorPV: motion, the speed pair,
    position redefinition and connection reporting. The fake sits at the PV
    boundary, so these exercise the production controller logic rather than
    a parallel stub that could drift from it.
  - The single-axis call surface the rest of the GUI reaches phi through -
    a scalar get_pos(), mv(target, relative), isconnected() returning a
    bool, a two-element get_speed - which is the shape motions_ver2's phi
    branches require and which differs from every other stage here.

Out of scope (explicitly NOT tested here):
  - Any real Channel Access traffic, IOC, or pyepics behavior - no test
    here constructs an epics.PV.
  - acspy, the other phi driver, and any Qt widget behavior.
"""
import math

import pytest

from debug_stubs import FakeMotorPV
from epics_phi import (
    MOTOR_SLOTS,
    SET_MODE_USE,
    PhiAxisController,
)

_PHI = "12idePtycho:m1"


@pytest.fixture
def phi():
    """A connected controller backed entirely by in-memory fake records."""
    FakeMotorPV.reset()
    controller = PhiAxisController(pv_class=FakeMotorPV)
    controller.connect()
    return controller


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_record_is_the_tomo_rotation():
    assert MOTOR_SLOTS == [("phi", _PHI, "deg")]


def test_name_and_unit_lists_are_what_motions_reads(phi):
    assert phi.motornames == ["phi"]
    assert phi.motorunits == ["deg"]
    assert phi.units == phi.motorunits


def test_the_unit_comes_from_the_record(phi):
    assert phi.get_unit("phi") == "deg"


# ---------------------------------------------------------------------------
# Motion - single axis, so mv() takes no axis name
# ---------------------------------------------------------------------------

def test_starts_at_zero(phi):
    assert phi.get_pos() == 0.0


def test_mv_is_absolute_by_default(phi):
    phi.mv(90.0)
    assert phi.get_pos() == 90.0


def test_mv_relative_accumulates(phi):
    phi.mv(90.0)
    phi.mv(10.0, relative=True)
    assert phi.get_pos() == pytest.approx(100.0)


def test_mvr_is_relative(phi):
    phi.mv(5.0)
    phi.mvr(-2.5)
    assert phi.get_pos() == pytest.approx(2.5)


def test_get_pos_ignores_a_slot_index(phi):
    """motions_ver2 calls it bare, but older call sites pass a 0."""
    phi.mv(1.0)
    assert phi.get_pos(0) == phi.get_pos()


def test_a_move_reads_as_moving_before_dmov_can_drop(phi):
    """The fake record is never busy, so this is the grace window alone -
    without it the GUI's move loop would exit before the stage turned."""
    phi.mv(90.0)
    assert phi.ismoving() is True


def test_an_idle_axis_is_not_moving(phi):
    assert phi.ismoving() is False


def test_stop_ends_the_grace_window_and_leaves_the_axis_ready(phi):
    phi.mv(90.0)
    phi.stop()
    assert phi.ismoving() is False
    assert FakeMotorPV(_PHI + ".SPMG").get() == 3
    phi.mv(10.0)
    assert phi.get_pos() == 10.0


# ---------------------------------------------------------------------------
# Speed - the record stores a ramp time, the GUI wants an acceleration
# ---------------------------------------------------------------------------

def test_set_speed_writes_velocity_and_the_matching_ramp_time(phi):
    phi.set_speed("phi", 36.0, 360.0)
    assert FakeMotorPV(_PHI + ".VELO").get() == pytest.approx(36.0)
    assert FakeMotorPV(_PHI + ".ACCL").get() == pytest.approx(0.1)


def test_get_speed_returns_the_pair_back(phi):
    phi.set_speed("phi", 36.0, 360.0)
    vel, acc = phi.get_speed("phi")
    assert vel == pytest.approx(36.0)
    assert acc == pytest.approx(360.0)


def test_set_speed_without_an_acceleration_leaves_the_ramp_alone(phi):
    phi.set_speed("phi", 36.0, 360.0)
    phi.set_speed("phi", 18.0, None)
    assert FakeMotorPV(_PHI + ".ACCL").get() == pytest.approx(0.1)


def test_a_zero_ramp_time_reports_no_acceleration_rather_than_dividing(phi):
    FakeMotorPV(_PHI + ".ACCL").put(0.0)
    assert phi.get_speed()[1] is None


# ---------------------------------------------------------------------------
# Position redefinition
# ---------------------------------------------------------------------------

def test_set_pos_redefines_without_moving(phi):
    phi.mv(90.0)
    assert phi.set_pos("phi", 0.0) == 0.0
    assert phi.get_pos() == 0.0


def test_set_mode_is_restored_so_later_moves_still_move(phi):
    phi.set_pos("phi", 0.0)
    assert FakeMotorPV(_PHI + ".SET").get() == SET_MODE_USE
    phi.mv(45.0)
    assert phi.get_pos() == 45.0


# ---------------------------------------------------------------------------
# Connection reporting
# ---------------------------------------------------------------------------

def test_isconnected_is_a_bool_not_a_list(phi):
    """motions_ver2 appends this straight into its per-motor list."""
    assert phi.isconnected() is True


def test_an_unreachable_record_reports_disconnected_and_nan(phi, monkeypatch):
    monkeypatch.setattr(FakeMotorPV, "connected", False)
    assert phi.isconnected() is False
    assert math.isnan(phi.get_pos())
    assert phi.get_speed() == (None, None)


def test_commutate_is_a_no_op_over_epics(phi):
    """Kept for callers written against the acspy phi."""
    assert phi.commutate() is None
