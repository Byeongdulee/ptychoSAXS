"""Several panels launched at once must produce exactly one server.

The danger is not untidiness. The PI C-887 accepts exactly one TCP
connection, so two daemons means one of them fails to build the hexapod --
and which one wins is a race. The daemon takes its flock before importing
anything, so the losers exit having touched nothing.
"""

import os
import subprocess
import sys
import time

import pytest

from tests.conftest import SIM_ENV, wait_for_socket

pytestmark = pytest.mark.timeout(120)


def _spawn(runtime, *args):
    env = {**os.environ, **SIM_ENV, "PTYCHOSAXS_RUNTIME_DIR": str(runtime)}
    return subprocess.Popen(
        [sys.executable, "-m", "ptychosaxs.server", "--simulate", *args],
        env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def test_five_concurrent_launches_leave_one_server(tmp_path):
    procs = [_spawn(tmp_path, "--linger", "60") for _ in range(5)]
    sock = tmp_path / "ctl.sock"
    try:
        assert wait_for_socket(str(sock)), "no server came up"

        # Four of the five must exit 0 having done nothing.
        deadline = time.monotonic() + 30
        exited = []
        while time.monotonic() < deadline:
            exited = [p for p in procs if p.poll() is not None]
            if len(exited) >= 4:
                break
            time.sleep(0.2)

        assert len(exited) >= 4, "the losing daemons did not stand down"
        assert all(p.returncode == 0 for p in exited), (
            "a losing daemon exited non-zero: "
            f"{[p.returncode for p in exited]}")

        alive = [p for p in procs if p.poll() is None]
        assert len(alive) == 1, f"{len(alive)} servers are running"

        # And the survivor is the one actually serving.
        from ptychosaxs.client.connection import BeamlineClient
        c = BeamlineClient(str(sock), kind="cli", label="race probe")
        c.connect(start_server=False)
        assert c.hello["server_pid"] == alive[0].pid
        c.close()
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def test_a_socket_left_by_a_killed_server_is_cleared(tmp_path):
    """SIGKILL leaves the socket file behind; the next start must not be
    blocked by it."""
    proc = _spawn(tmp_path, "--linger", "60")
    sock = tmp_path / "ctl.sock"
    assert wait_for_socket(str(sock))
    proc.kill()
    proc.wait(timeout=10)
    assert sock.exists(), "precondition: SIGKILL leaves the file"

    proc2 = _spawn(tmp_path, "--linger", "60")
    try:
        assert wait_for_socket(str(sock)), "could not rebind over a stale socket"
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()


def test_the_lockfile_survives_a_clean_shutdown(tmp_path):
    """Unlinking a held flock lets two processes each believe they hold it."""
    proc = _spawn(tmp_path, "--linger", "0.1")
    lock = tmp_path / "server.lock"
    assert wait_for_socket(str(tmp_path / "ctl.sock"))
    proc.terminate()
    proc.wait(timeout=15)
    assert lock.exists()
