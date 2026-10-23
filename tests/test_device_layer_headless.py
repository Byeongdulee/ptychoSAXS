"""The device layer must import without Qt and without touching anything.

Both properties are load-bearing for the server. It imports these modules
in a process that has no display and may have no PyQt5 at all, and it has
to be able to import them *before* deciding which hardware it owns -- the
flock is taken first precisely so that a daemon that loses the startup race
exits having opened nothing.

These are the regressions that are easy to reintroduce: a `from PyQt5...`
added for one signal, or a PV created at module scope "just to have it
ready".
"""

import ast
import pathlib
import subprocess
import sys

import pytest

import ptychosaxs

PKG = pathlib.Path(ptychosaxs.__file__).parent

#: Qt belongs in exactly one place: the adapter that turns server events
#: into pyqtSignals for the panels.
QT_IS_ALLOWED_IN = {"client/qt.py", "client/prompts.py"}


def _device_layer_files():
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith(("api/", "server/", "link/", "mcp/", "client/")):
            continue
        yield rel, path


def test_no_qt_in_the_device_layer():
    offenders = []
    for rel, path in _device_layer_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m.split(".")[0] in ("PyQt5", "PyQt6", "PySide2", "PySide6")
                   for m in mods):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"Qt imported in the device layer: {offenders}"


def test_qt_is_confined_to_the_client_adapter():
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel in QT_IS_ALLOWED_IN:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m.split(".")[0].startswith(("PyQt", "PySide")) for m in mods):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        f"Qt should only be imported in {sorted(QT_IS_ALLOWED_IN)}, "
        f"found: {offenders}")


# Modules whose third-party driver is not installed here. Not importable at
# all, so there is nothing to assert about their import side effects.
NEEDS_ABSENT_DRIVER = {
    "ptychosaxs.tw_galil": "gclib",
    "ptychosaxs.smaract_gonio": "smaract",
    "ptychosaxs.interferometers": "pyqudis",
    "ptychosaxs.ptychosaxs": "pyqudis",
    "ptychosaxs.tools.panda": "pandablocks",
    "ptychosaxs.tools.scptransfer": "paramiko",
    "ptychosaxs.tools.ptycosaxs_file_process": "paramiko",
}

#: Run in a subprocess: a module imported here would already be in
#: sys.modules and its import side effects would have happened long ago.
#: The module list arrives on argv rather than being templated in.
AUDIT_PROBE = r"""
import json, sys

opened = []
sys.addaudithook(
    lambda event, args: opened.append(event)
    if event in ("socket.connect", "socket.bind") else None)

import importlib
failed = {}
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed[name] = type(exc).__name__ + ": " + str(exc)
print(json.dumps({"opened": opened, "failed": failed}))
"""


def _audit_imports(modules):
    """Import `modules` in a clean process; report sockets opened."""
    result = subprocess.run(
        [sys.executable, "-c", AUDIT_PROBE, *modules],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    import json
    return json.loads(result.stdout.strip().splitlines()[-1])


def _importable_device_modules():
    out = []
    for rel, _path in _device_layer_files():
        if rel.endswith("__init__.py") or "/debug/" in f"/{rel}":
            continue
        mod = "ptychosaxs." + rel[:-3].replace("/", ".")
        if mod in NEEDS_ABSENT_DRIVER:
            continue
        out.append(mod)
    return out


def test_importing_the_device_layer_opens_no_connection():
    """A module-scope PV or socket would make the daemon connect to hardware
    before it has decided it owns any."""
    modules = _importable_device_modules()
    assert modules, "found no device modules to check"
    report = _audit_imports(modules)
    assert report["failed"] == {}, (
        f"device modules failed to import: {report['failed']}")
    assert report["opened"] == [], (
        f"importing the device layer opened {report['opened']}")


def test_importing_the_api_opens_no_connection():
    report = _audit_imports(["ptychosaxs.api.session",
                             "ptychosaxs.server.daemon",
                             "ptychosaxs.client.connection"])
    assert report["failed"] == {}, report["failed"]
    assert report["opened"] == [], (
        f"importing the server stack opened {report['opened']}")


@pytest.mark.parametrize("module,driver", sorted(NEEDS_ABSENT_DRIVER.items()))
def test_driver_backed_modules_are_documented_as_unimportable(module, driver):
    """If one of these starts importing cleanly, its driver arrived and it
    should move into the audited set above."""
    import importlib
    try:
        importlib.import_module(driver)
    except ImportError:
        pytest.skip(f"{driver} is not installed, as expected")
    pytest.fail(f"{driver} is now installed; add {module} to the audited set")


# -- the relocated scan execution --------------------------------------------

def test_the_moved_scan_execution_imports_without_qt():
    """ptychosaxs/api/scan_exec.py is where scan execution is moving to,
    one tranche at a time. It has to stay importable by a server with no
    display, which is the whole reason for moving it -- so a method that
    still needs a widget stays in the Qt handler until its widget reads
    become arguments.
    """
    import ast as _ast
    import pathlib as _pathlib

    path = PKG / "api" / "scan_exec.py"
    tree = _ast.parse(path.read_text())
    offenders = []
    for node in _ast.walk(tree):
        mods = []
        if isinstance(node, _ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, _ast.ImportFrom) and node.module:
            mods = [node.module]
        if any(m.split(".")[0].startswith(("PyQt", "PySide")) for m in mods):
            offenders.append(node.lineno)
    assert offenders == [], f"Qt imported in scan_exec at lines {offenders}"


def test_no_moved_method_reaches_for_a_widget_class():
    """A widget *class* in here means the method cannot run headless even
    though it has moved. Reads through ctx.ui are counted separately --
    those are the remaining coupling, and they are expected to shrink."""
    path = PKG / "api" / "scan_exec.py"
    src = path.read_text()
    for widget in ("QMessageBox", "QLabel", "QLineEdit", "findChild"):
        assert widget not in src, (
            f"{widget} is used in scan_exec; that method belongs in the "
            f"Qt handler until its widget access becomes an argument")
