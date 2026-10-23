"""The optics panel as a client of the shared server.

This is the first GUI to migrate, so these tests are as much about the
pattern as about the panel: it opens no channels of its own, it polls
nothing, it cannot be opened twice, and it is what starts and stops the
server when it is the only thing running.
"""

import ast
import os
import pathlib
import subprocess
import sys
import time

import pytest

from tests.conftest import SIM_ENV, wait_for_gone, wait_for_socket

pytestmark = pytest.mark.timeout(180)

PANEL = pathlib.Path(__file__).resolve().parents[1] / "gui" / "optics_motors.py"


# -- static properties -------------------------------------------------------

def test_the_panel_opens_no_channels_of_its_own():
    """Every record it binds now comes from the server's shared mirror."""
    src = PANEL.read_text()
    for forbidden in ("caget", "caput", "from epics", "import epics"):
        assert forbidden not in src, f"{forbidden!r} is back in the panel"


def test_the_panel_has_no_hardware_timer():
    """The 100 ms updatepos poll read every motor on the GUI thread and was
    the worst latency offender in the repo. Positions arrive as events now."""
    tree = ast.parse(PANEL.read_text())
    timers = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", getattr(n.func, "attr", "")) == "QTimer"]
    assert timers == [], "a QTimer is back in the optics panel"


def test_the_panel_builds_no_controllers_itself():
    src = PANEL.read_text()
    assert "_build_controllers" not in src
    assert "from ptychosaxs.optics import" not in src


# -- running it --------------------------------------------------------------

def _env(runtime):
    return {**os.environ, **SIM_ENV,
            "PTYCHOSAXS_RUNTIME_DIR": str(runtime),
            "QT_QPA_PLATFORM": "offscreen"}


def _launch_panel(runtime, **kw):
    return subprocess.Popen(
        [sys.executable, "-m", "gui.optics_motors"],
        env=_env(runtime), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, **kw)


@pytest.fixture
def panel(tmp_path):
    proc = _launch_panel(tmp_path)
    sock = str(tmp_path / "ctl.sock")
    if not wait_for_socket(sock, timeout=60):
        proc.kill()
        pytest.fail(f"panel did not bring up a server:\n{proc.stdout.read()}")
    time.sleep(3)          # let it finish connecting and subscribing
    yield proc, tmp_path, sock
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def _client(sock, **kw):
    from ptychosaxs.client.connection import BeamlineClient
    c = BeamlineClient(sock, kind="cli", label="test probe", **kw)
    c.connect(start_server=False)
    return c


def test_launching_the_panel_starts_a_server(panel):
    _proc, _rt, sock = panel
    with _client(sock) as c:
        assert c.ready
        types = {x["gui_type"] for x in c.system.clients()}
        assert "optics_motors" in types


def test_the_panel_opens_the_optics_group(panel):
    _proc, _rt, sock = panel
    with _client(sock) as c:
        open_groups = {g["name"] for g in c.system.status()["groups"] if g["open"]}
        assert "optics" in open_groups


def test_the_panel_subscribes_the_records_its_boxes_are_bound_to(panel):
    """Drive and tweak fields used to be channels the panel opened."""
    _proc, _rt, sock = panel
    with _client(sock) as c:
        seen = {}
        c.on("pv.values", lambda n, d: seen.update(d.get("values", {})))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(seen) < 10:
            time.sleep(0.1)
        assert any(n.endswith(".VAL") for n in seen), sorted(seen)
        assert any(n.endswith(".TWV") for n in seen), sorted(seen)


def test_a_move_made_elsewhere_reaches_the_panel(panel):
    """The point of one server: another client's move is visible here."""
    _proc, _rt, sock = panel
    with _client(sock) as c:
        start = c.optics.position("ZP_ver")
        c.optics.move("ZP_ver", start + 0.5)
        time.sleep(0.5)
        assert c.optics.position("ZP_ver") == pytest.approx(start + 0.5)


def test_a_second_panel_refuses_and_exits_cleanly(panel):
    _proc, runtime, _sock = panel
    second = subprocess.run([sys.executable, "-m", "gui.optics_motors"],
                            env=_env(runtime), capture_output=True,
                            text=True, timeout=120)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already running" in second.stdout


def test_a_second_panel_asks_for_the_first_to_be_raised(beamline_server):
    """Focus goes to the connection holding the slot, and nowhere else.

    So the holder has to be the observer: this test takes the
    optics_motors slot itself, then launches the real panel as the second
    instance and watches for the request it sends before exiting.
    """
    from ptychosaxs.client.connection import BeamlineClient

    holder = BeamlineClient(beamline_server.socket_path, kind="gui",
                            gui_type="optics_motors", label="stand-in")
    holder.connect(start_server=False)
    raised = []
    holder.on("client.focus_requested", lambda n, d: raised.append(d))
    try:
        env = {**beamline_server.env, "QT_QPA_PLATFORM": "offscreen"}
        second = subprocess.run([sys.executable, "-m", "gui.optics_motors"],
                                env=env, capture_output=True, text=True,
                                timeout=120)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "already running" in second.stdout

        deadline = time.monotonic() + 15
        while not raised and time.monotonic() < deadline:
            time.sleep(0.1)
        assert raised == [{"gui_type": "optics_motors"}]
    finally:
        holder.close()


def test_closing_the_panel_stops_the_server(tmp_path):
    proc = _launch_panel(tmp_path)
    sock = str(tmp_path / "ctl.sock")
    assert wait_for_socket(sock, timeout=60)
    time.sleep(2)
    proc.terminate()
    proc.wait(timeout=30)
    assert wait_for_gone(sock, timeout=30), (
        "the server outlived the only GUI that was using it")
