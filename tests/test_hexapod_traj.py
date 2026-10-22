"""
pytest suite for the hexapod's fly-scan transport resolution.

Fly scans need the pihexapod library and a reachable C-887. Both can be
missing, and the point of hexapod_traj is that every way of missing them
produces a named state and a usable message instead of an AttributeError
from inside a running scan.

In scope:
  - make_trajectory_backend's five outcomes, each driven through an injected
    importer so the suite neither needs pihexapod installed nor cares
    whether it is.
  - That every member of the trajectory surface raises TrajectoryUnavailable
    in a degraded state, parametrised over the surface itself so the two
    cannot drift apart.
  - The facade's lazy resolution and its forwarding.

Out of scope:
  - Any real GCS traffic, socket, or wave generator.
  - pihexapod's own method signatures: the backend forwards *args/**kwargs
    untouched precisely so a mismatch surfaces as pihexapod's own TypeError
    rather than being reshaped by a guess made here.
"""
import pytest

from debug_stubs import FakeMotorPV
from hexapod import HexapodFacade
from hexapod_traj import (
    STATE_ABSENT,
    STATE_INCOMPATIBLE,
    STATE_PIHEXAPOD,
    STATE_SIMULATED,
    STATE_UNREACHABLE,
    TRAJ_ATTRS,
    TRAJ_METHODS,
    SimulatedTrajectory,
    TrajectoryUnavailable,
    UnavailableTrajectory,
    make_trajectory_backend,
)


class _CompleteHexapod:
    """A device with everything the scan engine calls."""

    def __init__(self, address):
        self.address = address
        self.calls = []
        self.pulse_number = 7
        self.pulse_step = 0.01
        self.pulse_positions_index = [0, 1, 2]
        self.scantime = 1.5


