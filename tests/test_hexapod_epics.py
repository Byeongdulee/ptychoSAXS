"""
pytest suite for the hexapod's offline EPICS contract.

In scope:
  - HexapodAxisController driven against FakeMotorPV: motion, coordinated
    moves through the IOC's deferred-moves flag, servo handling, speed and
    connection reporting. The fake sits at the PV boundary, so these
    exercise the production controller logic rather than a parallel stub
    that could drift from it.
  - The compatibility surface the rest of the GUI reaches the hexapod
    through, which scan_handler.py and motions_ver2.py depend on.

Out of scope (explicitly NOT tested here):
  - Any real Channel Access traffic, IOC, or pyepics behavior - no test here
    constructs an epics.PV.
  - The trajectory/fly-scan transport, which is GCS and not EPICS at all.
    See test_hexapod_traj.py.
  - Whether the IOC's records behave as documented. The driver source says
    .CNEN reaches GCS "SVO" and that releasing DEFER coalesces the captured
    .VAL writes into one MOV; that is a hardware claim and only hardware can
    confirm it.
"""
import math

import pytest

from ptychosaxs.debug.debug_stubs import FakeMotorPV
from ptychosaxs.epics_hexapod import (
    ASYN_CNCT_PV,
    DEFER_OFF,
    DEFER_ON,
    DEFER_PV,
    MOTOR_SLOTS,
    HexapodAxisController,
)

_X = "12idbHXP:m1"
_Y = "12idbHXP:m2"
_Z = "12idbHXP:m3"
_U = "12idbHXP:m4"


@pytest.fixture
def hexapod():
    """A connected controller backed entirely by in-memory fake records."""
    FakeMotorPV.reset()
    controller = HexapodAxisController(pv_class=FakeMotorPV)
    controller.connect()
    return controller


# ---------------------------------------------------------------------------
# Axis naming and units
# ---------------------------------------------------------------------------

def test_slot_order_is_x_y_z_u_v_w():
    assert [name for name, _p, _u in MOTOR_SLOTS] == ["X", "Y", "Z", "U", "V", "W"]


def test_slots_map_to_m1_through_m6():
    assert [prefix for _n, prefix, _u in MOTOR_SLOTS] == [
        f"12idbHXP:m{i}" for i in range(1, 7)
    ]


def test_three_linear_axes_then_three_rotary():
    assert [unit for _n, _p, unit in MOTOR_SLOTS] == [
        "mm", "mm", "mm", "deg", "deg", "deg",
    ]


def test_units_come_back_from_the_records(hexapod):
    assert hexapod.motorunits == ["mm", "mm", "mm", "deg", "deg", "deg"]


def test_legacy_name_aliases_are_the_same_list(hexapod):
    """rungui reads channel_names/units; scan_handler reads motornames/axes."""
    assert hexapod.channel_names == hexapod.motornames == hexapod.axes
    assert hexapod.units == hexapod.motorunits


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

def test_axes_start_at_zero(hexapod):
    assert hexapod.get_pos("X") == 0.0


def test_absolute_move(hexapod):
    hexapod.mv_many([("X", 1.25)])
    assert hexapod.get_pos("X") == pytest.approx(1.25)


def test_axes_move_independently(hexapod):
    hexapod.mv_many([("X", 1.0)])
    assert hexapod.get_pos("Z") == 0.0


def test_a_disconnected_axis_reads_nan_not_none(hexapod):
    """motions_ver2 rounds this value and the GUI formats it with %f, so a
    None here would raise."""
    for (name, _field), pv in hexapod._pvs.items():
        if name == "U":
            pv.connected = False
    assert math.isnan(hexapod.get_pos("U"))


def test_axes_are_addressable_by_slot_number(hexapod):
    """ptychosaxs.scan() addresses axes by index."""
    hexapod.mv_many([(2, 0.5)])
    assert hexapod.get_pos("Z") == pytest.approx(0.5)


