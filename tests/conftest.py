"""Shared pytest configuration for the ptychoSAXS test suite.

No hardware or Qt event loop is started by any fixture here - tests import
plain Python classes (debug stubs, ini/JSON logic) directly, never a live
QApplication, so the suite runs headlessly with no display/X server/
QT_QPA_PLATFORM configuration required.

There is deliberately no sys.path manipulation. The repository is installed
with `pip install -e .`, so `gui` and `ptychosaxs` (with tools, debug and
analysis as subpackages) import the same way here as they do in the
application. The old path block also put ptychosaxs/ itself on sys.path,
which made each device module importable under two different names and
forced every one of them to carry a `try: from .x import ... except
ImportError: from x import ...` shim.

The server fixtures run a **real daemon in a subprocess**, not in this
process. That is not incidental: the device layer reads its simulation
flags from os.environ at module import, so an in-process daemon would have
to mutate this process's environment and whichever test imported
motions_ver2 first would decide the behaviour for every test after it. A
subprocess gets a clean environment and is the thing we actually ship.
"""

import os
import socket
import subprocess
import sys
import time

import pytest

SIM_ENV = {
    "PTYCHOSAXS_DEBUG_HEXAPOD": "1",
    "PTYCHOSAXS_DEBUG_TOMO_PHI": "1",
    "PTYCHOSAXS_DEBUG_GONIO": "1",
    "PTYCHOSAXS_DEBUG_QDS": "1",
    "PTYCHOSAXS_DEBUG_DG645": "1",
    "PTYCHOSAXS_DEBUG_EPICS": "1",
}


def wait_for_socket(path, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(path)
            s.close()
            return True
        except OSError:
            s.close()
            time.sleep(0.1)
    return False


def wait_for_gone(path, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(path)
            s.close()
            time.sleep(0.1)
        except OSError:
            return True
    return False


class ServerFixture:
    """A real daemon in a subprocess, on a private socket."""

    def __init__(self, runtime_dir, proc, env):
        self.runtime_dir = str(runtime_dir)
        self.proc = proc
        self.env = env
        self.socket_path = os.path.join(self.runtime_dir, "ctl.sock")

    def client(self, kind="cli", gui_type=None, label="", start=False):
        from ptychosaxs.client.connection import BeamlineClient
        c = BeamlineClient(self.socket_path, kind=kind, gui_type=gui_type,
                           label=label or gui_type or kind)
        c.connect(start_server=start, timeout_s=20)
        return c

    def log(self):
        try:
            with open(os.path.join(self.runtime_dir, "server.log")) as fh:
                return fh.read()
        except OSError:
            return ""

    def running(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(self.socket_path)
            s.close()
            return True
        except OSError:
            s.close()
            return False


def _spawn(runtime_dir, *args):
    env = {**os.environ, **SIM_ENV, "PTYCHOSAXS_RUNTIME_DIR": str(runtime_dir)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ptychosaxs.server", "--simulate", *args],
        env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    return proc, env


@pytest.fixture
def server_env(tmp_path_factory):
    """A private runtime dir and the environment a daemon should inherit."""
    runtime = tmp_path_factory.mktemp("ptys-rt")
    env = {**os.environ, **SIM_ENV, "PTYCHOSAXS_RUNTIME_DIR": str(runtime)}
    return runtime, env


@pytest.fixture
def beamline_server(tmp_path_factory):
    """A long-lingering daemon, so tests can connect and disconnect freely."""
    runtime = tmp_path_factory.mktemp("ptys-srv")
    proc, env = _spawn(runtime, "--linger", "120")
    fixture = ServerFixture(runtime, proc, env)
    if not wait_for_socket(fixture.socket_path):
        proc.kill()
        pytest.fail(f"server did not start:\n{fixture.log()}")
    yield fixture
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
