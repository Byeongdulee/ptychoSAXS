"""When the server may stop, and when it must not.

The rules differ by scan type because the hardware differs. A step scan is
driven step by step from here, so it can be asked to stop and will finish
the step it is on, close the shutter and write its data. A fly scan's
trajectory lives in the C-887's wave generator and runs autonomously once
started -- there is no host-side flag -- so the only honest answer is to
wait for it.
"""

import logging
import time

import pytest

from ptychosaxs.api.events import EventBus
from ptychosaxs.api import jobs as J
from ptychosaxs.server.lifecycle import ExitPolicy

pytestmark = pytest.mark.timeout(90)


class FakeRegistry:
    def __init__(self, count=0):
        self.count = count

    def live_count(self):
        return self.count


@pytest.fixture
def parts():
    log = logging.getLogger("test")
    jm = J.JobManager(EventBus(), log)
    yield FakeRegistry(), jm, log
    jm.shutdown(wait=False)


def _busy(seconds):
    def body(token, progress):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            token.check()
            time.sleep(0.01)
    return body


# -- the ordinary case -------------------------------------------------------

def test_a_server_with_clients_stays_up(parts):
    reg, jm, log = parts
    reg.count = 1
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    assert policy.should_exit() is False


def test_the_last_client_leaving_stops_the_server(parts):
    reg, jm, log = parts
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    assert policy.should_exit() is True


def test_a_grace_period_is_honoured(parts):
    """So closing one panel and opening another does not rebuild every
    hardware connection."""
    reg, jm, log = parts
    policy = ExitPolicy(reg, jm, log, linger_s=0.4)
    assert policy.should_exit() is False      # starts the clock
    time.sleep(0.5)
    assert policy.should_exit() is True


def test_a_client_arriving_during_the_grace_period_cancels_the_exit(parts):
    reg, jm, log = parts
    policy = ExitPolicy(reg, jm, log, linger_s=5)
    assert policy.should_exit() is False
    reg.count = 1
    assert policy.should_exit() is False
    reg.count = 0
    assert policy.should_exit() is False      # clock restarted, not resumed


# -- step scans --------------------------------------------------------------

def test_a_step_scan_is_asked_to_stop_and_then_the_server_exits(parts):
    reg, jm, log = parts
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    handle = jm.submit(J.STEP_SCAN, _busy(30), origin="test")
    time.sleep(0.1)

    assert policy.should_exit() is False, "must not exit mid-scan"
    assert handle.token.stop_requested, "the scan should have been asked to stop"

    assert jm.wait_idle(10), "the step scan did not stop"
    assert jm.info(handle.job_id).state == J.ABORTED

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not policy.should_exit():
        time.sleep(0.05)
    assert policy.should_exit() is True


# -- fly scans ---------------------------------------------------------------

def test_a_fly_scan_is_never_asked_to_stop(parts):
    reg, jm, log = parts
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    handle = jm.submit(J.FLY_SCAN, _busy(1.0), origin="test")
    time.sleep(0.1)

    assert policy.should_exit() is False
    assert not handle.token.stop_requested, (
        "a fly scan must not be asked to stop: its trajectory runs in the "
        "controller and there is no host-side stop")
    assert handle.info.cancellable is False


def test_the_server_waits_for_a_fly_scan_then_exits(parts):
    reg, jm, log = parts
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    handle = jm.submit(J.FLY_SCAN, _busy(0.8), origin="test")
    time.sleep(0.1)
    assert policy.should_exit() is False

    assert jm.wait_idle(15)
    assert jm.info(handle.job_id).state == J.DONE, "the fly scan was cut short"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not policy.should_exit():
        time.sleep(0.05)
    assert policy.should_exit() is True


def test_an_explicit_shutdown_during_a_fly_scan_is_refused_with_a_reason(parts):
    """This is what the GUI turns into "wait for the fly scan to finish"."""
    reg, jm, log = parts
    reg.count = 1
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    jm.submit(J.FLY_SCAN, _busy(0.6), origin="test")
    time.sleep(0.1)

    reason = policy.blocking_reason()
    assert reason is not None
    assert "cannot be interrupted" in reason

    jm.wait_idle(15)
    assert policy.blocking_reason() is None


def test_a_step_scan_does_not_block_an_explicit_shutdown(parts):
    """Only fly scans are uninterruptible."""
    reg, jm, log = parts
    reg.count = 1
    policy = ExitPolicy(reg, jm, log, linger_s=0)
    jm.submit(J.STEP_SCAN, _busy(5), origin="test")
    time.sleep(0.1)
    assert policy.blocking_reason() is None
    jm.request_stop()
    jm.wait_idle(10)


# -- the real daemon ---------------------------------------------------------

def test_a_real_server_exits_when_its_last_client_leaves(tmp_path):
    import os
    import subprocess
    import sys
    from tests.conftest import SIM_ENV, wait_for_socket, wait_for_gone

    env = {**os.environ, **SIM_ENV, "PTYCHOSAXS_RUNTIME_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ptychosaxs.server", "--simulate",
         "--linger", "0.1"],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    sock = str(tmp_path / "ctl.sock")
    try:
        assert wait_for_socket(sock)
        from ptychosaxs.client.connection import BeamlineClient
        c = BeamlineClient(sock, kind="gui", gui_type="rungui")
        c.connect(start_server=False)
        assert c.ready
        c.close()
        assert wait_for_gone(sock, timeout=20), "server outlived its clients"
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
