"""The CRL rig as a server-owned device, and its panel as a client.

The second GUI to migrate, which is what makes it worth testing separately:
it exercises a different device group, a different controller surface, and
the client-side proxy that lets its scalar-scan child keep working unchanged.
"""

import ast
import os
import pathlib
import subprocess
import sys
import time

import pytest

from tests.conftest import gui_env, wait_for_gone, wait_for_socket

pytestmark = pytest.mark.timeout(180)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = ROOT / "gui" / "CRL_3dprint.py"


# -- static ------------------------------------------------------------------

def test_the_panel_opens_no_channels_of_its_own():
    src = PANEL.read_text()
    for forbidden in ("caget", "caput", "from epics import", "import epics"):
        assert forbidden not in src, f"{forbidden!r} is back in the CRL panel"


def test_the_panel_has_no_hardware_timer():
    """It polled all four axes every 200 ms on the GUI thread."""
    tree = ast.parse(PANEL.read_text())
    starts = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", "") == "start"
              and getattr(getattr(n.func, "value", None), "attr", "") == "timer"]
    assert starts == [], "a polling timer is back in the CRL panel"


def test_the_panel_builds_no_controller_itself():
    assert "CRLAxisController(" not in PANEL.read_text()


# -- the client-side controller proxy ----------------------------------------

class _Facade:
    """Records what the panel's controller asked the server to do."""

    def __init__(self):
        self.calls = []
        self._pos = {"X": 0.0, "Y": 0.0, "TILT": 0.0, "PITCH": 0.0}

    def axes(self):
        return [{"name": n, "unit": "mm" if n in ("X", "Y") else "deg",
                 "connected": n != "PITCH"} for n in self._pos]

    def positions(self):
        return dict(self._pos)

    def position(self, axis):
        self.calls.append(("position", axis))
        return self._pos[axis]

    def move(self, axis, target, wait=False):
        self.calls.append(("move", axis, target))
        self._pos[axis] = target

    def tweak(self, axis, step, forward=True):
        self.calls.append(("tweak", axis, step, forward))

    def set_tweak_step(self, axis, step):
        self.calls.append(("set_tweak_step", axis, step))

    def stop(self, axis=None):
        self.calls.append(("stop", axis))

    def get_speed(self, axis):
        return 1.0

    def set_speed(self, axis, velocity):
        self.calls.append(("set_speed", axis, velocity))

    def set_position(self, axis, value=0.0):
        self.calls.append(("set_position", axis, value))


@pytest.fixture
def proxy():
    from ptychosaxs.client.axes import RemoteAxisController
    facade = _Facade()
    return RemoteAxisController(facade, {}), facade


def test_the_proxy_presents_the_controller_surface(proxy):
    """gui/handlers/scalar_scan.py was written against EpicsMotorController
    and is handed this instead, unchanged."""
    controller, _ = proxy
    for name in ("names", "get_pos", "mv", "tweak", "set_tweak_step",
                 "set_speed", "get_speed", "get_unit", "is_axis_connected",
                 "disconnected_axes", "connect", "stop", "set_pos"):
        assert hasattr(controller, name), f"proxy is missing {name}"


def test_the_proxy_reports_units_and_connection_per_axis(proxy):
    controller, _ = proxy
    assert controller.get_unit("TILT") == "deg"
    assert controller.is_axis_connected("X") is True
    assert controller.disconnected_axes() == ["PITCH"]


def test_a_readback_comes_from_the_mirror_without_a_round_trip():
    """Readbacks are pushed, so reading one must not be an RPC."""
    from ptychosaxs.client.axes import RemoteAxisController
    facade = _Facade()
    mirror = {"X": 3.25}
    controller = RemoteAxisController(facade, mirror)
    assert controller.get_pos("X") == 3.25
    assert ("position", "X") not in facade.calls


def test_a_readback_falls_back_to_the_server_when_not_yet_pushed(proxy):
    controller, facade = proxy
    controller.get_pos("Y")
    assert ("position", "Y") in facade.calls


