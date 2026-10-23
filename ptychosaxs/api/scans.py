"""Scans as jobs the server owns.

A scan takes minutes, so it cannot be a blocking call, and more than one
panel may want to watch it. Starting one returns a job id immediately and
progress is broadcast to every connected client -- which is also what gives
macro_window something better than polling `self.main.isscan` every 50 ms,
and gives a second panel a way to see a scan the first one started.

**ScanContext** is the seam for the migration. The eight executors and the
~57 helpers they call reach for 40 names on the GUI window -- overwhelmingly
state rather than widgets: messages, pts, parameters, motornames, softglue,
dg645, detector, and a dozen flags. ScanContext presents exactly those
names, sourced from a BeamlineSession where the session has them and from
the window where it still does. That makes relocating an executor a
rename of `self.w` to `self.ctx` rather than a rewrite, and lets the move
happen in tranches with a working instrument in between.

Nothing here decides whether a scan *should* run. That is
ptychosaxs/api/guards.py, whose checks are pure and answerable without a
screen.
"""

from dataclasses import dataclass, field

from ptychosaxs.api import guards as guard_checks
from ptychosaxs.api import jobs as job_kinds
from ptychosaxs.api import scanplan

#: Which job kind each scan entry point runs as. Fly scans are not
#: cancellable: the trajectory lives in the C-887's wave generator and
#: there is no host-side stop, which is why JobManager refuses to ask.
SCAN_KINDS = {
    "stepscan": job_kinds.STEP_SCAN,
    "stepscan2d": job_kinds.STEP_SCAN,
    "stepscan3d": job_kinds.STEP_SCAN,
    "takeshot": job_kinds.STEP_SCAN,
    "time_series": job_kinds.STEP_SCAN,
    "fly": job_kinds.FLY_SCAN,
    "fly2d": job_kinds.FLY_SCAN,
    "fly3d": job_kinds.FLY_SCAN,
    "helix_fly": job_kinds.FLY_SCAN,
}


@dataclass
class ScanRequest:
    """What a client asked for. Plain data, so it crosses the wire as-is."""

    kind: str
    xmotor: int = 0
    ymotor: int = 2
    phimotor: int = 6
    snake: bool = False
    scan_name: str = ""
    folder: str = ""
    detectors: tuple = ()
    saxs_mode: bool = False
    test_mode: bool = False
    slice_label_prefix: str = ""
    extra: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {"kind": self.kind, "xmotor": self.xmotor,
                "ymotor": self.ymotor, "phimotor": self.phimotor,
                "snake": self.snake, "scan_name": self.scan_name}


class ScanContext:
    """The names a scan executor reaches for, in one object.

    During the migration this wraps the GUI window, so an executor behaves
    identically whether it is called from the handler or from a job. As
    each piece of state moves onto BeamlineSession, the lookup here
    switches over and the executors do not change again.
    """

    #: Everything the eight executors and their helpers name on the window.
    #: Derived by walking the call graph, not by guesswork.
    WINDOW_NAMES = (
        "messages", "pts", "parameters", "motornames", "motorunits",
        "s12softglue", "dg645_12ID", "detector", "mpos", "rpos",
        "motor_p0", "shutter", "SCAN_NUMBER_IOC",
        "DEBUG_MOTORS", "DEBUG_DEVICES", "DEBUG_MODE",
        "hdf_plugin_name", "use_hdf_plugin", "hdf_plugin_savemode_fly",
        "hdf_plugin_savemode_step", "detector_mode", "is_ptychomode",
        "isscan", "isfly", "isfly2", "scandone", "signalmotor",
        "signalmotorunit", "scan_scalars", "save_list", "threadpool",
        "hexapod_flymode", "monitor_beamline_status",
        "shutter_close_after_scan", "run_stop_issued", "isOK2run",
        "is_selfsaved", "is_softglue_savingdone", "macro_running",
        "is_arming_detecotors_timedout", "DEFAULTS", "ui",
    )

    def __init__(self, session=None, window=None):
        # object.__setattr__ because __setattr__ below forwards everything
        # it does not recognise to the window.
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_window", window)

    @property
    def session(self):
        return self._session

    @property
    def window(self):
        return self._window

    def __getattr__(self, name):
        """Session first, window second.

        Looking at the session first is what makes the migration
        incremental: the day `parameters` moves onto BeamlineSession, every
        executor starts reading it from there with no edit.
        """
        session = object.__getattribute__(self, "_session")
        if session is not None and hasattr(session, name):
            return getattr(session, name)
        window = object.__getattribute__(self, "_window")
        if window is not None:
            try:
                return getattr(window, name)
            except AttributeError:
                pass
        # The message matters: during the migration this is what an
        # executor hits when it reaches for state that has not been given
        # to BeamlineSession yet, and the bare AttributeError a window
        # raises says nothing about why or what to do.
        raise AttributeError(
            f"{name!r} is on neither the session nor the window. An "
            f"executor needs it, so it has to be added to BeamlineSession "
            f"before that executor can run headless.")

    def __setattr__(self, name, value):
        """Writes follow reads: whoever owns the name takes the write.

        The executors set flags as they go -- isscan, isfly, scandone --
        and those writes have to land on the same object the reads come
        from, or a scan would set a flag nobody is looking at.
        """
        session = object.__getattribute__(self, "_session")
        if session is not None and hasattr(session, name):
            setattr(session, name, value)
            return
        window = object.__getattribute__(self, "_window")
        if window is not None:
            setattr(window, name, value)
            return
        object.__setattr__(self, name, value)

    def missing(self):
        """Names an executor needs that neither side can supply.

        Used by the migration tests: as state moves onto the session this
        should stay empty, and a name appearing here is an executor that
        would fail at runtime rather than at import.
        """
        return [n for n in self.WINDOW_NAMES if not self._has(n)]

    def _has(self, name):
        session = object.__getattribute__(self, "_session")
        window = object.__getattribute__(self, "_window")
        return ((session is not None and hasattr(session, name))
                or (window is not None and hasattr(window, name)))


