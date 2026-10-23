"""Scan execution, moving out of the Qt handler one tranche at a time.

These methods came from gui/handlers/scan_handler.py unchanged except for
one rename: `self.w` became `self.ctx`. That is the whole trick. Walking
the call graph showed the eight executors and the helpers they reach need
40 names from the GUI window, and that those names are overwhelmingly
state rather than widgets -- messages, pts, parameters, motornames,
softglue, dg645, detector and a dozen flags. ScanContext (in
ptychosaxs/api/scans.py) presents exactly those, session first and window
second, so a method can move before the state it uses has, and each name
can migrate to BeamlineSession afterwards without touching an executor
again.

ScanHandler inherits this, so every one of its several hundred internal
callers is unaffected by a method moving. Class attributes the methods
reach for -- OVERHEAD_FLY, MAX_VELOCITY and the rest -- still resolve
through self on the handler.

Moved in small commits on purpose. None of this can be meaningfully
exercised without beam, so when something turns out to be wrong the useful
thing is a small diff to point at rather than a green test run.
"""

import csv
import os
import pathlib
import threading
import time
import traceback

import numpy as np

from ptychosaxs.api import optics_status as _optics_status
from ptychosaxs.api import scanplan  # noqa: F401  (used by moved methods)
from ptychosaxs.api.scan_config import (
    COUNT_DONE_TIMEOUT_MARGIN_S,
    COUNT_POLL_INTERVAL_S,
    DETECTOR_NOT_STARTED_ERROR,
    DETECTOR_READOUTTIME,
    SCALER_CNT_PV,
    SCALER_CONT_PV,
    SCALER_TP_PV,
    SCAN_SCALAR_PVS,
)

try:  # pragma: no cover - absent only where pyepics is not installed
    import epics
    from epics import caget
except ImportError:  # pragma: no cover
    epics = None
    caget = None

try:  # pragma: no cover
    import h5py
except ImportError:  # pragma: no cover
    h5py = None