def test_set_pos_does_not_redefine_anything(hexapod):
    """A hexapod position is a pose in a coordinate system, so the motor
    record's .SET redefinition would desynchronise it from the controller's
    kinematics. The call is accepted and does nothing."""
    hexapod.mv_many([("X", 1.0)])
    hexapod.set_pos("X", 0.0)
    assert hexapod.get_pos("X") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Coordinated moves - the deferred-moves handshake
# ---------------------------------------------------------------------------

def test_coordinated_move_sets_and_releases_defer_around_the_puts(hexapod):
    """DEFER must go on before the targets and off after them: that gap is
    what the driver coalesces into a single MOV."""
    hexapod.move_coordinated([("X", 1.0), ("Z", 2.0)])
    written = [name for name, _value in FakeMotorPV.history()]
    assert written == [DEFER_PV, _X + ".VAL", _Z + ".VAL", DEFER_PV]
    assert FakeMotorPV.history(DEFER_PV) == [DEFER_ON, DEFER_OFF]


def test_coordinated_move_moves_both_axes(hexapod):
    assert hexapod.move_coordinated([("X", 1.0), ("Z", 2.0)]) is True
    assert hexapod.get_pos("X") == pytest.approx(1.0)
    assert hexapod.get_pos("Z") == pytest.approx(2.0)


def test_defer_is_released_even_when_a_put_raises(hexapod):
    """A DEFER left on silently swallows every later move, from every EPICS
    client - not just this one."""
    class _Raises:
        connected = True

        def put(self, *args, **kwargs):
            raise RuntimeError("record refused the write")

    hexapod._pvs[("X", "drive")] = _Raises()
    with pytest.raises(RuntimeError):
        hexapod.move_coordinated([("X", 1.0), ("Z", 2.0)])
    assert FakeMotorPV.history(DEFER_PV) == [DEFER_ON, DEFER_OFF]


def test_coordinated_move_falls_back_when_there_is_no_defer_record(hexapod):
    """An IOC without the record still has to move both axes."""
    hexapod._defer.connected = False
    assert hexapod.move_coordinated([("X", 1.0), ("Z", 2.0)]) is True
    assert hexapod.get_pos("X") == pytest.approx(1.0)
    assert hexapod.get_pos("Z") == pytest.approx(2.0)
    assert FakeMotorPV.history(DEFER_PV) == []


def test_coordinated_move_reports_a_dropped_put(hexapod):
    """A put on a disconnected record never goes out. Reporting success
    would let a scan measure at a position the stage never reached."""
    hexapod._pvs[("Z", "drive")].connected = False
    assert hexapod.move_coordinated([("X", 1.0), ("Z", 2.0)]) is False


def test_coordinated_move_times_out_rather_than_raising(hexapod):
    class _NeverCompletes:
        connected = True

        def put(self, *args, **kwargs):
            return 1  # accepted, but the completion callback never fires

    hexapod._pvs[("X", "drive")] = _NeverCompletes()
    assert hexapod.move_coordinated([("X", 1.0)], timeout=0.05) is False


def test_coordinated_move_does_not_inherit_a_previous_move_s_completion(hexapod):
    """Completion is judged per call. A move whose callback never arrives has
    to time out even though the same axis completed one a moment earlier -
    otherwise a scan measures mid-travel."""
    assert hexapod.move_coordinated([("X", 1.0)]) is True

    class _NeverCompletes:
        connected = True
        put_complete = True  # left over from the move above

        def put(self, *args, **kwargs):
            return 1

    hexapod._pvs[("X", "drive")] = _NeverCompletes()
    assert hexapod.move_coordinated([("X", 2.0)], timeout=0.05) is False


def test_a_single_axis_move_does_not_use_the_deferred_path(hexapod):
    """One axis is not a coordinated move, and the driver skips its
    velocity/acceleration block on the deferred branch - so taking that path
    for one axis would silently drop a .VELO change."""
    hexapod.move_coordinated([("X", 1.0)])
    assert FakeMotorPV.history(DEFER_PV) == []
    assert hexapod.get_pos("X") == pytest.approx(1.0)


def test_disconnect_before_connect_does_not_raise():
    """rungui can tear down a controller whose connect() never completed."""
    FakeMotorPV.reset()
    HexapodAxisController(pv_class=FakeMotorPV).disconnect()


