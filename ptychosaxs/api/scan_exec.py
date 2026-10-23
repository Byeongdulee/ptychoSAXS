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