class ScanExecutorsMixin:
    """The parts of scan execution that have left the GUI handler."""

    def _scaler_oneshot_begin(self, exp_time):
        """Put the scaler into OneShot mode with `exp_time` as its preset,
        mirroring ScalarScanWindow._start_scan.

        Returns True if the scaler is ours for the rest of the scan, in which
        case _scaler_oneshot_restore must run on every exit path.
        """
        try:
            import epics
        except ImportError:
            print("Warning: pyepics not available, scaler counts not collected")
            return False
        try:
            epics.caput(SCALER_CONT_PV, 0)  # OneShot, for the duration of this scan only
            epics.caput(SCALER_TP_PV, exp_time)
        except Exception as e:
            print(f"Warning: could not put scaler into OneShot mode: {e}")
            return False
        return True

    def _scaler_oneshot_count(self, exp_time):
        """Trigger one OneShot count, block until the scaler reports it done,
        then read the channels once.

        Mirrors _ScanWorker._wait_for_count_done / _read_scalars_into in
        scalar_scan.py. Returns one row of floats in SCAN_SCALAR_PVS order,
        using nan for anything that could not be read so the csv keeps one row
        per scan position even when the scaler misbehaves.
        """
        nan_row = [float("nan")] * len(SCAN_SCALAR_PVS)
        timeout = exp_time + COUNT_DONE_TIMEOUT_MARGIN_S
        try:
            import epics

            # Put-completion: the scaler record acknowledges .CNT only once the
            # count has finished, so this blocks for the whole exposure. A plain
            # non-blocking put would return before the IOC had even started
            # counting, and since the record zeroes .S1-.SN at count start, the
            # read below would then return 0 for every channel.
            epics.caput(SCALER_CNT_PV, 1, wait=True, timeout=timeout)

            # Backstop in case this IOC does not honour put-completion on .CNT.
            # use_monitor=False forces a fresh CA get rather than a possibly
            # stale monitor-cached value.
            deadline = time.monotonic() + timeout
            while epics.caget(SCALER_CNT_PV, use_monitor=False):
                if time.monotonic() > deadline:
                    print("Warning: timed out waiting for scaler count to finish")
                    return nan_row
                time.sleep(COUNT_POLL_INTERVAL_S)
        except Exception as e:
            print(f"Warning: scaler count failed: {e}")
            return nan_row

        row = []
        for name, pv_name in SCAN_SCALAR_PVS.items():
            try:
                value = epics.caget(pv_name, use_monitor=False)
            except Exception as e:
                print(f"Warning: could not read {name} ({pv_name}): {e}")
                value = None
            row.append(float("nan") if value is None else float(value))
        return row

    def _scaler_oneshot_restore(self):
        """Put the scaler back into AutoCount - the counterpart to
        _scaler_oneshot_begin, so a stopped or errored scan never leaves the
        scaler stuck out of free-running for other clients."""
        try:
            import epics

            epics.caput(SCALER_CONT_PV, 1)
        except Exception as e:
            print(f"Warning: could not restore scaler to free-running: {e}")

    @staticmethod
    def _signed_step(ast: float, afe: float, step: float) -> float:
        """See ptychosaxs.api.scanplan.signed_step."""
        return scanplan.signed_step(ast, afe, step)

    def _make_positions(
        self, p0: float, st: float, fe: float, step: float
    ) -> np.ndarray:
        """See ptychosaxs.api.scanplan.make_positions."""
        return scanplan.make_positions(p0, st, fe, step)

    @staticmethod
    def _snake_positions(x_positions, y_positions):
        """See ptychosaxs.api.scanplan.snake_positions."""
        return scanplan.snake_positions(x_positions, y_positions)

    def _extend_axis_positions(self, coords: np.ndarray, step: float, axis_index: int, scan_kind: str) -> np.ndarray:
        """See ptychosaxs.api.scanplan.extend_axis_positions."""
        return scanplan.extend_axis_positions(coords, step, axis_index, scan_kind)

    def _compute_n_positions(self, position_arrays, scan_kind="step"):
        """See ptychosaxs.api.scanplan.count_positions."""
        return scanplan.count_positions(position_arrays, scan_kind)

    @staticmethod
    def _join_posix(base, *parts) -> str:
        """Join into a Linux path, dropping empty components.

        `base` keeps any leading "/" it came with, so an absolute basepath
        stays absolute; every later component is stripped of its own
        separators so no "//" or trailing "/" can creep in.
        """
        out = str(base).replace("\\", "/").rstrip("/")
        for part in parts:
            part = str(part).replace("\\", "/").strip("/")
            if part:
                out = "%s/%s" % (out, part) if out else part
        return out

    def _format_scan_axis_string(self, axes_params, key):
        mapping = {
            "scan_center": "p0",
            "scan_negative_edge": "st",
            "scan_positive_edge": "fe",
            "scan_step_size": "step",
        }
        if key not in mapping:
            return ""
        parts = []
        for ax in axes_params:
            val = ax.get(mapping[key], "")
            try:
                formatted = f"{float(val):f}"
            except (TypeError, ValueError):
                formatted = ""
            parts.append(f"{ax['name']} {formatted} ")
        return "".join(parts)

    def _get_scan_summary_csv_path(self):
        if len(self.ctx.parameters.logfilename) == 0:
            return None
        return pathlib.Path(self.ctx.parameters.logfilename).with_suffix(".csv")

    def _piezo_axis_units(self, axes) -> str:
        """Engineering unit of each scanned piezo axis, as the controller
        reports it. A scan may pair a translation with a tilt, so these are
        not always the same unit."""
        gonio = self.ctx.pts.gonio
        units = []
        for axis in axes:
            try:
                units.append(gonio.motorunits[gonio.motornames.index(axis)])
            except Exception:
                units.append("")
        return units[0] if len(set(units)) == 1 else ",".join(units)

    def _get_mono_energy(self):
        try:
            import epics
        except ImportError:
            return ""
        try:
            val = epics.caget("12ida2:EnCalc")
        except Exception:
            return ""
        if val is None:
            return ""
        return str(val)

    def _get_gonio_trans_positions(self):
        values = []
        for axis in self._GONIO_TRANS_AXES:
            try:
                val = self.ctx.pts.get_pos(axis) if axis in self.ctx.pts.gonio.motornames else None
            except Exception:
                val = None
            values.append("" if val is None else str(val))
        return values

    def _get_us_optics_positions(self):
        try:
            import epics
        except ImportError:
            return [""] * len(self._US_OPTICS_PVS)
        values = []
        for _, pv in self._US_OPTICS_PVS:
            try:
                val = epics.caget(pv)
            except Exception:
                val = None
            values.append("" if val is None else str(val))
        return values

    def _get_detector_config(self, detector_type: str) -> dict:
        """Get the configuration dict for a detector type.

        Args:
            detector_type: 'SAXS', 'WAXS', etc.

        Returns:
            Configuration dictionary with PVs, constants, and metadata mapping
        """
        from .nexus_metadata_config import DETECTOR_CONFIGS

        if detector_type not in DETECTOR_CONFIGS:
            raise ValueError(f"Unknown detector type: {detector_type}")
        return DETECTOR_CONFIGS[detector_type]

    def _step_detector_period(self, expt: float) -> float:
        """The camera exposure period to program for a step scan.

        AcquirePeriod is camserver's `expperiod`: the floor on how closely
        two exposures may follow one another. Step mode never set it -- only
        fly_ready did -- so the camera kept whatever a previous fly scan or
        alignment had left behind, and a stale value put that floor under
        every point of the scan regardless of the exposure time.

        Deliberately the same formula as the DG645's Cycperiod below: a
        camera period longer than the trigger spacing would make the detector
        ignore triggers, which reads as a frame timeout rather than as a
        misconfiguration.
        """
        return round(max(expt + DETECTOR_READOUTTIME, 0.03), 6)

    def _get_scan_summary_header(self):
        return [
            "scan",
            "completed",
            "date",
            "time",
            "sample_name",
            "scan_type",
            "ExpTime",
            "n_pos",
            "monoE",
            "phi",
            "detectors_used",
            "scan_center",
            "scan_negative_edge",
            "scan_positive_edge",
            "scan_step_size",
            # Same list, same order, as the values spliced in by
            # _write_scan_summary_line via _get_us_optics_positions.
            *_optics_status.US_OPTICS_MOTORS,
            "trans1",
            "trans2",
            # Appended at the end on purpose: _migrate_scan_summary_header
            # can only ever append, so a column inserted mid-list would land
            # at the end of an existing CSV's header and desync it from the
            # rows written underneath.
            "optics_name",
        ]

    def _emit_progress(
        self,
        t0: float,
        i: int,
        N: int,
        update_progress,
        update_status,
        t_scanstart: float = None,
        progress_3d=None,
    ) -> None:
        """Emit progress-bar and status-bar updates for step scans.

        Handles both standalone 2-D and 3-D-slice contexts:
          - Standalone (progress_3d is None): fraction = (i+1)/N
          - 3-D slice (progress_3d = (slice_index, total_slices)):
              fraction = (N*slice + (i+1)) / (N*total_slices)
              elapsed time is measured from t_scanstart (the 3-D scan start).

        t0          — executor start time (time.time() at top of executor)
        t_scanstart — overall 3-D scan start time; only used when progress_3d set
        """
        if progress_3d is not None and t_scanstart is not None:
            c3d, all3d = progress_3d
            frac = (N * c3d + (i + 1)) / (N * all3d)
            elapsed = time.time() - t_scanstart
        else:
            frac = (i + 1) / N if N > 0 else 1.0
            elapsed = time.time() - t0
        frac = max(frac, 1e-6)
        remaining = elapsed / frac - elapsed
        if update_progress:
            update_progress(int(frac * 100))
        if update_status:
            update_status(
                f"Point {i + 1}/{N} — {elapsed:.0f}s elapsed, ~{remaining:.1f}s remaining"
            )

    def _emit_axis_positions(self, *axes):
        """Push each axis's settled position out on the motion signals the
        GUI's position labels listen to."""
        signals = getattr(self.ctx.pts, "signals", None)
        if signals is None:
            return
        for axis in axes:
            signals.AxisNameSignal.emit(axis)
            signals.AxisPosSignal.emit(self.ctx.pts.get_pos(axis))

    def _scan_mv(self, axis, target, wait=True, update_status=None):
        """Move a motor during a scan without letting a stage-motion timeout abort it.

        pts.mv() raises if a move doesn't settle within its internal 10 s
        window (TimeoutError), or if the hexapod still will not move after
        its servo-recovery attempts (RuntimeError). Uncaught, either
        propagates out of the worker-thread scan executor into Worker.run()
        (rungui.py), which treats any exception as fatal and aborts the whole
        scan. A single slow/stalled move shouldn't lose an entire scan's
        worth of data, so scan executors call this instead of
        self.ctx.pts.mv() directly and just log + continue.

        Deliberately does NOT set self.ctx.messages["recent error message"] —
        that dict key is wired (see _ErrorDict in rungui.py) to flash the GUI's
        red scan-status error box, which is more alarm than a tolerated,
        continued-past timeout warrants. Prints to the terminal and (if
        available) posts to the status bar instead.
        """
        try:
            self.ctx.pts.mv(axis, target, wait=wait)
        except (TimeoutError, RuntimeError) as exc:
            msg = (
                f"Stage motion failed moving {axis} to {target} "
                f"({exc}) — continuing scan. {time.ctime()}"
            )
            print(msg)

    def is_arming_detecotors_timedout(self):
        TIMEOUT = 10
        t_start = time.time()
        timeout_occurred = False
        for detN, det in enumerate(self.ctx.detector):
            if det is not None:
                if self.ctx.parameters._pulses_per_step > 1:
                    while det.Armed == 0 or det.getCapture() == 0:
                        det.StartCapture()
                        wait_for_det_capture_arm_s = 0.1  # retry interval while waiting for detector capture to arm
                        time.sleep(wait_for_det_capture_arm_s)
                        if (time.time() - t_start) > TIMEOUT:
                            timeout_occurred = True
                            print(
                                f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds."
                            )
                            break
                else:
                    while det.Armed == 0:
                        det.Arm()
                        wait_for_det_arm_s = (
                            0.1  # retry interval while waiting for detector to arm
                        )
                        time.sleep(wait_for_det_arm_s)
                        if (time.time() - t_start) > TIMEOUT:
                            timeout_occurred = True
                            print(
                                f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds."
                            )
                            break
                    if timeout_occurred:
                        print("Breaking out of detector loop due to timeout.")
                        break
        return timeout_occurred, TIMEOUT

    def is_waiting_detectors_timedout(self, expt, i):
        if self.ctx.parameters._pulses_per_step > 1.5:
            TIMEOUT = (expt + 0.03) * self.ctx.parameters._pulses_per_step + 10
        else:
            TIMEOUT = expt + 3
        t_start = time.time()
        timeout_occurred = False
        for ndet, det in enumerate(self.ctx.detector):
            if ndet > 1:
                continue
            if det is not None:
                while det.ArrayCounter_RBV < self.ctx.parameters._pulses_per_step * (
                    i + 1
                ):
                    wait_for_det_frame_s = 0.02  # poll interval while waiting for detector to collect the expected frame count
                    time.sleep(wait_for_det_frame_s)
                    if (time.time() - t_start) > TIMEOUT:
                        timeout_occurred = True
                        print(
                            f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds."
                        )
                        break
                if timeout_occurred:
                    print("Breaking out of detector loop due to timeout.")
                    break
        return timeout_occurred, TIMEOUT

    def _detector_camserver_error(self):
        """The camera's own error text when it is sitting in an error state.

        A camserver failure stops frames arriving, which the fly wait loop
        can only see as silence - it then waits out the full stall window
        (30 s for the last frame) with nothing to report. Asking the
        detector directly turns that into an immediate, explanatory failure.
        Empty string when the detector is healthy or unreadable.
        """
        from epics import caget

        for ndet, det in enumerate(self.ctx.detector):
            if ndet > 1 or det is None:
                continue
            prefix = getattr(det, "_prefix", "")
            if not prefix:
                continue
            base = prefix if prefix.endswith("cam1:") else prefix + "cam1:"
            try:
                if caget(base + "DetectorState_RBV", as_string=True,
                         timeout=1) != "Error":
                    return ""
                return "%s / %s" % (
                    caget(base + "StatusMessage_RBV", as_string=True, timeout=1),
                    caget(base + "StringFromServer_RBV", as_string=True, timeout=1),
                )
            except Exception:
                return ""
        return ""

    def refresh_detectors(self, update_status=None):
        """Refresh the detectors to ensure they are ready for the next scan."""
        stata = 1
        for detN, det in enumerate(self.ctx.detector):
            if detN > 1:
                continue
            if det is not None:
                scaninfo = []
                scaninfo.append("\n")
                scaninfo.append(f"#I {det._prefix} IOC error at %{time.ctime()}.\n")
                m1, m2, m3 = det.getMessages()
                scaninfo.append(f"{m1}\n{m2}\n{m3}")
                scaninfo.append("\n")
                self.ctx.write_scaninfo_to_logfile(scaninfo)
                try:
                    status = (
                        det.refresh()
                    )  # if failed, it will return 0. ohterwise it will return 1.
                    stata = stata * status
                except Exception as e:
                    msg = f"Error refreshing detector {det._prefix}: {e}"
                    print(msg)
                    if update_status:
                        update_status(msg)
        return stata

    def wait_for_beam(self, update_status, value):
        ct0 = time.time()
        while self.ctx.isOK2run is not True:
            wait_for_beam_poll_s = 10  # poll interval while waiting for beam to return
            time.sleep(wait_for_beam_poll_s)
            self.ctx.messages["current status"] = (
                f"Beam has been down for {int((time.time() - ct0) / 60)} minutes. {time.ctime()}"
            )
            update_status(self.ctx.messages["current status"])
            if self.isStopScanIssued:
                break
        # Need some action after shutter back up
        self.ctx.shutter.open_A()
        self.ctx.messages["current status"] = (
            f"Beam just came back. A-shutter open command was sent and run will resume in 10mins. {time.ctime()}"
        )
        update_status(self.ctx.messages["current status"])
        wait_for_shutter_stabilize_s = 60  # wait after opening A-shutter before reopening to allow beam to stabilize
        time.sleep(wait_for_shutter_stabilize_s)
        self.ctx.shutter.open_A()
        wait_for_beam_warmup_s = (
            60 * 9
        )  # additional warm-up wait after beam returns before resuming scan
        time.sleep(wait_for_beam_warmup_s)
        scaninfo = []
        scaninfo.append("\n")
        scaninfo.append(
            "#Note: Shutter has been closed for %i mins" % int((time.time() - ct0) / 60)
        )
        scaninfo.append("#Note: angle %0.3f will be re-run" % value)
        self.ctx.write_scaninfo_to_logfile(scaninfo)

    def _fetch_detector_epics_metadata(self, detector_config: dict) -> dict:
        """Query detector-specific EPICS metadata.

        Args:
            detector_config: Configuration dict for this detector

        Returns:
            Dictionary {nexus_path: value} for all detector PVs that succeeded.
            Skips any PV that returns None.
        """
        try:
            import epics
        except ImportError:
            print("Warning: pyepics not available, returning empty metadata")
            return {}

        metadata = {}
        detector_pvs = detector_config.get("detector_pvs", {})

        for nexus_path, pv_info in detector_pvs.items():
            pv_name = pv_info["pv"]
            try:
                value = epics.caget(pv_name)
                if value is not None:
                    metadata[nexus_path] = value
            except Exception as e:
                print(f"Warning: Failed to query detector PV {pv_name}: {e}")

        return metadata

    def _fetch_shared_epics_metadata(self, cached_zone_plate_optics: dict = None) -> dict:
        """Query all shared EPICS metadata (same for all detectors).

        Args:
            cached_zone_plate_optics: Pre-fetched zone plate optics metadata to avoid duplicate queries.
                                      If provided, use this instead of querying again.

        Returns:
            Dictionary {nexus_path: value} for all PVs that succeeded.
            Skips any PV that returns None.
        """
        from .nexus_metadata_config import SHARED_METADATA_MAP, SLITS_METADATA_MAP, ZONE_PLATE_METADATA_MAP

        try:
            import epics
        except ImportError:
            print("Warning: pyepics not available, returning empty metadata")
            return {}

        metadata = {}

        # Query shared metadata (beam, source, sample motors, scalars)
        for nexus_path, pv_info in SHARED_METADATA_MAP.items():
            pv_name = pv_info["pv"]
            try:
                value = epics.caget(pv_name)
                if value is not None:
                    metadata[nexus_path] = value
            except Exception as e:
                print(f"Warning: Failed to query PV {pv_name}: {e}")

        # Query slits (conditional on checkbox)
        save_us_optics = getattr(self.ctx.parameters, "_save_us_optics", False)
        if save_us_optics:
            slits_success = {}
            for nexus_path, pv_info in SLITS_METADATA_MAP.items():
                pv_name = pv_info["pv"]
                try:
                    value = epics.caget(pv_name)
                    if value is not None:
                        slits_success[nexus_path] = value
                except Exception as e:
                    print(f"Warning: Failed to query slit PV {pv_name}: {e}")
            # Add slits only if at least one succeeded
            if slits_success:
                metadata.update(slits_success)

        # Use cached zone plate optics if provided, otherwise query
        if cached_zone_plate_optics is not None:
            metadata.update(cached_zone_plate_optics)
        elif save_us_optics:
            zp_success = {}
            for nexus_path, pv_info in ZONE_PLATE_METADATA_MAP.items():
                pv_name = pv_info["pv"]
                try:
                    value = epics.caget(pv_name)
                    if value is not None:
                        zp_success[nexus_path] = value
                except Exception as e:
                    print(f"Warning: Failed to query zone plate PV {pv_name}: {e}")
            # Add zone plate only if at least one succeeded
            if zp_success:
                metadata.update(zp_success)

        # Query the goniometer translation stages through self.ctx.pts, so these
        # read back from the same place every other motion call does.
        for nexus_path, axis in (
            ('/entry/sample/trans1', 'trans1'),
            ('/entry/sample/trans2', 'trans2'),
        ):
            try:
                if axis in self.ctx.pts.gonio.motornames:
                    metadata[nexus_path] = self.ctx.pts.get_pos(axis)
            except Exception as e:
                print(f"Warning: Failed to query gonio axis {axis}: {e}")

        return metadata

    def _append_hexapod_positions_to_master_file(self, master_path: str, hexapod_pos: np.ndarray) -> None:
        """Write the hexapod-predicted position array to an already-created master file.

        Reopens the master file (created earlier in _pre_scan) in append mode
        and adds /entry/sample/hexapod_positions alongside the software-nominal
        /entry/sample/positions dataset.
        """
        import h5py

        try:
            with h5py.File(master_path, 'r+') as f:
                sample = f['/entry/sample']
                if 'hexapod_positions' in sample:
                    del sample['hexapod_positions']
                sample.create_dataset('hexapod_positions', data=hexapod_pos)
                sample['hexapod_positions'].attrs['units'] = b'mm'
                sample['hexapod_positions'].attrs['description'] = (
                    b'Nx2 array of (X, Z) positions predicted from the programmed '
                    b'hexapod trajectory (step size + phantom trigger + even-row '
                    b'padding), verified equal to hexapod.pulse_number'
                )
        except Exception as e:
            print(f"Warning: Failed to write hexapod_positions to master file {master_path}: {e}")

    def _scan_mv_xy(self, xaxis, xp, yaxis, yp, update_status=None):
        """Move a scan's two grid axes to one point.

        Moving them one after the other walks the stage through a corner
        position on the way to every point and pays two settle times. Axis
        pairs that share a controller able to start both at once take that
        path instead; everything else falls back to two sequential moves.

        Which of those applies is pts.mv_xy's decision, not this function's —
        it knows which controller owns each axis and whether that controller
        can start them together. That is what lets the scan axes be swapped
        without touching the scan engine.

        Like _scan_mv, any failure here is logged and the scan carries on —
        one bad point should not lose a whole run.
        """
        if not self.ctx.pts.can_coordinate(xaxis, yaxis):
            # Two independent moves, each with its own error handling: a
            # failure on one axis must not also skip the other.
            self._scan_mv(xaxis, xp, update_status=update_status)
            self._scan_mv(yaxis, yp, update_status=update_status)
            return
        try:
            ok = self.ctx.pts.mv_xy(
                xaxis, xp, yaxis, yp, wait=True,
                timeout=self.COORDINATED_MOVE_TIMEOUT_S,
            )
        except Exception as e:
            print(
                f"Move of {xaxis}/{yaxis} to ({xp}, {yp}) failed "
                f"({e}) — continuing scan. {time.ctime()}"
            )
            return
        if not ok:
            print(
                f"Stage motion timeout moving {xaxis}/{yaxis} to "
                f"({xp}, {yp}) — continuing scan. {time.ctime()}"
            )
            return
        # pts.mv emits these as it moves; a coordinated move bypasses it,
        # so the position readouts would otherwise freeze for the scan.
        self._emit_axis_positions(xaxis, yaxis)

    def fly_traj(self, xmotor=0, ymotor=-1):
        """Program the hexapod trajectory before starting a fly scan worker.

        Called from the GUI thread in the fly / fly2d / fly3d entry points, before
        the Worker thread is started.  Sets self.Xaxis (and self.Yaxis for 2-D),
        computes the step_time from the user's exposure time and idle time, updates
        _ratio_exp_period so fly0 / fly2d0_SNAKE can compute the actual exposure
        from the hexapod's measured pulse_step, and programs the waveform on the
        hexapod controller.

        ymotor=-1 → 1-D fly (programs a standard trajectory along X only)
        ymotor≥0  → 2-D SNAKE fly (programs a full 2-D snake trajectory)
        """
        # Read X axis parameters from the UI.
        n = xmotor + 1
        Xaxis = self.ctx.motornames[xmotor]
        Xst = self.fly1d_st + self.fly1d_p0  # absolute start
        Xfe = self.fly1d_fe + self.fly1d_p0  # absolute end
        Xstep = self.fly1d_step  # step distance (mm)
        Xtm = self.fly1d_tm  # user exposure time (s)

        # Compute step_time from the user-set acquisition time, clamped to safe floors.
        # The period must be at least as long as the detector readout so that
        # the next trigger does not arrive before the previous frame is read out,
        # and at least 33 ms (Pilatus 2M limit of 30 Hz).
        fly_acq_time = getattr(self.ctx.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
        step_time = max(fly_acq_time, Xtm + DETECTOR_READOUTTIME, self.OVERHEAD_FLY)
        # The hexapod wavetable clock is 1 ms/bin. Round step_time to the nearest
        # whole millisecond so that the pulse_period passed to make_pulse_arrays is
        # an exact integer number of bins. Without this, floating-point accumulation
        # in make_pulse_arrays causes int() truncation to occasionally produce a step
        # one bin short (e.g. 32 ms instead of 33 ms), violating the Pilatus minimum.
        step_time = round(step_time * 1000) / 1000

        # Store ratio_exp_period so fly0 can recover expt from hexapod.pulse_step.
        # (hexapod.pulse_step is measured after the trajectory starts, so we cannot
        # compute expt exactly until then; _ratio_exp_period bridges the gap.)
        self.ctx.parameters._ratio_exp_period = Xtm / step_time

        # Store axis labels for use by fly2d0_SNAKE (which runs on the worker thread
        # and cannot safely re-read the UI).
        self.Xaxis = Xaxis
        self.Xi = Xst
        self.Xf = Xfe

        if ymotor >= 0:
            # 2-D SNAKE: read Y parameters and program the 2-D snake waveform.
            Yaxis = self.ctx.motornames[ymotor]
            Yst = self.fly2d_st + self.fly2d_p0
            Yfe = self.fly2d_fe + self.fly2d_p0
            Ystep = self.fly2d_step

            self.Yaxis = Yaxis
            self.Yi = Yst
            self.Yf = Yfe

            # set_traj_SNAKE2 programs both axes simultaneously: the hexapod
            # will execute a boustrophedon (snake) path covering the full X×Y grid.
            self.ctx.pts.hexapod.set_traj_SNAKE2(
                step_time, Xst, Xfe - Xst, Xstep, Yst, Yfe, Ystep
            )

            minstep, commonstep = self.ctx.pts.hexapod.analyze_pulse_steps()
            if minstep != commonstep:
                binsize = 0.001  # index-to-seconds conversion
                print(
                    f"Warning: most pulse steps are {commonstep * binsize * 1000:.1f} ms "
                    f"but some are as short as {minstep * binsize * 1000:.3f} ms. "
                    "Consider adjusting the fly step size to a multiple of the "
                    f"common step ({commonstep * binsize * 1000:.3f} ms)."
                )
        else:
            # 1-D fly: program a standard single-axis trajectory.
            # fly0 uses HEXAPOD_FLYMODE_WAVELET which calls assign_axis2wavtable
            # and run_traj, so set_traj here primes the standard (non-wavelet) path.
            # Use the same rounding-based formula as _make_positions to avoid
            # truncation edge cases when step doesn't evenly divide the range.
            positions = self._make_positions(Xst, 0, Xfe - Xst, Xstep)
            Nsteps = len(positions)
            total_time = Nsteps * step_time
            self.ctx.pts.hexapod.set_traj(
                Xaxis, total_time, Xfe - Xst, Xst, 1, abs(step_time), 50
            )

    def _compute_helix_total_time(self, phi_st: float, phi_fe: float, phi_step: float, phi_tm: float) -> tuple:
        """Return (total_time, Nsteps, step_time) for a helix fly scan's phi axis.

        Shared by helix_fly's pre-scan velocity guard (computed on the GUI thread
        before anything moves) and helix_fly0's DG645 arming (on the worker
        thread), so the two can never diverge.
        """
        fly_acq_time = getattr(self.ctx.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
        step_time = max(fly_acq_time, phi_tm + self.det_readout_time, self.OVERHEAD_FLY)
        Nsteps = self._compute_n_positions([self._make_positions(0, phi_st, phi_fe, phi_step)], scan_kind="fly_phi")
        total_time = Nsteps * step_time
        return total_time, Nsteps, step_time

    def _reconcile_hexapod_snake_positions(self, write_to_master: bool) -> np.ndarray:
        """Build the hexapod-predicted position array for the just-programmed
        2-D snake trajectory, verify its length against hexapod.pulse_number,
        write it into the master file(s), and record the pulse count in the CSV.

        Must be called immediately after fly_traj() programs set_traj_SNAKE2
        (the earliest point hexapod.pulse_number is known), before the
        trajectory is actually run. Reads only the cached fly1d_*/fly2d_*
        instance attributes fly_traj() itself relies on — no live Qt widget
        reads — so this is safe to call from both the GUI thread (fly2d) and
        the worker thread (fly3d0's per-phi-slice loop).

        """
        x_st = self.fly1d_st + self.fly1d_p0
        x_fe = self.fly1d_fe + self.fly1d_p0
        x_step = self.fly1d_step
        y_st = self.fly2d_st + self.fly2d_p0
        y_fe = self.fly2d_fe + self.fly2d_p0
        y_step = self.fly2d_step

        x_coords = self._make_positions(0, x_st, x_fe, x_step)
        y_coords = self._make_positions(0, y_st, y_fe, y_step)
        x_step_signed = self._signed_step(x_st, x_fe, x_step)
        y_step_signed = self._signed_step(y_st, y_fe, y_step)
        x_coords = self._extend_axis_positions(x_coords, x_step_signed, 0, "fly_snake")
        y_coords = self._extend_axis_positions(y_coords, y_step_signed, 1, "fly_snake")
        hexapod_pos = self._snake_positions(x_coords, y_coords)
        if len(hexapod_pos) > 0:
            hexapod_pos = hexapod_pos - np.mean(hexapod_pos, axis=0)  # match nominal 'positions' centering

        if write_to_master:
            for master_path in getattr(self, "_current_scan_master_paths", {}).values():
                self._append_hexapod_positions_to_master_file(master_path, hexapod_pos)
        return hexapod_pos

    def _append_piezo_positions_to_master_file(
        self, master_path: str, piezo_pos: np.ndarray, axes
    ) -> None:
        """Write the piezo-stage position array to an already-created master file.

        Unlike hexapod_positions (predicted from the programmed trajectory),
        piezo_pos holds positions actually read back from the stages after each
        point's move settled, since step-scan moves are closed-loop but not
        guaranteed pulse-exact like the hexapod's fly trajectory.

        Reopens the master file (created earlier in _pre_scan) in append mode
        and adds /entry/sample/piezo_positions alongside the software-nominal
        /entry/sample/positions dataset.
        """
        import h5py

        axes_label = ", ".join(axes)
        try:
            with h5py.File(master_path, 'r+') as f:
                sample = f['/entry/sample']
                if 'piezo_positions' in sample:
                    del sample['piezo_positions']
                sample.create_dataset('piezo_positions', data=piezo_pos)
                sample['piezo_positions'].attrs['units'] = (
                    self._piezo_axis_units(axes).encode('utf-8')
                )
                sample['piezo_positions'].attrs['axes'] = axes_label.encode('utf-8')
                sample['piezo_positions'].attrs['description'] = (
                    f'Nx2 array of ({axes_label}) positions read back from the '
                    f'piezo stages after each scan point'
                ).encode('utf-8')
        except Exception as e:
            print(f"Warning: Failed to write piezo_positions to master file {master_path}: {e}")

    def _populate_data_group(self, entry, sample_name: str) -> None:
        """Create /entry/data group structure (links added post-scan).

        Args:
            entry: HDF5 entry group
            sample_name: Sample name to be stored
        """
        data = entry.create_group('data')
        data.attrs['NX_class'] = b'NXdata'
        data.attrs['signal'] = b'data'
        data.attrs['axes'] = b'. .'

        # Store sample name
        data.create_dataset('sample_name', data=sample_name.encode('utf-8'))

        # Constant metadata
        data.create_dataset('local_name', data=b'APS')
        data.create_dataset('make', data=b'Dectris')
        data.create_dataset('model', data=b'Pilatus')

    def _populate_sample_group(self, entry, shared_meta: dict, scan_positions_dict: dict) -> None:
        """Create /entry/sample group with motor positions and scan position arrays.

        Args:
            entry: HDF5 entry group
            shared_meta: Shared metadata dict (contains sth, stv, theta, trans1, trans2)
            scan_positions_dict: {motor_name: np.ndarray} from _compute_scan_positions
        """
        from .nexus_metadata_config import SHARED_METADATA_MAP

        sample = entry.create_group('sample')
        sample.attrs['NX_class'] = b'NXsample'

        # Write sample name (will be set by caller)
        sample.create_dataset('sample_name', data=b'')

        # Write constant motor positions from EPICS
        if '/entry/sample/sth' in shared_meta:
            val = shared_meta['/entry/sample/sth']
            sample.create_dataset('sth', data=val)
            sample['sth'].attrs['units'] = b'mm'

        if '/entry/sample/stv' in shared_meta:
            val = shared_meta['/entry/sample/stv']
            sample.create_dataset('stv', data=val)
            sample['stv'].attrs['units'] = b'mm'

        if '/entry/sample/theta' in shared_meta:
            val = shared_meta['/entry/sample/theta']
            sample.create_dataset('theta', data=val)
            sample['theta'].attrs['units'] = b'degree'

        # Write SmarAct goniometer translation stage positions
        if '/entry/sample/trans1' in shared_meta:
            val = shared_meta['/entry/sample/trans1']
            sample.create_dataset('trans1', data=val)
            sample['trans1'].attrs['units'] = b'mm'

        if '/entry/sample/trans2' in shared_meta:
            val = shared_meta['/entry/sample/trans2']
            sample.create_dataset('trans2', data=val)
            sample['trans2'].attrs['units'] = b'mm'

        # Write scan position array (Nx2 array of (x, y) positions in scan order)
        if 'positions' in scan_positions_dict:
            pos_array = scan_positions_dict['positions']
            sample.create_dataset('positions', data=pos_array)
            sample['positions'].attrs['units'] = b'mm'
            sample['positions'].attrs['description'] = b'Nx2 array of (X, Z) motor positions in scan traversal order'

    def _populate_scalars_group(self, entry, shared_meta: dict) -> None:
        """Create /entry/scalars group with detector scalars.

        Args:
            entry: HDF5 entry group
            shared_meta: Shared metadata dict (contains IC, BS, BS2, IfCRL)
        """
        scalars_data = {k: v for k, v in shared_meta.items() if k.startswith('/entry/scalars/')}

        if scalars_data:
            scalars = entry.create_group('scalars')
            scalars.attrs['NX_class'] = b'NXcollection'

            for nexus_path, value in scalars_data.items():
                dataset_name = nexus_path.split('/')[-1]
                scalars.create_dataset(dataset_name, data=value)
                scalars[dataset_name].attrs['units'] = b'counts'

    def _populate_instrument_group(self, entry, shared_meta: dict, detector_meta: dict,
                                   detector_config: dict) -> None:
        """Create /entry/instrument hierarchy with metadata.

        Args:
            entry: HDF5 entry group
            shared_meta: Shared metadata dict from _fetch_shared_epics_metadata
            detector_meta: Detector metadata dict from _fetch_detector_epics_metadata
            detector_config: Detector configuration dict
        """
        import h5py
        from .nexus_metadata_config import SHARED_METADATA_MAP, SLITS_METADATA_MAP, ZONE_PLATE_METADATA_MAP

        inst = entry.create_group('instrument')
        inst.attrs['NX_class'] = b'NXinstrument'
        inst.create_dataset('name', data=detector_config['name'].encode('utf-8'))

        # Create beam group
        beam = inst.create_group('beam')
        beam.attrs['NX_class'] = b'NXbeam'
        if '/entry/instrument/beam/incident_wavelength' in shared_meta:
            val = shared_meta['/entry/instrument/beam/incident_wavelength']
            beam.create_dataset('incident_wavelength', data=val)
            beam['incident_wavelength'].attrs['units'] = b'angstrom'

        # Create monochromator group
        mono = inst.create_group('monochromator')
        mono.attrs['NX_class'] = b'NXmonochromator'
        if '/entry/instrument/monochromator/monoE' in shared_meta:
            val = shared_meta['/entry/instrument/monochromator/monoE']
            mono.create_dataset('monoE', data=val)
            mono['monoE'].attrs['units'] = b'keV'

        # Create source group
        source = inst.create_group('source')
        source.attrs['NX_class'] = b'NXsource'
        source.create_dataset('facility_name', data=b'APS')
        source.create_dataset('facility_beamline', data=b'12-ID-E')
        source.create_dataset('facility_sector', data=b'12-ID')
        source.create_dataset('facility_station', data=b'E')
        source.create_dataset('name', data=b'Advanced Photon Source')
        source.create_dataset('type', data=b'Synchrotron')
        source.create_dataset('probe', data=b'x-ray')

        if '/entry/instrument/source/current' in shared_meta:
            val = shared_meta['/entry/instrument/source/current']
            source.create_dataset('current', data=val)
            source['current'].attrs['units'] = b'mA'

        if '/entry/instrument/source/undE' in shared_meta:
            val = shared_meta['/entry/instrument/source/undE']
            source.create_dataset('undE', data=val)
            source['undE'].attrs['units'] = b'keV'

        # Create detector group
        det = inst.create_group('detector')
        det.attrs['NX_class'] = b'NXdetector'
        det.create_dataset('description', data=detector_config['description'].encode('utf-8'))
        det.create_dataset('type', data=detector_config['type'].encode('utf-8'))
        det.create_dataset('sensor_material', data=detector_config['sensor_material'].encode('utf-8'))

        det.create_dataset('sensor_thickness', data=detector_config['sensor_thickness'])
        det['sensor_thickness'].attrs['units'] = b'mm'

        det.create_dataset('x_pixel_size', data=detector_config['x_pixel_size'])
        det['x_pixel_size'].attrs['units'] = b'mm'

        det.create_dataset('y_pixel_size', data=detector_config['y_pixel_size'])
        det['y_pixel_size'].attrs['units'] = b'mm'

        det.create_dataset('bit_depth_image', data=detector_config['bit_depth_image'])
        det.create_dataset('bit_depth_readout', data=detector_config['bit_depth_readout'])
        det.create_dataset('saturation_value', data=detector_config['saturation_value'])

        det.create_dataset('detector_readout_time', data=detector_config['detector_readout_time'])
        det['detector_readout_time'].attrs['units'] = b'ms'

        # Add detector-specific metadata
        for nexus_path, value in detector_meta.items():
            if nexus_path.startswith('/entry/instrument/detector/'):
                # Extract dataset name from path
                dataset_name = nexus_path.split('/')[-1]
                det.create_dataset(dataset_name, data=value)
                # Find units from config
                for path, pv_info in detector_config['detector_pvs'].items():
                    if path == nexus_path:
                        units = pv_info.get('units', '')
                        if units:
                            det[dataset_name].attrs['units'] = units.encode('utf-8')

        # Create slits group (only if at least one slit PV succeeded)
        slits_data = {k: v for k, v in shared_meta.items() if k.startswith('/entry/instrument/slits/')}
        if slits_data:
            slits = inst.create_group('slits')
            slits.attrs['NX_class'] = b'NXcollection'
            for nexus_path, value in slits_data.items():
                dataset_name = nexus_path.split('/')[-1]
                slits.create_dataset(dataset_name, data=value)
                slits[dataset_name].attrs['units'] = b'mm'

        # Create zone plate group (if any zone plate PV succeeded, or if we
        # know which optic is installed). The identity is written even when
        # "Save Upstream Optics" is off, which suppresses the positions:
        # which optic was used is identity, not a position log, and losing it
        # makes the data unidentifiable afterwards.
        zp_data = {k: v for k, v in shared_meta.items() if k.startswith('/entry/instrument/zone_plate/')}
        identity = getattr(self, '_cached_optics_identity', None) or {}
        if zp_data or identity:
            zp = inst.create_group('zone_plate')
            zp.attrs['NX_class'] = b'NXcollection'
            for nexus_path, value in zp_data.items():
                dataset_name = nexus_path.split('/')[-1]
                zp.create_dataset(dataset_name, data=value)
                zp[dataset_name].attrs['units'] = b'mm'
            if identity:
                zp.attrs['optics_type'] = identity['optics_type'].encode('utf-8')
                zp.attrs['optics_name'] = identity['optics_name'].encode('utf-8')
                # A dataset, not an attribute: only a dataset can carry the
                # units attribute NeXus wants. Created outside the loop above
                # so it does not inherit that loop's unconditional "mm".
                zp.create_dataset('optics_ozw', data=int(identity['optics_ozw']))
                zp['optics_ozw'].attrs['units'] = b'nm'

    def _migrate_scan_summary_header(self, csv_path) -> None:
        """Append any columns from _get_scan_summary_header() missing from the
        on-disk header (e.g. a column added by a newer version of this file),
        so newly-written rows stay aligned with the header instead of
        silently drifting on a pre-existing CSV."""
        try:
            with open(csv_path, newline="") as f:
                rows = list(csv.reader(f))
            if not rows:
                return
            header = rows[0]
            missing = [col for col in self._get_scan_summary_header() if col not in header]
            if not missing:
                return
            header.extend(missing)
            rows[0] = header
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(rows)
        except Exception:
            pass

    def _update_scan_summary_completed(self, status: str, scan_id: str = None) -> None:
        csv_path = self._get_scan_summary_csv_path()
        if csv_path is None or not csv_path.exists():
            return
        if scan_id is None:
            scan_id = getattr(self, "_last_scan_summary_id", None)
        if not scan_id:
            scan_id = "S%04d" % self.ctx.parameters.scan_number
        try:
            with open(csv_path, newline="") as f:
                rows = list(csv.reader(f))
            if not rows:
                return
            header = rows[0]
            try:
                completed_index = header.index("completed")
                scan_index = header.index("scan")
            except ValueError:
                return
            updated = False
            for row in rows[1:]:
                if len(row) > completed_index and row[scan_index] == scan_id:
                    if row[completed_index] != status:
                        row[completed_index] = status
                        updated = True
                    break
            if updated:
                with open(csv_path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerows(rows)
        except Exception:
            pass

    def _update_3d_slice_completed(self):
        self._update_scan_summary_completed("yes")

    def _write_piezo_positions_to_master(self, piezo_pos_actual: list, axes) -> None:
        """Convert accumulated per-point readbacks for the two scanned piezo
        axes to an array and write them to /entry/sample/piezo_positions in the
        scan's master file(s). No-op if the scan was stopped before any point
        completed.
        """
        if not piezo_pos_actual:
            return
        piezo_pos = np.array(piezo_pos_actual)
        for master_path in getattr(self, "_current_scan_master_paths", {}).values():
            self._append_piezo_positions_to_master_file(master_path, piezo_pos, axes)

    def _working_folder(self) -> str:
        """The Working folder exactly as typed on the main window.

        GUI thread only. Worker threads pass the copy captured at scan start
        (`_scan_working_folder`) into the two functions below instead.
        """
        return self.ctx.ui.edit_workingfolder.text()

    def _gui_scan_folder(self, folder_type, scannumberstring, working_folder=None) -> str:
        """The scan's output folder, as this process must open it."""
        wf = self._working_folder() if working_folder is None else working_folder
        if self.ctx.running_on_linux():
            return self._join_posix(wf, folder_type, scannumberstring)
        # Native separators, so this is byte-identical to what the Windows
        # console produced before these helpers existed.
        return os.path.join(wf, folder_type, scannumberstring)

    def _epics_scan_folder(
        self, basepath, folder_type, scannumberstring, working_folder=None
    ) -> str:
        """The scan's output folder, as a detector IOC must open it.

        Always a Linux path. `basepath` is the IOC-side root the Working
        folder hangs off, and is ignored entirely when the GUI is itself on
        Linux -- there the Working folder is already the full Linux path, so
        prepending a basepath is what produced the doubled
        /net/micdata/data2/net/micdata/data2/... paths.
        """
        wf = self._working_folder() if working_folder is None else working_folder
        if self.ctx.running_on_linux():
            return self._join_posix(wf, folder_type, scannumberstring)
        # Windows: drop the drive letter (the first path component) and hang
        # the rest off the IOC's Linux basepath. PureWindowsPath rather than
        # Path so the split still works when a Windows path is configured
        # from a Linux box, where a bare Path would see no separators at all
        # and yield a single component.
        parts = pathlib.PureWindowsPath(wf).parts
        return self._join_posix(basepath, *parts[1:], folder_type, scannumberstring)

    def _get_master_file_path_with_number(self, detector_type: str, sample_name: str, scan_number: int) -> str:
        """Get the path to the master file given an explicit scan number.

        Same as _get_master_file_path but takes scan_number as parameter instead of reading current.
        Used by the background thread which may run after scan number has incremented.

        Args:
            detector_type: 'SAXS' or 'WAXS'
            sample_name: Sample name extracted from detector filename
            scan_number: Explicit scan number to use

        Returns:
            Full path to the master file, as this process must open it.
        """
        # Determine detector index and mode
        detector_index_map = {'SAXS': 0, 'WAXS': 1}
        det_index = detector_index_map.get(detector_type)
        if det_index is None:
            raise ValueError(f"Unknown detector type: {detector_type}")

        # Get detector object
        if det_index >= len(self.ctx.detector) or self.ctx.detector[det_index] is None:
            raise ValueError(f"Detector {detector_type} not initialized")

        # Determine folder structure based on mode
        if self.ctx.is_ptychomode and self.ctx.detector_mode[det_index] == "ptycho":
            folder_type = "ptycho"
        else:
            # Scattering mode
            tp_map = {'SAXS': 'S', 'WAXS': 'W'}
            tp = tp_map.get(detector_type)
            folder_type = tp + "AXS"

        # The master file is written by this process, so it needs the GUI-side
        # folder -- the same one make_positions_folder created at scan start.
        #
        # This used to build its own path instead, by stripping leading
        # separators off the Working folder and joining the rest with
        # backslashes. On Windows that happened to be right. On Linux it turned
        # an absolute path into a relative one and then produced a single
        # directory component literally named "02_test\ptycho\S0004", so
        # os.makedirs created a stray tree under the launch directory and h5py
        # failed to create the file at all.
        scannumberstring = f"S{scan_number:04d}"
        detector_folder = self._gui_scan_folder(
            folder_type,
            scannumberstring,
            getattr(self, "_scan_working_folder", None) or self._working_folder(),
        )
        filename = f"{sample_name}_{scan_number:04d}_master.h5"
        return os.path.join(detector_folder, filename)

    def _get_master_file_path(self, detector_type: str, sample_name: str) -> str:
        """Get the path to the master file for a given detector and sample.

        Args:
            detector_type: 'SAXS' or 'WAXS'
            sample_name: Sample name extracted from detector filename

        Returns:
            Full path to the master file, as this process must open it:
            {working folder}/{ptycho|SAXS|WAXS}/{S####}/{sample}_{####}_master.h5
        """
        scan_num = self.ctx.parameters.scan_number
        return self._get_master_file_path_with_number(detector_type, sample_name, scan_num)

    def _write_master_file_metadata(self, detector_type: str, scan_positions_dict: dict, sample_name: str) -> str:
        """Create master file with metadata before scan starts.

        Args:
            detector_type: 'SAXS' or 'WAXS'
            scan_positions_dict: Dict of {motor_name: np.ndarray} for scan axes
            sample_name: Sample name string (from detector filename)

        Returns:
            Path to created master .h5 file
        """
        import h5py

        try:
            # Fetch metadata (use cached zone plate optics to avoid duplicate queries)
            cached_zp = self._cached_zone_plate_optics if hasattr(self, '_cached_zone_plate_optics') else None
            shared_metadata = self._fetch_shared_epics_metadata(cached_zone_plate_optics=cached_zp)
            detector_config = self._get_detector_config(detector_type)
            detector_metadata = self._fetch_detector_epics_metadata(detector_config)

            # Create master file. No separator rewriting here: the path comes
            # from _gui_scan_folder already in this platform's own form, and
            # normalising only the directory was what let h5py be handed a
            # path whose directory component did not exist.
            master_path = self._get_master_file_path(detector_type, sample_name)
            master_dir = os.path.dirname(master_path)
            try:
                os.makedirs(master_dir, exist_ok=True)
            except (OSError, FileNotFoundError):
                print(f"Warning: Could not create directory {master_dir}; assuming it exists")

            with h5py.File(master_path, 'w') as f:
                entry = f.create_group('entry')
                entry.attrs['NX_class'] = b'NXentry'
                entry.attrs['default'] = b'data'

                # Basic metadata
                entry.create_dataset('definition', data=b'NXmx')
                entry['definition'].attrs['version'] = b'1.4'
                entry.create_dataset('Detector_program_name', data=b'EPICS areaDetector')

                # Build hierarchy
                self._populate_instrument_group(entry, shared_metadata, detector_metadata, detector_config)
                self._populate_sample_group(entry, shared_metadata, scan_positions_dict)
                self._populate_data_group(entry, sample_name)
                self._populate_scalars_group(entry, shared_metadata)

                # Write sample name to sample group
                entry['sample']['sample_name'][()] = sample_name.encode('utf-8')

            print(f"Created master file: {master_path}")
            return master_path

        except Exception as e:
            print(f"Error creating master file for {detector_type}: {e}")
            traceback.print_exc()
            return ""

    def _write_slice_master_file(self) -> dict:
        """(Re)write the master file for the CURRENT scan folder (one per phi slice
        of a 3-D fly/step scan). Reuses the X/Y grid cached once in _pre_scan
        (avoids re-reading Qt widgets) but re-fetches EPICS metadata fresh, so the
        written 'theta' reflects the angle this slice actually settled at.

        Worker-thread safe: touches no Qt widgets.

        Returns {detector_type: master_path} for the files written; also updates
        self._current_scan_master_paths so any later single-shot linking (e.g.
        flydone3d) targets this, the most recently written, slice.
        """
        scan_positions_dict = getattr(self, '_cached_scan_positions_dict', {})
        sample_name = self.ctx.parameters.scan_name

        detectors_active = []
        if len(self.ctx.detector) > 0 and self.ctx.detector[0] is not None:
            detectors_active.append('SAXS')
        if len(self.ctx.detector) > 1 and self.ctx.detector[1] is not None:
            detectors_active.append('WAXS')

        master_paths = {}
        for detector_type in detectors_active:
            path = self._write_master_file_metadata(detector_type, scan_positions_dict, sample_name)
            if path:
                master_paths[detector_type] = path

        self._current_scan_master_paths = master_paths
        return master_paths

    def _link_slice_detector_data(self, scan_number: int) -> None:
        """Link one phi slice's detector data into ITS OWN master file, identified
        by the given (just-used) scan_number -- as opposed to scandone()'s default
        of always relinking self._scan_number_at_start, which is only correct for
        single-folder scans. Spawns the same short delayed daemon thread scandone()
        uses. Worker-thread safe.
        """
        sample_name = self.ctx.parameters.scan_name
        master_paths = {}
        if len(self.ctx.detector) > 0 and self.ctx.detector[0] is not None:
            try:
                master_paths['SAXS'] = self._get_master_file_path_with_number('SAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute SAXS master file path: {e}")
        if len(self.ctx.detector) > 1 and self.ctx.detector[1] is not None:
            try:
                master_paths['WAXS'] = self._get_master_file_path_with_number('WAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute WAXS master file path: {e}")

        if master_paths:
            import threading
            thread = threading.Thread(
                target=self._link_detector_data_delayed,
                args=(master_paths, 6.0),
                daemon=True
            )
            thread.start()

    def _push_filepaths_to_detectors(self):
        """Push updated FilePath and FileName to all detector IOCs.

        Worker-thread-safe version of _prepare_scan_files.  Uses the Working
        folder captured by _prepare_scan_files at scan start
        (_scan_working_folder) together with the current scan_number /
        scannumberstring, so it can be called from the worker thread between
        phi slices in 3D scans without touching any Qt widgets.  Does not
        create new top-level folders (those are created once by
        _prepare_scan_files); only creates the per-slice scannumber
        subdirectory.
        """
        scan_name = self.ctx.parameters.scan_name
        scan_number = self.ctx.parameters.scan_number
        txt = "%s_%0.4i" % (scan_name, scan_number)
        scannumberstring = "S%04d" % scan_number
        working_folder = self._scan_working_folder


        for i, det in enumerate(self.ctx.detector):
            if i == 0:
                tp = "S"
            elif i == 1:
                tp = "W"
            else:
                tp = ""

            if det is None:
                continue
            if "3820" in det._prefix:
                continue

            if i < 2:
                if self.ctx.is_ptychomode:
                    if self.ctx.detector_mode[i] == "ptycho":
                        tp = ""
                    basepath = det.basepath
                    folder_type = "ptycho"
                else:
                    if len(tp) == 0:
                        continue
                    basepath = self.ctx.parameters.base_linux_datafolder
                    folder_type = tp + "AXS"

            if "SG" in det._prefix:
                folder_type = "positions"
                basepath = (
                    det.basepath
                    if self.ctx.is_ptychomode
                    else self.ctx.parameters.base_linux_datafolder
                )

            if ("dante" in det._prefix) or ("XSP" in det._prefix):
                folder_type = "DANTE"
                basepath = (
                    det.basepath
                    if self.ctx.is_ptychomode
                    else self.ctx.parameters.base_linux_datafolder
                )

            hdfname = tp + txt

            self.ctx.make_positions_folder(
                self._gui_scan_folder(folder_type, scannumberstring, working_folder)
            )

            hdf_path = self._epics_scan_folder(
                basepath, folder_type, scannumberstring, working_folder
            )
            det.filePut("FilePath", hdf_path)
            det.filePut("FileName", hdfname)
            self.ctx.hdf_plugin_name[i] = hdfname



    def _get_detectors_used(self):
        det_names = []
        if len(self.ctx.detector) > 0 and self.ctx.detector[0] is not None:
            det_names.append("SAXS")
        if len(self.ctx.detector) > 1 and self.ctx.detector[1] is not None:
            det_names.append("WAXS")
        if len(self.ctx.detector) > 2 and self.ctx.detector[2] is not None:
            det_names.append("Struck")
        if len(self.ctx.detector) > 3 and self.ctx.detector[3] is not None:
            det_names.append("SG")
        if len(self.ctx.detector) > 4 and self.ctx.detector[4] is not None:
            if self.ctx.ui.actionDante.isChecked():
                det_names.append("Dante")
            elif self.ctx.ui.actionXSP3.isChecked():
                det_names.append("XSP3")
            else:
                det_names.append("Other")
        return ",".join(det_names)



    def _log_3d_slice_start(self, scan_label, axes_params, phi_value, scan_kind: str = "step"):
        modified_axes = []
        for ax in axes_params:
            if ax.get("name", "").lower() == "phi":
                modified_ax = ax.copy()
                modified_ax["p0"] = phi_value
                modified_ax["st"] = 0
                modified_ax["fe"] = 0
                modified_ax["step"] = 0
                modified_axes.append(modified_ax)
            else:
                modified_axes.append(ax)
        position_arrays = [self._make_positions(ax["p0"], ax["st"], ax["fe"], ax["step"]) for ax in modified_axes]
        n_pos = self._compute_n_positions(position_arrays, scan_kind=scan_kind)
        self._write_scan_summary_line(scan_label, modified_axes, n_pos)
