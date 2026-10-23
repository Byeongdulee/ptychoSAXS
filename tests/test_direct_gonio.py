"""
pytest suite for the SmarAct goniometer adapter.

The real ptychosaxs/smaract_gonio.py cannot be imported here - it needs the
SmarAct SDK and opens the MCS2 at import time - so these drive the adapter
against a fake module that reproduces the real one's signatures and
quirks. That is the point: this is code that only ever runs on the machine
wired to the crate, so the parts that can be pinned down off-hardware
should be.

The quirks being reproduced, all read off smaract_gonio.py:
  - mv/mvr/get_pos/set_pos/ismoving/stop/get_speed/set_speed take a name
    OR a channel index;
  - isconnected takes only an int - its body is `if ax > -1`, which raises
    against a string;
  - get_pos returns None when the SDK raises;
  - channel_names and units are filled in at import and are empty when the
    device did not open.
"""
import math

import pytest

from ptychosaxs.direct_gonio import GONIO_AXES, GONIO_UNITS, SmaractGonioController


class _FakeCtl:
    def __init__(self):
        self.closed = []

    def Close(self, handle):
        self.closed.append(handle)


class _FakeSmaract:
    """Stands in for ptychosaxs.smaract_gonio."""

    motornames = list(GONIO_AXES)
    motorunits = list(GONIO_UNITS)

    def __init__(self, channel_names=None, units=None, sensors=None,
                 pos_raises=False):
        self.ctl = _FakeCtl()
        self.smaract = "handle"
        self.channel_names = list(channel_names) if channel_names is not None else []
        self.units = list(units) if units is not None else []
        self._pos = [0.0] * 4
        self._speed = [(5.0, 10.0)] * 4
        self._sensors = list(sensors or [True] * 4)
        self._pos_raises = pos_raises
        self.calls = []

    # The real module resolves a name itself; an int must survive untouched.
    def _ch(self, ax):
        assert not isinstance(ax, str), "adapter must pass a channel index"
        return ax

    def mv(self, ax, target, wait=True):
        self.calls.append(("mv", self._ch(ax), target, wait))
        self._pos[self._ch(ax)] = float(target)

    def mvr(self, ax, target, wait=True):
        self.calls.append(("mvr", self._ch(ax), target, wait))
        self._pos[self._ch(ax)] += float(target)

    def get_pos(self, ax):
        if self._pos_raises:
            return None  # what the real one returns when ctl.Error is caught
        return self._pos[self._ch(ax)]

    def set_pos(self, ax, position=0):
        self._pos[self._ch(ax)] = float(position)
        return self._pos[self._ch(ax)]

    def ismoving(self, ax):
        return False

    def stop(self, ax):
        self.calls.append(("stop", self._ch(ax)))

    def get_speed(self, channel):
        return self._speed[self._ch(channel)]

    def set_speed(self, channel, vel=5, acc=10):
        self._speed[self._ch(channel)] = (vel, acc)

    def isconnected(self, ax=-1):
        # Int only, exactly like the real one.
        if isinstance(ax, str):
            raise TypeError("'>' not supported between 'str' and 'int'")
        if ax > -1:
            return self._sensors[ax]
        return list(self._sensors)


@pytest.fixture
def gonio():
    return SmaractGonioController(module=_FakeSmaract())


# ---------------------------------------------------------------------------
# Addressing - the bug this adapter exists to not have
# ---------------------------------------------------------------------------

def test_a_name_never_reaches_isconnected(gonio):
    """isconnected is `if ax > -1`, so a name would raise TypeError. Every
    public method here must resolve to a channel first."""
    assert gonio.isconnected("tilt1") is True
    assert gonio.is_axis_connected("tilt1") is True


def test_isconnected_shapes(gonio):
    assert gonio.isconnected() == [True] * 4
    assert gonio.isconnected(2) is True


def test_a_missing_sensor_is_reported_per_axis():
    g = SmaractGonioController(module=_FakeSmaract(sensors=[True, False, True, True]))
    assert g.isconnected() == [True, False, True, True]
    assert g.isconnected("trans2") is False
    assert g.connected == [True, False, True, True]


@pytest.mark.parametrize("axis,channel", list(zip(GONIO_AXES, range(4))))
def test_names_and_indices_address_the_same_channel(gonio, axis, channel):
    gonio.mv(axis, 1.5)
    assert gonio.get_pos(channel) == pytest.approx(1.5)
    assert gonio.get_pos(axis) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# The gaps the adapter closes
# ---------------------------------------------------------------------------

def test_an_unreadable_position_is_nan_not_none():
    """motors.get_pos rounds whatever comes back, and round(None) raises.
    Every other controller here answers nan."""
    g = SmaractGonioController(module=_FakeSmaract(pos_raises=True))
    assert math.isnan(g.get_pos("trans1"))


def test_disconnect_closes_the_device():
    module = _FakeSmaract()
    SmaractGonioController(module=module).disconnect()
    assert module.ctl.closed == ["handle"]


def test_connect_is_a_no_op_because_the_module_opens_at_import(gonio):
    gonio.connect()


def test_channel_names_fall_back_when_the_device_did_not_open(gonio):
    """They are built from the hardware at import and left empty on
    failure; callers index them against motornames, so an empty list would
    silently misalign."""
    assert gonio.channel_names == GONIO_AXES
    assert gonio.units == GONIO_UNITS


def test_channel_names_come_from_the_hardware_when_it_did_open():
    module = _FakeSmaract(channel_names=["a", "b", "c", "d"], units=["mm"] * 4)
    g = SmaractGonioController(module=module)
    assert g.channel_names == ["a", "b", "c", "d"]
    assert g.units == ["mm"] * 4


# ---------------------------------------------------------------------------
# The surface motions_ver2 dispatches onto
# ---------------------------------------------------------------------------

def test_speed_round_trips_as_a_two_element_tuple(gonio):
    """scan_handler does `vel, acc = pts.get_speed(axis)`. This rig really
    does report acceleration, unlike the EPICS one."""
    assert gonio.get_speed("trans1") == (5.0, 10.0)
    gonio.set_speed("trans1", 2.0, 4.0)
    assert gonio.get_speed("trans1") == (2.0, 4.0)


def test_relative_move_and_tweak(gonio):
    gonio.mv("trans1", 1.0)
    gonio.mvr("trans1", 0.25)
    assert gonio.get_pos("trans1") == pytest.approx(1.25)
    gonio.tweak("trans1", 0.25, forward=False)
    assert gonio.get_pos("trans1") == pytest.approx(1.0)


def test_names_units_and_moving(gonio):
    assert gonio.motornames == GONIO_AXES
    assert gonio.motorunits == GONIO_UNITS
    assert gonio.ismoving("trans1") is False


def test_it_offers_no_coordinated_move(gonio):
    """The MCS2 has no coordinated primitive here, so motors.can_coordinate
    must report False and a 2-D scan must move the axes one at a time."""
    assert not hasattr(gonio, "mv_many")