def _record(name):
    def method(self, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return name
    return method


for _name in TRAJ_METHODS:
    setattr(_CompleteHexapod, _name, _record(_name))
del _name


class _StaleHexapod:
    """What an out-of-date install looks like: it imports, but the 2-D snake
    entry point and several others simply are not there."""

    def __init__(self, address):
        self.address = address

    def set_traj(self, *a, **k):
        pass

    def run_traj(self, *a, **k):
        pass

    def stop_traj(self, *a, **k):
        pass

    def get_records(self, *a, **k):
        return []


class _RefusesToConnect(_CompleteHexapod):
    """Complete surface, but the controller is not there. Subclasses the
    complete one on purpose: an incomplete class would be caught by the
    surface check first and never reach the connection attempt."""

    def __init__(self, address):
        raise OSError(f"no route to {address}")


def _module(hexapod_cls, wave_gen_id=None, ip="10.0.0.1"):
    """A stand-in for pihexapod.gcs."""
    ns = type("gcs", (), {})
    ns.Hexapod = hexapod_cls
    ns.IP = ip
    if wave_gen_id is not None:
        ns.WaveGenID = wave_gen_id
    return lambda: ns


_WAVE_GEN_ID = {"X": 1, "Y": 2, "Z": 3, "U": 4, "V": 5, "W": 6}

_QUIET = lambda *args, **kwargs: None


def _make(hexapod_cls=_CompleteHexapod, wave_gen_id=_WAVE_GEN_ID, **kwargs):
    kwargs.setdefault("log", _QUIET)
    return make_trajectory_backend(
        "10.0.0.1", "12idbHXP", importer=_module(hexapod_cls, wave_gen_id), **kwargs
    )


# ---------------------------------------------------------------------------
# The five outcomes
# ---------------------------------------------------------------------------

def test_library_present_and_reachable():
    backend = _make()
    assert backend.state == STATE_PIHEXAPOD
    assert backend.available is True
    assert backend.transport == "direct"
    assert backend.address == "10.0.0.1"


def test_library_missing_is_reported_not_raised():
    def _raises():
        raise ModuleNotFoundError("No module named 'pihexapod'")

    backend = make_trajectory_backend(importer=_raises, log=_QUIET)
    assert backend.state == STATE_ABSENT
    assert backend.available is False
    assert "pihexapod" in backend.reason


def test_a_stale_install_is_caught_at_startup_not_mid_scan():
    """The whole value of this state: an install that imports cleanly but
    lacks set_traj_SNAKE2 would otherwise fail deep inside a scan, with the
    detector already armed."""
    backend = _make(_StaleHexapod)
    assert backend.state == STATE_INCOMPATIBLE
    assert backend.available is False
    for missing in ("set_traj_SNAKE2", "analyze_pulse_steps",
                    "assign_axis2wavtable", "goto_start_pos", "wait"):
        assert missing in backend.reason


def test_a_library_without_wavegenid_is_incompatible():
    """rungui defaults hexapod_flymode to the wavelet path, so even a 1-D
    fly needs WaveGenID."""
    backend = _make(wave_gen_id=None)
    assert backend.state == STATE_INCOMPATIBLE
    assert "WaveGenID" in backend.reason


def test_unreachable_controller_reports_every_attempt():
    backend = _make(_RefusesToConnect)
    assert backend.state == STATE_UNREACHABLE
    assert "direct (10.0.0.1)" in backend.reason
    assert "epics (12idbHXP:asyn_1)" in backend.reason


def test_simulated_never_touches_the_library():
    def _explode():
        raise AssertionError("the library must not be imported")

    backend = make_trajectory_backend(importer=_explode, prefer="simulated")
    assert backend.state == STATE_SIMULATED
    assert backend.available is True


# ---------------------------------------------------------------------------
# The EPICS passthrough fallback
# ---------------------------------------------------------------------------

def test_epics_fallback_is_used_when_the_direct_connection_fails():
    class _DirectOnlyFails(_CompleteHexapod):
        def __init__(self, address):
            if "." in address:
                raise OSError("pipython is not usable here")
            super().__init__(address)

    backend = _make(_DirectOnlyFails, epics_ready=lambda: (True, ""))
    assert backend.state == STATE_PIHEXAPOD
    assert backend.transport == "epics"
    assert backend.address == "12idbHXP"  # the base PV, not the IP


def test_epics_fallback_is_refused_when_the_passthrough_cannot_work():
    """Constructing the library's EPICS transport succeeds as long as the
    asyn record exists, which says nothing about the controller behind it.
    Without this gate a dead controller reports a working fly transport."""
    class _DirectOnlyFails(_CompleteHexapod):
        def __init__(self, address):
            if "." in address:
                raise OSError("pipython is not usable here")
            super().__init__(address)

    backend = _make(
        _DirectOnlyFails,
        epics_ready=lambda: (False, "the IOC's socket to the C-887 is down"),
    )
    assert backend.state == STATE_UNREACHABLE
    assert "socket to the C-887 is down" in backend.reason


def test_prefer_direct_never_tries_the_passthrough():
    backend = _make(_RefusesToConnect, prefer="direct")
    assert "epics" not in backend.reason


# ---------------------------------------------------------------------------
# The degraded surface - what protects the scan engine's call sites
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("member", TRAJ_METHODS)
def test_every_method_raises_when_there_is_no_transport(member):
    backend = UnavailableTrajectory(STATE_ABSENT, "pihexapod is not installed")
    with pytest.raises(TrajectoryUnavailable) as excinfo:
        getattr(backend, member)()
    assert member in str(excinfo.value)
    assert STATE_ABSENT in str(excinfo.value)


@pytest.mark.parametrize("member", TRAJ_ATTRS)
def test_every_attribute_raises_when_there_is_no_transport(member):
    backend = UnavailableTrajectory(STATE_ABSENT, "pihexapod is not installed")
    with pytest.raises(TrajectoryUnavailable) as excinfo:
        getattr(backend, member)
    assert member in str(excinfo.value)


def test_the_failure_is_not_an_attributeerror():
    """hasattr() would swallow an AttributeError and report False, which is
    how a missing transport used to reach a running scan unnoticed."""
    backend = UnavailableTrajectory(STATE_ABSENT, "nope")
    with pytest.raises(TrajectoryUnavailable):
        backend.run_traj("X")


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------

@pytest.fixture
def facade():
    FakeMotorPV.reset()
    return HexapodFacade(pv_class=FakeMotorPV, traj=SimulatedTrajectory())


def test_construction_does_not_resolve_the_transport():
    """A step-scan-only session must not open a second connection to the
    controller."""
    FakeMotorPV.reset()
    h = HexapodFacade(pv_class=FakeMotorPV)
    assert h.traj_resolved is False


def test_the_transport_is_resolved_once():
    FakeMotorPV.reset()
    calls = []

    class _CountingHexapod(_CompleteHexapod):
        def __init__(self, address):
            calls.append(address)
            super().__init__(address)

    h = HexapodFacade(pv_class=FakeMotorPV, connect=True)
    h._traj = None
    h._traj_preference = "direct"
    import hexapod_traj

    original = hexapod_traj.make_trajectory_backend
    try:
        hexapod_traj.make_trajectory_backend = lambda *a, **k: original(
            "10.0.0.1", "12idbHXP",
            importer=_module(_CountingHexapod, _WAVE_GEN_ID),
            prefer="direct", log=_QUIET,
        )
        import hexapod as hexapod_module

        hexapod_module.make_trajectory_backend = hexapod_traj.make_trajectory_backend
        assert h.can_fly is True
        h.run_traj("X")
        h.stop_traj()
        assert calls == ["10.0.0.1"]
    finally:
        hexapod_traj.make_trajectory_backend = original
        import hexapod as hexapod_module

        hexapod_module.make_trajectory_backend = original


def test_facade_forwards_trajectory_calls_verbatim():
    FakeMotorPV.reset()
    device = _CompleteHexapod("10.0.0.1")
    from hexapod_traj import PiHexapodTrajectory

    h = HexapodFacade(
        pv_class=FakeMotorPV,
        traj=PiHexapodTrajectory(device, _WAVE_GEN_ID),
    )
    h.set_traj_SNAKE2(0.01, -0.1, 0.2, 0.01, 0.0, 0.05, 0.01)
    assert device.calls == [
        ("set_traj_SNAKE2", (0.01, -0.1, 0.2, 0.01, 0.0, 0.05, 0.01), {})
    ]
    assert h.pulse_number == 7
    assert h.WaveGenID == _WAVE_GEN_ID


def test_facade_reports_the_degraded_state_without_raising():
    FakeMotorPV.reset()
    h = HexapodFacade(
        pv_class=FakeMotorPV,
        traj=UnavailableTrajectory(STATE_ABSENT, "pihexapod is not installed"),
    )
    assert h.can_fly is False
    assert h.traj_state == STATE_ABSENT
    assert "not installed" in h.traj_reason
    # Step motion is unaffected - that is the whole point of the split.
    assert h.mv("X", 1.0) is True
    assert h.get_pos("X") == pytest.approx(1.0)
    with pytest.raises(TrajectoryUnavailable):
        h.run_traj("X")


def test_simulated_snake_pulse_count_mirrors_the_hardware_quirks(facade):
    """One phantom trigger per X line, and the Y line count padded to even -
    the same adjustments scan_handler's _adjust_axis_length makes."""
    facade.set_traj_SNAKE2(0.01, 0.0, 0.04, 0.01, 0.0, 0.02, 0.01)
    # X: 0, .01, .02, .03, .04 -> 5, +1 phantom = 6. Y: 0, .01, .02 -> 3 -> 4.
    assert facade.pulse_number == 24
    assert facade.pulse_step == 0.01


def test_mv_rejects_an_odd_argument_list(facade):
    with pytest.raises(TypeError):
        facade.mv("X", 1.0, "Z")


# ---------------------------------------------------------------------------
# The real library, when it happens to be installed
# ---------------------------------------------------------------------------

def test_the_installed_library_has_the_surface_we_call():
    """A regression test against the real pihexapod, skipped where it is not
    installed. Importing it opens nothing; only connecting would."""
    gcs = pytest.importorskip("pihexapod.gcs")
    missing = [name for name in TRAJ_METHODS if not hasattr(gcs.Hexapod, name)]
    assert missing == []
    assert set(getattr(gcs, "WaveGenID", {})) == {"X", "Y", "Z", "U", "V", "W"}


# ---------------------------------------------------------------------------
# get_records is transport-dependent
# ---------------------------------------------------------------------------

def test_get_records_is_refused_over_the_epics_transport():
    """Draining DRR? through asyn_1 is one CA transaction per scan point;
    a 3000-point attempt took the IOC down. The fly scan does not need it,
    so the transport stays available and only this call refuses."""
    from hexapod_traj import PiHexapodTrajectory, TrajectoryUnavailable

    backend = PiHexapodTrajectory(_CompleteHexapod("12idbHXP"), _WAVE_GEN_ID,
                                  transport="epics", address="12idbHXP")
    assert backend.available is True
    with pytest.raises(TrajectoryUnavailable) as exc:
        backend.get_records()
    assert "IMAX" in str(exc.value)


def test_get_records_still_works_over_the_direct_transport():
    from hexapod_traj import PiHexapodTrajectory

    device = _CompleteHexapod("10.0.0.1")
    backend = PiHexapodTrajectory(device, _WAVE_GEN_ID, transport="direct",
                                  address="10.0.0.1")
    assert backend.get_records() == "get_records"  # _record() echoes the name
    assert device.calls[-1][0] == "get_records"
