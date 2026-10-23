"""The optics control surface, and the shared record mirror behind it.

These run against the API directly -- no socket, no Qt -- because that is
the layer the panel now talks to and the layer a future MCP tool would.
"""

import logging

import pytest

from ptychosaxs import optics_config
from ptychosaxs.api.optics import OpticsService, open_controllers
from ptychosaxs.api.profile import Profile
from ptychosaxs.api.pvs import PVCache

LOG = logging.getLogger("test")


class _Devices:
    """Stands in for DeviceRegistry: the service only asks it for a group."""

    def __init__(self, controllers):
        self._controllers = controllers

    def get(self, name):
        return self._controllers if name == "optics" else None


@pytest.fixture
def optics():
    controllers = open_controllers(Profile(simulated=True), LOG)
    return OpticsService(_Devices(controllers), None, LOG)


# -- reads -------------------------------------------------------------------

def test_every_configured_motor_is_offered(optics):
    assert optics.motors() == list(optics_config.MOTORS)


def test_positions_covers_every_motor(optics):
    assert set(optics.positions()) == set(optics_config.MOTORS)


def test_a_motor_that_cannot_be_read_reports_none_rather_than_vanishing(optics):
    """A panel needs to show the row as unavailable, which it cannot do if
    the key is simply missing."""
    optics.controllers.pop("OSA")
    positions = optics.positions()
    assert set(positions) == set(optics_config.MOTORS)
    assert positions["OSA_X"] is None
    assert positions["ZP_ver"] is not None


# -- moves -------------------------------------------------------------------

def test_an_absolute_move_lands_where_it_was_sent(optics):
    assert optics.move("ZP_ver", 1.25) == pytest.approx(1.25)
    assert optics.position("ZP_ver") == pytest.approx(1.25)


def test_a_relative_move_is_relative(optics):
    optics.move("ZP_ver", 1.0)
    assert optics.move_rel("ZP_ver", 0.25) == pytest.approx(1.25)


def test_moving_an_unknown_motor_raises_rather_than_moving_something_else(optics):
    with pytest.raises(KeyError):
        optics.move("NOT_A_MOTOR", 1.0)


def test_a_move_with_the_group_closed_says_so(optics):
    optics.controllers.clear()
    with pytest.raises(RuntimeError, match="not open"):
        optics.move("ZP_ver", 1.0)


# -- stop --------------------------------------------------------------------

def test_stop_with_no_motor_stops_all_of_them(optics):
    assert sorted(optics.stop()["stopped"]) == sorted(optics_config.MOTORS)


def test_stop_keeps_going_when_one_axis_refuses(optics):
    """One unreachable axis must not leave the others running."""
    class Refuses:
        motornames = optics_config.GROUPS["OSA"]

        def stop(self, axis):
            raise RuntimeError("no route to host")

    optics.controllers["OSA"] = Refuses()
    result = optics.stop()
    assert set(result["failed"]) == set(optics_config.GROUPS["OSA"])
    assert "ZP_ver" in result["stopped"]


def test_stopping_one_motor_stops_only_that_one(optics):
    assert optics.stop("ZP_ver")["stopped"] == ["ZP_ver"]


# -- the record mirror -------------------------------------------------------

@pytest.fixture
def pvs():
    return PVCache(Profile(simulated=True), LOG)


def test_a_subscription_reports_current_values(pvs):
    assert pvs.subscribe("c1", ["a", "b"]) == {"a": None, "b": None}


def test_a_write_is_visible_to_every_subscriber(pvs):
    """Two panels bound to one record is the case this exists for."""
    pvs.subscribe("c1", ["12idc:m10.TWV"])
    pvs.subscribe("c2", ["12idc:m10.TWV"])
    pvs.put("12idc:m10.TWV", 0.05)
    assert pvs.snapshot()["12idc:m10.TWV"] == 0.05


def test_a_record_stays_open_while_anyone_still_wants_it(pvs):
    pvs.subscribe("c1", ["x"])
    pvs.subscribe("c2", ["x"])
    pvs.unsubscribe("c1")
    assert pvs.subscribed() == ["x"]
    pvs.unsubscribe("c2")
    assert pvs.subscribed() == []


def test_dropping_a_connection_drops_only_its_records(pvs):
    pvs.subscribe("c1", ["x", "y"])
    pvs.subscribe("c2", ["y"])
    pvs.unsubscribe("c1")
    assert pvs.subscribed() == ["y"]


def test_an_unreadable_record_reports_none_rather_than_raising(pvs):
    class Exploding:
        def __init__(self, name):
            raise RuntimeError("no such record")

    cache = PVCache(Profile(simulated=False), LOG, pv_class=Exploding)
    cache.subscribe("c1", ["bad"])
    assert cache.snapshot() == {"bad": None}


# -- the publisher must never block ------------------------------------------

class _BlockingPV:
    """A record whose IOC is down.

    pyepics behaves like this: the channel never connects, and get() blocks
    for the full timeout every time rather than failing fast.
    """

    BLOCK_S = 1.0

    def __init__(self, pvname):
        self.pvname = pvname
        self.connected = False

    def get(self, timeout=None):
        import time as _t
        _t.sleep(timeout if timeout is not None else self.BLOCK_S)
        return None

    def put(self, value):
        raise AssertionError("must not write to a disconnected record")


def test_an_unreachable_record_does_not_stall_the_snapshot():
    """The regression this exists for.

    Two unreachable X-ray-eye camera records were enough to make every
    publisher tick take about four seconds, which showed up at the optics
    panel as roughly a second of lag between pressing a dpad button and the
    readback moving -- because one thread publishes to every client, so
    anything that blocks it delays everyone's readout.
    """
    import time

    cache = PVCache(Profile(simulated=False), LOG, pv_class=_BlockingPV)
    names = [f"DEAD:record{i}" for i in range(10)]
    cache.subscribe("c1", names)

    start = time.perf_counter()
    values = cache.snapshot()
    elapsed = time.perf_counter() - start

    assert values == {n: None for n in names}
    assert elapsed < 0.2, (
        f"snapshot of 10 dead records took {elapsed*1000:.0f} ms; it must "
        f"not block the publisher")


def test_a_read_of_a_live_record_is_bounded():
    """Even a connected record gets an explicit timeout, so a wedged IOC
    that accepts the connection and then stops answering cannot stall the
    publisher either."""
    from ptychosaxs.api import pvs as pvs_mod

    class SlowButConnected(_BlockingPV):
        def __init__(self, pvname):
            super().__init__(pvname)
            self.connected = True

    import time
    cache = PVCache(Profile(simulated=False), LOG, pv_class=SlowButConnected)
    cache.subscribe("c1", ["SLOW:record"])
    start = time.perf_counter()
    cache.snapshot()
    elapsed = time.perf_counter() - start
    assert elapsed < pvs_mod.READ_TIMEOUT_S * 3, (
        f"a slow record took {elapsed*1000:.0f} ms despite the "
        f"{pvs_mod.READ_TIMEOUT_S}s read timeout")


def test_writing_to_a_disconnected_record_is_refused_not_dropped():
    """pyepics drops a put to a disconnected PV silently, which makes a
    bound box look like it was written when it was not."""
    cache = PVCache(Profile(simulated=False), LOG, pv_class=_BlockingPV)
    cache.subscribe("c1", ["DEAD:record"])
    with pytest.raises(RuntimeError, match="not connected"):
        cache.put("DEAD:record", 1.0)