def test_mv_many_takes_the_coordinated_path_on_this_rig(hexapod):
    hexapod.mv_many([("X", 1.0), ("Z", 2.0)])
    assert FakeMotorPV.history(DEFER_PV) == [DEFER_ON, DEFER_OFF]


# ---------------------------------------------------------------------------
# Servo
# ---------------------------------------------------------------------------

def test_servo_reads_on_by_default(hexapod):
    assert hexapod.is_servo_on() is True
    assert hexapod.is_servo_on("X") is True


def test_servo_off_on_one_axis_reports_off_overall(hexapod):
    FakeMotorPV(_Y + ".CNEN").put(0)
    assert hexapod.is_servo_on("Y") is False
    assert hexapod.is_servo_on() is False
    assert hexapod.is_servo_on("X") is True


def test_is_servo_on_fails_open_when_the_record_is_unreadable(hexapod):
    """"Cannot tell" must not read as "off": rungui checks this at startup
    and would otherwise try to recover from a fault on every launch."""
    hexapod._pvs[("X", "servo")].connected = False
    assert hexapod.is_servo_on("X") is True


def test_is_servo_on_fails_open_on_an_invalid_reading(hexapod):
    """A record whose device support could not reach the controller keeps
    its last value and goes INVALID. Reading 0 off it and calling that
    "servo off" is how six .CNEN records got written to a C-887 that was
    not even connected."""
    pv = hexapod._pvs[("X", "servo")]
    pv.put(0)
    pv.severity = 3  # INVALID
    assert hexapod.is_servo_on("X") is True
    pv.severity = 0
    assert hexapod.is_servo_on("X") is False


def test_handle_error_enables_the_servo_on_every_axis(hexapod):
    for _name, prefix, _unit in MOTOR_SLOTS:
        FakeMotorPV(prefix + ".CNEN").put(0)
    assert hexapod.handle_error() is True
    assert all(
        FakeMotorPV(prefix + ".CNEN").get() == 1 for _n, prefix, _u in MOTOR_SLOTS
    )


def test_handle_error_does_nothing_when_the_controller_is_unreachable(hexapod):
    """Writing .CNEN then changes six records and commands nothing - it
    looks like a successful recovery until the link returns and the driver
    pushes the stale request through."""
    for _name, prefix, _unit in MOTOR_SLOTS:
        FakeMotorPV(prefix + ".CNEN").put(0)
    FakeMotorPV(ASYN_CNCT_PV).put(0)
    assert hexapod.handle_error() is False
    assert all(
        FakeMotorPV(prefix + ".CNEN").get() == 0 for _n, prefix, _u in MOTOR_SLOTS
    )


def test_handle_error_never_initiates_a_referencing_move(hexapod):
    """The GCS library's handle_error() calls FRF, which drives all six legs
    unattended. Nothing here may do that."""
    hexapod.handle_error()
    written = {name for name, _value in FakeMotorPV.history()}
    forbidden = (".HOMF", ".HOMR", ".HVEL", ":FRF", ".HOMR")
    assert not [pv for pv in written if pv.endswith(forbidden)]


# ---------------------------------------------------------------------------
# Stop, speed, connection
# ---------------------------------------------------------------------------

def test_stop_leaves_the_axis_movable(hexapod):
    hexapod.stop("X")
    assert FakeMotorPV(_X + ".SPMG").get() == 3
    hexapod.mv_many([("X", 1.0)])
    assert hexapod.get_pos("X") == pytest.approx(1.0)


def test_get_speed_returns_velocity_and_none(hexapod):
    """scan_handler does `vel, acc = pts.get_speed(axis)`."""
    assert hexapod.get_speed("X") == (1.0, None)


def test_get_speed_without_an_axis_reports_the_platform(hexapod):
    """The driver maps .VELO to the C-887's VLS, which is one speed for the
    whole platform, so the call is meaningful with no axis."""
    assert hexapod.get_speed() == (1.0, None)