def test_moves_and_tweaks_reach_the_server(proxy):
    controller, facade = proxy
    controller.mv("X", 2.0)
    controller.tweak("Y", 0.1, forward=False)
    controller.set_speed("TILT", 5.0)
    assert ("move", "X", 2.0) in facade.calls
    assert ("tweak", "Y", 0.1, False) in facade.calls
    assert ("set_speed", "TILT", 5.0) in facade.calls


def test_a_relative_move_is_resolved_against_the_current_position(proxy):
    controller, facade = proxy
    controller.mv("X", 1.0)
    controller.mvr("X", 0.5)
    assert facade.positions()["X"] == pytest.approx(1.5)


def test_wait_for_connection_does_not_block():
    """scalar_scan calls this on its PV cache miss path; a blocking wait
    there would reintroduce the stall unreachable records used to cause."""
    from ptychosaxs.client.pv import RemotePV

    class Cache:
        connected = True

        def value(self, n):
            return None

    start = time.perf_counter()
    assert RemotePV("any:record", Cache()).wait_for_connection(timeout=5) is True
    assert time.perf_counter() - start < 0.05


# -- running it --------------------------------------------------------------

def _env(runtime):
    return gui_env(runtime)


@pytest.fixture
def crl_panel(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "gui.CRL_3dprint"],
                            env=_env(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    sock = str(tmp_path / "ctl.sock")
    if not wait_for_socket(sock, timeout=60):
        proc.kill()
        pytest.fail(f"CRL panel did not start a server:\n{proc.stdout.read()}")
    time.sleep(3)
    yield proc, tmp_path, sock
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def _client(sock):
    from ptychosaxs.client.connection import BeamlineClient
    c = BeamlineClient(sock, kind="cli", label="probe")
    c.connect(start_server=False)
    return c


def test_the_panel_opens_the_crl_group_and_nothing_else(crl_panel):
    """It must not drag the hexapod up just by being opened."""
    _proc, _rt, sock = crl_panel
    with _client(sock) as c:
        open_groups = {g["name"] for g in c.system.status()["groups"] if g["open"]}
        assert open_groups == {"crl"}


def test_the_crl_axes_are_drivable_through_the_server(crl_panel):
    _proc, _rt, sock = crl_panel
    with _client(sock) as c:
        assert [a["name"] for a in c.crl.axes()] == ["X", "Y", "TILT", "PITCH"]
        c.crl.move("X", 1.5)
        time.sleep(0.4)
        assert c.crl.position("X") == pytest.approx(1.5)


def test_crl_readbacks_are_published(crl_panel):
    _proc, _rt, sock = crl_panel
    with _client(sock) as c:
        seen = []
        c.on("crl.positions", lambda n, d: seen.append(d))
        time.sleep(1.2)
        assert len(seen) >= 5, f"only {len(seen)} events in 1.2s"
        assert set(seen[-1]["positions"]) == {"X", "Y", "TILT", "PITCH"}


def test_a_second_crl_panel_refuses_and_exits_cleanly(crl_panel):
    _proc, runtime, _sock = crl_panel
    second = subprocess.run([sys.executable, "-m", "gui.CRL_3dprint"],
                            env=_env(runtime), capture_output=True,
                            text=True, timeout=120)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already running" in second.stdout


def test_the_optics_panel_and_the_crl_panel_coexist(tmp_path):
    """Different GUIs sharing one server is the whole point."""
    crl = subprocess.Popen([sys.executable, "-m", "gui.CRL_3dprint"],
                           env=_env(tmp_path), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    sock = str(tmp_path / "ctl.sock")
    assert wait_for_socket(sock, timeout=60)
    optics = subprocess.Popen([sys.executable, "-m", "gui.optics_motors"],
                              env=_env(tmp_path), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        time.sleep(6)
        with _client(sock) as c:
            types = {x["gui_type"] for x in c.system.clients() if x["gui_type"]}
            assert types == {"crl_3dprint", "optics_motors"}
            groups = {g["name"] for g in c.system.status()["groups"] if g["open"]}
            assert groups == {"crl", "optics"}
    finally:
        for p in (optics, crl):
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
    assert wait_for_gone(sock, timeout=30)
