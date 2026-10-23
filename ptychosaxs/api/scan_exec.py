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

import math  # noqa: F401  (used by moved executors)

from ptychosaxs.api import optics_status as _optics_status
from ptychosaxs.api.scan_errors import (  # noqa: F401
    DET_MIN_READOUT_Error,
    DET_OVER_READOUT_SPEED_Error,
    DG645_Error,
    SOFTGLUE_Setup_Error,
)
from ptychosaxs.hexapod_traj import TrajectoryUnavailable  # noqa: F401
from ptychosaxs.api import scanplan  # noqa: F401  (used by moved methods)
from ptychosaxs.api.scan_config import (  # noqa: F401
    COUNT_DONE_TIMEOUT_MARGIN_S,
    HEXAPOD_FLYMODE_STANDARD,
    HEXAPOD_FLYMODE_WAVELET,
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

    def stepscan0(self, motornumber=-1, update_progress=None, update_status=None):
        axis = self.ctx.motornames[motornumber]
        self.ctx.signalmotor = axis
        self.ctx.signalmotorunit = self.ctx.motorunits[motornumber]
        self.ctx.rpos = []
        self.ctx.mpos = []
        self.ctx.scan_scalars = []
        pos = self.ctx.pts.get_pos(axis)
        pos0 = pos
        self.ctx.isfly = False
        n = motornumber + 1

        if not self.ctx.ui.chk_keep_prev_scan.isChecked():
            self.ctx.clearplot()

        st = self.stepscan_st + self.stepscan_p0
        fe = self.stepscan_fe + self.stepscan_p0
        expt = self.stepscan_expt
        step = self.stepscan_step
        # disable fit menu
        self.ctx.ui.actionFit_QDS_phi.setEnabled(False)
        # enable fit menu
        if axis == "phi":
            self.ctx.ui.actionFit_QDS_phi.setEnabled(True)
        if self.ctx.ui.chk_reverse_scan_dir.isChecked():
            if abs(st - pos) > abs(fe - pos):
                t = fe
                fe = st
                st = t

        self._scan_mv(axis, st, update_status=update_status)
        pos = self._make_positions(0, st - 0, fe - 0, step)
        if len(pos) == 1:
            pos = np.array([st, fe])

        # scaninfo = []
        # scaninfo.append('#H')
        # if self.ctx.detector[2] is not None:
        #     scaninfo.append(axis)
        #     scaninfo.append(self.ctx.detector[2].scaler.NM2)
        #     scaninfo.append(self.ctx.detector[2].scaler.NM3)
        #     scaninfo.append(self.ctx.detector[2].scaler.NM4)
        # else:
        #     scaninfo.append(axis)
        #     scaninfo.append('QDS1')
        #     scaninfo.append('QDS2')
        #     scaninfo.append('QDS3')
        # self.ctx.write_scaninfo_to_logfile(scaninfo)

        # prepare to collect Detector images
        isDET_selected = False

        if self.ctx.DEBUG_MOTORS:
            # --- Debug 1-D step scan (motor stubs) ---
            mpos_data = []
            N = len(pos)
            self.isStopScanIssued = False
            for i, p in enumerate(pos):
                if self.isStopScanIssued:
                    break
                self._scan_mv(axis, p, update_status=update_status)
                time.sleep(min(expt, 0.05))
                mpos_data.append(self.ctx.pts.get_pos(axis))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / N))
            self.ctx.mpos = mpos_data
            return

        # ── REAL MODE ─────────────────────────────────────────────────────────
        # Set up the DG645 delay generator for step-scan triggering.
        # For single-pulse steps: trigger_source=5 means software trigger,
        # period=0 means "fire once per trigger call" (no auto-repeat).
        # For multi-pulse steps: DGNimage pulses fire per trigger at Cycperiod spacing.
        if self.ctx.parameters._pulses_per_step == 1:
            period = 0
        else:
            period = round(max(expt + 0.020, 0.03), 6)
        self.ctx.dg645_12ID.set_pilatus(
            expt,
            trigger_source=5,
            DGNimage=int(self.ctx.parameters._pulses_per_step),
            Cycperiod=period,
        )
        det_period = self._step_detector_period(expt)

        # Arm all selected detectors for the full scan length.
        # step_ready() puts each detector in external-trigger mode, programs
        # the exposure time and period, and sets the total frame count it
        # should expect (len(pos) * pulses_per_step). Once, here -- not per
        # point.
        isDET_selected = False
        for detN, det in enumerate(self.ctx.detector):
            if det is not None:
                # Skip Struck detector which doesn't support HDF5 file templates
                if "3820" in det._prefix:
                    continue
                isDET_selected = True
                print(
                    f"Arming detector {detN} ({det._prefix}) for {len(pos)} positions."
                )
                try:
                    det.setFileTemplate('%s%s_%5.5d.h5')
                    det.step_ready(
                        expt,
                        len(pos),
                        pulsespershot=self.ctx.parameters._pulses_per_step,
                        fn=self.ctx.hdf_plugin_name[detN],
                        period=det_period,
                    )
                except TimeoutError:
                    self.ctx.messages["recent error message"] = (
                        f"Detector {det._prefix} timed out during step_ready."
                    )
                    print(self.ctx.messages["recent error message"])
                    return

        # Count the scalars one position at a time, the way scalar_scan.py's
        # OneShot mode does. Only when Struck is on, so every other scan leaves
        # the scaler free-running and undisturbed for its other clients.
        scaler_oneshot = self.ctx.isStruckCountNeeded and self._scaler_oneshot_begin(expt)

        t0 = time.time()
        try:
            for i, value in enumerate(pos):
                if self.isStopScanIssued:
                    break

                # Move motor to this scan position and wait for motor to settle.
                self._scan_mv(axis, value, update_status=update_status)

                # Configurable idle time between exposures (avoids vibration artefacts).
                time.sleep(self.ctx.parameters._step_acq_time)

                # One OneShot count at this position for the scaler csv, taken
                # after the settle above so the scalers and the detector frame
                # both see the same stopped stage, and before the trigger.
                if scaler_oneshot:
                    self.ctx.scan_scalars.append(self._scaler_oneshot_count(expt))

                # Re-confirm detector is armed before each trigger.
                # The detector can fall out of armed state after a timeout or IOC error.
                timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
                if timeout_occurred:
                    self.ctx.messages["recent error message"] = (
                        f"Detector arm timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                    )
                    print(self.ctx.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR

                # Fire the DG645 trigger.  This causes _pulses_per_step exposures.
                if isDET_selected:
                    self.ctx.dg645_12ID.trigger()

                # Block until the detector has collected the expected cumulative frame count.
                # is_waiting_detectors_timedout checks ArrayCounter_RBV >= (i+1)*pulses_per_step.
                timeout_occurred, TIMEOUT = self.is_waiting_detectors_timedout(expt, i)
                if timeout_occurred:
                    self.ctx.messages["recent error message"] = (
                        f"Detector frame timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                    )
                    print(self.ctx.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR

                # Record the commanded position for the scan log.
                self.ctx.mpos.append(value)
                self._emit_progress(t0, i, len(pos), update_progress, update_status)
        finally:
            if scaler_oneshot:
                self._scaler_oneshot_restore()

        # Return motor to its home position (where it was before the scan started).
        self._scan_mv(axis, pos0, update_status=update_status)

    def stepscan3d0(
        self, xmotor=0, ymotor=-1, phimotor=-1, update_progress=None, update_status=None,
        notify_folder_advanced=None
    ):
        axis = self.ctx.motornames[phimotor]
        self.ctx.signalmotor3 = axis
        self.ctx.signalmotorunit3 = self.ctx.motorunits[phimotor]
        self.ctx.isfly3 = False

        st = self.stepscan3d_st + self.stepscan3d_p0
        fe = self.stepscan3d_fe + self.stepscan3d_p0
        step = self.stepscan3d_step

        self._scan_mv(axis, st, update_status=update_status)
        pos = self._make_positions(0, st - 0, fe - 0, step)

        i = 0
        Npos = len(pos)
        retried_dueto_timeout = 0

        if self.ctx.DEBUG_MOTORS:
            phiaxis = axis
            xaxis = self.ctx.motornames[xmotor]
            yaxis = self.ctx.motornames[ymotor]

            x_st = self.stepscan1d_st + self.stepscan1d_p0
            x_fe = self.stepscan1d_fe + self.stepscan1d_p0
            x_step = self.stepscan1d_step
            x_coords = self._make_positions(0, x_st - 0, x_fe - 0, x_step)
            if len(x_coords) == 1:
                x_coords = np.array([x_st, x_fe])

            y_st = self.stepscan2d_st + self.stepscan2d_p0
            y_fe = self.stepscan2d_fe + self.stepscan2d_p0
            y_step = self.stepscan2d_step
            y_coords = self._make_positions(0, y_st - 0, y_fe - 0, y_step)
            if len(y_coords) == 1:
                y_coords = np.array([y_st, y_fe])

            expt = self.stepscan1d_tm

            xy_pos = self._snake_positions(x_coords, y_coords)
            Nxy = len(xy_pos)
            Ntot = Npos * Nxy

            mpos_data = []
            self.isStopScanIssued = False
            count = 0
            for phip in pos:
                if self.isStopScanIssued:
                    break
                self._scan_mv(phiaxis, phip, update_status=update_status)
                for xp, yp in xy_pos:
                    if self.isStopScanIssued:
                        break
                    self._scan_mv(xaxis, xp, update_status=update_status)
                    self._scan_mv(yaxis, yp, update_status=update_status)
                    time.sleep(min(expt, 0.05))
                    mpos_data.append(
                        [self.ctx.pts.get_pos(xaxis), self.ctx.pts.get_pos(yaxis)]
                    )
                    count += 1
                    if update_progress is not None:
                        update_progress(int(100 * count / Ntot))
                time.sleep(0.5)
            self.ctx.mpos = mpos_data
            for key, p0val in self.ctx.motor_p0.items():
                self._scan_mv(self.ctx.motornames[key], p0val, update_status=update_status)
            return

        while i < Npos:
            wait_long = False
            value = pos[i]
            if self.isStopScanIssued:
                break

            # loging phi angle information
            print("")
            print("*****")
            print(f"phi position : {value:.3e}")
            scaninfo = []
            scaninfo.append("#I phi = ")
            scaninfo.append(value)
            self.ctx.write_scaninfo_to_logfile(scaninfo)

            self._scan_mv(axis, value, update_status=update_status)
            self._push_filepaths_to_detectors()
            # Give this slice's folder its own master file, with theta reflecting
            # the angle just settled at (worker-thread safe: no Qt widget reads).
            self._write_slice_master_file()
            self.progress_3d = (i, Npos)
            scan = f"{axis}{i:03d}"
            self._log_3d_slice_start(scan, self.stepscan3d_axes_params, value)
            retval = self.ctx.stepscan2d0(
                xmotor=xmotor,
                ymotor=ymotor,
                update_progress=update_progress,
                update_status=update_status,
            )
            if retval == DETECTOR_NOT_STARTED_ERROR:
                msg = f"Detector refresh failed ."
                update_status(msg)
                retried_dueto_timeout = retried_dueto_timeout + 1
                wait_long = True
                i = i - 1  # retry the same angle
                if retried_dueto_timeout > 2:
                    msg = f"Detector refresh failed 3 times. Aborting 3D scan."
                    update_status(msg)
                    break
            else:
                self._update_3d_slice_completed()
            if update_status:
                msg = f"Elapsed time = {time.time() - self.time_scanstart}s to finish {(i + 1) / len(pos) * 100}%."
                update_status(msg)

            # Link this slice's detector data into its own master file (not the
            # frozen scan-start one), then advance to the next folder. Runs on
            # the worker thread, so direct_gui_updates=False keeps this call from
            # touching any Qt widgets directly -- label refresh is deferred to
            # the queued scanFolderAdvanced signal via notify_folder_advanced.
            self.ctx.scandone(
                True, False, update_gui=False,
                direct_gui_updates=False,
                notify_folder_advanced=notify_folder_advanced,
                link_scan_number=self.ctx.parameters.scan_number,
            )
            if wait_long:
                wait_for_det_recovery_s = 60  # extended wait after detector timeout to allow IOC recovery before retry
                time.sleep(wait_for_det_recovery_s)

            # monitoring the station ready
            if self.ctx.monitor_beamline_status:
                # if beam is down, wait here
                if self.ctx.isOK2run is not True:
                    self.ctx.wait_for_beam(update_status, value)
                    # retry the same angle
                    i -= 1
            i = i + 1

    def fly3d0(
        self,
        xmotor=0,
        ymotor=1,
        phimotor=6,
        slice_label_prefix="",
        snake=False,
        update_progress=None,
        update_status=None,
        notify_folder_advanced=None,
    ):
        # xmotor is for flying
        # ymotor is for stepping
        # phimotor is for rotation
        axis = self.ctx.motornames[phimotor]
        self.ctx.signalmotor3 = axis
        self.ctx.signalmotorunit3 = self.ctx.motorunits[phimotor]
        pos = self.ctx.pts.get_pos(axis)
        self.ctx.isfly3 = False

        # fly2d0/fly2d0_SNAKE run synchronously on this same worker thread for
        # each phi slice below. Stash the queued-signal callback here so they can
        # advance scan_number/refresh the GUI label the same thread-safe way
        # stepscan3d0 does, instead of touching Qt widgets directly (see the
        # end of fly2d0 / fly2d0_SNAKE).
        self._notify_folder_advanced_3d = notify_folder_advanced

        st = self.fly3d_st + self.fly3d_p0
        fe = self.fly3d_fe + self.fly3d_p0
        step = self.fly3d_step

        if st > fe:
            step = -1 * abs(step)
        if st < fe:
            step = abs(step)

        # revsere scan disabled: always scan from start to final regardless of the initial position.
        self._scan_mv(axis, st, update_status=update_status)
        pos = np.arange(st, fe + step / 2, step)
        retried_dueto_timeout = 0

        if self.ctx.DEBUG_MOTORS:
            phiaxis = axis
            xaxis = self.ctx.motornames[xmotor]
            yaxis = self.ctx.motornames[ymotor]

            x_st = self.fly1d_st + self.fly1d_p0
            x_fe = self.fly1d_fe + self.fly1d_p0
            x_step = self.fly1d_step
            x_step = -abs(x_step) if x_st > x_fe else abs(x_step)
            x_positions = np.arange(x_st, x_fe + x_step / 2, x_step)
            if len(x_positions) == 1:
                x_positions = np.array([x_st, x_fe])

            y_st = self.fly2d_st + self.fly2d_p0
            y_fe = self.fly2d_fe + self.fly2d_p0
            y_step = self.fly2d_step
            y_step = -abs(y_step) if y_st > y_fe else abs(y_step)
            y_positions = np.arange(y_st, y_fe + y_step / 2, y_step)
            if len(y_positions) == 1:
                y_positions = np.array([y_st, y_fe])

            tm = self.fly1d_tm
            xy_pos = self._snake_positions(x_positions, y_positions)
            Ntot = len(pos) * len(xy_pos)

            mpos_data = []
            self.isStopScanIssued = False
            count = 0
            for phip in pos:
                if self.isStopScanIssued:
                    break
                self._scan_mv(phiaxis, phip, update_status=update_status)
                for xp, yp in xy_pos:
                    if self.isStopScanIssued:
                        break
                    self._scan_mv(yaxis, yp, update_status=update_status)
                    self._scan_mv(xaxis, xp, update_status=update_status)
                    time.sleep(min(tm, 0.05))
                    mpos_data.append(
                        [self.ctx.pts.get_pos(xaxis), self.ctx.pts.get_pos(yaxis)]
                    )
                    count += 1
                    if update_progress is not None:
                        update_progress(int(100 * count / Ntot))
                time.sleep(0.5)
            self.ctx.mpos = mpos_data
            return

        # ── REAL MODE ─────────────────────────────────────────────────────────
        # Build a per-slice scan label used in log entries.
        if slice_label_prefix:
            slice_label_prefix = f"{slice_label_prefix}{axis}"
        else:
            slice_label_prefix = axis

        i = 0
        retried_dueto_timeout = 0
        while i < len(pos):
            wait_long = False
            value = pos[i]

            if self.isStopScanIssued:
                break

            # Log this phi slice.
            print(f"\n***** phi position: {value:.3e}")
            self.ctx.write_scaninfo_to_logfile(["#I phi = ", value])

            # Move phi to this angle.
            self._scan_mv(axis, value, update_status=update_status)

            self.progress_3d = (i, len(pos))
            scan = f"{slice_label_prefix}{i:03d}"
            self._log_3d_slice_start(
                scan, self.fly3d_axes_params, value,
                scan_kind="fly_snake" if snake else "fly_hexapod_1d",
            )

            # Give this slice its own folder's master file, with theta reflecting
            # the angle just settled at (worker-thread safe: no Qt widget reads).
            # Also refreshes self._current_scan_master_paths to this slice, so the
            # hexapod-positions append below (and flydone3d's final link) target it.
            self._write_slice_master_file()
            # fly2d0/fly2d0_SNAKE end with their own run_stop_issued(), which
            # advances scan_number to the *next* slice's folder before we get
            # control back below -- so capture this slice's number now, while
            # it's still current, for the post-sweep data-linking call.
            slice_scan_number = self.ctx.parameters.scan_number

            # Program the hexapod trajectory for this phi slice, then run the
            # 2-D executor directly on the current worker thread (no sub-worker).
            if snake:
                # Disable the wave generator before reprogramming; set_traj_SNAKE2
                # fails with GCSError 73 if the generator output is still active.
                if i > 0:
                    self.ctx.pts.hexapod.stop_traj()
                self.fly_traj(xmotor, ymotor)
                # As early as possible after pulse_number becomes known, verify
                # the software-predicted hexapod trigger count matches it and
                # record both. Every slice now has its own master file, so every
                # slice writes its own hexapod_positions dataset; no try/except
                # here — let a mismatch propagate to Worker.run()'s generic
                # exception handler like DET_MIN_READOUT_Error and DG645_Error
                # already do elsewhere in this file.
                self._reconcile_hexapod_snake_positions(write_to_master=True)
                retval = self.fly2d0_SNAKE(
                    xmotor, ymotor,
                    update_progress=update_progress, update_status=update_status,
                )
            else:
                self.fly_traj(xmotor)
                retval = self.fly2d0(
                    xmotor, ymotor,
                    update_progress=update_progress, update_status=update_status,
                )
            self.ctx.s12softglue.flush()
            print(f"softglue flushed at {time.ctime()}")

            # Link this slice's detector data into its own master file. The
            # scan-number advance for the next slice's folder already happened
            # inside fly2d0/fly2d0_SNAKE's own end-of-scan bookkeeping above.
            self._link_slice_detector_data(slice_scan_number)

            if i < len(pos) - 1:
                self.ctx.get_detectors_ready()
                self._push_filepaths_to_detectors()
                # Extra settle time before the next phi slice starts arming its
                # detectors -- some slices otherwise stall for a long time waiting
                # on the detector IOC before the next trigger sequence begins.
                time.sleep(self.OVERHEAD_FLY3D_PHI)

            # On detector failure, retry this phi angle up to 2 extra times.
            if retval == DETECTOR_NOT_STARTED_ERROR:
                retried_dueto_timeout += 1
                wait_long = True
                i -= 1  # stay on this angle
                msg = f"Detector timeout at phi={value:.3f}, retry {retried_dueto_timeout}."
                if update_status:
                    update_status(msg)
                print(msg)
                if retried_dueto_timeout > 2:
                    if update_status:
                        update_status("Detector failed 3 times. Aborting 3-D fly scan.")
                    break
            
            self._update_3d_slice_completed()

            if update_status:
                elapsed = time.time() - self.time_scanstart
                update_status(
                    f"Elapsed {elapsed:.0f}s to finish {(i + 1) / len(pos) * 100:.1f}%."
                )

            if wait_long:
                # Give the detector IOC 60 s to recover after a timeout.
                time.sleep(60)

            # If the beam is down, wait here and retry this angle.
            if self.ctx.monitor_beamline_status and not self.ctx.isOK2run:
                self.wait_for_beam(update_status, value)
                i -= 1  # retry the same angle after beam recovery

            i += 1

        # Don't let a standalone 2-D fly scan started later on this handler
        # accidentally pick up this 3-D scan's (now-finished) signal emitter.
        self._notify_folder_advanced_3d = None

    def fly2d0_SNAKE(
        self, xmotor=0, ymotor=1, update_progress=None, update_status=None
    ):
        self.ctx.isfly2 = False
        ##### ############## need to work from this........

        print()
        # scaninfo = []
        # scaninfo.append('#I Y = ')
        # scaninfo.append(value)
        # self.ctx.write_scaninfo_to_logfile(scaninfo)
        # print("In fly2d0")
        t0 = time.time()

        self.plotlabels = []
        if self.ctx.ui.actionckTime_reset_before_scan.isChecked():
            if self.ctx.s12softglue.isConnected:
                self.ctx.s12softglue.ckTime_reset()
        if self.ctx.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if self.ctx.s12softglue.isConnected:
                    self.ctx.s12softglue.memory_clear()
            except TimeoutError:
                self.ctx.messages["recent error message"] = (
                    "softglue memory_clear timeout"
                )
                print(self.ctx.messages["recent error message"])

        isTestRun = self.ctx.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.ctx.isfly = True
        self.ctx.isscan = True

        # disable fit menu
        self.ctx.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ctx.ui.chk_keep_prev_scan.isChecked():
            self.ctx.clearplot()

        if self.ctx.DEBUG_MOTORS:
            xaxis = self.ctx.motornames[xmotor]
            yaxis = self.ctx.motornames[ymotor]
            x_st = self.fly1d_st + self.fly1d_p0
            x_fe = self.fly1d_fe + self.fly1d_p0
            x_step = self.fly1d_step
            y_st = self.fly2d_st + self.fly2d_p0
            y_fe = self.fly2d_fe + self.fly2d_p0
            y_step = self.fly2d_step
            tm = self.fly1d_tm
            x_step = -abs(x_step) if x_st > x_fe else abs(x_step)
            y_step = -abs(y_step) if y_st > y_fe else abs(y_step)
            x_positions = np.arange(x_st, x_fe + x_step / 2, x_step)
            y_positions = np.arange(y_st, y_fe + y_step / 2, y_step)
            if len(x_positions) == 1:
                x_positions = np.array([x_st, x_fe])
            if len(y_positions) == 1:
                y_positions = np.array([y_st, y_fe])
            xy_pos = self._snake_positions(x_positions, y_positions)
            mpos_data = []
            self.isStopScanIssued = False
            Ntot = len(xy_pos)
            count = 0
            for xp, yp in xy_pos:
                if self.isStopScanIssued:
                    break
                self._scan_mv(yaxis, yp, update_status=update_status)
                self._scan_mv(xaxis, xp, update_status=update_status)
                time.sleep(min(tm, 0.05))
                mpos_data.append([self.ctx.pts.get_pos(xaxis), self.ctx.pts.get_pos(yaxis)])
                count += 1
                if update_progress is not None:
                    update_progress(int(100 * count / Ntot))
            self.ctx.mpos = mpos_data
            return

        # expt = np.around(self.ctx.pts.hexapod.scantime/self.ctx.pts.hexapod.pulse_number*0.75, 3)
        period = self.ctx.pts.hexapod.pulse_step
        print(self.ctx.pts.hexapod.pulse_number, "This is the number of pulses......")
        # expt = period-self.det_readout_time  JD
        expt = (
            period * self.ctx.parameters._ratio_exp_period
        )  # JMM, *0.2 previously for JD. -0.02 previously for BL
        # if period-expt < DETECTOR_READOUTTIME:
        #    raise RuntimeError("expouretime is too short to readout DET images.")

        if expt <= 0:
            self.ctx.messages["recent error message"] = (
                f"Exposure time is ≤ 0 (period={period:.4f}, "
                f"ratio={self.ctx.parameters._ratio_exp_period:.3f})."
            )
            print(self.ctx.messages["recent error message"])
            raise DET_MIN_READOUT_Error(self.ctx.messages["recent error message"])

        if abs(period) < self.OVERHEAD_FLY:
            self.ctx.messages["recent error message"] = (
                "Period < %d ms — Pilatus 2M maximum rate is 30 Hz."
                % (1000 * self.OVERHEAD_FLY)
            )
            print(self.ctx.messages["recent error message"])
            raise DET_OVER_READOUT_SPEED_Error(self.ctx.messages["recent error message"])

        # ── REAL MODE ─────────────────────────────────────────────────────────
        # Set delay generator for fly-scan timing.
        if expt != self.ctx.dg645_12ID._exposuretime:
            try:
                self.ctx.dg645_12ID.set_pilatus_fly(expt)
            except Exception:
                raise DG645_Error

        if isTestRun:
            return  # dry run: timing validated but no hardware motion

        # self.Xaxis and self.Yaxis are set by fly_traj() (called on the GUI thread
        # in the fly2d entry point, before the worker was started).  fly_traj also
        # called hexapod.set_traj_SNAKE2(), which programmed the complete 2-D snake
        # waveform onto the hexapod controller.
        axes = [self.Xaxis, self.Yaxis]

        # Move to the start position of the entire 2-D snake trajectory.
        self.ctx.pts.hexapod.goto_start_pos(axes)

        # Arm all detectors for the TOTAL pulse count across the full 2-D snake.
        # The hexapod fires one trigger per position; pulse_number is the total
        # across all lines (not per line).
        for detN, det in enumerate(self.ctx.detector):
            if det is not None:
                try:
                    det.fly_ready(
                        expt,
                        self.ctx.pts.hexapod.pulse_number,
                        1,
                        period=period,
                        isTest=isTestRun,
                        capture=(self.ctx.use_hdf_plugin, self.ctx.hdf_plugin_savemode_fly),
                        fn=self.ctx.hdf_plugin_name[detN],
                    )
                except TimeoutError:
                    self.ctx.messages["recent error message"] = (
                        f"Detector {det._prefix} timed out during fly_ready."
                    )
                    print(self.ctx.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR

        timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
        if timeout_occurred:
            self.ctx.messages["recent error message"] = (
                f"Detector arm timeout ({TIMEOUT}s). {time.ctime()}"
            )
            print(self.ctx.messages["recent error message"])
            return DETECTOR_NOT_STARTED_ERROR

        print("fly2d0_SNAKE: executing 2-D snake trajectory...")
        # Execute the complete 2-D snake trajectory in a single hexapod command.
        # The hexapod moves both X and Y axes simultaneously along the pre-programmed
        # snake path, firing one encoder-synchronized trigger per position.
        self.ctx.pts.hexapod.run_traj(axes)

        # Wait until all expected frames have been collected.
        Nstep = self.ctx.pts.hexapod.pulse_number
        TIMEOUT = period * Nstep + 2  # stall window: total scan time + 2 s buffer
        # The last frame's ArrayCounter_RBV update is delayed by HDF5 finalization,
        # which can take many seconds. Give it a much longer stall window.
        LAST_FRAME_TIMEOUT = max(TIMEOUT, 30)
        N_imgcollected = 0
        t_since_last_frame = time.time()
        t0_scan = time.time()
        while N_imgcollected < Nstep:
            if self.isStopScanIssued:
                break

            # Read the current frame count from the first available camera detector.
            val = 0
            for ndet, det in enumerate(self.ctx.detector):
                if ndet > 1:
                    continue
                if det is not None:
                    val = det.ArrayCounter_RBV
                    break

            # Emit 3-D-aware or standalone progress.
            self._emit_progress(
                t0_scan,
                val,
                Nstep,
                update_progress,
                update_status,
                t_scanstart=self.time_scanstart,
                progress_3d=self.progress_3d if self.fly3d_p0 is not None else None,
            )

            time.sleep(0.1)

            # If frame count advanced, reset the stall timer.
            if val > N_imgcollected:
                N_imgcollected = val
                t_since_last_frame = time.time()

            # Stall timeout: use the longer window for the last frame, since its
            # ArrayCounter_RBV update can be delayed by HDF5 finalization. This used
            # to also accept Armed==0 at Nstep-1 as "close enough" and break early,
            # but Armed can drop to 0 just as easily because the last trigger was
            # never registered at all (dropped trigger, not just a slow RBV) --
            # that silently produced a NaN or missing final frame. Waiting out the
            # full stall window and timing out on a genuine miss (which fly2d/fly3d
            # already retry on DETECTOR_NOT_STARTED_ERROR) is safer than guessing.
            # DIAGNOSTIC - a camserver error stops frames arriving, so the loop
            # above just waits out the full stall window with nothing to say.
            # Surface the detector's own account instead of a 30 s silence.
            camserver_error = self._detector_camserver_error()
            if camserver_error:
                self.ctx.messages["recent error message"] = (
                    f"Detector reported an error after {N_imgcollected}/{Nstep} "
                    f"frames: {camserver_error}"
                )
                print(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            stall_limit = LAST_FRAME_TIMEOUT if N_imgcollected == Nstep - 1 else TIMEOUT
            if time.time() - t_since_last_frame > stall_limit:
                self.ctx.messages["recent error message"] = (
                    f"Data collection stalled after {stall_limit:.1f}s "
                    f"({N_imgcollected}/{Nstep} frames). {time.ctime()}"
                )
                print(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

        self.ctx.pts.hexapod.wait()
        self._advance_scan_number_thread_safe()
        return 1

    def fly0(self, motornumber=-1, update_progress=None, update_status=None):
        t0 = time.time()
        axis = self.ctx.motornames[motornumber]
        self.ctx.signalmotor = axis
        self.ctx.signalmotorunit = self.ctx.motorunits[motornumber]
        self.plotlabels = []
        if self.ctx.ui.actionckTime_reset_before_scan.isChecked():
            if self.ctx.s12softglue.isConnected:
                self.ctx.s12softglue.ckTime_reset()
        if self.ctx.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if self.ctx.s12softglue.isConnected:
                    self.ctx.s12softglue.memory_clear()
            except TimeoutError:
                self.ctx.messages["recent error message"] = (
                    "softglue memory_clear timeout"
                )
                print(self.ctx.messages["recent error message"])

        print("")
        isTestRun = self.ctx.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.ctx.isfly = True
        self.ctx.isscan = True

        # disable fit menu
        self.ctx.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ctx.ui.chk_keep_prev_scan.isChecked():
            self.ctx.clearplot()

        st = self.fly1d_st + self.fly1d_p0
        fe = self.fly1d_fe + self.fly1d_p0
        step = self.fly1d_step
        tm = self.fly1d_tm

        if self.ctx.DEBUG_MOTORS:
            mpos_data = []
            if st > fe:
                step = -abs(step)
            else:
                step = abs(step)
            positions = np.arange(st, fe + step / 2, step)
            if len(positions) == 1:
                positions = np.array([st, fe])
            N = len(positions)
            self.isStopScanIssued = False
            for i, p in enumerate(positions):
                if self.isStopScanIssued:
                    break
                self._scan_mv(axis, p, update_status=update_status)
                time.sleep(min(tm, 0.05))
                mpos_data.append(self.ctx.pts.get_pos(axis))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / N))
            self.ctx.mpos = mpos_data
            return

        pos = self.ctx.pts.get_pos(axis)
        # print("Time to finish line 2127: %0.3f" % (time.time()-t0)) very fast down to this far
        if (axis in self.ctx.pts.hexapod.axes) and (
            self.ctx.hexapod_flymode == HEXAPOD_FLYMODE_WAVELET
        ):
            if self.ctx.ui.chk_reverse_scan_dir.isChecked():
                if abs(st - pos) > abs(fe - pos):
                    t = fe
                    fe = st
                    st = t
                    step = -step
            direction = int(step) / abs(step)
            if direction == 1:
                dirv = 0
            else:
                dirv = 6
            self.ctx.pts.hexapod.assign_axis2wavtable(
                axis, self.ctx.pts.hexapod.WaveGenID[axis] + dirv
            )

            period = self.ctx.pts.hexapod.pulse_step  # pulse step time.
            expt = (
                period * self.ctx.parameters._ratio_exp_period
            )  # JMM, *0.2 previously for JD. -0.02 previously for BL
            if isTestRun:
                print(
                    f"{self.ctx.pts.hexapod.pulse_number} images will be collected every {period}s with exposure time of {expt}s."
                )

            if period - expt < DETECTOR_READOUTTIME:
                self.ctx.messages["recent error message"] = (
                    f"Exposure time {expt:.4f} and period {period:.4f} requires the readout time {period - expt}, which is too short."
                )
                print(self.ctx.messages["recent error message"])
                if update_status:
                    update_status(self.ctx.messages["recent error message"])
                return None

            if expt <= 0:
                self.ctx.messages["recent error message"] = (
                    f"Note that after subtracting the detector readout time {self.det_readout_time:.3e} s, the exposure time becomes equal or less than 0."
                )
                print(self.ctx.messages["recent error message"])
                raise DET_MIN_READOUT_Error(self.ctx.messages["recent error message"])

            if abs(period) < self.OVERHEAD_FLY:
                self.ctx.messages["recent error message"] = (
                    f"Note that Max speed of Pilatus2M is 30Hz."
                )
                print(self.ctx.messages["recent error message"])
                raise DET_OVER_READOUT_SPEED_Error(
                    self.ctx.messages["recent error message"]
                )

            # set the delay generator
            if expt != self.ctx.dg645_12ID._exposuretime:
                try:
                    self.ctx.dg645_12ID.set_pilatus_fly(expt)
                except:
                    raise DG645_Error

            # SoftGlue ready for recording interferometer values
            movestep = (
                abs(fe - st)
                / self.ctx.pts.hexapod.pulse_number
                * 1000
                * self.ctx.parameters._ratio_exp_period
            )
            print(
                f"Actual exposure time: {1000.*expt:0.3f} ms, during which {axis} will move {movestep:.3f} um."
            )

            # If softglue SG is not selected, use prepare for the softglue.
            if self.ctx.detector[3] is None:
                if self.ctx.s12softglue.isConnected:
                    N_counts = self.ctx.s12softglue.number_acquisition(
                        expt, self.ctx.pts.hexapod.pulse_number
                    )
                    self.ctx.parameters.countsperexposure = np.round(
                        N_counts / self.ctx.pts.hexapod.pulse_number
                    )
                    print(
                        f"Total {self.ctx.parameters.countsperexposure} encoder positions will be collected per a DET image."
                    )
                    if N_counts > 100000:
                        self.ctx.messages["recent error message"] = (
                            f"******** CAUTION: Number of softglue counts: {N_counts} is larger than 100E3. Slow down the clock speed."
                        )
                        raise SOFTGLUE_Setup_Error(
                            self.ctx.messages["recent error message"]
                        )

            if isTestRun:
                return

            # Scan start ............................
            self.ctx.pts.hexapod.goto_start_pos(axis)  # took 0.4 second
            for detN, det in enumerate(self.ctx.detector):
                if det is not None:
                    try:
                        det.fly_ready(
                            expt,
                            self.ctx.pts.hexapod.pulse_number,
                            period=period,
                            isTest=isTestRun,
                            capture=(
                                self.ctx.use_hdf_plugin,
                                self.ctx.hdf_plugin_savemode_fly,
                            ),
                            fn=self.ctx.hdf_plugin_name[detN],
                        )
                    except TimeoutError:
                        self.ctx.messages["recent error message"] = (
                            f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        )
                        print(self.ctx.messages["recent error message"])
                        if update_status:
                            update_status(self.ctx.messages["recent error message"])
                        return DETECTOR_NOT_STARTED_ERROR
            print("Ready for traj")
            pos = self.ctx.pts.get_pos(axis)
            print(f"pos is {pos} before traj run start.")

            timeout_occurred, TIMEOUT = self.ctx.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.ctx.messages["recent error message"] = (
                    f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                )
                print(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            istraj_running = False
            timeout = 5
            i = 0
            print("Trajectory scan initiated..")
            while not istraj_running:
                try:
                    self.ctx.pts.hexapod.run_traj(axis)
                except TrajectoryUnavailable:
                    # No point retrying, and no point waiting out the five
                    # attempts with the detector armed and the shutter open.
                    self.ctx.messages["recent error message"] = (
                        "Hexapod trajectory transport is unavailable (%s): %s"
                        % (self.ctx.pts.hexapod.traj_state,
                           self.ctx.pts.hexapod.traj_reason)
                    )
                    print(self.ctx.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR
                except Exception as exc:
                    # A transient GCS error is worth another attempt, but it
                    # should not vanish silently the way a bare except made it.
                    print(f"run_traj attempt failed ({exc}); retrying.")
                wait_for_traj_start_s = 0.05  # brief pause before checking if hexapod trajectory has started moving
                time.sleep(wait_for_traj_start_s)
                pos_tmp = self.ctx.pts.get_pos(axis)
                # An unreachable record reads nan, and nan != nan is True -
                # so without this a dropped IOC would look like a running
                # trajectory and the scan would "finish" with no data.
                if not math.isnan(pos_tmp) and pos_tmp != pos:
                    istraj_running = True
                # istraj_running = self.ctx.is_traj_running()
                i = i + 1
                if i > timeout:
                    self.ctx.messages["recent error message"] = (
                        "traj scan command is resent for 5 times to the hexapod without success."
                    )
                    print(self.ctx.messages["recent error message"])
                    break
            print("Run_traj is sent command in rungui.")
            isattarget = False
            timeelapsed = 0
            t0 = time.time()
            while not isattarget:
                try:
                    isattarget = self.ctx.pts.hexapod.isattarget(axis)
                except:
                    isattarget = False
                wait_for_traj_at_target_s = 0.02  # poll interval while waiting for hexapod to reach trajectory end position
                time.sleep(wait_for_traj_at_target_s)
                # pos_tmp = self.ctx.pts.get_pos(axis)
                timeelapsed = time.time() - t0
                prog = float(timeelapsed) / float(tm)
                if update_progress:
                    update_progress(int(prog * 100))
                msg1 = f"Elapsed time = {int(timeelapsed)}s since the start."
                if prog > 0:
                    remainingtime = timeelapsed / prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime, 2)}s\n"
                self.ctx.messages["current status"] = "%s%s" % (msg1, msg2)
                if update_status:
                    update_status(self.ctx.messages["current status"])

                if self.isStopScanIssued:
                    break

            pos = self.ctx.pts.get_pos(axis)
            print(f"pos is {pos:.3e} after the traj run done.")
        # fly scan with a constant velocity of motions.
        else:
            print("Fly scan with phi.")
            # fly for phi scan is unique.
            # tm is the total time for the fly scan, which is determined by the user input.
            # step is the angle step, which is determined by the user input.
            Xstep = self.fly1d_step  # step angle (this was step time before)
            # This was the total time before, but now we will use it as the exposure time
            # a time for each step will be calculated.
            Xtm = self.fly1d_tm

            # step time calculation
            fly_acq_time = getattr(self.ctx.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
            step_time = max(fly_acq_time, Xtm + self.det_readout_time, self.OVERHEAD_FLY)
            # self.ctx.parameters._ratio_exp_period = Xtm / step_time
            # total time calculation
            # Use the same rounding-guarded formula as _make_positions/_compute_n_positions
            # (bare int((fe-st)/Xstep) truncation can drop the last trigger on
            # float-rounding edge cases where the division isn't exactly integral).
            Nsteps = self._compute_n_positions([self._make_positions(0, st, fe, Xstep)], scan_kind="fly_phi")
            total_time = Nsteps * step_time
            # expt = step_time*self.ctx.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
            expt = Xtm
            if step_time - expt < 0.015:
                raise DET_MIN_READOUT_Error(
                    f"Period - Exposure Time,{step_time - expt}s, should be longer than 50 microseconds."
                )

            # set the delay generator
            try:
                self.ctx.dg645_12ID.set_pilatus2(
                    expt, Nsteps, step_time
                )  # exposuretime, number of images, and time period for fly scan.
            except:
                raise DG645_Error
            print(
                f"Exposure time: {expt:0.3e} s, number of steps: {Nsteps}, Step time: {step_time:.3e} s, Total time for the scan: {total_time:.3f} s."
            )
            if self.ctx.ui.chk_reverse_scan_dir.isChecked():
                if abs(st - pos) > abs(fe - pos):
                    t = fe
                    fe = st
                    st = t

            if motornumber == 6:
                # enable fit menu
                self.ctx.ui.actionFit_QDS_phi.setEnabled(True)

            self._prev_vel, self._prev_acc = self.ctx.pts.get_speed(axis)
            self._scan_mv(axis, st, wait=True, update_status=update_status)
            wait_for_motor_settle_s = 0.1  # brief settle time after moving phi to start position before setting fly speed
            time.sleep(wait_for_motor_settle_s)
            # print(f"Setting speed for fly scan. Total time: {abs(fe-st)/total_time:.3f} s, acceleration: {abs(fe-st)/total_time*10:.3f}.")
            self.ctx.pts.set_speed(
                axis, abs(fe - st) / total_time, abs(fe - st) / total_time * 10
            )
            wait_for_speed_set_s = (
                0.02  # brief pause after setting fly scan speed before arming detectors
            )
            time.sleep(wait_for_speed_set_s)

            # Need to make detectors ready
            for detN, det in enumerate(self.ctx.detector):
                if det is not None:
                    try:
                        det.fly_ready(
                            expt,
                            Nsteps,
                            period=step_time,
                            isTest=isTestRun,
                            capture=(
                                self.ctx.use_hdf_plugin,
                                self.ctx.hdf_plugin_savemode_fly,
                            ),
                            fn=self.ctx.hdf_plugin_name[detN],
                        )
                    #            print("Time to finish line 2190: %0.3f" % (time.time()-t0)) # take 0.3 second
                    except TimeoutError:
                        self.ctx.messages["recent error message"] = (
                            f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        )
                        print(self.ctx.messages["recent error message"])
                        if update_status:
                            update_status(self.ctx.messages["recent error message"])
                        # showerror("Detector timeout.")
                        return

            timeout_occurred, TIMEOUT = self.ctx.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.ctx.messages["recent error message"] = (
                    f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                )
                print(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            scaninfo = []
            print("")
            print(f"{axis} scan started..")
            scaninfo.append(f"FileIndex, {axis},    time(s)")
            scaninfo.append(f"0,   {st},   {time.time()}")
            self.ctx.pts.mv(axis, fe, wait=False)

            print("about to send out trigger.")
            # Start collect data while an axis is moving.
            self.ctx.dg645_12ID.trigger()
            print("Delay generator is triggered to start the fly scan.")
            # Update progress bar and status message.
            N_imgcollected = 0
            timeelapsed = time.time() - t0
            TIMEOUT = total_time + 5
            if TIMEOUT < 5:
                TIMEOUT = 5
            timestart = time.time()
            val = 0
            # print(N_imgcollected, Nsteps)
            while N_imgcollected < Nsteps:
                for ndet, det in enumerate(self.ctx.detector):
                    if ndet > 1:
                        continue
                    if det is not None:
                        val = det.ArrayCounter_RBV
                        break
                prog = float(val) / float(Nsteps)
                pos = self.ctx.pts.get_pos(axis)
                scaninfo.append(f"{val},    {pos},  {time.time()}")

                if update_progress:
                    update_progress(int(prog * 100))
                msg1 = f"Elapsed time = {int(timeelapsed)}s since the start."
                if prog > 0:
                    remainingtime = timeelapsed / prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime, 2)}s\n"
                self.ctx.messages["current status"] = "%s%s" % (msg1, msg2)
                if update_status:
                    update_status(self.ctx.messages["current status"])

                wait_for_det_progress_s = 0.1  # poll interval while monitoring phi fly-scan frame collection progress
                time.sleep(wait_for_det_progress_s)
                if val > N_imgcollected:
                    N_imgcollected = val
                    timestart = time.time()

                updatetime = time.time() - timestart
                if updatetime > TIMEOUT:
                    self.ctx.messages["recent error message"] = (
                        f"Detector {det._prefix} data collection timeout after {TIMEOUT} seconds."
                    )
                    print(self.ctx.messages["recent error message"])
                    if update_status:
                        update_status(self.ctx.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR
                timeelapsed = time.time() - t0
                if self.isStopScanIssued:
                    break
            self.ctx.write_scaninfo_to_logfile(scaninfo)

        return 1

    def helix_fly0(self, phimotor=6, zmotor=2, update_progress=None, update_status=None):
        """Worker-thread executor for helix fly scan: phi + Z simultaneous constant-velocity fly."""
        t0 = time.time()
        phi_axis = self.ctx.motornames[phimotor]
        z_axis = self.ctx.motornames[zmotor]
        self.ctx.signalmotor = f"{phi_axis},{z_axis}"
        self.ctx.signalmotorunit = self.ctx.motorunits[phimotor]

        if self.ctx.ui.actionckTime_reset_before_scan.isChecked():
            if self.ctx.s12softglue.isConnected:
                self.ctx.s12softglue.ckTime_reset()
        if self.ctx.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if self.ctx.s12softglue.isConnected:
                    self.ctx.s12softglue.memory_clear()
            except TimeoutError:
                self.ctx.messages["recent error message"] = "softglue memory_clear timeout"
                print(self.ctx.messages["recent error message"])

        print("")
        isTestRun = self.ctx.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run (helix):")
        self.ctx.isfly = True
        self.ctx.isscan = True

        self.ctx.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ctx.ui.chk_keep_prev_scan.isChecked():
            self.ctx.clearplot()

        phi_st = self.helix_phi_st + self.helix_phi_p0
        phi_fe = self.helix_phi_fe + self.helix_phi_p0
        phi_step = self.helix_phi_step
        phi_tm = self.helix_phi_tm

        z_st = self.helix_z_st + self.helix_z_p0
        z_fe = self.helix_z_fe + self.helix_z_p0

        if self.ctx.DEBUG_MOTORS:
            phi_mpos_data = []
            z_mpos_data = []
            if phi_st > phi_fe:
                phi_step = -abs(phi_step)
            else:
                phi_step = abs(phi_step)
            phi_positions = np.arange(phi_st, phi_fe + phi_step / 2, phi_step)
            if len(phi_positions) == 1:
                phi_positions = np.array([phi_st, phi_fe])
            N = len(phi_positions)
            z_step_debug = (z_fe - z_st) / (N - 1) if N > 1 else 0
            self.isStopScanIssued = False
            for i, p in enumerate(phi_positions):
                if self.isStopScanIssued:
                    break
                z_p = z_st + i * z_step_debug
                self._scan_mv(phi_axis, p, update_status=update_status)
                self._scan_mv(z_axis, z_p, update_status=update_status)
                time.sleep(min(phi_tm, 0.05))
                phi_mpos_data.append(self.ctx.pts.get_pos(phi_axis))
                z_mpos_data.append(self.ctx.pts.get_pos(z_axis))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / N))
            self.ctx.mpos = phi_mpos_data
            self.ctx.zpos = z_mpos_data
            return

        phi_pos = self.ctx.pts.get_pos(phi_axis)
        z_pos = self.ctx.pts.get_pos(z_axis)

        Xstep = self.helix_phi_step
        Xtm = self.helix_phi_tm

        # Same formula used by helix_fly's pre-scan velocity guard, so the
        # logged/guarded values and the actual DG645 arm count can never diverge.
        total_time, Nsteps, step_time = self._compute_helix_total_time(phi_st, phi_fe, Xstep, Xtm)
        expt = Xtm
        if step_time - expt < 0.015:
            raise DET_MIN_READOUT_Error(
                f"Period - Exposure Time,{step_time - expt}s, should be longer than 50 microseconds."
            )

        try:
            self.ctx.dg645_12ID.set_pilatus2(expt, Nsteps, step_time)
        except:
            raise DG645_Error
        print(
            f"Exposure time: {expt:0.3e} s, number of steps: {Nsteps}, Step time: {step_time:.3e} s, Total time for the scan: {total_time:.3f} s."
        )

        if self.ctx.ui.chk_reverse_scan_dir.isChecked():
            if abs(phi_st - phi_pos) > abs(phi_fe - phi_pos):
                t = phi_fe
                phi_fe = phi_st
                phi_st = t
            if abs(z_st - z_pos) > abs(z_fe - z_pos):
                t = z_fe
                z_fe = z_st
                z_st = t

        self._prev_vel_z, _ = self.ctx.pts.get_speed(z_axis)

        self._scan_mv(phi_axis, phi_st, wait=True, update_status=update_status)
        self._scan_mv(z_axis, z_st, wait=True, update_status=update_status)
        wait_for_motor_settle_s = 0.1
        time.sleep(wait_for_motor_settle_s)

        phi_vel = abs(phi_fe - phi_st) / total_time
        z_vel = abs(z_fe - z_st) / total_time

        self.ctx.pts.set_speed(phi_axis, phi_vel, phi_vel * 10)
        self.ctx.pts.set_speed(z_axis, z_vel, None)
        wait_for_speed_set_s = 0.02
        time.sleep(wait_for_speed_set_s)

        for detN, det in enumerate(self.ctx.detector):
            if det is not None:
                try:
                    det.fly_ready(
                        expt,
                        Nsteps,
                        period=step_time,
                        isTest=isTestRun,
                        capture=(
                            self.ctx.use_hdf_plugin,
                            self.ctx.hdf_plugin_savemode_fly,
                        ),
                        fn=self.ctx.hdf_plugin_name[detN],
                    )
                except TimeoutError:
                    self.ctx.messages["recent error message"] = (
                        f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                    )
                    print(self.ctx.messages["recent error message"])
                    if update_status:
                        update_status(self.ctx.messages["recent error message"])
                    return

        timeout_occurred, TIMEOUT = self.ctx.is_arming_detecotors_timedout()
        if timeout_occurred:
            self.ctx.messages["recent error message"] = (
                f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
            )
            print(self.ctx.messages["recent error message"])
            return DETECTOR_NOT_STARTED_ERROR

        scaninfo = []
        print("")
        print(f"{phi_axis},{z_axis} helix scan started..")
        scaninfo.append(f"FileIndex, {phi_axis}, {z_axis},    time(s)")
        scaninfo.append(f"0,   {phi_st},   {z_st},   {time.time()}")

        self.ctx.pts.mv(phi_axis, phi_fe, wait=False)
        self.ctx.pts.mv(z_axis, z_fe, wait=False)

        print("about to send out trigger.")
        self.ctx.dg645_12ID.trigger()
        print("Delay generator is triggered to start the helix fly scan.")

        N_imgcollected = 0
        timeelapsed = time.time() - t0
        TIMEOUT = total_time + 5
        if TIMEOUT < 5:
            TIMEOUT = 5
        timestart = time.time()
        val = 0

        while N_imgcollected < Nsteps:
            for ndet, det in enumerate(self.ctx.detector):
                if ndet > 1:
                    continue
                if det is not None:
                    val = det.ArrayCounter_RBV
                    break
            prog = float(val) / float(Nsteps)
            phi_pos = self.ctx.pts.get_pos(phi_axis)
            z_pos = self.ctx.pts.get_pos(z_axis)
            scaninfo.append(f"{val},    {phi_pos},   {z_pos},  {time.time()}")

            if update_progress:
                update_progress(int(prog * 100))
            msg1 = f"Elapsed time = {int(timeelapsed)}s since the start."
            if prog > 0:
                remainingtime = timeelapsed / prog - timeelapsed
            else:
                remainingtime = 999
            msg2 = f"; Remaining time for the current helix scan is {np.round(remainingtime, 2)}s\n"
            self.ctx.messages["current status"] = "%s%s" % (msg1, msg2)
            if update_status:
                update_status(self.ctx.messages["current status"])

            wait_for_det_progress_s = 0.1
            time.sleep(wait_for_det_progress_s)
            if val > N_imgcollected:
                N_imgcollected = val
                timestart = time.time()

            updatetime = time.time() - timestart
            if updatetime > TIMEOUT:
                self.ctx.messages["recent error message"] = (
                    f"Detector {det._prefix} data collection timeout after {TIMEOUT} seconds."
                )
                print(self.ctx.messages["recent error message"])
                if update_status:
                    update_status(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR
            timeelapsed = time.time() - t0
            if self.isStopScanIssued:
                break
        self.ctx.write_scaninfo_to_logfile(scaninfo)

        return 1

    def fly2d0(
        self, xmotor=0, ymotor=1, update_progress=None, update_status=None
    ):
        """2-D fly scan: step the slow (Y) axis, fire fly0 along X for each Y line.

        This is the non-snake variant.  Each X line is an independent 1-D hexapod
        trajectory.  Works for any motor combination where X is a hexapod axis.

        Contrast with fly2d0_SNAKE, which programs the entire 2-D path as a single
        hexapod trajectory (requires both axes to be hexapod axes, but is faster
        and more precise).
        """

        axis = self.ctx.motornames[ymotor]
        self.ctx.signalmotor2 = axis
        self.ctx.signalmotorunit2 = self.ctx.motorunits[ymotor]
        self.ctx.isfly2 = False

        # Re-read Y and X parameters from the UI.  This executor may be called
        # repeatedly from fly3d0 while the user has access to the UI, so re-reading
        # here picks up any edits made between phi slices.
        _fy = self._scan_fields(ymotor)
        y_p0 = float(_fy["p0_text"])
        y_st = _fy["st"]
        y_fe = _fy["fe"]
        y_step = _fy["step"]
        self.fly2d_p0 = y_p0
        self.fly2d_st = y_st
        self.fly2d_fe = y_fe

        _fx = self._scan_fields(xmotor)
        x_p0 = float(_fx["p0_text"])
        x_st = _fx["st"]
        x_fe = _fx["fe"]
        self.fly1d_p0 = x_p0
        self.fly1d_st = x_st
        self.fly1d_fe = x_fe

        # Build the Y position array.
        y_positions = self._make_positions(y_p0, y_st, y_fe, y_step)
        Nline = len(y_positions)

        t0 = time.time()
        isreshreshed = 1
        for i, yval in enumerate(y_positions):
            if self.isStopScanIssued:
                break

            print(f"\nY position: {yval:.4f}")
            self.ctx.write_scaninfo_to_logfile(["#I Y = ", yval])

            # Step the slow axis to this Y position and wait for it to stop.
            self._scan_mv(axis, yval, update_status=update_status)
            while self.ctx.pts.ismoving(axis):
                time.sleep(0.02)

            # Optional: update per-line HDF file number for multi-line HDF capture.
            # This ensures each X-line lands in a separate HDF entry.
            if self.ctx.use_hdf_plugin and (self.ctx.hdf_plugin_savemode_fly > 0):
                for det in self.ctx.detector:
                    if det is not None and hasattr(det, "filePut"):
                        if any(
                            s in det._prefix.lower()
                            for s in ("cam", "sg", "dante", "xsp3")
                        ):
                            det.filePut("FileNumber", i + 1)

            # Run the 1-D fly scan along X for this Y line.
            # fly0 returns 1 on success, DETECTOR_NOT_STARTED_ERROR on failure.
            # On failure, refresh_detectors() attempts an IOC reset before retrying.
            status = 0
            while status < 1:
                status = self.fly0(
                    xmotor, update_progress=update_progress, update_status=update_status
                )
                if status is DETECTOR_NOT_STARTED_ERROR:
                    isreshreshed = self.refresh_detectors(update_status=update_status)
                if isreshreshed == 0:
                    print("Detector refresh failed. Stopping scan.")
                    if update_status:
                        update_status(f"Detector refresh failed. {time.ctime()}")
                    return DETECTOR_NOT_STARTED_ERROR

            # Advance the scan number log entry for this completed X line.
            # return_motor=False keeps the motor at the end of the X trajectory
            # (the next line starts from wherever fly0 left off, or goto_start_pos
            # handles repositioning internally). direct_gui_updates=False because
            # this runs on the worker thread, once per Y line -- touching Qt
            # widgets directly here (rather than via a queued signal) is what
            # was corrupting the backing store and crashing the GUI.
            self.ctx.flydone(return_motor=False, reset_scannumber=False, direct_gui_updates=False)

            # Inter-line idle time (configurable). Reuses the step-scan acquisition
            # time parameter for the per-line wait in this fly scan.
            t1 = time.time()
            while time.time() - t1 < self.ctx.parameters._step_acq_time:
                time.sleep(0.01)

            # Progress: fly3d_p0 is non-None when called from fly3d0.
            self._emit_progress(
                t0,
                i,
                Nline,
                update_progress,
                update_status,
                t_scanstart=self.time_scanstart,
                progress_3d=self.progress_3d if self.fly3d_p0 is not None else None,
            )

        self._advance_scan_number_thread_safe()
        return 1

    def stepscan2d0(self, xmotor=0, ymotor=1, update_progress=None, update_status=None):
        # print(ymotor, " this is ymortor")
        yaxis = self.ctx.motornames[ymotor]
        xaxis = self.ctx.motornames[xmotor]
        self.ctx.signalmotor2 = yaxis
        self.ctx.signalmotorunit2 = self.ctx.motorunits[ymotor]
        # pos = self.ctx.pts.get_pos(yaxis)
        self.ctx.isfly2 = False

        # Just in case when the user update edit box (during 3d scan)
        # Will need to update the positions.
        _fy = self._scan_fields(ymotor)
        p0 = float(_fy["p0_text"])
        st = _fy["st"]
        fe = _fy["fe"]
        step = _fy["step"]
        self.stepscan2d_p0 = p0
        self.stepscan2d_st = st
        self.stepscan2d_fe = fe
        self.stepscan2d_step = step

        # _scan_fields falls back to the shared ed_lup_1_t for slots with
        # no exposure widget of their own (trans1/trans2 at 7/8), the same
        # way _read_motor_params does.
        _fx = self._scan_fields(xmotor)
        p0 = float(_fx["p0_text"])
        st = _fx["st"]
        fe = _fx["fe"]
        expt = float(_fx["t_widget"].text())
        step = _fx["step"]
        self.stepscan1d_p0 = p0
        self.stepscan1d_st = st
        self.stepscan1d_fe = fe
        self.stepscan1d_step = step

        yst = self.stepscan2d_st + self.stepscan2d_p0
        yfe = self.stepscan2d_fe + self.stepscan2d_p0
        ystep = self.stepscan2d_step

        xst = self.stepscan1d_st + self.stepscan1d_p0
        xfe = self.stepscan1d_fe + self.stepscan1d_p0
        xstep = self.stepscan1d_step

        x_coords = self._make_positions(0, xst - 0, xfe - 0, xstep)
        y_coords = self._make_positions(0, yst - 0, yfe - 0, ystep)

        # Nx2 numpy array of (x, y) in snake (boustrophedon) order
        pos = self._snake_positions(x_coords, y_coords)
        Nline = len(pos)
        # keep for later use if needed
        self.stepscan2d_positions = pos

        # trans1/trans2 (the SmarAct MCS2 'piezo' stages) are closed-loop but not
        # pulse-exact like the hexapod's fly trajectory, so for a piezo scan we
        # read back the actual settled position after each move (see below) and
        # write it to the master file as /entry/sample/piezo_positions.
        is_piezo_scan = xaxis in self.ctx.pts.gonio.motornames and yaxis in self.ctx.pts.gonio.motornames
        piezo_pos_actual = [] if is_piezo_scan else None
        # self.ctx.dg645_12ID.set_pilatus(expt, trigger_source=5, DGNimage=1)
        # each time it will send a pulse

        # scaninfo = []
        # scaninfo.append('#H')
        # if self.ctx.detector[2] is not None:
        #     scaninfo.append(xaxis)
        #     scaninfo.append(yaxis)
        #     scaninfo.append(self.ctx.detector[2].scaler.NM2)
        #     scaninfo.append(self.ctx.detector[2].scaler.NM3)
        #     scaninfo.append(self.ctx.detector[2].scaler.NM4)
        # else:
        #     scaninfo.append(xaxis)
        #     scaninfo.append(yaxis)
        #     scaninfo.append('QDS1')
        #     scaninfo.append('QDS2')
        #     scaninfo.append('QDS3')
        # self.ctx.write_scaninfo_to_logfile(scaninfo)

        if self.ctx.DEBUG_MOTORS:
            # --- Debug 2-D step scan (motor stubs) ---
            mpos_data = []
            Nline = len(pos)
            self.isStopScanIssued = False
            for i, (xp, yp) in enumerate(pos):
                if self.isStopScanIssued:
                    break
                self._scan_mv_xy(xaxis, xp, yaxis, yp, update_status=update_status)
                time.sleep(min(expt, 0.05))
                xy_readback = [self.ctx.pts.get_pos(xaxis), self.ctx.pts.get_pos(yaxis)]
                mpos_data.append(xy_readback)
                if is_piezo_scan:
                    piezo_pos_actual.append(xy_readback)
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / Nline))
            self.ctx.mpos = mpos_data
            if is_piezo_scan:
                self._write_piezo_positions_to_master(piezo_pos_actual, (xaxis, yaxis))
            return

        if self.ctx.parameters._pulses_per_step == 1:
            period = 0
        else:
            period = round(max(expt + 0.020, 0.03), 6)
        self.ctx.dg645_12ID.set_pilatus(
            expt,
            trigger_source=5,
            DGNimage=int(self.ctx.parameters._pulses_per_step),
            Cycperiod=period,
        )
        det_period = self._step_detector_period(expt)

        ## prepre detectors ............
        # Exposure time, exposure period and frame count are all programmed
        # here, once, before the point loop starts.
        for detN, det in enumerate(self.ctx.detector):  # JD
            if det is not None:  # JD
                # Skip Struck detector which doesn't support HDF5 file templates
                if "3820" in det._prefix:
                    continue
                det.setFileTemplate('%s%s_%5.5d.h5')
                det.step_ready(
                    expt,
                    Nline,
                    pulsespershot=self.ctx.parameters._pulses_per_step,
                    fn=self.ctx.hdf_plugin_name[detN],
                    period=det_period,
                )  # Arm detector for multiple data.
                print(f"step _ready, detector {detN}'s status: {det.Armed}")  # JD

        t0 = time.time()
        self.isStopScanIssued = False

        # make sure detectors get armed.
        self.ctx.get_detectors_armed()

        self.ctx.messages["recent error message"] = ""
        self.ctx.messages["current status"] = ""
        self.ctx.messages["progress"] = ""

        print("Starting 2D step scan now...........................")
        for i, (xp, yp) in enumerate(pos):
            if self.isStopScanIssued:
                break

            self._scan_mv_xy(xaxis, xp, yaxis, yp, update_status=update_status)

            if is_piezo_scan:
                # Read back the actual settled position from the SmarAct
                # controllers rather than trusting the commanded (xp, yp).
                piezo_pos_actual.append(
                    [self.ctx.pts.get_pos(xaxis), self.ctx.pts.get_pos(yaxis)]
                )

            # Configurable idle time between exposures.
            time.sleep(self.ctx.parameters._step_acq_time)

            # Confirm detector armed before each trigger (may fall out of arm
            # state after a previous timeout or transient IOC error).
            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.ctx.messages["recent error message"] = (
                    f"Detector arm timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                )
                print(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            # Fire DG645 trigger — causes _pulses_per_step detector exposures.
            self.ctx.dg645_12ID.trigger()
            print(
                f"Trigger sent for point {i + 1} ({xp:.4f}, {yp:.4f}). {time.ctime()}"
            )

            # Block until the expected frame count is reached.
            timeout_occurred, TIMEOUT = self.is_waiting_detectors_timedout(expt, i)
            if timeout_occurred:
                self.ctx.messages["recent error message"] = (
                    f"Detector frame timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                )
                print(self.ctx.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            # Record position and emit progress.
            self.ctx.mpos.append([xp, yp])

            # stepscan3d_p0 is None when this is a standalone 2-D scan.
            # When non-None, this executor is a slice inside stepscan3d0 and we
            # report progress as a fraction of the total 3-D scan.
            self._emit_progress(
                t0,
                i,
                Nline,
                update_progress,
                update_status,
                t_scanstart=self.time_scanstart,
                progress_3d=self.progress_3d
                if self.stepscan3d_p0 is not None
                else None,
            )

        if is_piezo_scan:
            self._write_piezo_positions_to_master(piezo_pos_actual, (xaxis, yaxis))

        return 1