def test_set_speed_writes_every_axis(hexapod):
    """VLS applies to all six, so leaving five readbacks on the old value
    would just be misleading."""
    hexapod.set_speed(None, 2.5)
    for _name, prefix, _unit in MOTOR_SLOTS:
        assert FakeMotorPV(prefix + ".VELO").get() == pytest.approx(2.5)
    assert hexapod.get_speed() == (pytest.approx(2.5), None)


def test_set_speed_accepts_and_ignores_acceleration(hexapod):
    hexapod.set_speed("X", 2.5, 25.0)
    assert hexapod.get_speed() == (pytest.approx(2.5), None)


def test_isconnected_shapes(hexapod):
    assert hexapod.isconnected() == [True] * 6
    assert hexapod.isconnected(0) is True


def test_connected_reports_reachability_not_servo_state(hexapod):
    """An axis whose servo is off is still a real axis and belongs in the
    motor panel; seeding this from the servo made startup fail."""
    for _name, prefix, _unit in MOTOR_SLOTS:
        FakeMotorPV(prefix + ".CNEN").put(0)
    assert hexapod.connected == [True] * 6


def test_connectivity_ignores_the_controller_link(hexapod):
    """Deliberate: rungui treats a disconnected motor as fatal, so folding
    the link in here would stop the GUI starting whenever the C-887 is
    unplugged. ioc_link_ok() reports that separately."""
    FakeMotorPV(ASYN_CNCT_PV).put(0)
    assert hexapod.isconnected() == [True] * 6
    assert hexapod.ioc_link_ok() is False


def test_the_field_set_stays_small(hexapod):
    """connect() builds a PV per field per axis and is_axis_connected()
    requires every one of them, so an extra field is another way for all
    six axes to be reported dead. PI_Support.db's :SERVO and the motor
    record's .MSTA were both tried and removed for that reason."""
    assert ":SERVO" not in HexapodAxisController.FIELDS.values()
    assert ".MSTA" not in HexapodAxisController.FIELDS.values()


def test_ioc_link_reports_the_controller_socket(hexapod):
    assert hexapod.ioc_link_ok() is True
    FakeMotorPV(ASYN_CNCT_PV).put(0)
    assert hexapod.ioc_link_ok() is False


def test_asyn_passthrough_is_refused_when_the_buffer_is_too_small(hexapod):
    """IMAX bounds one read, and IFMT=Hybrid returns one line per read, so
    what has to fit is the longest single reply line. Below that even a
    POS? comes back clipped, and the transport must not be offered."""
    FakeMotorPV("12idbHXP:asyn_1.IMAX").put(128)
    ok, why = hexapod.asyn_passthrough_status()
    assert ok is False
    assert "IMAX" in why


def test_asyn_passthrough_is_accepted_at_the_ioc_s_actual_buffer_size(hexapod):
    """256 is what 12idbHXP boots with. It carries every command a fly scan
    sends (longest measured reply line: 130 B for POS?, 124 B for a DRR?
    row), so the transport is offered; get_records() is what refuses."""
    FakeMotorPV("12idbHXP:asyn_1.IMAX").put(256)
    ok, why = hexapod.asyn_passthrough_status()
    assert ok is True, why


def test_asyn_passthrough_is_refused_when_the_controller_is_unreachable(hexapod):
    FakeMotorPV("12idbHXP:asyn_1.IMAX").put(16384)
    FakeMotorPV(ASYN_CNCT_PV).put(0)
    ok, why = hexapod.asyn_passthrough_status()
    assert ok is False
    assert "Disconnect" in why


def test_asyn_passthrough_accepted_when_both_hold(hexapod):
    FakeMotorPV("12idbHXP:asyn_1.IMAX").put(16384)
    assert hexapod.asyn_passthrough_status() == (True, "")


@pytest.mark.parametrize("suffix", sorted(set(HexapodAxisController.FIELDS.values())))
def test_fake_pv_splits_every_field_this_rig_uses(suffix):
    """An unrecognised suffix would be treated as part of the record name and
    route the write to a different record."""
    prefix, field = FakeMotorPV._split(_X + suffix)
    assert (prefix, field) == (_X, suffix)
