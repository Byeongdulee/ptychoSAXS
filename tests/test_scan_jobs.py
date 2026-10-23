"""Scans as server jobs: submitted, watched, stopped.
No hardware and no Qt. The runner is a stand-in that behaves the way the
real executors do -- it checks the stop token between steps, and a fly
runner ignores it because its trajectory runs in the controller.
What these pin down is the thing macro_window currently has to fake by
polling `self.main.isscan` every 50 ms, and the thing a second panel
currently cannot do at all: see a scan somebody else started.
"""
import logging
import time
import numpy as np
import pytest
from ptychosaxs.api import jobs as job_kinds
from ptychosaxs.api.profile import Profile
from ptychosaxs.api.scans import ScanContext, ScanRequest
from ptychosaxs.api.session import BeamlineSession
LOG = logging.getLogger("test")
class FakeRunner:
    """Behaves like ScanHandler does: polls the token between steps."""
    def __init__(self, steps=20, step_s=0.01, ignore_stop=False):
        self.steps = steps
        self.step_s = step_s
        self.ignore_stop = ignore_stop
        self.token = None
        self.completed = 0
        self.bound = []
    def bind_stop_token(self, token):
        self.token = token
        self.bound.append(token)
    def run_scan(self, req, progress, token):
        for i in range(self.steps):
            if not self.ignore_stop:
                token.check()
            progress(i + 1, self.steps, "scanning")
            self.completed = i + 1
            time.sleep(self.step_s)
        return {"points": self.completed}
@pytest.fixture
def session():
    s = BeamlineSession(Profile(simulated=True))
    s.start()
    yield s
    s.close()
# -- planning ----------------------------------------------------------------
def test_a_plan_counts_points_and_costs_them(session):
    plan = session.scans.plan(ScanRequest(kind="stepscan2d"),
                              [(np.zeros(20), 0.5), (np.zeros(20), 0.5)])
    assert plan["positions"] == 400
    assert plan["estimate_s"] == pytest.approx(400 * (0.5 + 0.5))
    assert plan["estimate"] == "6.7 min"
def test_planning_runs_nothing(session):
    """It is arithmetic. No job is created and nothing moves."""
    session.scans.plan(ScanRequest(kind="fly2d"), [(np.zeros(100), 0.01)])
    assert session.jobs.snapshot() == []
def test_a_fly_plan_uses_the_detector_floor_not_the_motor_settle(session):
    fly = session.scans.plan(ScanRequest(kind="fly2d"), [(np.zeros(10), 0.1)])
    step = session.scans.plan(ScanRequest(kind="stepscan"), [(np.zeros(10), 0.1)])
    assert fly["overhead_s"] < step["overhead_s"]
# -- guards ------------------------------------------------------------------
def test_a_big_plan_collects_the_large_scan_guard(session):
    plan = session.scans.plan(ScanRequest(kind="stepscan2d"),
                              [(np.zeros(30), 0.1), (np.zeros(30), 0.1)])
    assert [g.key for g in session.scans.check(plan)] == ["large_scan"]
def test_an_unattended_run_is_not_asked(session):
    plan = session.scans.plan(ScanRequest(kind="stepscan2d"),
                              [(np.zeros(30), 0.1), (np.zeros(30), 0.1)])
    assert session.scans.check(plan, unattended=True) == []
def test_a_velocity_breach_is_reported_before_the_scan_starts(session):
    plan = session.scans.plan(ScanRequest(kind="fly2d"), [(np.zeros(5), 0.1)])
    keys = [g.key for g in session.scans.check(plan, velocities={"X": 99.0})]
    assert "velocity_too_high" in keys
# -- running -----------------------------------------------------------------
def test_starting_a_scan_returns_immediately(session):
    session.scans.attach_runner(FakeRunner(steps=40, step_s=0.02))
    start = time.perf_counter()
    job_id = session.scans.start(ScanRequest(kind="stepscan2d"))
    assert time.perf_counter() - start < 0.2, "start blocked on the scan"
    assert job_id
    session.jobs.wait_idle(30)