class ScanService:
    """Plan a scan, check it, run it as a job.

    Split three ways on purpose. plan_* is pure arithmetic and testable
    with no instrument; check_* returns Guards, which an unattended client
    can answer from a policy; only start_* needs hardware.
    """

    def __init__(self, session, log):
        self._session = session
        self._log = log
        self._runner = None

    def attach_runner(self, runner):
        """Give the service something that can actually execute a scan.

        During the migration that is the GUI's ScanHandler. The service
        owns the job, the progress and the stop flag; the runner owns the
        steps. Once the executors have moved, the runner is this module.
        """
        self._runner = runner

    @property
    def runner(self):
        return self._runner

    # -- plan ----------------------------------------------------------------

    #: Per-point overhead, by scan kind. A fly point costs the detector's
    #: own readout floor; a step point additionally pays motor settle.
    OVERHEAD_FLY_S = 0.033
    OVERHEAD_STEP_S = 0.5

    def plan(self, req: ScanRequest, axes, overhead_s=None):
        """Point count and time estimate. Pure: no hardware is touched.

        `axes` is a sequence of (positions, exposure_s) pairs, already
        resolved by the caller -- this reads no widgets.
        """
        arrays = [a for a, _t in axes]
        is_fly = req.kind.startswith("fly") or req.kind == "helix_fly"
        kind = ("fly_snake" if req.snake and is_fly
                else "fly_hexapod_1d" if is_fly else "step")
        n = scanplan.count_positions(arrays, kind)
        per_point = max((t for _a, t in axes), default=0.0)
        if overhead_s is None:
            overhead_s = self.OVERHEAD_FLY_S if is_fly else self.OVERHEAD_STEP_S
        return {"positions": n, "per_point_s": per_point,
                "overhead_s": overhead_s, "scan_kind": kind,
                "estimate_s": n * (per_point + overhead_s),
                "estimate": scanplan.format_duration(
                    n * (per_point + overhead_s))}

    # -- check ---------------------------------------------------------------

    def check(self, plan, *, velocities=None, pulses_per_step=1,
              hdf_plugin_enabled=True, saxs_mode=None, unattended=False):
        """Every Guard that applies, in the order a human would meet them."""
        found = []
        for guard in (
            guard_checks.velocity_limits(velocities or {}),
            guard_checks.multi_pulse_needs_hdf(pulses_per_step,
                                               hdf_plugin_enabled),
            guard_checks.saxs_detector_mode(saxs_mode) if saxs_mode is not None else None,
            guard_checks.large_scan(plan["positions"], plan["per_point_s"],
                                    plan.get("overhead_s", 0.0),
                                    unattended=unattended),
        ):
            if guard is not None:
                found.append(guard)
        return found

    # -- run -----------------------------------------------------------------

    def start(self, req: ScanRequest, *, origin="") -> str:
        """Submit the scan as a job. Returns its id immediately.

        The caller does not block, and progress reaches every connected
        client rather than only whoever asked.
        """
        if self._runner is None:
            raise RuntimeError(
                "no scan runner is attached; this server cannot execute "
                "scans (the GUI attaches one when it connects)")
        kind = SCAN_KINDS.get(req.kind)
        if kind is None:
            raise KeyError(f"unknown scan kind {req.kind!r}")

        runner = self._runner

        def body(token, progress):
            # The runner's own stop flag is what the executors poll between
            # steps, so the job's token has to drive it rather than being a
            # second, separate way to stop.
            runner.bind_stop_token(token)
            try:
                return runner.run_scan(req, progress=progress, token=token)
            finally:
                runner.bind_stop_token(None)

        handle = self._session.jobs.submit(
            kind, body, origin=origin, params=req.describe())
        self._log.info("scan %s started as %s (%s)", req.kind,
                       handle.job_id, kind)
        return handle.job_id

    def active(self):
        return self._session.jobs.snapshot()

    def stop(self, job_id=None):
        """Ask a running step scan to stop. A fly scan cannot be asked."""
        asked = self._session.jobs.request_stop(job_id)
        return {"asked": asked,
                "fly_in_progress": self._session.jobs.fly_active()}
