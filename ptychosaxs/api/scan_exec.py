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