def test_progress_is_broadcast_while_the_scan_runs(session):
    """What macro_window has to fake by polling a flag every 50 ms."""
    seen = []
    session.bus.subscribe(
        lambda e: seen.append(e) if e.name == "job.progress" else None)
    session.scans.attach_runner(FakeRunner(steps=10, step_s=0.01))
    session.scans.start(ScanRequest(kind="stepscan2d"))
    assert session.jobs.wait_idle(30)
    assert len(seen) >= 5
    assert seen[-1].data["done"] == 10
    assert seen[-1].data["total"] == 10
def test_a_finished_scan_announces_itself(session):
    done = []
    session.bus.subscribe(
        lambda e: done.append(e) if e.name == "job.finished" else None)
    session.scans.attach_runner(FakeRunner(steps=5, step_s=0.01))
    session.scans.start(ScanRequest(kind="stepscan2d"))
    assert session.jobs.wait_idle(30)
    assert done and done[-1].data["state"] == job_kinds.DONE
def test_a_step_scan_can_be_stopped_and_stops_where_it_was(session):
    runner = FakeRunner(steps=500, step_s=0.01)
    session.scans.attach_runner(runner)
    job_id = session.scans.start(ScanRequest(kind="stepscan2d"))
    time.sleep(0.15)
    assert session.scans.stop()["asked"] == [job_id]
    assert session.jobs.wait_idle(30)
    assert session.jobs.info(job_id).state == job_kinds.ABORTED
    assert 0 < runner.completed < 500, "it should have stopped part-way"
def test_a_fly_scan_cannot_be_stopped(session):
    """Its trajectory runs in the C-887's wave generator; there is no
    host-side stop, so the honest answer is that nothing was asked."""
    session.scans.attach_runner(FakeRunner(steps=8, step_s=0.01,
                                           ignore_stop=True))
    session.scans.start(ScanRequest(kind="fly2d"))
    time.sleep(0.05)
    result = session.scans.stop()
    assert result["asked"] == []
    assert result["fly_in_progress"] is True
    assert session.jobs.wait_idle(30)
def test_the_runner_gets_the_token_and_has_it_taken_back(session):
    """The job's token must drive the runner's own stop flag, not be a
    second independent way to stop."""
    runner = FakeRunner(steps=3, step_s=0.01)
    session.scans.attach_runner(runner)
    session.scans.start(ScanRequest(kind="stepscan2d"))
    assert session.jobs.wait_idle(30)
    assert len(runner.bound) == 2
    assert runner.bound[0] is not None and runner.bound[1] is None
def test_a_scan_with_no_runner_says_so_rather_than_failing_obscurely(session):
    with pytest.raises(RuntimeError, match="no scan runner"):
        session.scans.start(ScanRequest(kind="stepscan2d"))
def test_an_unknown_scan_kind_is_refused(session):
    session.scans.attach_runner(FakeRunner())
    with pytest.raises(KeyError):
        session.scans.start(ScanRequest(kind="not_a_scan"))
# -- the migration seam ------------------------------------------------------
def test_the_context_prefers_the_session_over_the_window():
    """How the executors migrate one name at a time: the day a piece of
    state lands on BeamlineSession, they read it from there unedited."""
    class Sess:
        parameters = "session"
    class Win:
        parameters = "window"
        pts = "window-pts"
    ctx = ScanContext(session=Sess(), window=Win())
    assert ctx.parameters == "session"
    assert ctx.pts == "window-pts"
def test_a_write_lands_where_the_read_comes_from():
    """Executors set isscan/isfly as they go. If a write went somewhere
    other than the read, a scan would set a flag nobody is watching."""
    class Win:
        isscan = False
    win = Win()
    ctx = ScanContext(window=win)
    ctx.isscan = True
    assert win.isscan is True
def test_a_name_neither_side_has_fails_loudly():
    ctx = ScanContext(session=object(), window=object())
    with pytest.raises(AttributeError, match="BeamlineSession"):
        ctx.something_nobody_provides
def test_the_context_can_report_what_is_still_missing():
    """Used to track the migration: as state moves onto the session this
    shrinks, and a name here is an executor that would fail at runtime."""
    ctx = ScanContext(session=object(), window=object())
    assert set(ctx.missing()) == set(ScanContext.WINDOW_NAMES)
