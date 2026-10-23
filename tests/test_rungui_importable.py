"""rungui must be importable without starting anything.

It was not. `app = QApplication(sys.argv)` and `main_panel =
ptyco_main_control()` ran at module scope, and so did the construction of
the hexapod, the softglue, the DG645 and the scan-number PV. Importing this
one file therefore built a GUI and connected to the instrument, which is
why two other modules carried comments forbidding it, why nothing could
introspect the file, and why a second copy of the panel meant a second set
of connections to hardware that allows exactly one client.

Everything downstream in the migration needs this: a window cannot take a
client, and a handler cannot be tested, while importing the module they
live in powers up a beamline.

These run the import in a subprocess with the REAL hardware path selected
-- no --debug, no simulation flags -- because that is the configuration
where a regression would actually connect to something.
"""

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.timeout(180)

PROBE = r"""
import json, sys
connects = []
sys.addaudithook(
    lambda event, args: connects.append(event)
    if event == "socket.connect" else None)

import gui.rungui as rungui
from PyQt5.QtWidgets import QApplication

print(json.dumps({
    "connects": connects,
    "qapplication": QApplication.instance() is not None,
    "pts": rungui.pts is not None,
    "softglue": rungui.s12softglue is not None,
    "dg645": rungui.dg645_12ID is not None,
    "scan_number_pv": rungui.SCAN_NUMBER_IOC is not None,
    "has_build_devices": callable(getattr(rungui, "build_devices", None)),
}))
"""


@pytest.fixture(scope="module")
def imported():
    """Import rungui in a clean process, on the real-hardware path."""
    result = subprocess.run([sys.executable, "-c", PROBE],
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, (
        f"importing gui.rungui failed:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_rungui_builds_no_gui(imported):
    assert imported["qapplication"] is False


def test_importing_rungui_opens_no_connection(imported):
    """socket.connect specifically.

    A socket.bind does happen, from urllib3's IPv6 capability probe when
    `requests` is imported. That is a third-party startup check against the
    local stack, not a connection to an instrument, so it is not what this
    guards.
    """
    assert imported["connects"] == []


@pytest.mark.parametrize("device",
                         ["pts", "softglue", "dg645", "scan_number_pv"])
def test_importing_rungui_builds_no_hardware(imported, device):
    assert imported[device] is False, (
        f"{device} was built at import; it belongs in build_devices()")


def test_the_devices_are_built_by_an_explicit_call(imported):
    """main() calls this after the GUI exists, so a failure can be reported
    rather than happening during an import nobody asked for."""
    assert imported["has_build_devices"]


def test_build_devices_is_idempotent():
    """main() calls it; a test or a second entry point might too. A second
    call must not open a second set of connections."""
    probe = r"""
import gui.rungui as rungui
rungui.pts = "already built"
rungui.build_devices()
print(rungui.pts)
"""
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "already built"


# -- joining the shared server ----------------------------------------------

def test_rungui_asks_the_server_for_no_devices_yet():
    """It still builds its own, so the server must open none on its behalf.

    Opening `motion` here while the panel also holds its own would put two
    clients on the C-887, which accepts one -- the exact conflict the
    server exists to remove. This flips when the executors move.
    """
    from ptychosaxs.server.registry import REQUIREMENTS

    assert REQUIREMENTS["rungui"] == ()


def test_the_udp_remote_control_channel_is_gone():
    """One-way datagrams on port 20002 that could not reply, whose only
    in-repo client had been broken since April. Replaced by the server's
    request/response methods and ptychosaxs.client.cli."""
    import importlib

    for gone in ("gui.network", "gui.network.server", "gui.network.client"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(gone)


def test_rungui_no_longer_references_the_udp_server():
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "gui" / "rungui.py").read_text()
    for gone in ("create_server", "jsonReceived", "UDPserver"):
        assert gone not in src, f"{gone} still referenced in rungui"


def test_the_command_vocabulary_survived_the_socket():
    """run_json is still callable. The socket that fed it is gone, but the
    list of things the panel can be asked to do is the clearest statement
    of what link/methods.py has to grow to cover."""
    from gui.handlers.scan_handler import ScanHandler

    assert callable(ScanHandler.run_json)
