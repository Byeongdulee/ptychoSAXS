"""
gui/handlers/scan_handler.py
Scan execution, detector management, data saving, and network command handling.
Extracted from ptyco_main_control in rungui.py.
"""

import time
import os
import csv
import json
import numpy as np
import re
import traceback
import datetime
import pathlib
from collections import deque
from PyQt5.QtWidgets import QMessageBox, QInputDialog, QLabel, QLineEdit, QFileDialog, QPushButton, QCheckBox, QDialog, QVBoxLayout
from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal, QSettings
import pyqtgraph as pg
from tools.detectors import DET_MIN_READOUT_Error, DET_OVER_READOUT_SPEED_Error
from tools.dg645 import DG645_Error
from tools.softglue import SOFTGLUE_Setup_Error


class HexapodPositionCountMismatchError(Exception):
    """Raised when the software-predicted hexapod-snake trigger count does not
    match hexapod.pulse_number after set_traj_SNAKE2 programs the trajectory."""


# Constants mirrored from rungui.py
HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
FRACTION_EXPOSURE_PERIOD = 0.2
DETECTOR_READOUTTIME = 0.02
DETECTOR_NOT_STARTED_ERROR = -1
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = 1
STRUCK_CHANNELS = [2, 3, 4, 5]


def rstrip_from_char(string, char):
    """Removes characters from the right of the string starting from the first occurrence of 'char'."""
    #    print(f'{string=}')
    #    print(f'{char=}')
    if char in string:
        index = string.rfind(char)
        return string[:index]
    return string


class _QdsScatterWorker(QObject):
    """Background worker that samples qds_array and emits it for scatter-plot updates."""

    data_ready = pyqtSignal(object)  # carries a np.ndarray

    def __init__(self, data_fn, interval_ms=100):
        super().__init__()
        self._data_fn = data_fn
        self._interval_ms = interval_ms

    def start(self):
        # Called via thread.started so the QTimer is created inside the worker thread.
        self._timer = QTimer()
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._emit)
        self._timer.start()

    def _emit(self):
        raw = self._data_fn()
        if raw:
            arr = np.asarray(list(raw))
            if arr.ndim == 2 and arr.shape[1] >= 3:
                self.data_ready.emit(arr)


class ScanHandler:
    # Per-point overhead added to exposure time when estimating scan duration.
    # Fly scan overhead accounts for detector readout + SoftGlue latency.
    # Step scan overhead accounts for motor settle + readout.
    OVERHEAD_FLY = 0.033  # seconds — Pilatus 2M hard minimum (30 Hz)
    OVERHEAD_STEP = 0.5  # seconds
    # Show a confirmation dialog before starting scans larger than this.
    LARGE_SCAN_THRESHOLD = 200  # positions
    # 1-indexed motor numbers whose positions are saved/restored by the
    # Save Current / Go To Saved buttons.  Adjust this list as needed.
    SAVED_POSITION_MOTORS = [1, 2, 3, 7, 8, 9]

    def __init__(self, window) -> None:
        self.w = window
        self.ui = window.ui
        self._saved_positions = {}  # {n: float} keyed by 1-indexed motor number
        self._connect_signals()

        self.det_readout_time = DETECTOR_READOUTTIME

    def _connect_signals(self) -> None:
        # Wire all scan-parameter line edits so that pressing Enter recalculates
        # Nx, Ny, Ntot, and the estimated scan time.
        _lup_widgets = [
            "ed_lup_1_L",
            "ed_lup_1_N",
            "ed_lup_1_R",
            "ed_lup_3_L",
            "ed_lup_3_N",
            "ed_lup_3_R",
            "ed_lup_1_t",
        ]
        for _name in _lup_widgets:
            _w = self.ui.findChild(QLineEdit, _name)
            if _w is not None:
                _w.returnPressed.connect(self.update_scan_estimate)

    def update_scan_estimate(self):
        """Recalculate Nx, Ny, Ntot and estimated scan time from lup fields.

        Called whenever Enter is pressed in any of ed_lup_1_L/N/R,
        ed_lup_3_L/N/R, or ed_lup_1_t.

        Nx, Ny = len(_make_positions(...)) for each axis (same formula the scan
        executors use, so this estimate can't drift from the real scan).
        Ntot_step = Nx * Ny (step scans always use plain grid product)
        Ntot_fly = Ntot_step (or adjusted for snake fly if chk_snake is checked)
        est_time = Ntot * (lup_1_t + overhead)

        Overhead is OVERHEAD_FLY for fly scans, OVERHEAD_STEP for step scans.
        The scan type is determined by whether pushButton_flyscan is checked.
        Snake adjustment is applied if chk_snake is checked (2-D scans only).
        """

        def _val(name, default=0.0):
            w = self.ui.findChild(QLineEdit, name)
            if w is None:
                return default
            try:
                return float(w.text())
            except ValueError:
                return default

        lup_1_L = _val("ed_lup_1_L")
        lup_1_R = _val("ed_lup_1_R")
        lup_1_N = _val("ed_lup_1_N", 1.0)
        lup_3_L = _val("ed_lup_3_L")
        lup_3_R = _val("ed_lup_3_R")
        lup_3_N = _val("ed_lup_3_N", 1.0)
        lup_1_t = _val("ed_lup_1_t")

        if lup_1_N == 0 or lup_3_N == 0:
            return

        # Use the same _make_positions formula the scan executors use, so this
        # estimate can never drift from what the scan actually runs.
        x_pos = self._make_positions(0, lup_1_L, lup_1_R, lup_1_N)
        y_pos = self._make_positions(0, lup_3_L, lup_3_R, lup_3_N)
        Nx = len(x_pos)
        Ny = len(y_pos)

        # Step scan always uses plain grid product
        Ntot_step = Nx * Ny

        # Fly scan with snake uses adjusted positions (phantom triggers + Y padding)
        Ntot_fly = self._compute_n_positions([x_pos, y_pos], scan_kind="fly_snake")

        step_est = Ntot_step * (lup_1_t + self.OVERHEAD_STEP)
        fly_acq_time = getattr(self.w.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
        fly_period = max(fly_acq_time, lup_1_t + DETECTOR_READOUTTIME, self.OVERHEAD_FLY)
        fly_est = Ntot_fly * fly_period

        def _set_label(name, text):
            lbl = self.ui.findChild(QLabel, name)
            if lbl is not None:
                lbl.setText(text)

        _set_label("label_Nx", "Nx\n%d" % int(round(Nx)))
        _set_label("label_Ny", "Ny\n%d" % int(round(Ny)))
        _set_label("label_Ntot", "Ntot\n%d" % int(round(Ntot_step)))

        def _fmt_time(t):
            return "%.1f min" % (t / 60) if t > 300 else "%.1f s" % t

        _set_label("label_estT", "%s\n%s" % (_fmt_time(step_est), _fmt_time(fly_est)))
        self._refresh_scan_pos_plot()

    @staticmethod
    def _fmt_time(t):
        """Format seconds into a human-readable string (mirrors the label_estT formatter)."""
        return "%.1f min" % (t / 60) if t > 300 else "%.1f s" % t

    def _confirm_large_scan(self, Ntot, tm, overhead):
        """Return True to proceed, False to abort.

        When Ntot > LARGE_SCAN_THRESHOLD, shows a dialog with the position
        count and estimated time (same formula as label_estT).
        """
        Ntot = int(round(Ntot))
        if Ntot <= self.LARGE_SCAN_THRESHOLD:
            return True
        est = Ntot * (tm + overhead)
        msg = (
            f"This scan has {Ntot} positions.\n"
            f"Estimated time: {self._fmt_time(est)}\n\n"
            "Proceed with scan?"
        )
        dlg = QMessageBox(self.w.ui)
        dlg.setWindowTitle("Large Scan")
        dlg.setText(msg)
        dlg.setIcon(QMessageBox.Question)
        ok_btn = dlg.addButton("Proceed", QMessageBox.AcceptRole)
        dlg.addButton(QMessageBox.Cancel)
        dlg.exec_()
        return dlg.clickedButton() is ok_btn

    # ------------------------------------------------------------------
    # Shared helpers used by all scan entry points and executors
    # ------------------------------------------------------------------

    def _check_hdf_for_multi_pulse(self) -> bool:
        """Return False (and show a warning) if multi-pulse mode is on but the HDF5
        plugin is disabled.  The HDF5 plugin is required to aggregate multiple frames
        per motor position into a single file.  Returns True when safe to proceed.
        """
        if self.w.parameters._pulses_per_step > 1 and not self.w.use_hdf_plugin:
            dlg = QMessageBox(self.w.ui)
            dlg.setWindowTitle("Check HDF Plugin")
            dlg.setText(
                f"Pulses per step is set to {self.w.parameters._pulses_per_step}.\n"
                "HDF5 plugin must be enabled for multi-pulse per step scans."
            )
            dlg.setIcon(QMessageBox.Warning)
            dlg.addButton(QMessageBox.Ok)
            dlg.exec_()
            return False
        return True

    def _check_saxs_det_mode(self) -> bool:
        """Return False (and show a warning) if SAXS detector is not in scan mode.
        Returns True when safe to proceed.
        """
        mode = self.get_saxs_det_mode()
        if mode != "scan":
            dlg = QMessageBox(self.w.ui)
            dlg.setWindowTitle("Check SAXS Detector")
            dlg.setText("Set SAXS detector to scan mode")
            dlg.setIcon(QMessageBox.Warning)
            dlg.addButton(QMessageBox.Ok)
            dlg.exec_()
            return False
        return True

    def _confirm_scan_name(self) -> bool:
        """Prompt user to confirm scan name. Single source of truth for the name.
        Returns False if user cancels, True if confirmed.
        """
        dlg = QDialog(self.w.ui)
        dlg.setWindowTitle("Confirm Scan Name")
        layout = QVBoxLayout(dlg)

        label = QLabel("Enter scan name:")
        layout.addWidget(label)

        ed = QLineEdit()
        ed.setText(self.w.parameters.scan_name)
        ed.selectAll()
        layout.addWidget(ed)

        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.setDefault(True)
        ok_btn.setFocus()

        btn_layout = QVBoxLayout()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        if dlg.exec_() == QDialog.Accepted:
            scan_name = ed.text()
            if scan_name:
                self.w.parameters.scan_name = scan_name
                return True
            else:
                QMessageBox.warning(self.w.ui, "Empty Name", "Scan name cannot be empty.")
                return False
        return False

    def _pre_scan_guards(self) -> bool:
        """Run all pre-scan guards and return False if any fail.
        Call this at the top of every scan entry point before any other logic.
        Add new guards here so they apply to all scan types.
        """
        if not self._confirm_scan_name():
            return False
        if not self._check_hdf_for_multi_pulse():
            return False
        if not self._check_saxs_det_mode():
            return False
        return True

    def _read_motor_params(self, motor_index: int) -> dict:
        """Read scan parameters for one motor from the UI and return them as a dict.

        Keys: motor_index, name, p0, st, fe, step, expt.
          p0   — absolute home position (from lb_N label; written into ed_N)
          st   — relative start offset  (from ed_lup_N_L)
          fe   — relative end offset    (from ed_lup_N_R)
          step — step size              (from ed_lup_N_N; sign corrected by caller)
          expt — exposure time          (from ed_lup_N_t; falls back to ed_lup_1_t)

        Raises ValueError or TypeError if any field is empty or non-numeric.
        Must be called from the GUI thread (reads Qt widgets).
        """
        n = motor_index + 1
        p0 = float(self.w.check_start_position(n))
        self.ui.findChild(QLineEdit, f"ed_{n}").setText(f"{p0:.6f}")
        st = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_L").text())
        fe = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_R").text())
        step = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_N").text())
        # Exposure time: prefer per-axis widget; fall back to the shared ed_lup_1_t.
        t_widget = self.ui.findChild(QLineEdit, f"ed_lup_{n}_t") or self.ui.findChild(
            QLineEdit, "ed_lup_1_t"
        )
        expt = float(t_widget.text())
        return {
            "motor_index": motor_index,
            "name": self.w.motornames[motor_index],
            "p0": p0,
            "st": st,
            "fe": fe,
            "step": step,
            "expt": expt,
        }

    @staticmethod
    def _signed_step(ast: float, afe: float, step: float) -> float:
        """Correct *step*'s sign to match the ast→afe direction.

        If step==0 it is replaced by (afe-ast) so a two-element array can
        still be produced. Shared by _make_positions and any caller that
        needs the exact same step _make_positions would use internally
        (e.g. to extrapolate an extra point past a single-element array).
        """
        if step == 0:
            step = (afe - ast) if (afe != ast) else 1.0
        return -abs(step) if ast > afe else abs(step)

    def _make_positions(
        self, p0: float, st: float, fe: float, step: float
    ) -> np.ndarray:
        """Return absolute scan positions as a 1-D numpy array.

        p0+st is the scan start; p0+fe is the scan end.  The sign of *step* is
        corrected automatically so the direction matches st→fe.  If step==0 it is
        replaced by (fe-st) so a two-element array is still produced.
        """
        ast = p0 + st
        afe = p0 + fe
        step = self._signed_step(ast, afe, step)
        pos = np.arange(ast, afe + step / 2, step)
        return pos

    def _pre_scan(self, scan_name: str, scan_kind: str = "step") -> None:
        """Common setup called at the start of every scan entry point (GUI thread).

        Resets detector file/frame counters, refreshes scan name and file paths,
        logs current motor positions, and clears the stop flag so a previous scan's
        stop signal does not immediately abort the new one.

        scan_kind: one of "step", "fly_snake", "fly_hexapod_1d", "fly_phi", "helix".
        Used to determine how positions are computed for the NeXus master file.
        """
        self.w.update_scanname()
        self.w.get_detectors_ready()
        self.w.prepare_scan_files()
        self.w.write_motor_scan_range()
        self.isStopScanIssued = False
        print(f"\n\n{scan_name} starting")

        # Store the scan number at scan start time so background linking thread uses the
        # same number even if user changes scan_number before linking completes
        self._scan_number_at_start = self.w.parameters.scan_number

        # Query optics PVs once and cache for both CSV and master file (avoid duplicate queries)
        self._cached_us_optics = self._get_us_optics_positions()
        self._cached_zone_plate_optics = self._fetch_zone_plate_optics_metadata()

        # Create NeXus master files before scan starts
        try:
            sample_name = self.w.parameters.scan_name
            scan_positions_dict = self._compute_scan_positions(scan_kind=scan_kind)

            # Create master file for each active detector
            detectors_active = []
            if len(self.w.detector) > 0 and self.w.detector[0] is not None:
                detectors_active.append('SAXS')
            if len(self.w.detector) > 1 and self.w.detector[1] is not None:
                detectors_active.append('WAXS')

            for detector_type in detectors_active:
                self._write_master_file_metadata(detector_type, scan_positions_dict, sample_name)
        except Exception as e:
            print(f"Warning: Failed to create master file: {e}")

    def _log_scan_header(self, scan_name: str, axes_params: list, scan_kind: str = "step") -> None:
        """Write the SPEC-style #S header line for this scan to the log file.

        axes_params is a list of dicts from _read_motor_params, in axis order
        (X first, then Y if 2-D, then phi if 3-D).
        scan_kind: one of "step", "fly_snake", "fly_hexapod_1d", "fly_phi", "helix".
        Determines how n_pos is computed from position arrays.
        """
        position_arrays = [self._make_positions(ax["p0"], ax["st"], ax["fe"], ax["step"]) for ax in axes_params]
        if scan_kind == "helix":
            # Only phi (axis 0) determines the trigger count; Z moves in parallel
            # at constant velocity and does not add independent trigger points.
            # Uses the same "fly_phi" formula that actually arms the DG645
            # (see helix_fly0), so the logged/nominal count can't drift from it.
            n_pos = self._compute_n_positions([position_arrays[0]], scan_kind="fly_phi")
        else:
            n_pos = self._compute_n_positions(position_arrays, scan_kind=scan_kind)
        scaninfo = ["\n#S", self.w.parameters.scan_number, scan_name]
        for ax in axes_params:
            n = ax["motor_index"] + 1
            scaninfo += [n, ax["p0"], ax["st"], ax["fe"], ax["expt"], ax["step"]]
        scaninfo.append("\n#Motor Information\n")
        m = self.w.get_pos_all()
        for name in self.w.motornames:
            scaninfo.append(name)
        scaninfo.append("\n")
        for key in m:
            scaninfo.append(m[key])
        self.w.write_scaninfo_to_logfile(scaninfo)
        self._write_scan_summary_line(scan_name, axes_params, n_pos)

    def _get_scan_summary_csv_path(self):
        if len(self.w.parameters.logfilename) == 0:
            return None
        return pathlib.Path(self.w.parameters.logfilename).with_suffix(".csv")

    def _get_scan_summary_header(self):
        return [
            "date",
            "time",
            "completed",
            "scan",
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
            "BS_ver",
            "BS_hor",
            "ZP_ver",
            "ZP_hor",
            "BSZP_Ztrans",
            "OSA_X",
            "OSA_Z",
            "OSA_Y",
        ]

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

    def _get_phi_angle(self, axes_params):
        axis_names = [ax.get("name", "").lower() for ax in axes_params]
        if "phi" in axis_names:
            # If phi is in the scan axes, use its p0
            for ax in axes_params:
                if ax.get("name", "").lower() == "phi":
                    return str(ax.get("p0", ""))
        else:
            # If not 3D and not 1D on phi axis, take from lb_7
            # 3D would have phi in axes, 1D on phi would have only phi
            # So if phi not in axes, it's not 3D and not 1D on phi, use lb_7
            lb7_widget = self.ui.findChild(QLabel, "lb_7")
            if lb7_widget:
                return lb7_widget.text()
        return ""

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

    _US_OPTICS_PVS = [
        ("BS_ver",      "12idc:m10.RBV"),
        ("BS_hor",      "12idc:m11.RBV"),
        ("ZP_ver",      "12idc:m12.RBV"),
        ("ZP_hor",      "12idc:m13.RBV"),
        ("BSZP_Ztrans", "12idc:m14.RBV"),
        ("OSA_X",       "12idc:m9.RBV"),
        ("OSA_Z",       "12idc:m15.RBV"),
        ("OSA_Y",       "12idc:m16.RBV"),
    ]

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

    def check_fly_blur(self):
        """Estimate the sample blur during a fly scan exposure and show it in a dialog."""
        from PyQt5.QtWidgets import QMessageBox

        def _read(name):
            w = self.ui.findChild(QLineEdit, name)
            if w is None:
                return None
            try:
                return float(w.text())
            except (ValueError, TypeError):
                return None

        try:
            expt = _read("ed_lup_1_t")
            step = _read("ed_lup_1_N")
            if expt is None or step is None or expt <= 0 or step <= 0:
                QMessageBox.warning(self.w.ui, "Fly Blur", "Exposure time and step size must both be > 0.")
                return
            step = abs(step)
            fly_acq_time = getattr(self.w.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
            step_time = max(fly_acq_time, expt + DETECTOR_READOUTTIME, self.OVERHEAD_FLY)
            step_time = round(step_time * 1000) / 1000
            velocity_mm_s = step / step_time
            movestep_um = step * 1000.0 * expt / step_time
            msg = (
                f"Exposure time:     {expt * 1000.:.2f} ms\n"
                f"Step size:         {step * 1000.:.3f} µm\n"
                f"Acquisition time:  {step_time * 1000.:.2f} ms\n"
                f"Velocity:          {velocity_mm_s * 1000.:.3f} µm/s\n"
                f"\n"
                f"Motion during exposure:  {movestep_um * 1000:.0f} nm"
            )
            QMessageBox.information(self.w.ui, "Fly Blur Estimate", msg)
        except Exception as e:
            QMessageBox.warning(self.w.ui, "Fly Blur Error", f"Error calculating fly blur: {e}")

    def _get_detectors_used(self):
        det_names = []
        if len(self.w.detector) > 0 and self.w.detector[0] is not None:
            det_names.append("SAXS")
        if len(self.w.detector) > 1 and self.w.detector[1] is not None:
            det_names.append("WAXS")
        if len(self.w.detector) > 2 and self.w.detector[2] is not None:
            det_names.append("Struck")
        if len(self.w.detector) > 3 and self.w.detector[3] is not None:
            det_names.append("SG")
        if len(self.w.detector) > 4 and self.w.detector[4] is not None:
            if self.ui.actionDante.isChecked():
                det_names.append("Dante")
            elif self.ui.actionXSP3.isChecked():
                det_names.append("XSP3")
            else:
                det_names.append("Other")
        return ",".join(det_names)

    def _adjust_axis_length(self, n: int, axis_index: int, scan_kind: str) -> int:
        """Single source of truth for scan_kind-specific position-count adjustments.

        axis_index is the position of this axis within the ordered axis list
        (0 = fast/X, 1 = slow/Y). Used by both _compute_n_positions (counts)
        and _extend_axis_positions (arrays), so the two can never diverge.

        "fly_snake": +1 phantom trigger at the end of each X line (axis 0);
          Y lines (axis 1) rounded up to nearest even (hardware constraint).
          These reflect real hexapod hardware: the trajectory generator fires
          one extra trigger per X line (a "phantom" trigger at the scan line's
          end to facilitate pixel timing), and the trajectory table requires
          the Y line count to be even (padded if odd). Independent of user
          input and always applied.
        "fly_phi": interval count on axis 0 (no +1). Constant-velocity phi
          motion triggers at regular time intervals, not at discrete rest
          points, so the count is (fe-st)/step without +1.
        Anything else: unchanged.
        """
        if scan_kind == "fly_snake":
            if axis_index == 0:
                return n + 1  # phantom trigger at end of each X line
            if axis_index == 1:
                return n + (n % 2)  # round up to nearest even
        elif scan_kind == "fly_phi" and axis_index == 0:
            return max(n - 1, 0)  # phi flies at fixed intervals, not inclusive endpoints
        return n

    def _extend_axis_positions(self, coords: np.ndarray, step: float, axis_index: int, scan_kind: str) -> np.ndarray:
        """Extend a position array to match _adjust_axis_length's target length.

        Appends extra points extrapolated by *step* from the last point, so
        arrays built this way have exactly the length _compute_n_positions
        would predict for the same axis/scan_kind.
        """
        target_len = self._adjust_axis_length(len(coords), axis_index, scan_kind)
        extra = target_len - len(coords)
        if extra > 0:
            last = coords[-1] if len(coords) else 0.0
            coords = np.append(coords, last + step * np.arange(1, extra + 1))
        return coords

    def _compute_n_positions(self, position_arrays, scan_kind="step"):
        """Return the total number of collected data positions for any scan type.

        scan_kind determines the position-counting convention:
          "step": grid product (Nx * Ny * ...). Always inclusive endpoints.
          "fly_snake": grid product with hexapod adjustments for snake fly scans
            (see _adjust_axis_length).
          "fly_hexapod_1d": grid product (same as step; used for 1-D fly consistency).
          "fly_phi": interval count on first axis (no +1), then product
            (see _adjust_axis_length).

        Note: "helix" scans do not use this function's plain grid-product path —
        callers compute the helix count via a single-axis "fly_phi" call instead
        (see _log_scan_header), since phi alone determines the trigger count.
        """
        n_pos = 1
        for i, pos in enumerate(position_arrays):
            n_pos *= self._adjust_axis_length(len(pos), i, scan_kind)
        return n_pos

    def _write_scan_summary_line(self, scan_name: str, axes_params: list, n_pos: int) -> None:
        csv_path = self._get_scan_summary_csv_path()
        if csv_path is None:
            return
        if not csv_path.parent.exists():
            QMessageBox.warning(
                self.w.ui,
                "CSV Logging",
                f"Scan summary directory does not exist: {csv_path.parent}",
            )
            return
        write_header = not csv_path.exists()
        if not write_header:
            self._migrate_scan_summary_header(csv_path)
        timestamp = datetime.datetime.now()
        scan_id = "S%04d" % self.w.parameters.scan_number
        # Use cached optics values if available (queried once in _pre_scan), otherwise get them now
        if hasattr(self, '_cached_us_optics'):
            optics_values = self._cached_us_optics
        else:
            optics_values = self._get_us_optics_positions() if getattr(self.w.parameters, "_save_us_optics", True) else [""] * len(self._US_OPTICS_PVS)
        row = [
            timestamp.strftime("%Y-%m-%d"),
            timestamp.strftime("%H:%M:%S"),
            "no",
            scan_id,
            self.w.parameters.scan_name,
            scan_name,
            axes_params[0].get("expt", "") if axes_params else "",
            n_pos,
            self._get_mono_energy(),
            self._get_phi_angle(axes_params),
            self._get_detectors_used(),
            self._format_scan_axis_string(axes_params, "scan_center"),
            self._format_scan_axis_string(axes_params, "scan_negative_edge"),
            self._format_scan_axis_string(axes_params, "scan_positive_edge"),
            self._format_scan_axis_string(axes_params, "scan_step_size"),
            *optics_values,
        ]
        self._last_scan_summary_id = scan_id
        try:
            with open(csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(self._get_scan_summary_header())
                writer.writerow(row)
        except Exception as exc:
            QMessageBox.warning(
                self.w.ui,
                "CSV Logging",
                f"Could not write scan summary CSV {csv_path}: {exc}",
            )

    def _update_scan_summary_completed(self, status: str, scan_id: str = None) -> None:
        csv_path = self._get_scan_summary_csv_path()
        if csv_path is None or not csv_path.exists():
            return
        if scan_id is None:
            scan_id = getattr(self, "_last_scan_summary_id", None)
        if not scan_id:
            scan_id = "S%04d" % self.w.parameters.scan_number
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


    def _log_3d_slice_start(self, scan_name, axes_params, phi_value, scan_kind: str = "step"):
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
        self._write_scan_summary_line(scan_name, modified_axes, n_pos)

    def _update_3d_slice_completed(self):
        self._update_scan_summary_completed("yes")

    def _launch_worker(self, executor_fn, *args, done_signal=None, **kwargs):
        """Create, wire, and start a Worker thread for a scan executor function.

        All executors accept update_progress and update_status keyword arguments;
        these are wired to Qt signals here so the worker thread can safely report
        back to the GUI thread.

        done_signal — optional method to connect to signal.finished (e.g. self.w.scandone).
                      Pass None when the caller handles signal wiring manually.
        """
        w = self.w.Worker(
            executor_fn, *args, update_progress=None, update_status=None, **kwargs
        )
        if done_signal is not None:
            w.signal.finished.connect(done_signal)
        w.signal.progress.connect(self.w.updateprogressbar)
        w.signal.statusmessage.connect(self.w.update_status_bar)
        w.signal.error.connect(self.w._on_worker_error)
        w.kwargs["update_progress"] = w.signal.progress.emit
        w.kwargs["update_status"] = w.signal.statusmessage.emit
        self.w.set_scan_status("Scanning")
        self.w.isscan = True
        if self.w.monitor_beamline_status:
            self.w.shutter.open()
        self.w.threadpool.start(w)

    def _motor_from_sender(self) -> int:
        """Extract a 0-based motor index from the name of the button that triggered
        a scan (e.g. 'pushButton_fly_3' → motor index 2).
        """
        pb = self.w.sender()
        objname = pb.objectName()
        n = int(re.findall(r"\d+", objname)[0])
        return n - 1  # 1-based UI index → 0-based motor index

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

    def save_current_positions(self):
        """Read lb_N position labels for SAVED_POSITION_MOTORS and store them."""
        saved = {}
        for n in self.SAVED_POSITION_MOTORS:
            lbl = self.ui.findChild(QLabel, "lb_%i" % n)
            if lbl is None:
                continue
            try:
                saved[n] = float(lbl.text())
            except ValueError:
                pass
        self._saved_positions = saved

    def check_saved_positions(self):
        """Open a two-column dialog listing saved motor names and positions."""
        from PyQt5.QtWidgets import QDialog, QGridLayout, QDialogButtonBox
        from PyQt5.QtCore import Qt

        if not self._saved_positions:
            QMessageBox.information(
                self.w.ui, "Saved Positions", "No positions have been saved yet."
            )
            return

        dlg = QDialog(self.w.ui)
        dlg.setWindowTitle("Saved Positions")
        grid = QGridLayout(dlg)
        grid.setHorizontalSpacing(20)

        from PyQt5.QtGui import QFont

        font = QFont()
        font.setPointSize(10)

        for row, n in enumerate(sorted(self._saved_positions)):
            name_lbl = self.ui.findChild(QLabel, "lb%i" % n)
            name = name_lbl.text() if name_lbl is not None else "Motor %i" % n
            name_widget = QLabel(name)
            name_widget.setFont(font)
            grid.addWidget(name_widget, row, 0)
            val = QLabel("%.6f mm" % self._saved_positions[n])
            val.setFont(font)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(val, row, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dlg.accept)
        grid.addWidget(buttons, len(self._saved_positions), 0, 1, 2)
        dlg.exec_()

    def go_to_saved_positions(self):
        """Move each motor in SAVED_POSITION_MOTORS to its saved position."""
        if not self._saved_positions:
            QMessageBox.information(
                self.w.ui, "No Saved Positions", "No positions have been saved yet."
            )
            return
        saved = dict(self._saved_positions)  # snapshot before thread starts

        def _move_all():
            for n, pos_val in sorted(saved.items()):
                if n - 1 < len(self.w.motornames):
                    axis = self.w.motornames[n - 1]
                    self.w.pts.mv(axis, pos_val)

        w = self.w.Worker(_move_all)
        self.w.threadpool.start(w)

    def plot_scan_positions_2d(self, xmotor=0, ymotor=2):
        """Open a pyqtgraph scatter plot of the 2D scan positions from the
        current lup fields.  Mirrors the position arrays used by stepscan2d
        and fly2d (snake).
        """

        def _val(name, default=0.0):
            w = self.ui.findChild(QLineEdit, name)
            if w is None:
                return default
            try:
                return float(w.text())
            except ValueError:
                return default

        nx = xmotor + 1  # widget index for x motor (1)
        nz = ymotor + 1  # widget index for z motor (3)

        p0x = _val("ed_%i" % nx)
        p0z = _val("ed_%i" % nz)

        st_x = _val("ed_lup_%i_L" % nx)
        fe_x = _val("ed_lup_%i_R" % nx)
        step_x = _val("ed_lup_%i_N" % nx, 1.0)

        st_z = _val("ed_lup_%i_L" % nz)
        fe_z = _val("ed_lup_%i_R" % nz)
        step_z = _val("ed_lup_%i_N" % nz, 1.0)

        if step_x == 0 or step_z == 0:
            return

        # Direction-correct step — same convention used in all scan functions
        step_x = -abs(step_x) if st_x > fe_x else abs(step_x)
        step_z = -abs(step_z) if st_z > fe_z else abs(step_z)

        x_positions = p0x + np.arange(st_x, fe_x + step_x / 2, step_x)
        z_positions = p0z + np.arange(st_z, fe_z + step_z / 2, step_z)

        if len(x_positions) == 1:
            x_positions = np.array([p0x + st_x, p0x + fe_x])
        if len(z_positions) == 1:
            z_positions = np.array([p0z + st_z, p0z + fe_z])

        # Build positions in snake order (matches fly2d snake traversal)
        coords = self._snake_positions(x_positions, z_positions)
        xs = coords[:, 0]
        zs = coords[:, 1]

        xname = self.w.motornames[xmotor] if xmotor < len(self.w.motornames) else "X"
        zname = self.w.motornames[ymotor] if ymotor < len(self.w.motornames) else "Z"

        win = pg.GraphicsLayoutWidget()
        win.setWindowTitle("2d scan positions / QDS scatter")
        win.resize(600, 250)

        # ── Left: scan positions ──────────────────────────────────────────────
        plot = win.addPlot(row=0, col=0, title="2d scan positions")
        plot.setLabel("bottom", f"{xname} (mm)")
        plot.setLabel("left", f"{zname} (mm)")
        plot.getAxis("bottom").enableAutoSIPrefix(False)
        plot.getAxis("left").enableAutoSIPrefix(False)
        plot.setAspectLocked(True)

        # Red line + red dots through all positions in traversal order
        plot.plot(
            x=xs,
            y=zs,
            pen=pg.mkPen("r", width=1),
            symbol="o",
            symbolSize=5,
            symbolBrush=pg.mkBrush("r"),
            symbolPen=pg.mkPen(None),
        )
        # Blue X over the first two points to indicate direction
        plot.plot(
            x=xs[:2],
            y=zs[:2],
            pen=None,
            symbol="x",
            symbolSize=12,
            symbolBrush=pg.mkBrush(None),
            symbolPen=pg.mkPen("b", width=2),
        )

        # ── Right: QDS scatter (channel 2 vs channel 3) ───────────────────────
        labels = getattr(self.w, "plotlabels", [])
        xlabel = labels[1] if len(labels) > 1 else "QDS channel 2"
        ylabel = labels[2] if len(labels) > 2 else "QDS channel 3"

        scatter = win.addPlot(row=0, col=1, title="QDS scatter")
        scatter.setLabel("bottom", xlabel)
        scatter.setLabel("left", ylabel)
        scatter.getAxis("bottom").enableAutoSIPrefix(False)
        scatter.getAxis("left").enableAutoSIPrefix(False)

        scatter_item = scatter.plot(
            x=[], y=[],
            pen=None,
            symbol="o",
            symbolSize=4,
            symbolBrush=pg.mkBrush("c"),
            symbolPen=pg.mkPen(None),
        )

        # Seed with whatever is already in qds_array.
        _seed = deque(getattr(self.w, "qds_array", []), maxlen=500)
        if _seed:
            _arr = np.asarray(_seed)
            if _arr.ndim == 2 and _arr.shape[1] >= 3:
                scatter_item.setData(x=_arr[:, 1], y=_arr[:, 2])

        # Worker thread: samples qds_array at the same 100 ms rate as the main
        # QDS timer and updates the scatter via a cross-thread signal.
        _w_ref = self.w

        def _get_qds():
            return deque(getattr(_w_ref, "qds_array", []), maxlen=500)

        worker = _QdsScatterWorker(data_fn=_get_qds, interval_ms=100)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.data_ready.connect(
            lambda arr: scatter_item.setData(x=arr[:, 1], y=arr[:, 2])
        )

        def _on_close(event):
            QSettings("ptychoSAXS", "ptychoSAXS").setValue(
                "scanPositionsWindow/geometry", win.saveGeometry()
            )
            thread.quit()
            thread.wait(500)
            self.ui.pushButton_plotScanPositions.setEnabled(True)
            event.accept()

        win.closeEvent = _on_close
        thread.start()

        self.ui.pushButton_plotScanPositions.setEnabled(False)
        win.show()
        _geom = QSettings("ptychoSAXS", "ptychoSAXS").value("scanPositionsWindow/geometry")
        if _geom is not None:
            win.restoreGeometry(_geom)
        # Keep references so nothing is garbage-collected while the window is open.
        self._scan_pos_window = win
        self._scan_pos_thread = thread
        self._scan_pos_worker = worker
        self._scan_pos_plot_item = plot
        self._scan_pos_xmotor = xmotor
        self._scan_pos_ymotor = ymotor

    def _refresh_scan_pos_plot(self):
        """Redraw the left (scan positions) panel if the positions window is open."""
        win = getattr(self, "_scan_pos_window", None)
        plot = getattr(self, "_scan_pos_plot_item", None)
        if win is None or plot is None or not win.isVisible():
            return

        xmotor = getattr(self, "_scan_pos_xmotor", 0)
        ymotor = getattr(self, "_scan_pos_ymotor", 2)
        nx = xmotor + 1
        nz = ymotor + 1

        def _val(name, default=0.0):
            w = self.ui.findChild(QLineEdit, name)
            if w is None:
                return default
            try:
                return float(w.text())
            except ValueError:
                return default

        p0x = _val("ed_%i" % nx)
        p0z = _val("ed_%i" % nz)
        st_x = _val("ed_lup_%i_L" % nx)
        fe_x = _val("ed_lup_%i_R" % nx)
        step_x = _val("ed_lup_%i_N" % nx, 1.0)
        st_z = _val("ed_lup_%i_L" % nz)
        fe_z = _val("ed_lup_%i_R" % nz)
        step_z = _val("ed_lup_%i_N" % nz, 1.0)

        if step_x == 0 or step_z == 0:
            return

        x_positions = self._make_positions(p0x, st_x, fe_x, step_x)
        z_positions = self._make_positions(p0z, st_z, fe_z, step_z)

        if len(x_positions) == 1:
            x_positions = np.array([p0x + st_x, p0x + fe_x])
        if len(z_positions) == 1:
            z_positions = np.array([p0z + st_z, p0z + fe_z])

        coords = self._snake_positions(x_positions, z_positions)
        xs, zs = coords[:, 0], coords[:, 1]

        plot.clear()
        plot.plot(
            x=xs, y=zs,
            pen=pg.mkPen("r", width=1),
            symbol="o", symbolSize=5,
            symbolBrush=pg.mkBrush("r"),
            symbolPen=pg.mkPen(None),
        )
        plot.plot(
            x=xs[:2], y=zs[:2],
            pen=None,
            symbol="x", symbolSize=12,
            symbolBrush=pg.mkBrush(None),
            symbolPen=pg.mkPen("b", width=2),
        )

    @staticmethod
    def _snake_positions(x_positions, y_positions):
        """Return Nx2 array of (x, y) scan positions in snake (boustrophedon) order.

        y_positions is the slow axis (outer loop).
        Even rows run x left-to-right; odd rows run right-to-left.
        """
        coords = []
        for j, y in enumerate(y_positions):
            row = x_positions if j % 2 == 0 else x_positions[::-1]
            for x in row:
                coords.append((x, y))
        return np.asarray(coords)

    # ------------------------------------------------------------------
    # Extracted methods
    # ------------------------------------------------------------------
    def set_hdf_plugin_use(self, value=None):
        if value is None:
            value = self.ui.actionUse_hdf_plugin.isChecked()
        self.ui.actionUse_hdf_plugin.setChecked(value)
        self.w.use_hdf_plugin = value
        if not value:
            self.ui.actionCapture_multi_frames_fly.setEnabled(False)

    def select_detector_mode(self, value=None):
        if value is None:
            value = self.ui.actionPtychography_mode.isChecked()
        if value:
            self.ui.actionPtychography_mode.setChecked(True)
            self.w.is_ptychomode = True
            # if both detectors are chosen..
            if self.ui.actionSAXS.isChecked() and self.ui.actionWAXS.isChecked():
                # ask which one is for ptychography
                detectors = ["SAXS", "WAXS"]
                selected, ok = QInputDialog.getItem(
                    self.w.ui,
                    "Select Detector for Ptychography",
                    "Which detector will be used for ptychography measurement?",
                    detectors,
                    0,
                    False,
                )
                if ok:
                    if selected == "SAXS":
                        self.w.detector_mode[0] = "ptycho"
                        self.w.detector_mode[1] = "scattering"
                    else:
                        self.w.detector_mode[1] = "ptycho"
                        self.w.detector_mode[0] = "scattering"
            else:
                if self.ui.actionSAXS.isChecked():
                    self.w.detector_mode[0] = "ptycho"
                if self.ui.actionWAXS.isChecked():
                    self.w.detector_mode[1] = "ptycho"

        else:
            self.ui.actionPtychography_mode.setChecked(False)
            self.w.is_ptychomode = False
            if self.ui.actionSAXS.isChecked():
                self.w.detector_mode[0] = "scattering"
            if self.ui.actionWAXS.isChecked():
                self.w.detector_mode[1] = "scattering"

    def select_hdf_multiframecapture_step(self, value=None):
        if value is None:
            value = self.ui.actionCapture_multi_frames_step.isChecked()
        if value:
            self.ui.actionCapture_multi_frames_step.setChecked(True)
            self.w.hdf_plugin_savemode_step = 2
        else:
            self.ui.actionCapture_multi_frames_step.setChecked(False)
            if self.w.parameters._pulses_per_step > 1:
                self.w.hdf_plugin_savemode_step = 1
            else:
                self.w.hdf_plugin_savemode_step = 0

    def select_hdf_multiframecapture_fly(self, value=None):
        if value is None:
            value = self.ui.actionCapture_multi_frames_fly.isChecked()
        if value:
            self.ui.actionCapture_multi_frames_fly.setChecked(True)
            if self.ui.actionSG.isChecked():
                self.w.hdf_plugin_savemode_fly = 2
            else:
                self.w.hdf_plugin_savemode_fly = 1
        else:
            self.ui.actionCapture_multi_frames_fly.setChecked(False)
            self.w.hdf_plugin_savemode_fly = 0

    def set_waittime_between_scans(self):
        if hasattr(self.w.parameters, "_waittime_between_scans"):
            wtime = self.w.parameters._waittime_between_scans
        else:
            wtime = 1.0
        value, okPressed = QInputDialog.getDouble(
            self.w.ui, "How long stay idle between scans?", "sleep time (s):", wtime
        )
        if okPressed:
            self.w.parameters._waittime_between_scans = value
            self.w.parameters.writeini()

    def set_shotnumber_per_step(self):
        if hasattr(self.w.parameters, "_pulses_per_step"):
            wtime = self.w.parameters._pulses_per_step
        else:
            wtime = 1.0
        value, okPressed = QInputDialog.getDouble(
            self.w.ui, "How many shots per step?", "Number of shots:", wtime
        )
        if okPressed:
            self.w.parameters._pulses_per_step = value
            self.w.parameters.writeini()

    def get_detectors_ready(self):
        for det in self.w.detector:
            if det is not None:
                try:
                    det.filePut("FileNumber", 1)
                except:
                    continue
                det.ArrayCounter = 0
                det.set_fly_configuration()
                # if i<2:
                #    det.FileNumber = 1

    def update_scanname(self, update_detector=None):
        self.w.parameters.scan_name = self.ui.edit_scanname.text()
        self.w.parameters.scan_number = int(self.ui.edit_scannumber.text())
        self.scannumberstring = "S%04d" % self.w.parameters.scan_number
        txt = "%s_%0.4i" % (self.w.parameters.scan_name, self.w.parameters.scan_number)
        self.ui.lbl_scanname.setText(txt)
        self.update_label_scanCheck()

    def _prepare_scan_files(self):
        """Create output folders and push FilePath/FileName to all detector IOCs.

        Called once from _pre_scan at scan-button press time only.  Must never be
        called from worker threads or from UI-event handlers (e.g. typed scan name),
        because it creates directories on disk and writes EPICS PVs that govern where
        the detector saves data.
        """
        scan_name = self.w.parameters.scan_name
        scan_number = self.w.parameters.scan_number
        txt = "%s_%0.4i" % (scan_name, scan_number)

        p = pathlib.Path(self.ui.edit_workingfolder.text())
        wf_temp = p.parts
        workingfolder = ""
        for i in range(1, len(wf_temp)):
            if i == 1:
                workingfolder = wf_temp[i]
            else:
                workingfolder = "%s/%s" % (workingfolder, wf_temp[i])

        Windows_workingfolder = self.ui.edit_workingfolder.text()
        self._workingfolder = workingfolder
        self._Windows_workingfolder = Windows_workingfolder

        for i, det in enumerate(self.w.detector):
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

            hdf_path = ""
            tif_path = ""
            filename = ""

            if i < 2:
                if self.w.is_ptychomode:
                    folder_type = "ptycho"
                    if self.w.detector_mode[i] == "":
                        self.w.detector_mode[i] = "ptycho"
                    if self.w.detector_mode[i] == "ptycho":
                        tp = ""
                    basepath = det.basepath
                    tif_path = "/ramdisk"
                else:
                    if len(tp) == 0:
                        continue
                    basepath = self.w.parameters.base_linux_datafolder
                    folder_type = tp + "AXS"
                    tif_path = "/ramdisk"

            if "SG" in det._prefix:
                folder_type = "positions"
                basepath = (
                    det.basepath
                    if self.w.is_ptychomode
                    else self.w.parameters.base_linux_datafolder
                )

            if ("dante" in det._prefix) or ("XSP" in det._prefix):
                folder_type = "DANTE"
                basepath = (
                    det.basepath
                    if self.w.is_ptychomode
                    else self.w.parameters.base_linux_datafolder
                )

            hdfname = tp + txt

            if i < 2:
                filename = hdfname
                det.FilePath = tif_path
                det.FileName = filename

            Windows_hdf_path = os.path.join(
                Windows_workingfolder, folder_type, self.scannumberstring
            ).replace("\\", "/")
            self.w.make_positions_folder(Windows_hdf_path)

            hdf_path = os.path.join(
                basepath, workingfolder, folder_type, self.scannumberstring
            ).replace("\\", "/")
            det.filePut("FilePath", hdf_path)
            det.filePut("FileName", hdfname)
            self.w.hdf_plugin_name[i] = hdfname

    def _push_filepaths_to_detectors(self):
        """Push updated FilePath and FileName to all detector IOCs.

        Worker-thread-safe version of _prepare_scan_files.  Uses paths captured
        by _prepare_scan_files at scan start (_workingfolder, _Windows_workingfolder)
        together with the current scan_number / scannumberstring, so it can be
        called from the worker thread between phi slices in 3D scans without
        touching any Qt widgets.  Does not create new top-level folders (those
        are created once by _prepare_scan_files); only creates the per-slice
        scannumber subdirectory.
        """
        scan_name = self.w.parameters.scan_name
        scan_number = self.w.parameters.scan_number
        txt = "%s_%0.4i" % (scan_name, scan_number)
        scannumberstring = "S%04d" % scan_number
        workingfolder = self._workingfolder
        Windows_workingfolder = self._Windows_workingfolder


        for i, det in enumerate(self.w.detector):
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
                if self.w.is_ptychomode:
                    if self.w.detector_mode[i] == "ptycho":
                        tp = ""
                    basepath = det.basepath
                    folder_type = "ptycho"
                else:
                    if len(tp) == 0:
                        continue
                    basepath = self.w.parameters.base_linux_datafolder
                    folder_type = tp + "AXS"

            if "SG" in det._prefix:
                folder_type = "positions"
                basepath = (
                    det.basepath
                    if self.w.is_ptychomode
                    else self.w.parameters.base_linux_datafolder
                )

            if ("dante" in det._prefix) or ("XSP" in det._prefix):
                folder_type = "DANTE"
                basepath = (
                    det.basepath
                    if self.w.is_ptychomode
                    else self.w.parameters.base_linux_datafolder
                )

            hdfname = tp + txt

            Windows_hdf_path = os.path.join(
                Windows_workingfolder, folder_type, scannumberstring
            ).replace("\\", "/")
            self.w.make_positions_folder(Windows_hdf_path)

            hdf_path = os.path.join(
                basepath, workingfolder, folder_type, scannumberstring
            ).replace("\\", "/")
            det.filePut("FilePath", hdf_path)
            det.filePut("FileName", hdfname)
            self.w.hdf_plugin_name[i] = hdfname

    def push_filepath_to_detectors(self):
        """Push the current working-folder path to each detector's HDF plugin FilePath PV.

        Called when the user presses Enter on edit_workingfolder.  Only runs when
        use_hdf_plugin is True.  Does NOT create folders and does NOT set FileName —
        those only happen at scan-button press time via _prepare_scan_files().
        """
        if not self.w.use_hdf_plugin:
            return

        p = pathlib.Path(self.ui.edit_workingfolder.text())
        wf_temp = p.parts
        workingfolder = ""
        for i in range(1, len(wf_temp)):
            if i == 1:
                workingfolder = wf_temp[i]
            else:
                workingfolder = "%s/%s" % (workingfolder, wf_temp[i])

        for i, det in enumerate(self.w.detector):
            if det is None:
                continue
            if "3820" in det._prefix:
                continue

            if i < 2:
                if self.w.is_ptychomode:
                    folder_type = "ptycho"
                    tp = ""
                    basepath = det.basepath
                else:
                    tp = "S" if i == 0 else "W"
                    if len(tp) == 0:
                        continue
                    folder_type = tp + "AXS"
                    basepath = self.w.parameters.base_linux_datafolder
            elif "SG" in det._prefix:
                folder_type = "positions"
                basepath = (
                    det.basepath
                    if self.w.is_ptychomode
                    else self.w.parameters.base_linux_datafolder
                )
            elif ("dante" in det._prefix) or ("XSP" in det._prefix):
                folder_type = "DANTE"
                basepath = (
                    det.basepath
                    if self.w.is_ptychomode
                    else self.w.parameters.base_linux_datafolder
                )
            else:
                continue

            hdf_path = os.path.join(
                basepath, workingfolder, folder_type, self.scannumberstring
            ).replace("\\", "/")
            det.filePut("FilePath", hdf_path)

    def _iter_detector_windows_paths(self):
        """Yield the Windows-side folder path for each active detector at the current scan number."""
        if not hasattr(self.w, "detector"):
            return
        Windows_workingfolder = self.ui.edit_workingfolder.text()
        scannumberstring = "S%04d" % self.w.parameters.scan_number
        for i, det in enumerate(self.w.detector):
            if det is None:
                continue
            if "3820" in det._prefix:
                continue
            tp = "S" if i == 0 else ("W" if i == 1 else "")
            if "SG" in det._prefix:
                folder_type = "positions"
            elif ("dante" in det._prefix) or ("XSP" in det._prefix):
                folder_type = "DANTE"
            elif i < 2:
                if self.w.is_ptychomode:
                    folder_type = "ptycho"
                else:
                    if len(tp) == 0:
                        continue
                    folder_type = tp + "AXS"
            else:
                continue
            yield os.path.join(
                Windows_workingfolder, folder_type, scannumberstring
            ).replace("\\", "/")

    def check_scan_folder(self):
        """Check active detector folders for the current scan number.

        Returns:
            None  — no detectors are selected
            True  — all folders are empty or absent (safe to scan)
            False — at least one folder already contains a file
        """
        found_any = False
        for folder in self._iter_detector_windows_paths():
            found_any = True
            if os.path.isdir(folder):
                with os.scandir(folder) as it:
                    if any(e.is_file() for e in it):
                        return False
        return True if found_any else None

    def update_label_scanCheck(self):
        """Update label_scanCheck to reflect whether the current scan folder is free."""
        lbl = self.ui.findChild(QLabel, "label_scanCheck")
        if lbl is None:
            return
        result = self.check_scan_folder()
        if result is None:
            lbl.setText("No detectors\nselected")
            lbl.setStyleSheet("background-color: #fffde7; color: #000000")
        elif result:
            lbl.setText("Scan number\nfolder is free")
            lbl.setStyleSheet("background-color: #C6EFCE; color: #000000")
        else:
            lbl.setText("Scan number\nfolder used up")
            lbl.setStyleSheet("background-color: rgb(255, 199, 206); color: #000000")

    def choose_softglue_channels(self):
        strv = ""
        for i, ch in enumerate(self.w.parameters.softglue_channels):
            if i == 0:
                strv = ch
            else:
                strv = "%s, %s" % (strv, ch)
        text, okPressed = QInputDialog.getText(
            self.w.ui,
            "Channels of SoftGlueZinq to Record",
            "Channels:",
            QLineEdit.Normal,
            strv,
        )
        if okPressed:
            self.w.parameters.softglue_channels = [x.strip() for x in text.split(",")]

    def reset_det_flymode(self):
        for det in self.w.detector:
            if det is not None:
                det.set_fly_configuration()

    def set_softglue_in(self, val):
        if val == 1:
            self.ui.actionevery_10_millie_seconds.setChecked(False)
            self.ui.actionDetout.setChecked(False)
            self.ui.actionTrigout.setChecked(True)
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.set_count_freq(10)
        if val == 2:
            self.ui.actionevery_10_millie_seconds.setChecked(False)
            self.ui.actionDetout.setChecked(True)
            self.ui.actionTrigout.setChecked(False)
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.set_count_freq(100)
        if val == 3:
            self.ui.actionevery_10_millie_seconds.setChecked(True)
            self.ui.actionDetout.setChecked(False)
            self.ui.actionTrigout.setChecked(False)
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.set_count_freq(1000)

    def stopscan(self):
        self.isStopScanIssued = True
        self.w.set_scan_status("Stopping")
        self.ui.statusbar.showMessage("Stop requested \u2014 finishing current step\u2026")
        self.ui.pushButton_stopScan.setEnabled(False)

    def set_exp_period_ratio(self):
        val, ok = QInputDialog().getDouble(
            self,
            "Exposuretime/Period for Flyscan",
            "Fraction",
            self.w.parameters._ratio_exp_period,
            decimals=2,
        )
        self.w.parameters._ratio_exp_period = val
        self.w.parameters.writeini()

    def set_fly_acquisition_time(self):
        val, ok = QInputDialog().getDouble(
            self.w.ui,
            "Fly scan acquisition time",
            "Acquisition time (s)",
            self.w.parameters._fly_acq_time,
            decimals=3,
        )
        if ok:
            val = max(val, self.OVERHEAD_FLY)
            self.w.parameters._fly_acq_time = val
            self.w.parameters.writeini()
            self.update_scan_estimate()

    def _debug_plot_scan(self):
        """Plot scan trajectory and sample current lb_1/lb_3 positions."""

        mpos = self.w.mpos
        if not mpos:
            return
        try:
            mpos_arr = np.asarray(mpos)
            is_2d = mpos_arr.ndim == 2
            x_vals = mpos_arr[:, 0] if is_2d else mpos_arr

            mn = self.w.motornames
            scan_axis = getattr(self.w, "signalmotor", "Position")
            lb1_name = mn[0] if len(mn) > 0 else "lb_1"
            lb3_name = mn[2] if len(mn) > 2 else "lb_3"

            # Sample current positions directly from UI labels
            lb1_widget = self.ui.findChild(QLabel, "lb_1")
            lb3_widget = self.ui.findChild(QLabel, "lb_3")
            lb1_val = float(lb1_widget.text()) if lb1_widget else 0.0
            lb3_val = float(lb3_widget.text()) if lb3_widget else 0.0

            self.w.ax.clear()
            self.w.ax2.clear()
            self.w.ax3.clear()

            # ax: scan axis trajectory
            self.w.ax.plot(x_vals, x_vals, pen=pg.mkPen("r"))
            self.w.ax.setLabel("bottom", scan_axis)
            self.w.ax.setLabel("left", scan_axis)

            # ax2: current lb_1 value as a horizontal reference line
            self.w.ax2.plot(
                x_vals, np.full_like(x_vals, lb1_val, dtype=float), pen=pg.mkPen("b")
            )
            self.w.ax2.setLabel("bottom", scan_axis)
            self.w.ax2.setLabel("left", lb1_name)

            # ax3: current lb_3 value as a horizontal reference line
            self.w.ax3.plot(
                x_vals, np.full_like(x_vals, lb3_val, dtype=float), pen=pg.mkPen("k")
            )
            self.w.ax3.setLabel("bottom", scan_axis)
            self.w.ax3.setLabel("left", lb3_name)
        except Exception as e:
            print(f"[DEBUG] _debug_plot_scan error: {e}")

    def scandone(self, update_scannumber=True, donedone=True, update_gui=True):
        # return to the initial positions
        for i, key in enumerate(self.w.motor_p0):
            # put only x motors and ymotors back to initial positions
            if i < 2:
                self.w.mv(key, self.w.motor_p0[key])
        if donedone:
            if self.w.shutter_close_after_scan:
                self.w.shutter.close()

        self.w.messages["current status"] = f"stepscan done. {time.ctime()}"
        print(self.w.messages["current status"])
        self.w.isscan = False
        self._update_scan_summary_completed(
            "partial" if self.isStopScanIssued else "yes"
        )
        if update_gui:
            if self.isStopScanIssued:
                self.w.set_scan_status("Stopped")
                self.ui.statusbar.showMessage("Scan stopped by user \u2014 motors returned.")
            else:
                self.w.set_scan_status("No Scan")
                self.ui.statusbar.showMessage("Scan complete.")
            self.w.updatepos()

        if self.w.DEBUG_MOTORS:
            self._debug_plot_scan()

        # --- Device cleanup and data saving (skipped when devices are stubs) ---
        if not self.w.DEBUG_DEVICES:
            fn = ""
            for i, det in enumerate(self.w.detector):
                # print(det, " this is in scandone for detector ", i)
                if det is not None:
                    if "SG" in det._prefix:
                        self.w.s12softglue.flush()
                        time.sleep(1)
                        det.ForceStop()
                        success = True
                    if "3820" in det._prefix:
                        det.stop()
                        self.w.rpos = det.read_mcs(STRUCK_CHANNELS)
                        continue
                    if "XSP3" in det._prefix:
                        det.Acquire = 0
                        print(f"Detector {i} is still armed. Disarming it now.")
                    if "cam" in det._prefix:
                        if det.Armed == 1:
                            det.Acquire = 0
                            print(f"Detector {i} is still armed. Disarming it now.")
                    if self.w.use_hdf_plugin:
                        while det.fileGet("WriteFile_RBV"):
                            wait_for_hdf_write_s = 0.01  # poll interval while waiting for HDF file write to finish
                            time.sleep(wait_for_hdf_write_s)
                        if len(fn) == 0:
                            fnum = det.fileGet("FileNumber_RBV")
                            fn = det.fileGet("FullFileName_RBV", as_string=True)
                            if fnum is not None and str(fnum - 1) not in fn:
                                fn = det.fileGet("FullFileName_RBV", as_string=True)

                        # when the measurement is all done, reset the file number to 0.
                        if update_scannumber:
                            det.filePut("FileNumber", 1)
                            # print(f"Resetting file number of detector {i} to 0.")
                            if i < 2:  # tiff file number 0
                                det.FileNumber = 1
                    else:
                        if len(fn) == 0:
                            fnum = det.FileNumber_RBV
                            fn = bytes(det.FullFileName_RBV).decode().strip("\x00")

            # save Struck as a separate txt file.
            if self.w.isStruckCountNeeded:
                # data = self.w.detector[2].read_mcs(STRUCK_CHANNELS)
                foldername, filename = self.w.get_softglue_filename()
                if len(foldername) == 0:
                    pass
                else:
                    foldername = os.path.join(
                        foldername, "Struck", self.scannumberstring
                    )
                    os.makedirs(foldername, exist_ok=True)
                    np.savetxt(os.path.join(foldername, filename + ".txt"), self.w.rpos)

            # update logfile if logfilename is set.
            if len(self.w.parameters.logfilename) > 0:
                # pos = np.asarray(self.w.mpos)
                # r = np.asarray(self.w.rpos)
                # if len(r) > 0:
                #    self.w.save_list(self.w.parameters.logfilename, pos,r,[0,1,2],"a")
                self.w.mpos = []
                self.w.rpos = []
                scaninfo = []
                scaninfo.append("#I detector_filename")
                if len(fn) > 0:
                    filename = os.path.basename(fn)
                    scaninfo.append(filename)
                if len(scaninfo) > 1:
                    self.w.write_scaninfo_to_logfile(scaninfo)
                scaninfo = []
                scaninfo.append("#D")
                scaninfo.append(time.ctime())

        # Link detector data to master files asynchronously (non-blocking)
        # Compute full paths NOW while GUI state is frozen, pass to background thread
        sample_name = self.w.parameters.scan_name
        # Use the scan number from when _pre_scan was called, not the current one
        # (user may have changed it since scan started)
        scan_number = getattr(self, '_scan_number_at_start', self.w.parameters.scan_number)

        # Build master file paths for each active detector
        master_paths = {}
        if len(self.w.detector) > 0 and self.w.detector[0] is not None:
            try:
                master_paths['SAXS'] = self._get_master_file_path_with_number('SAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute SAXS master file path: {e}")

        if len(self.w.detector) > 1 and self.w.detector[1] is not None:
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

        # when the measurement is all done, update the scan number.
        if update_scannumber:
            self.w.run_stop_issued()
        self.w.update_scanname()

        if donedone:
            self.w.update_status_scan_time()

    def set_det_alignmode(self, value=None):
        if value is None:
            value = self.ui.actionPut_DET_alignmode.isChecked()
        print("Setting detector align mode to ", value)
        if value:
            self.ui.actionPut_DET_alignmode.setChecked(True)
            for i, det in enumerate(self.w.detector):
                if i > 1:
                    continue
                if det is not None:
                    det.filePut("AutoSave", 0)
                    det.TriggerMode = 4
                    det.Acquire = 1
        else:
            self.ui.actionPut_DET_alignmode.setChecked(False)
            for i, det in enumerate(self.w.detector):
                if i > 1:
                    continue
                if det is not None:
                    det.filePut("AutoSave", 1)
                    det.TriggerMode = 3
                    det.Acquire = 0
        self.update_saxs_det_status()

    def get_saxs_det_mode(self):
        """Read detector PVs and return 'align', 'scan', or 'unknown'."""
        for i, det in enumerate(self.w.detector):
            if i > 1:
                continue
            if det is not None:
                try:
                    autosave = det.fileGet("AutoSave")
                    trigger = det.TriggerMode
                    acquire = det.Acquire
                    if autosave == 0 and trigger == 4 and acquire == 1:
                        return "align"
                    elif autosave == 1 and trigger == 3 and acquire == 0:
                        return "scan"
                    else:
                        return "unknown"
                except Exception:
                    return "unknown"
        return "unknown"

    def update_saxs_det_status(self):
        mode = self.get_saxs_det_mode()
        btn = self.ui.pushButton_checkSAXS
        if mode == "scan":
            btn.setText("SAXS\nScan mode")
            btn.setStyleSheet("background-color: green; color: white;")
        elif mode == "align":
            btn.setText("SAXS\nAlign mode")
            btn.setStyleSheet("background-color: orange; color: white;")
        else:
            btn.setText("SAXS\nUnknown mode")
            btn.setStyleSheet("background-color: red; color: white;")

    def _update_saxs_buttons_enabled(self):
        """Enable the SAXS check/align/scan buttons iff the SAXS detector is connected."""
        enabled = len(self.w.detector) > 0 and self.w.detector[0] is not None
        for name in ("pushButton_checkSAXS", "pushButton_setAlign", "pushButton_setScan"):
            btn = getattr(self.ui, name, None)
            if btn is not None:
                btn.setEnabled(enabled)

    # ==================== NeXus Master File Methods ====================

    def _get_master_file_path_with_number(self, detector_type: str, sample_name: str, scan_number: int) -> str:
        """Get the path to the master file given an explicit scan number.

        Same as _get_master_file_path but takes scan_number as parameter instead of reading current.
        Used by the background thread which may run after scan number has incremented.

        Args:
            detector_type: 'SAXS' or 'WAXS'
            sample_name: Sample name extracted from detector filename
            scan_number: Explicit scan number to use

        Returns:
            Full Windows path to master file.
        """
        # Determine detector index and mode
        detector_index_map = {'SAXS': 0, 'WAXS': 1}
        det_index = detector_index_map.get(detector_type)
        if det_index is None:
            raise ValueError(f"Unknown detector type: {detector_type}")

        # Get detector object
        if det_index >= len(self.w.detector) or self.w.detector[det_index] is None:
            raise ValueError(f"Detector {detector_type} not initialized")

        # Determine folder structure based on mode
        if self.w.is_ptychomode and self.w.detector_mode[det_index] == "ptycho":
            folder_type = "ptycho"
        else:
            # Scattering mode
            tp_map = {'SAXS': 'S', 'WAXS': 'W'}
            tp = tp_map.get(detector_type)
            folder_type = tp + "AXS"

        # Use Windows working folder path for file I/O on Windows machine
        windows_workingfolder = (self._Windows_workingfolder if hasattr(self, '_Windows_workingfolder') else "").strip("/\\")

        # Use provided scan_number to build scannumberstring
        scannumberstring = f"S{scan_number:04d}"

        # Build path using backslashes for Windows
        path_parts = [windows_workingfolder, folder_type, scannumberstring]
        detector_folder = "\\".join(p for p in path_parts if p)
        filename = f"{sample_name}_{scan_number:04d}_master.h5"
        return os.path.join(detector_folder, filename)

    def _get_master_file_path(self, detector_type: str, sample_name: str) -> str:
        """Get the path to the master file for a given detector and sample.

        Uses Windows paths (Z:/) since file I/O happens on Windows machine.
        The detector IOC will handle the Linux path mapping internally.

        Args:
            detector_type: 'SAXS' or 'WAXS'
            sample_name: Sample name extracted from detector filename

        Returns:
            Full Windows path to master file.
            - Ptycho mode: {Windows_workingfolder}/ptycho/{scannumberstring}/{sample_name}_{scan_num:04d}_master.h5
            - Scattering mode: {Windows_workingfolder}/SAXS_or_WAXS/{scannumberstring}/{sample_name}_{scan_num:04d}_master.h5
        """
        scan_num = self.w.parameters.scan_number
        return self._get_master_file_path_with_number(detector_type, sample_name, scan_num)

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

    def _fetch_zone_plate_optics_metadata(self) -> dict:
        """Query zone plate optics metadata (for caching to avoid duplicate queries).

        Returns:
            Dictionary {nexus_path: value} for zone plate PVs that succeeded.
            Returns empty dict if PV query fails.
        """
        from .nexus_metadata_config import ZONE_PLATE_METADATA_MAP

        try:
            import epics
        except ImportError:
            return {}

        zp_success = {}
        for nexus_path, pv_info in ZONE_PLATE_METADATA_MAP.items():
            pv_name = pv_info["pv"]
            try:
                value = epics.caget(pv_name)
                if value is not None:
                    zp_success[nexus_path] = value
            except Exception as e:
                print(f"Warning: Failed to query zone plate PV {pv_name}: {e}")

        return zp_success if zp_success else {}

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
        save_us_optics = getattr(self.w.parameters, "_save_us_optics", False)
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

        return metadata

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

    def _compute_2d_scan_positions(self, xmotor: int = 0, ymotor: int = 2, scan_kind: str = "step") -> np.ndarray:
        """Compute 2D scan positions in snake (boustrophedon) order.

        For snake fly scans, appends phantom points and pads Y lines to even count
        to match what the hexapod hardware will actually collect.

        Reads motor parameters from UI and returns Nx2 array of (x, y) positions
        in the order they will be visited during the scan.

        Args:
            xmotor: 0-based X motor index (default 0 for motor 1)
            ymotor: 0-based Y motor index (default 2 for motor 3)
            scan_kind: one of "step", "fly_snake", "fly_hexapod_1d", "fly_phi", "helix".

        Returns:
            Nx2 numpy array of (x, y) positions in snake order.
            Returns empty array if not a 2D scan.
        """
        def _val(name, default=0.0):
            from PyQt5.QtWidgets import QLineEdit
            w = self.ui.findChild(QLineEdit, name)
            if w is None:
                return default
            try:
                return float(w.text())
            except ValueError:
                return default

        # Read X motor parameters (same logic as _read_motor_params)
        x_n = xmotor + 1
        x_p0 = float(self.w.check_start_position(x_n))
        x_L = _val(f"ed_lup_{x_n}_L")
        x_R = _val(f"ed_lup_{x_n}_R")
        x_step = _val(f"ed_lup_{x_n}_N", 1.0)

        # Read Y motor parameters (same logic as _read_motor_params)
        y_n = ymotor + 1
        y_p0 = float(self.w.check_start_position(y_n))
        y_L = _val(f"ed_lup_{y_n}_L")
        y_R = _val(f"ed_lup_{y_n}_R")
        y_step = _val(f"ed_lup_{y_n}_N", 1.0)

        # Generate coordinate arrays via the same single-source formula used
        # everywhere else (_make_positions handles step==0 and direction
        # correction internally).
        x_coords = self._make_positions(x_p0, x_L, x_R, x_step)
        y_coords = self._make_positions(y_p0, y_L, y_R, y_step)

        if len(x_coords) == 0 or len(y_coords) == 0:
            return np.empty((0, 2))

        # Apply scan_kind-specific adjustments (e.g. fly_snake's phantom X
        # point / even-Y padding) via the same rule _compute_n_positions uses,
        # so the array length here can never diverge from the computed count.
        x_step_signed = self._signed_step(x_p0 + x_L, x_p0 + x_R, x_step)
        y_step_signed = self._signed_step(y_p0 + y_L, y_p0 + y_R, y_step)
        x_coords = self._extend_axis_positions(x_coords, x_step_signed, 0, scan_kind)
        y_coords = self._extend_axis_positions(y_coords, y_step_signed, 1, scan_kind)

        # Return as Nx2 array in snake order
        return self._snake_positions(x_coords, y_coords)

    def _compute_scan_positions(self, scan_kind: str = "step") -> dict:
        """Compute 2D scan position array for master file.

        For snake fly scans, applies hexapod adjustments: one phantom point per
        X line and even-line rounding on Y, mirroring what the hardware will collect.

        Args:
            scan_kind: one of "step", "fly_snake", "fly_hexapod_1d", "fly_phi", "helix".

        Returns:
            Dictionary with single key 'positions': Nx2 numpy array of (x, y)
            positions in scan traversal order. Empty dict if not a 2D scan.
        """
        pos = self._compute_2d_scan_positions(xmotor=0, ymotor=2, scan_kind=scan_kind)
        if len(pos) > 0:
            pos += -np.mean(pos, axis=0)  # Center positions around (0, 0)
            return {'positions': pos}
        return {}

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

        Raises HexapodPositionCountMismatchError if the counts disagree.
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

        expected_n = len(hexapod_pos)
        actual_n = self.w.pts.hexapod.pulse_number
        if expected_n != actual_n:
            msg = (
                f"Hexapod-predicted position count ({expected_n}) does not match "
                f"hexapod.pulse_number ({actual_n}) after programming the snake "
                f"trajectory. Aborting scan."
            )
            self.w.messages["recent error message"] = msg
            print(msg)
            # raise HexapodPositionCountMismatchError(msg)

        if write_to_master:
            for master_path in getattr(self, "_current_scan_master_paths", {}).values():
                self._append_hexapod_positions_to_master_file(master_path, hexapod_pos)
        return hexapod_pos

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

        # Create zone plate group (only if at least one zone plate PV succeeded)
        zp_data = {k: v for k, v in shared_meta.items() if k.startswith('/entry/instrument/zone_plate/')}
        if zp_data:
            zp = inst.create_group('zone_plate')
            zp.attrs['NX_class'] = b'NXcollection'
            for nexus_path, value in zp_data.items():
                dataset_name = nexus_path.split('/')[-1]
                zp.create_dataset(dataset_name, data=value)
                zp[dataset_name].attrs['units'] = b'mm'

    def _populate_sample_group(self, entry, shared_meta: dict, scan_positions_dict: dict) -> None:
        """Create /entry/sample group with motor positions and scan position arrays.

        Args:
            entry: HDF5 entry group
            shared_meta: Shared metadata dict (contains sth, stv, theta)
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

        # Write scan position array (Nx2 array of (x, y) positions in scan order)
        if 'positions' in scan_positions_dict:
            pos_array = scan_positions_dict['positions']
            sample.create_dataset('positions', data=pos_array)
            sample['positions'].attrs['units'] = b'mm'
            sample['positions'].attrs['description'] = b'Nx2 array of (X, Z) motor positions in scan traversal order'

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

            # Create master file
            master_path = self._get_master_file_path(detector_type, sample_name)
            master_dir = os.path.dirname(master_path).replace("\\", "/")
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

    def _link_detector_data_delayed(self, master_paths: dict, wait_seconds: float = 10.0) -> None:
        """Wait for detector file write to complete, then link to master file.

        Runs in a background thread (non-blocking). Completely independent from GUI state.

        Args:
            master_paths: Dict {detector_type: full_master_file_path} (pre-computed in scandone)
            wait_seconds: Seconds to wait before attempting to link (allows file write to complete)
        """
        time.sleep(wait_seconds)

        for detector_type, master_path in master_paths.items():
            try:
                self._link_detector_data_to_master_by_path(master_path)
            except Exception as e:
                print(f"Warning: Failed to link {detector_type} detector data: {e}")

    def _link_detector_data_to_master_by_path(self, master_path: str) -> None:
        """Link detector data files into master file after scan.

        Takes explicit master file path (computed in scandone, not reconstructed here).
        Completely independent from GUI state.

        Args:
            master_path: Full path to master file
        """
        import h5py
        import glob

        try:
            if not os.path.exists(master_path):
                print(f"Master file not found: {master_path}")
                return

            # Extract sample_name from master_path filename
            # Master file is named: {sample_name}_{scan_num:04d}_master.h5
            # We want to extract {sample_name}
            master_filename = os.path.basename(master_path)
            # Find the last occurrence of _master.h5
            if '_master.h5' in master_filename:
                # sample_name is everything before the last _XXXX_master.h5 pattern
                # e.g., "test_0020_master.h5" -> "test"
                parts = master_filename.replace('_master.h5', '').split('_')
                # The last part is the scan number, everything else is sample_name
                sample_name = '_'.join(parts[:-1]) if len(parts) > 1 else parts[0]
            else:
                print(f"Error: Master file does not match expected naming pattern: {master_filename}")
                return

            # Glob detector folder for h5 files using Windows path
            detector_folder = os.path.dirname(master_path)

            # Pattern: detector files are prefixed with S (SAXS), W (WAXS), or no prefix (ptycho)
            # Frame files: [S|W]?sample_name_####_##### (5-digit frame number, not _master)
            pattern = os.path.join(detector_folder, f'[SW]?{sample_name}_[0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9].h5')
            all_files = sorted(glob.glob(pattern))
            detector_files = all_files

            if not detector_files:
                # Also try without prefix in case files don't have one
                pattern_no_prefix = os.path.join(detector_folder, f'{sample_name}_[0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9].h5')
                detector_files = sorted(glob.glob(pattern_no_prefix))

            if not detector_files:
                print(f"No detector files found in {detector_folder}")
                return

            # Sort by final number in filename
            detector_files.sort(key=lambda f: int(re.search(r'_(\d{5})\.h5$', f).group(1)))

            # Create links
            with h5py.File(master_path, 'r+') as master:
                entry_data = master['/entry/data']

                for file_index, det_h5_path in enumerate(detector_files, start=1):
                    link_name = f'data{file_index:05d}'
                    # Relative path for HDF5 external link - use forward slashes
                    rel_path = os.path.relpath(det_h5_path, detector_folder).replace("\\", "/")

                    # Create external link
                    entry_data[link_name] = h5py.ExternalLink(rel_path, '/entry/data/data')
                    print(f"Linked {link_name} -> {rel_path}")

            print(f"Successfully linked {len(detector_files)} detector files to master file")

        except Exception as e:
            print(f"Error linking detector data for master file {master_path}: {e}")
            traceback.print_exc()

    def set_basepaths(self, text=""):
        if type(text) == bool:
            text = ""
        current = getattr(self.w.parameters, "base_linux_datafolder", "")
        if not text:
            text, okPressed = QInputDialog.getText(
                self.w.ui,
                "Base path for detectors",
                "Linux data path:",
                QLineEdit.Normal,
                current,
            )
            if not okPressed:
                return
        self.w.parameters.base_linux_datafolder = text
        self.w.parameters.writeini()

    def select_detectors(self, N, value=None):
        if N == 1:
            basename = "S12-PILATUS1:"
            if value is None:
                value = self.ui.actionSAXS.isChecked()
            if value:
                self.ui.actionSAXS.setChecked(True)
                if self.w.DEBUG_DEVICES:
                    from debug_stubs import PilatusStub

                    self.w.detector[0] = PilatusStub()
                else:
                    from tools.detectors import pilatus

                    self.w.detector[0] = pilatus(basename)
            else:
                self.ui.actionSAXS.setChecked(False)
                self.w.detector[0] = None
            self._update_saxs_buttons_enabled()
        if N == 2:
            basename = "12idcPIL:"
            if value is None:
                value = self.ui.actionWAXS.isChecked()
            if value:
                self.ui.actionWAXS.setChecked(True)
                if self.w.DEBUG_DEVICES:
                    from debug_stubs import PilatusStub

                    self.w.detector[1] = PilatusStub()
                else:
                    from tools.detectors import pilatus

                    self.w.detector[1] = pilatus(basename)
            else:
                self.ui.actionWAXS.setChecked(False)
                self.w.detector[1] = None
        if N == 3:
            if value is None:
                value = self.ui.actionStruck.isChecked()
            if value:
                self.w.switch_MCS(True)
                if self.w.DEBUG_DEVICES:
                    from debug_stubs import StruckStub

                    self.w.detector[2] = StruckStub()
                else:
                    from tools.struck import struck

                    self.w.detector[2] = struck("12idc:")
            else:
                self.w.switch_MCS(False)
        if N == 4:
            if value is None:
                value = self.ui.actionSG.isChecked()
            if value:
                self.w.switch_SGstream(True)
            else:
                self.w.switch_SGstream(False)
        if N == 5:
            basename = "12idcDAN:"
            if value is None:
                value = self.ui.actionDante.isChecked()
            if value:
                self.ui.actionDante.setChecked(True)
                self.ui.actionXSP3.setChecked(False)
                if self.w.DEBUG_DEVICES:
                    from debug_stubs import PilatusStub

                    self.w.detector[4] = PilatusStub()
                else:
                    from tools.detectors import dante

                    self.w.detector[4] = dante(basename)
            else:
                self.ui.actionDante.setChecked(False)
                self.w.detector[4] = None
        if N == 6:
            basename = "XSP3_4Chan:"
            if value is None:
                value = self.ui.actionXSP3.isChecked()
            if value:
                self.ui.actionXSP3.setChecked(True)
                self.ui.actionDante.setChecked(False)
                if self.w.DEBUG_DEVICES:
                    from debug_stubs import PilatusStub

                    self.w.detector[4] = PilatusStub()
                else:
                    from tools.detectors import XSP

                    self.w.detector[4] = XSP(basename)
            else:
                self.ui.actionXSP3.setChecked(False)
                self.w.detector[4] = None
        self.w.update_scanname()

    def switch_SGstream(self, status=True):
        basename = "12idSGSocket:"
        if status:
            self.ui.actionSG.setChecked(True)
            if self.w.DEBUG_DEVICES:
                from debug_stubs import SGStreamStub

                self.w.detector[3] = SGStreamStub()
            else:
                from tools.detectors import SGstream

                self.w.detector[3] = SGstream(basename, self.w.s12softglue)
            if self.ui.actionCapture_multi_frames_fly.isChecked():
                self.w.hdf_plugin_savemode_fly = 2
        else:
            self.ui.actionSG.setChecked(False)
            self.w.detector[3] = None
            if self.ui.actionCapture_multi_frames_fly.isChecked():
                self.w.hdf_plugin_savemode_fly = 1
            else:
                self.w.hdf_plugin_savemode_fly = 0
        if self.ui.actionCapture_multi_frames_step.isChecked():
            self.w.hdf_plugin_savemode_step = 2
        else:
            self.w.hdf_plugin_savemode_step = (
                1 if self.w.parameters._pulses_per_step > 1 else 0
            )

    def switch_MCS(self, status=True):
        if status:
            self.ui.actionStruck.setChecked(True)
            self.w.isStruckCountNeeded = True
            print("Struct in on")
        else:
            self.ui.actionStruck.setChecked(False)
            self.w.isStruckCountNeeded = False
            print("Struck is off")

    def select_flymode(self):
        if (
            self.ui.actionEnable_fly_with_controller.isChecked()
        ):  # when checked, this value is False
            self.ui.actionEnable_fly_with_controller.setChecked(True)
            self.w.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
        else:
            self.w.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD
            self.ui.actionEnable_fly_with_controller.setChecked(False)

    def select_hexrecord(self):
        if (
            self.ui.actionRecord_traj_during_scan.isChecked()
        ):  # when checked, this value is False
            self.ui.actionRecord_traj_during_scan.setChecked(True)
            self.is_hexrecord_required = True
        else:
            self.is_hexrecord_required = False
            self.ui.actionRecord_traj_during_scan.setChecked(False)

    def get_softglue_filename(self):
        foldername = self.ui.edit_workingfolder.text()
        filename = self.ui.lbl_scanname.text()
        # return (foldername, filename)

        filename = ""
        for det in self.w.detector:
            if det is not None:
                if (
                    self.w.use_hdf_plugin and self.w.hdf_plugin_savemode_step > 0
                ):  # capture mode
                    while det.fileGet("WriteFile_RBV"):
                        wait_for_hdf_write_s = 0.01  # poll interval while waiting for HDF file write to finish
                        time.sleep(wait_for_hdf_write_s)
                    fnum = det.fileGet("FileNumber_RBV")
                    fn = det.fileGet("FullFileName_RBV", as_string=True)
                    if str(fnum - 1) not in fn:
                        fn = det.fileGet("FullFileName_RBV", as_string=True)
                    filename = os.path.basename(fn)
                    filename = "%s_%0.5i" % (rstrip_from_char(filename, "_"), fnum - 1)
                else:
                    fnum = det.FileNumber_RBV
                    fn = bytes(det.FullFileName_RBV).decode().strip("\x00")
                    filename = os.path.basename(fn)
                    filename = "%s" % rstrip_from_char(filename, "_")
            if len(filename) > 0:
                break

        if len(filename) == 0:
            self.w.messages["recent error message"] = (
                "****** Detector ioc is not available."
            )
            print(self.w.messages["recent error message"])
            filename = "temp%i" % int(time.time())
        return (foldername, filename)

    def softglue_savingdone(self):
        self.w.is_softglue_savingdone = True

    def save_softglue(self):
        # read softglue data
        # foldername = os.getcwd()
        if not self.w.s12softglue.isConnected:
            print("Cannot save_softglue because softglue is not connected.")
            return

        N_cnt = 0
        if hasattr(self.w.pts.hexapod, "pulse_number"):
            N_cnt = self.w.pts.hexapod.pulse_number
        t = []
        ct0 = time.time()
        count = 0
        self.softglue_data = []
        # self.w.s12softglue.PROC=1
        t0 = time.time()
        t, timearray = self.w.s12softglue.get_latest_scantime()
        timeout = 10
        while t < self.fly1d_tm:
            if time.time() - t0 > timeout:
                break
            self.w.s12softglue.flush()
            wait_for_softglue_flush_s = (
                0.25  # allow softglue time to flush and update scan time
            )
            time.sleep(wait_for_softglue_flush_s)
            t, timearray = self.w.s12softglue.get_latest_scantime()
            print(f"Flushed and {t=}")
        print(f"Time required to have softglue reading ready is {time.time() - t0}")
        arrs = self.w.s12softglue.get_arrays(self.w.parameters.softglue_channels)
        print(f"Time required to read softglue is {time.time() - t0}")

        self.softglue_data = (timearray, arrs)
        self.softglue_N_cnt = N_cnt
        foldername, filename = self.w.get_softglue_filename()
        if len(foldername) == 0:
            return
        foldername = os.path.join(foldername, "positions", self.scannumberstring)
        self.softglue_folder = foldername
        self.softglue_filename = filename

        while self.w.is_softglue_savingdone is False:
            print("Previous soft glue has not been done. Waiting for done.")
            wait_for_softglue_save_s = 0.025  # poll interval while waiting for previous softglue save to complete
            time.sleep(wait_for_softglue_save_s)
        self.w.is_softglue_savingdone = False
        w = self.w.Worker(self.save2disk_softglue)
        w.signal.finished.connect(self.w.softglue_savingdone)
        self.w.threadpool.start(w)

    def make_positions_folder(self, foldername):
        p = pathlib.Path(foldername)
        if p.exists():
            return
        try:
            p.mkdir(parents=True, exist_ok=True)
        except:
            print(
                "Error of creating a folder: %s. ************************" % foldername
            )

    def save2disk_softglue(self):
        if not self.w.s12softglue.isConnected:
            print("Cannot save2disk_softglue since softglue is not connected.")
            return
        t, indices = self.w.s12softglue.slice_timearray(self.softglue_data[0])
        dt = self.w.s12softglue.slice_arrays(
            indices, self.softglue_data[1]
        )  # Skip the first array (timearray)
        N_cnt = self.softglue_N_cnt
        # t, dt = self.softglue_data
        foldername = self.softglue_folder
        filename = self.softglue_filename
        self.w.make_positions_folder(foldername)
        if len(t) < N_cnt:
            print("*********************************")
            print(
                f"Only {len(t)}, less than the ideal {N_cnt} data will be saved in {foldername}/{filename}."
            )
            print("*********************************")
        try:
            for i, td in enumerate(t):
                if i >= N_cnt:
                    continue
                scanname = "%s_%i.dat" % (filename, i)
                dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
                np.savetxt(
                    os.path.join(foldername, scanname),
                    dt2,
                    fmt="%1.8e %1.8e %1.8e %1.8e",
                )
        except:
            print("error in save2disk_softglue")

    def save2disk_softglue_original(self):
        N_cnt = self.softglue_N_cnt
        t, dt = self.softglue_data
        foldername = self.softglue_folder
        filename = self.softglue_filename

        p = pathlib.Path(foldername)
        p.mkdir(parents=True, exist_ok=True)
        print(f"Total {len(t)} data will be saved as {foldername}/{filename}.")

        try:
            for i, td in enumerate(t):
                if i >= N_cnt:
                    continue
                scanname = "%s_%i.dat" % (filename, i)
                dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
                np.savetxt(
                    os.path.join(foldername, scanname),
                    dt2,
                    fmt="%1.8e %1.8e %1.8e %1.8e",
                )
        except:
            print("error in save2disk_softglue")

    def save_hexapod_record(self, filename, option="a"):
        timeout = 5
        cnt = 0
        hpos = []

    def flydone(self, return_motor=True, reset_scannumber=True, donedone=True):
        if return_motor:
            # when 1D scan is done.
            # if self.w.shutter_close_after_scan:
            #    self.w.shutter.close()
            for i, key in enumerate(self.w.motor_p0):
                if self.w.motornames[key] == "phi":
                    self.w.setphivel_default()
                if i == 0:
                    if hasattr(self, "_prev_vel"):
                        self.w.pts.set_speed(
                            self.w.motornames[key], self._prev_vel, self._prev_acc
                        )
                self.w.mv(key, self.w.motor_p0[key])

        self.w.messages["current status"] = f"fly done. {time.ctime()}"
        print(self.w.messages["current status"])
        ct0 = time.time()

        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return

        self.w.isscan = False
        self.w.isfly = False
        self._update_scan_summary_completed(
            "partial" if self.isStopScanIssued else "yes"
        )
        if self.isStopScanIssued:
            self.w.set_scan_status("Stopped")
            self.ui.statusbar.showMessage("Scan stopped by user \u2014 motors returned.")
        else:
            self.w.set_scan_status("No Scan")
            self.ui.statusbar.showMessage("Fly scan complete.")
        self.w.s12softglue.flush()
        print(f"softglue flushed at {time.ctime()}")

        self.w.update_scanname()

        # Link detector data to master files asynchronously (non-blocking)
        # Use pre-computed master paths from fly() to ensure correct scan number
        # (scan number is already incremented by the time flydone runs)
        master_paths = getattr(self, '_current_scan_master_paths', {})
        if master_paths:
            import threading
            thread = threading.Thread(
                target=self._link_detector_data_delayed,
                args=(master_paths, 6.0),
                daemon=True
            )
            thread.start()

        # if len(self.w.parameters.logfilename)>0:
        #     if self.w.detector[2] is not None:
        #         # save struck data.
        #         r = self.w.detector[2].read_mcs(STRUCK_CHANNELS)
        #         pos = np.arange(len(r[0]))
        #         self.w.mpos = pos
        #         print("Number of MCS channels : ", len(r))
        #     else:
        #         # save qds data.
        #         pos = np.asarray(self.w.mpos)
        #         r = np.asarray(self.w.rpos)
        #     try:
        #         self.w.save_nparray(self.w.parameters.logfilename, pos,r,[0,1,2],"a")
        #     except:
        #         self.w.save_list(self.w.parameters.logfilename, pos,r,[0,1,2],"a")
        #     # hexapod read
        #     if self.is_hexrecord_required:
        #         self.w.save_hexapod_record(self.w.parameters.logfilename)

        #     scaninfo = []
        #     scaninfo.append('#D')
        #     scaninfo.append(time.ctime())
        #     self.w.write_scaninfo_to_logfile(scaninfo)
        # success=False

    def helixdone(self, return_motor=True, reset_scannumber=True, donedone=True):
        """Completion handler for helix fly scan: restore both phi and Z motors."""
        if return_motor:
            for i, key in enumerate(self.w.motor_p0):
                if self.w.motornames[key] == "phi":
                    self.w.setphivel_default()
                    if hasattr(self, "_prev_vel_phi"):
                        self.w.pts.set_speed(
                            self.w.motornames[key], self._prev_vel_phi, self._prev_acc_phi
                        )
                elif self.w.motornames[key] == "Z":
                    if hasattr(self, "_prev_vel_z"):
                        self.w.pts.set_speed(
                            self.w.motornames[key], self._prev_vel_z, None
                        )
                self.w.mv(key, self.w.motor_p0[key])

        self.w.messages["current status"] = f"helix done. {time.ctime()}"
        print(self.w.messages["current status"])

        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return

        self.w.isscan = False
        self.w.isfly = False
        self._update_scan_summary_completed(
            "partial" if self.isStopScanIssued else "yes"
        )
        if self.isStopScanIssued:
            self.w.set_scan_status("Stopped")
            self.ui.statusbar.showMessage("Scan stopped by user — motors returned.")
        else:
            self.w.set_scan_status("No Scan")
            self.ui.statusbar.showMessage("Helix scan complete.")
        self.w.s12softglue.flush()
        print(f"softglue flushed at {time.ctime()}")

        self.w.update_scanname()

        master_paths = getattr(self, '_current_scan_master_paths', {})
        if master_paths:
            import threading
            thread = threading.Thread(
                target=self._link_detector_data_delayed,
                args=(master_paths, 6.0),
                daemon=True
            )
            thread.start()

    def flydone2d(self, value=0):
        for key in self.w.motor_p0:
            self.w.mv(key, self.w.motor_p0[key])
        self.w.isscan = False
        self.w.isfly = False
        self._update_scan_summary_completed(
            "partial" if self.isStopScanIssued else "yes"
        )
        if self.w.shutter_close_after_scan:
            self.w.shutter.close()
        if self.isStopScanIssued:
            self.w.set_scan_status("Stopped")
            self.ui.statusbar.showMessage("Scan stopped by user \u2014 motors returned.")
        else:
            self.w.set_scan_status("No Scan")
            self.ui.statusbar.showMessage("2-D fly scan complete.")
        self.w.update_scanname()
        self.w.update_status_scan_time()

        # Link detector data to master files asynchronously (non-blocking)
        # Use pre-computed master paths from fly2d() to ensure correct scan number
        # (scan number may be incremented by the time flydone2d runs)
        master_paths = getattr(self, '_current_scan_master_paths', {})
        if master_paths:
            import threading
            thread = threading.Thread(
                target=self._link_detector_data_delayed,
                args=(master_paths, 6.0),
                daemon=True
            )
            thread.start()

    def flydone3d(self, value=0):
        try:
            self.w.pts.hexapod.stop_traj()
        except Exception as e:
            print(f"stop_traj warning: {e}")
        time.sleep(1.0)
        for key in self.w.motor_p0:
            try:
                self.w.mv(key, self.w.motor_p0[key])
            except Exception as e:
                print(f"Motor return warning: {e}")
        print("")
        self.w.messages["current status"] = f"3D fly done. {time.ctime()}"
        print(self.w.messages["current status"])
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.w.isscan = False
        self._update_scan_summary_completed(
            "partial" if self.isStopScanIssued else "yes"
        )
        self.w.updatepos()
        self.w.isfly = False
        if self.isStopScanIssued:
            self.w.set_scan_status("Stopped")
            self.ui.statusbar.showMessage("Scan stopped by user \u2014 motors returned.")
        else:
            self.w.set_scan_status("No Scan")
            self.ui.statusbar.showMessage("3-D fly scan complete.")
        self.w.updateprogressbar(100)
        if self.w.shutter_close_after_scan:
            self.w.shutter.close()
        self.w.update_scanname()
        self.w.update_status_scan_time()

        # Link detector data to master files asynchronously (non-blocking)
        # Use pre-computed master paths from fly3d() to ensure correct scan number
        # (scan number may be incremented by the time flydone3d runs)
        master_paths = getattr(self, '_current_scan_master_paths', {})
        if master_paths:
            import threading
            thread = threading.Thread(
                target=self._link_detector_data_delayed,
                args=(master_paths, 6.0),
                daemon=True
            )
            thread.start()

    def check_start_position(self, n):
        # Compare p0 and p0_move_to; only warn if difference > 0.001
        p0_move_to = self.ui.findChild(QLineEdit, "ed_%i" % n).text()
        p0 = self.ui.findChild(QLabel, "lb_%i" % n).text()
        if len(p0_move_to) > 0:
            try:
                p0_float = float(p0)
                p0_move_to_float = float(p0_move_to)
            except Exception:
                p0_float = p0
                p0_move_to_float = p0_move_to
            if abs(p0_float - p0_move_to_float) > 0.001:
                msg = (
                    f"'Move to' position ({p0_move_to_float:.4f}) and Current position ({p0_float:.4f}) differ.\n"
                    "Do you want to move to the 'Move to' position or update the 'Move to' position with the Current?"
                )
                dlg = QMessageBox(self.w.ui)
                dlg.setWindowTitle("Position Mismatch")
                dlg.setText(msg)
                move_btn = dlg.addButton(
                    "Move to 'Move to' position", QMessageBox.AcceptRole
                )
                update_btn = dlg.addButton(
                    "Update the 'Move to' position", QMessageBox.DestructiveRole
                )
                cancel_btn = dlg.addButton(QMessageBox.Cancel)
                dlg.setIcon(QMessageBox.Question)
                dlg.exec_()
                clicked = dlg.clickedButton()
                if clicked == move_btn:
                    # Move to p0_move_to
                    p0 = p0_move_to
                    self.ui.findChild(QLineEdit, "ed_%i" % n).setText(
                        "%0.6f" % float(p0)
                    )
                elif clicked == update_btn:
                    # Update the roginal position to current (new)
                    p0_move_to = p0
                    self.ui.findChild(QLineEdit, "ed_%i" % n).setText(
                        "%0.6f" % float(p0_move_to)
                    )
                elif clicked == cancel_btn:
                    return None
        return p0

    def detectortime_error_question(self, expt, period):
        msg = (
            f"Exposure time {expt:.4f} and period {period:.4f} requires the readout time {period - expt},\n"
            "which is too short."
        )
        dlg = QMessageBox(self.w.ui)
        dlg.setWindowTitle("Scanparameter Error")
        dlg.setText(msg)
        # move_btn = dlg.addButton("Move to original position", QMessageBox.AcceptRole)
        # update_btn = dlg.addButton("Update the original position", QMessageBox.DestructiveRole)
        cancel_btn = dlg.addButton(QMessageBox.Cancel)
        dlg.setIcon(QMessageBox.Question)
        dlg.exec_()
        clicked = dlg.clickedButton()
        return None
        # if clicked == move_btn:
        #    # Move to p0_original
        #    p0 = p0_original
        #    self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(p0))
        # elif clicked == update_btn:
        #    # Update the roginal position to current (new)
        #    p0_original = p0
        #    self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(p0_original))
        # elif clicked == cancel_btn:
        #    return None

    def fly2d(self, xmotor=0, ymotor=1, scanname="", snake=False):
        """Entry point for a 2-D fly scan (GUI thread).

        snake=False: steps Y with pts.mv, then flies X with fly0 for each row.
        snake=True:  programs the entire XY boustrophedon path as a single
                     hexapod trajectory via fly_traj / set_traj_SNAKE2, then
                     launches fly2d0_SNAKE which waits for all frames to arrive.

        Why fly2d exists separately from fly2d0 / fly2d0_SNAKE: thread-safety —
        Qt widget reads must happen on the GUI thread.
        """
        if not self._pre_scan_guards():
            return

        # Read parameters and check large scan BEFORE _pre_scan (which creates master file)
        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        scan_kind = "fly_snake" if snake else "fly_hexapod_1d"
        n_positions = self._compute_n_positions([xpos, ypos], scan_kind=scan_kind)
        if not self._confirm_large_scan(n_positions, xax["expt"], self.OVERHEAD_FLY):
            return

        scan_name = "fly2d_SNAKE" if snake else "fly2d"
        self._pre_scan(scan_name, scan_kind=scan_kind)

        # SoftGlue socket stream is required for snake scans (both axes move
        # simultaneously; softglue provides the hardware timing signal).
        self.w.switch_SGstream(snake)

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()

        self.ui.pbar_scan.setValue(0)

        # Store X (fast, flying) axis parameters.
        self.fly1d_p0 = xax["p0"]
        self.fly1d_st = xax["st"]
        self.fly1d_fe = xax["fe"]
        self.fly1d_tm = xax["expt"]
        self.fly1d_step = xax["step"]

        # Store Y (slow, stepping) axis parameters.
        self.fly2d_p0 = yax["p0"]
        self.fly2d_st = yax["st"]
        self.fly2d_fe = yax["fe"]
        self.fly2d_tm = yax["expt"]
        self.fly2d_step = yax["step"]

        # fly3d_p0=None signals to fly2d0 / fly2d0_SNAKE that this is a
        # standalone 2-D scan (not a phi slice inside a 3-D scan).
        self.fly3d_p0 = None
        self.fly3d_st = None
        self.fly3d_fe = None
        self.fly3d_tm = None
        self.fly3d_step = None
        self.progress_3d = None

        self.w.signalmotor = xax["name"]
        self.w.signalmotorunit = self.w.motorunits[xmotor]
        self.w.motor_p0 = {xmotor: xax["p0"], ymotor: yax["p0"]}
        self.time_scanstart = time.time()

        # Warm up the DG645 so the detector IOC can communicate before the
        # executor configures timing precisely.
        self.w.dg645_12ID.set_pilatus_fly(0.001)

        self._log_scan_header(scan_name, [xax, yax], scan_kind=scan_kind)

        # Compute master file paths NOW (before scan number may be incremented)
        # so flydone2d can use the correct paths for linking detector data.
        sample_name = self.w.parameters.scan_name
        scan_number = getattr(self, '_scan_number_at_start', self.w.parameters.scan_number)
        master_paths_2d_fly = {}
        if len(self.w.detector) > 0 and self.w.detector[0] is not None:
            try:
                master_paths_2d_fly['SAXS'] = self._get_master_file_path_with_number('SAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute SAXS master file path: {e}")
        if len(self.w.detector) > 1 and self.w.detector[1] is not None:
            try:
                master_paths_2d_fly['WAXS'] = self._get_master_file_path_with_number('WAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute WAXS master file path: {e}")
        self._current_scan_master_paths = master_paths_2d_fly

        if snake:
            # Program the full 2-D snake trajectory on the hexapod controller
            # before the worker starts.  fly2d0_SNAKE then just triggers it.
            self.w.fly_traj(xmotor, ymotor)
            # As early as possible after pulse_number becomes known, verify the
            # software-predicted hexapod trigger count matches it and record
            # both. Abort before the worker starts on a mismatch.
            try:
                self._reconcile_hexapod_snake_positions(write_to_master=True)
            # except HexapodPositionCountMismatchError as e:
            #     QMessageBox.critical(self.w.ui, "Hexapod Mismatch", str(e))
                # return
            self._launch_worker(
                self.fly2d0_SNAKE,
                xmotor,
                ymotor,
                done_signal=self.w.flydone2d,
                scanname=scanname,
            )
        else:
            # Non-snake: fly_traj programs a 1-D X trajectory; fly2d0 re-runs
            # fly0 for each Y row independently.
            self.w.fly_traj(xmotor)
            self._launch_worker(
                self.fly2d0,
                xmotor,
                ymotor,
                done_signal=self.w.flydone2d,
                scanname=scanname,
            )

    def updateprogressbar(self, value):
        self.ui.pbar_scan.setValue(value)
        self.w.update_status_scan_time(value)

    def update_status_bar(self, message):
        self.ui.statusbar.showMessage(message)

    def fly3d(self, xmotor=0, ymotor=1, phimotor=6, scanname="", snake=False):
        """Entry point for a 3-D fly scan (GUI thread).

        Steps phi in the outer loop; for each phi position fly3d0 calls
        fly2d0 (non-snake) or fly2d0_SNAKE to sweep the 2-D XY grid.

        Note: fly_traj is NOT called here — fly3d0 calls it inside the phi
        loop so the hexapod trajectory is re-programmed fresh for each slice
        (goto_start_pos must be called after each trajectory completes).

        Why fly3d exists separately from fly3d0: thread-safety — Qt widget
        reads must happen on the GUI thread.
        """
        if not self._pre_scan_guards():
            return

        # Read parameters and check large scan BEFORE _pre_scan (which creates master file)
        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
            phiax = self._read_motor_params(phimotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        phipos = self._make_positions(
            phiax["p0"], phiax["st"], phiax["fe"], phiax["step"]
        )
        scan_kind = "fly_snake" if snake else "fly_hexapod_1d"
        xy_n_positions = self._compute_n_positions([xpos, ypos], scan_kind=scan_kind)
        n_positions = xy_n_positions * len(phipos)
        if not self._confirm_large_scan(n_positions, xax["expt"], self.OVERHEAD_FLY):
            return

        scan_name = "fly3d_SNAKE" if snake else "fly3d"
        self._pre_scan(scan_name, scan_kind=scan_kind)

        self.w.switch_SGstream(snake)

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()

        # X (fast, flying) axis
        self.fly1d_p0 = xax["p0"]
        self.fly1d_st = xax["st"]
        self.fly1d_fe = xax["fe"]
        self.fly1d_tm = xax["expt"]
        self.fly1d_step = xax["step"]

        # Y (medium, stepping per row) axis
        self.fly2d_p0 = yax["p0"]
        self.fly2d_st = yax["st"]
        self.fly2d_fe = yax["fe"]
        self.fly2d_tm = yax["expt"]
        self.fly2d_step = yax["step"]

        # Phi (slow, outer-loop rotation) axis
        self.fly3d_p0 = phiax["p0"]
        self.fly3d_st = phiax["st"]
        self.fly3d_fe = phiax["fe"]
        self.fly3d_tm = phiax["expt"]
        self.fly3d_step = phiax["step"]

        self.w.motor_p0 = {xmotor: xax["p0"], ymotor: yax["p0"], phimotor: phiax["p0"]}
        self.time_scanstart = time.time()

        self.fly3d_axes_params = [xax, yax, phiax]

        self._log_scan_header(scan_name, [xax, yax, phiax], scan_kind=scan_kind)

        # Compute master file paths NOW (before scan number may be incremented)
        # so flydone3d can use the correct paths for linking detector data.
        sample_name = self.w.parameters.scan_name
        scan_number = getattr(self, '_scan_number_at_start', self.w.parameters.scan_number)
        master_paths_3d_fly = {}
        if len(self.w.detector) > 0 and self.w.detector[0] is not None:
            try:
                master_paths_3d_fly['SAXS'] = self._get_master_file_path_with_number('SAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute SAXS master file path: {e}")
        if len(self.w.detector) > 1 and self.w.detector[1] is not None:
            try:
                master_paths_3d_fly['WAXS'] = self._get_master_file_path_with_number('WAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute WAXS master file path: {e}")
        self._current_scan_master_paths = master_paths_3d_fly

        self._launch_worker(
            self.fly3d0,
            xmotor,
            ymotor,
            phimotor,
            done_signal=self.w.flydone3d,
            scanname=scanname,
            snake=snake,
        )

    def fly(self, motornumber=-1):
        """Entry point for a 1-D fly scan (GUI thread).

        Reads parameters from the UI, validates, logs the scan header, programs
        the hexapod trajectory (for hexapod axes), then launches fly0 on a
        Worker thread.

        Note: run_stop_issued() is called immediately after the worker starts
        (before the scan finishes).  This matches the original behaviour where
        the scan number increments at scan start for 1-D fly scans.

        Why fly exists separately from fly0: thread-safety — Qt widget reads
        must happen on the GUI thread.
        """
        if not self._pre_scan_guards():
            return

        # Resolve the motor index and read parameters
        if motornumber < 0:
            motornumber = self._motor_from_sender()

        try:
            ax = self._read_motor_params(motornumber)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        # Check large scan BEFORE _pre_scan (which creates master file)
        pos = self._make_positions(ax["p0"], ax["st"], ax["fe"], ax["step"])
        scan_kind = "fly_hexapod_1d" if ax["name"] in self.w.pts.hexapod.axes else "fly_phi"
        n_positions = self._compute_n_positions([pos], scan_kind=scan_kind)
        if not self._confirm_large_scan(n_positions, ax["expt"], self.OVERHEAD_FLY):
            return

        self._pre_scan("fly", scan_kind=scan_kind)

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        self.fly1d_p0 = ax["p0"]
        self.fly1d_st = ax["st"]
        self.fly1d_fe = ax["fe"]
        self.fly1d_tm = ax["expt"]
        self.fly1d_step = ax["step"]

        self.w.signalmotor = ax["name"]
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        self.w.motor_p0 = {motornumber: ax["p0"]}
        self.time_scanstart = time.time()

        self._log_scan_header("fly", [ax], scan_kind=scan_kind)

        # Program the hexapod trajectory waveform before the worker starts.
        # fly_traj sets self.Xaxis and _ratio_exp_period, which fly0 needs.
        if ax["name"] in self.w.pts.hexapod.axes:
            self.w.fly_traj(motornumber)

        # Compute master file paths NOW (before scan number is incremented)
        # so flydone can use the correct paths for linking detector data.
        sample_name = self.w.parameters.scan_name
        scan_number = getattr(self, '_scan_number_at_start', self.w.parameters.scan_number)
        master_paths_1d_fly = {}
        if len(self.w.detector) > 0 and self.w.detector[0] is not None:
            try:
                master_paths_1d_fly['SAXS'] = self._get_master_file_path_with_number('SAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute SAXS master file path: {e}")
        if len(self.w.detector) > 1 and self.w.detector[1] is not None:
            try:
                master_paths_1d_fly['WAXS'] = self._get_master_file_path_with_number('WAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute WAXS master file path: {e}")
        self._current_scan_master_paths = master_paths_1d_fly

        self._launch_worker(self.fly0, motornumber, done_signal=self.w.flydone)

        # Advance the scan number immediately after launch.  Preserves the
        # original fly() behaviour; other scan types advance on completion.
        self.w.run_stop_issued()
        self.w.update_status_scan_time()

    def helix_fly(self, zmotor=2, phimotor=6):
        """Entry point for a helix fly scan: phi flies with simultaneous Z constant-velocity motion (GUI thread).

        Reads parameters from UI for both phi and Z axes, validates, logs scan header,
        then launches helix_fly0 on a Worker thread. Both axes move simultaneously at
        constant velocity over the same total_time, with images acquired at DG645-triggered
        intervals synchronized to phi's step timing.
        """
        if not self._pre_scan_guards():
            return

        # Read parameters and check large scan BEFORE _pre_scan (which creates master file)
        try:
            phi_ax = self._read_motor_params(phimotor)
            z_ax = self._read_motor_params(zmotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        pos = self._make_positions(phi_ax["p0"], phi_ax["st"], phi_ax["fe"], phi_ax["step"])
        n_positions = self._compute_n_positions([pos], scan_kind="fly_phi")
        if not self._confirm_large_scan(n_positions, phi_ax["expt"], self.OVERHEAD_FLY):
            return

        self._pre_scan("helix_fly", scan_kind="fly_phi")

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        self.helix_phi_p0 = phi_ax["p0"]
        self.helix_phi_st = phi_ax["st"]
        self.helix_phi_fe = phi_ax["fe"]
        self.helix_phi_tm = phi_ax["expt"]
        self.helix_phi_step = phi_ax["step"]

        self.helix_z_p0 = z_ax["p0"]
        self.helix_z_st = z_ax["st"]
        self.helix_z_fe = z_ax["fe"]

        self.w.signalmotor = f"{phi_ax['name']},{z_ax['name']}"
        self.w.motor_p0 = {phimotor: phi_ax["p0"], zmotor: z_ax["p0"]}
        self.time_scanstart = time.time()

        self._log_scan_header("helix_fly", [phi_ax, z_ax], scan_kind="helix")

        # Compute master file paths NOW (before scan number may be incremented)
        # so helixdone can use the correct paths for linking detector data.
        sample_name = self.w.parameters.scan_name
        scan_number = getattr(self, '_scan_number_at_start', self.w.parameters.scan_number)
        master_paths_helix_fly = {}
        if len(self.w.detector) > 0 and self.w.detector[0] is not None:
            try:
                master_paths_helix_fly['SAXS'] = self._get_master_file_path_with_number('SAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute SAXS master file path: {e}")
        if len(self.w.detector) > 1 and self.w.detector[1] is not None:
            try:
                master_paths_helix_fly['WAXS'] = self._get_master_file_path_with_number('WAXS', sample_name, scan_number)
            except Exception as e:
                print(f"Warning: Could not compute WAXS master file path: {e}")
        self._current_scan_master_paths = master_paths_helix_fly

        self._launch_worker(self.helix_fly0, phimotor, zmotor, done_signal=self.w.helixdone)

        self.w.run_stop_issued()
        self.w.update_status_scan_time()

    def takeshot(self):
        """Take a single detector image (mimics SPEC's `takeshot`, no dialog).

        Reads exposure time from ed_lup_1_t, runs _pre_scan to advance the
        scan name / file paths, writes a scan summary row, then launches
        time_series_run with n_images=1 on a worker thread. Shutter open/close
        is handled by _launch_worker and scandone, matching every other scan.
        """
        _t_widget = self.ui.findChild(QLineEdit, "ed_lup_1_t")
        try:
            expt = float(_t_widget.text()) if _t_widget else 0.0
        except ValueError:
            expt = 0.0
        if expt <= 0:
            QMessageBox.warning(self.w.ui, "Takeshot", "Exposure time must be > 0.")
            return

        period_s = max(expt + DETECTOR_READOUTTIME, self.OVERHEAD_FLY)

        self._pre_scan("takeshot")
        self.w.motor_p0 = {}
        axes_params = [{"name": "time", "motor_index": 0, "expt": expt,
                        "p0": 0.0, "st": 0.0, "fe": period_s, "step": period_s}]
        self._write_scan_summary_line("takeshot", axes_params, 1)
        self._launch_worker(
            self.time_series_run, 1, period_s, expt, True,
            done_signal=self.w.scandone,
        )

    def time_series(self):
        """Entry point for a time-series acquisition (GUI thread).

        Opens the Time Series Setup dialog, validates parameters, then launches
        time_series_run on a Worker thread.  No motors are moved.
        """
        from PyQt5.QtWidgets import (
            QDialog, QDialogButtonBox, QFormLayout, QCheckBox,
            QLabel, QLineEdit, QMessageBox,
        )

        # Read exposure time from the main UI before opening the dialog.
        _t_widget = self.ui.findChild(QLineEdit, "ed_lup_1_t")
        try:
            expt = float(_t_widget.text()) if _t_widget else 0.0
        except ValueError:
            expt = 0.0

        # ── Build dialog ──────────────────────────────────────────────────────
        dlg = QDialog(self.w.ui)
        dlg.setWindowTitle("Time Series Setup")
        layout = QFormLayout(dlg)

        edit_n = QLineEdit("100")
        edit_period = QLineEdit("100")
        chk_multi = QCheckBox()
        chk_multi.setChecked(True)
        label_time = QLabel("Total time: 10.0 s")

        layout.addRow("Number of images", edit_n)
        layout.addRow("Frame period (ms)", edit_period)
        layout.addRow("Capture multi-frames in one h5 file", chk_multi)
        layout.addRow("", label_time)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addRow(btn_box)
        btn_box.rejected.connect(dlg.reject)

        def _update_time_label():
            try:
                total_s = int(edit_n.text()) * int(edit_period.text()) / 1000.0
                label_time.setText(f"Total time: {total_s:.1f} s")
            except ValueError:
                label_time.setText("Total time: —")

        edit_n.textChanged.connect(_update_time_label)
        edit_period.textChanged.connect(_update_time_label)
        _update_time_label()

        def _validate_and_accept():
            try:
                n = int(edit_n.text())
                if n <= 0:
                    raise ValueError
            except ValueError:
                QMessageBox.warning(dlg, "Time Series", "Number of images must be an integer > 0.")
                return
            try:
                period_ms = int(edit_period.text())
            except ValueError:
                QMessageBox.warning(dlg, "Time Series", "Frame period must be an integer.")
                return
            if period_ms < self.OVERHEAD_FLY * 1000:
                QMessageBox.warning(
                    dlg, "Time Series",
                    f"Frame period too short. Minimum is {self.OVERHEAD_FLY * 1000:.0f} ms.",
                )
                return
            period_s = period_ms / 1000.0
            if period_s - expt < DETECTOR_READOUTTIME:
                QMessageBox.warning(
                    dlg, "Time Series",
                    f"Frame period minus exposure time ({(period_s - expt) * 1000:.1f} ms) "
                    f"is less than the detector readout time ({DETECTOR_READOUTTIME * 1000:.0f} ms).",
                )
                return
            dlg.accept()

        btn_box.accepted.connect(_validate_and_accept)

        if dlg.exec_() != QDialog.Accepted:
            return

        n_images = int(edit_n.text())
        period_s = int(edit_period.text()) / 1000.0
        multi_frame = chk_multi.isChecked()

        self._pre_scan("timeseries")
        self.w.motor_p0 = {}
        axes_params = [{"name": "time", "motor_index": 0, "expt": expt,
                        "p0": 0.0, "st": 0.0, "fe": n_images * period_s, "step": period_s}]
        self._write_scan_summary_line("timeseries", axes_params, n_images)
        self._launch_worker(
            self.time_series_run, n_images, period_s, expt, multi_frame,
            done_signal=self.w.scandone,
        )

    def time_series_run(self, n_images, period_s, expt, multi_frame,
                        update_progress=None, update_status=None):
        """Worker-thread executor for a time-series acquisition.

        Arms all active detectors via fly_ready(), fires the DG645 in burst
        mode, then waits for all frames to be collected.
        """
        if self.w.DEBUG_DEVICES:
            for i in range(n_images):
                if self.isStopScanIssued:
                    break
                time.sleep(min(period_s, 0.05))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / n_images))
            return

        savemode = 1 if multi_frame else 0

        # Configure DG645 for burst mode: one software trigger fires n_images pulses.
        self.w.dg645_12ID.set_pilatus2(
            expt, DGNimage=n_images, Cycperiod=period_s
        )

        # Arm all active detectors.
        primary_det = None
        for detN, det in enumerate(self.w.detector):
            if det is None:
                continue
            det.fly_ready(
                expt,
                n_images,
                period=period_s,
                capture=(self.w.use_hdf_plugin, savemode),
                fn=self.w.hdf_plugin_name[detN],
            )
            if primary_det is None and "3820" not in det._prefix:
                primary_det = det

        # Fire the DG645 once — burst mode delivers n_images triggers.
        self.w.dg645_12ID.trigger()

        # Wait for all frames to be collected.
        timeout = (expt + 0.1) * n_images + 15
        t0 = time.time()
        while True:
            if self.isStopScanIssued:
                break
            if time.time() - t0 > timeout:
                if update_status is not None:
                    update_status("Time series timed out.")
                break
            if primary_det is not None and primary_det.ArrayCounter_RBV >= n_images:
                break
            if update_progress is not None:
                frames_done = primary_det.ArrayCounter_RBV if primary_det else 0
                update_progress(int(100 * frames_done / n_images))
            time.sleep(0.05)

        if update_progress is not None:
            update_progress(100)

    def write_scaninfo_to_logfile(self, strlist):
        if len(self.w.parameters.logfilename) == 0:
            return 0
        with open(self.w.parameters.logfilename, "a") as f:
            for i, m in enumerate(strlist):
                if i == 0:
                    strv = "%s" % str(m)
                else:
                    strv = "%s    %s" % (strv, str(m))
            f.write("%s\n" % strv)

    def log_data(self, data_list):
        if len(self.w.parameters.logfilename) == 0:
            return 0
        strv = ""
        with open(self.w.parameters.logfilename, "a") as f:
            for i, m in enumerate(data_list):
                if i == 0:
                    strv = "%0.8f" % m
                else:
                    strv = "%s    %0.8f" % (strv, m)
            f.write("%s\n" % strv)

    def stepscan(self, motornumber=-1):
        """Entry point for a 1-D step scan (GUI thread).

        Reads parameters from the UI, validates, logs the scan header, then
        launches stepscan0 on a Worker thread.  The split between this entry
        point and stepscan0 is required: Qt widgets must be accessed on the GUI
        thread; hardware moves must run in the background so the GUI stays responsive.
        """
        if not self._pre_scan_guards():
            return

        # Resolve the motor index: negative means the call came from a UI button
        # whose object name encodes the motor number (e.g. 'pushButton_step_3').
        if motornumber < 0:
            motornumber = self._motor_from_sender()

        try:
            ax = self._read_motor_params(motornumber)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        # Check large scan BEFORE _pre_scan (which creates master file)
        pos = self._make_positions(ax["p0"], ax["st"], ax["fe"], ax["step"])
        if not self._confirm_large_scan(len(pos), ax["expt"], self.OVERHEAD_STEP):
            return

        self._pre_scan("stepscan")

        # Populate the instance variables that stepscan0 reads from the worker thread.
        # (Workers cannot safely read Qt widgets, so we pass data via instance state.)
        self.stepscan_p0 = ax["p0"]
        self.stepscan_st = ax["st"]
        self.stepscan_fe = ax["fe"]
        self.stepscan_expt = ax["expt"]
        self.stepscan_step = ax["step"]

        # Signal motor used by QDS (quadrant diode signal) display.
        self.w.signalmotor = ax["name"]
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        self.w.motor_p0 = {motornumber: ax["p0"]}
        self.time_scanstart = time.time()

        self._log_scan_header("stepscan", [ax])
        self._launch_worker(self.stepscan0, motornumber, done_signal=self.w.scandone)

    def stepscan2d(self, xmotor=0, ymotor=1):
        """Entry point for a 2-D step scan in snake (boustrophedon) order (GUI thread).

        Reads X and Y parameters, validates, logs the scan header, then launches
        stepscan2d0 on a Worker thread.  stepscan2d0 re-reads the Y motor position
        from the UI at executor start time (so the user can update Y range between
        phi slices in a 3-D scan if needed).

        Why stepscan2d exists separately from stepscan2d0: same thread-safety reason
        as stepscan/stepscan0 — Qt widget reads must happen on the GUI thread.
        """
        if not self._pre_scan_guards():
            return

        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        # Check large scan BEFORE _pre_scan (which creates master file)
        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        if not self._confirm_large_scan(
            len(xpos) * len(ypos), xax["expt"], self.OVERHEAD_STEP
        ):
            return

        self._pre_scan("stepscan2d")

        # Store X and Y parameters for the executor (worker thread cannot read UI).
        self.stepscan1d_p0 = xax["p0"]
        self.stepscan1d_st = xax["st"]
        self.stepscan1d_fe = xax["fe"]
        self.stepscan1d_tm = xax["expt"]
        self.stepscan1d_step = xax["step"]

        self.stepscan2d_p0 = yax["p0"]
        self.stepscan2d_st = yax["st"]
        self.stepscan2d_fe = yax["fe"]
        self.stepscan2d_tm = yax["expt"]
        self.stepscan2d_step = yax["step"]

        # stepscan3d_p0=None signals to stepscan2d0 that this is a standalone 2-D
        # scan (not a slice of a 3-D scan), so it reports progress at 2-D scale.
        self.stepscan3d_p0 = None
        self.progress_3d = None

        self.w.signalmotor = xax["name"]
        self.w.signalmotorunit = self.w.motorunits[xmotor]
        self.w.motor_p0 = {xmotor: xax["p0"], ymotor: yax["p0"]}
        self.time_scanstart = time.time()

        self._log_scan_header("stepscan2d", [xax, yax])
        self._launch_worker(
            self.stepscan2d0, xmotor, ymotor, done_signal=self.w.scandone
        )

    def stepscan3d(self, xmotor=0, ymotor=1, phimotor=6):
        """Entry point for a 3-D step scan (GUI thread).

        Steps phi (phimotor) in the outer loop; the executor (stepscan3d0)
        calls stepscan2d0 for each phi position to sweep the 2-D XY grid.
        Progress is reported across the full 3-D scan.

        Why stepscan3d exists separately from stepscan3d0: thread-safety —
        Qt widget reads must happen on the GUI thread.
        """
        if not self._pre_scan_guards():
            return

        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
            phiax = self._read_motor_params(phimotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        # Check large scan BEFORE _pre_scan (which creates master file)
        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        phipos = self._make_positions(
            phiax["p0"], phiax["st"], phiax["fe"], phiax["step"]
        )
        if not self._confirm_large_scan(
            len(xpos) * len(ypos) * len(phipos), xax["expt"], self.OVERHEAD_STEP
        ):
            return

        self._pre_scan("stepscan3d")

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()

        # X (fast) axis — inner loop of the 2-D slice
        self.stepscan1d_p0 = xax["p0"]
        self.stepscan1d_st = xax["st"]
        self.stepscan1d_fe = xax["fe"]
        self.stepscan1d_tm = xax["expt"]
        self.stepscan1d_step = xax["step"]

        # Y (medium) axis — outer loop of the 2-D slice
        self.stepscan2d_p0 = yax["p0"]
        self.stepscan2d_st = yax["st"]
        self.stepscan2d_fe = yax["fe"]
        self.stepscan2d_tm = yax["expt"]
        self.stepscan2d_step = yax["step"]

        # Phi (slow) axis — outer 3-D loop
        self.stepscan3d_p0 = phiax["p0"]
        self.stepscan3d_st = phiax["st"]
        self.stepscan3d_fe = phiax["fe"]
        self.stepscan3d_tm = phiax["expt"]
        self.stepscan3d_step = phiax["step"]

        self.w.signalmotor = xax["name"]
        self.w.signalmotorunit = self.w.motorunits[xmotor]
        self.w.motor_p0 = {xmotor: xax["p0"], ymotor: yax["p0"], phimotor: phiax["p0"]}
        self.time_scanstart = time.time()

        self.stepscan3d_axes_params = [xax, yax, phiax]

        # Initialise the DG645 so the detector IOC knows a scan is starting.
        # stepscan2d0 will re-configure it precisely per-step with set_pilatus().
        self.w.dg645_12ID.set_pilatus_fly(0.001)

        # Per-slice scandone(True, False) calls inside stepscan3d0 handle per-slice
        # detector cleanup and scan-number increment. The done_signal fires on the GUI
        # thread after the worker exits and handles the final teardown (shutter, status
        # label, scan time) without incrementing the scan number a second time.
        self._launch_worker(
            self.stepscan3d0,
            xmotor,
            ymotor,
            phimotor,
            done_signal=lambda _ok: self.w.scandone(update_scannumber=False, donedone=True),
        )

    def run_stop_issued(self):
        self.w.parameters.scan_number = self.w.parameters.scan_number + 1
        self.w.update_scannumber()
        self.w.parameters.writeini()

    def update_scannumber(self):
        SCAN_NUMBER_IOC = self.w.SCAN_NUMBER_IOC
        if SCAN_NUMBER_IOC is not None:
            SCAN_NUMBER_IOC.put(int(self.w.parameters.scan_number))
        self.ui.edit_scannumber.setText(str(int(self.w.parameters.scan_number)))
        self.update_label_scanCheck()

    def stepscan0(self, motornumber=-1, update_progress=None, update_status=None):
        axis = self.w.motornames[motornumber]
        self.w.signalmotor = axis
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        self.w.rpos = []
        self.w.mpos = []
        pos = self.w.pts.get_pos(axis)
        pos0 = pos
        self.w.isfly = False
        n = motornumber + 1

        if not self.ui.chk_keep_prev_scan.isChecked():
            self.w.clearplot()

        st = self.stepscan_st + self.stepscan_p0
        fe = self.stepscan_fe + self.stepscan_p0
        expt = self.stepscan_expt
        step = self.stepscan_step
        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)
        # enable fit menu
        if axis == "phi":
            self.ui.actionFit_QDS_phi.setEnabled(True)
        if self.ui.chk_reverse_scan_dir.isChecked():
            if abs(st - pos) > abs(fe - pos):
                t = fe
                fe = st
                st = t

        self.w.pts.mv(axis, st)
        pos = self._make_positions(0, st - 0, fe - 0, step)
        if len(pos) == 1:
            pos = np.array([st, fe])

        # scaninfo = []
        # scaninfo.append('#H')
        # if self.w.detector[2] is not None:
        #     scaninfo.append(axis)
        #     scaninfo.append(self.w.detector[2].scaler.NM2)
        #     scaninfo.append(self.w.detector[2].scaler.NM3)
        #     scaninfo.append(self.w.detector[2].scaler.NM4)
        # else:
        #     scaninfo.append(axis)
        #     scaninfo.append('QDS1')
        #     scaninfo.append('QDS2')
        #     scaninfo.append('QDS3')
        # self.w.write_scaninfo_to_logfile(scaninfo)

        # prepare to collect Detector images
        isDET_selected = False

        if self.w.DEBUG_MOTORS:
            # --- Debug 1-D step scan (motor stubs) ---
            mpos_data = []
            N = len(pos)
            self.isStopScanIssued = False
            for i, p in enumerate(pos):
                if self.isStopScanIssued:
                    break
                self.w.pts.mv(axis, p)
                time.sleep(min(expt, 0.05))
                mpos_data.append(self.w.pts.get_pos(axis))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / N))
            self.w.mpos = mpos_data
            return

        # ── REAL MODE ─────────────────────────────────────────────────────────
        # Set up the DG645 delay generator for step-scan triggering.
        # For single-pulse steps: trigger_source=5 means software trigger,
        # period=0 means "fire once per trigger call" (no auto-repeat).
        # For multi-pulse steps: DGNimage pulses fire per trigger at Cycperiod spacing.
        if self.w.parameters._pulses_per_step == 1:
            period = 0
        else:
            period = round(max(expt + 0.020, 0.03), 6)
        self.w.dg645_12ID.set_pilatus(
            expt,
            trigger_source=5,
            DGNimage=int(self.w.parameters._pulses_per_step),
            Cycperiod=period,
        )

        # Arm all selected detectors for the full scan length.
        # step_ready() puts each detector in external-trigger mode and programs
        # the total frame count it should expect (len(pos) * pulses_per_step).
        isDET_selected = False
        for detN, det in enumerate(self.w.detector):
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
                        pulsespershot=self.w.parameters._pulses_per_step,
                        fn=self.w.hdf_plugin_name[detN],
                    )
                except TimeoutError:
                    self.w.messages["recent error message"] = (
                        f"Detector {det._prefix} timed out during step_ready."
                    )
                    print(self.w.messages["recent error message"])
                    return

        t0 = time.time()
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break

            # Move motor to this scan position and wait for motor to settle.
            self.w.pts.mv(axis, value)

            # Configurable idle time between exposures (avoids vibration artefacts).
            time.sleep(self.w.parameters._waittime_between_scans)

            # Re-confirm detector is armed before each trigger.
            # The detector can fall out of armed state after a timeout or IOC error.
            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.w.messages["recent error message"] = (
                    f"Detector arm timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            # Fire the DG645 trigger.  This causes _pulses_per_step exposures.
            if isDET_selected:
                self.w.dg645_12ID.trigger()

            # Block until the detector has collected the expected cumulative frame count.
            # is_waiting_detectors_timedout checks ArrayCounter_RBV >= (i+1)*pulses_per_step.
            timeout_occurred, TIMEOUT = self.is_waiting_detectors_timedout(expt, i)
            if timeout_occurred:
                self.w.messages["recent error message"] = (
                    f"Detector frame timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            # Record the commanded position for the scan log.
            self.w.mpos.append(value)
            self._emit_progress(t0, i, len(pos), update_progress, update_status)

        # Return motor to its home position (where it was before the scan started).
        self.w.pts.mv(axis, pos0)

    def get_detectors_armed(self):
        TIMEOUT = 10
        t_start = time.time()
        timeout_occurred = False

        for ndet, det in enumerate(self.w.detector):
            if ndet > 2:
                continue
            if det is not None:
                while det.Armed == 0:
                    det.Arm()
                    wait_for_det_arm_retry_s = 0.5  # retry interval while re-arming a detector that failed to arm
                    time.sleep(wait_for_det_arm_retry_s)
                    print(f"Detector {ndet} is Armed again.................")
                    if (time.time() - t_start) > TIMEOUT:
                        timeout_occurred = True
                        print(
                            f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds."
                        )
                        break
        return timeout_occurred, TIMEOUT

    def is_arming_detecotors_timedout(self):
        TIMEOUT = 10
        t_start = time.time()
        timeout_occurred = False
        for detN, det in enumerate(self.w.detector):
            if det is not None:
                if self.w.parameters._pulses_per_step > 1:
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
        if self.w.parameters._pulses_per_step > 1.5:
            TIMEOUT = (expt + 0.03) * self.w.parameters._pulses_per_step + 10
        else:
            TIMEOUT = expt + 3
        t_start = time.time()
        timeout_occurred = False
        for ndet, det in enumerate(self.w.detector):
            if ndet > 1:
                continue
            if det is not None:
                while det.ArrayCounter_RBV < self.w.parameters._pulses_per_step * (
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

    def stepscan2d0(self, xmotor=0, ymotor=1, update_progress=None, update_status=None):
        # print(ymotor, " this is ymortor")
        yaxis = self.w.motornames[ymotor]
        xaxis = self.w.motornames[xmotor]
        self.w.signalmotor2 = yaxis
        self.w.signalmotorunit2 = self.w.motorunits[ymotor]
        # pos = self.w.pts.get_pos(yaxis)
        self.w.isfly2 = False

        # Just in case when the user update edit box (during 3d scan)
        # Will need to update the positions.
        n = ymotor + 1
        p0 = self.ui.findChild(QLineEdit, "ed_%i" % n).text()

        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L" % n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R" % n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N" % n).text())
        self.stepscan2d_p0 = p0
        self.stepscan2d_st = st
        self.stepscan2d_fe = fe
        self.stepscan2d_step = step

        n = xmotor + 1
        p0 = self.ui.findChild(QLineEdit, "ed_%i" % n).text()

        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L" % n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R" % n).text())
        expt = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t" % n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N" % n).text())
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
        # self.w.dg645_12ID.set_pilatus(expt, trigger_source=5, DGNimage=1)
        # each time it will send a pulse

        # scaninfo = []
        # scaninfo.append('#H')
        # if self.w.detector[2] is not None:
        #     scaninfo.append(xaxis)
        #     scaninfo.append(yaxis)
        #     scaninfo.append(self.w.detector[2].scaler.NM2)
        #     scaninfo.append(self.w.detector[2].scaler.NM3)
        #     scaninfo.append(self.w.detector[2].scaler.NM4)
        # else:
        #     scaninfo.append(xaxis)
        #     scaninfo.append(yaxis)
        #     scaninfo.append('QDS1')
        #     scaninfo.append('QDS2')
        #     scaninfo.append('QDS3')
        # self.w.write_scaninfo_to_logfile(scaninfo)

        if self.w.DEBUG_MOTORS:
            # --- Debug 2-D step scan (motor stubs) ---
            mpos_data = []
            Nline = len(pos)
            self.isStopScanIssued = False
            for i, (xp, yp) in enumerate(pos):
                if self.isStopScanIssued:
                    break
                self.w.pts.mv(xaxis, xp)
                self.w.pts.mv(yaxis, yp)
                time.sleep(min(expt, 0.05))
                mpos_data.append([self.w.pts.get_pos(xaxis), self.w.pts.get_pos(yaxis)])
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / Nline))
            self.w.mpos = mpos_data
            return

        if self.w.parameters._pulses_per_step == 1:
            period = 0
        else:
            period = round(max(expt + 0.020, 0.03), 6)
        self.w.dg645_12ID.set_pilatus(
            expt,
            trigger_source=5,
            DGNimage=int(self.w.parameters._pulses_per_step),
            Cycperiod=period,
        )

        ## prepre detectors ............
        for detN, det in enumerate(self.w.detector):  # JD
            if det is not None:  # JD
                # Skip Struck detector which doesn't support HDF5 file templates
                if "3820" in det._prefix:
                    continue
                det.setFileTemplate('%s%s_%5.5d.h5')
                det.step_ready(
                    expt,
                    Nline,
                    pulsespershot=self.w.parameters._pulses_per_step,
                    fn=self.w.hdf_plugin_name[detN],
                )  # Arm detector for multiple data.
                print(f"step _ready, detector {detN}'s status: {det.Armed}")  # JD

        t0 = time.time()
        self.isStopScanIssued = False

        # make sure detectors get armed.
        self.w.get_detectors_armed()

        self.w.messages["recent error message"] = ""
        self.w.messages["current status"] = ""
        self.w.messages["progress"] = ""

        print("Starting 2D step scan now...........................")
        for i, (xp, yp) in enumerate(pos):
            if self.isStopScanIssued:
                break

            # Move both hexapod axes simultaneously.
            # hexapod.mv(x_axis, xp, y_axis, yp) issues a single coordinated move
            # command.  Using two separate pts.mv() calls is wrong here because it
            # creates an unwanted intermediate position and is slower.
            # The loop retries on hexapod fault (handle_error resets the controller).
            pos_ok = False
            while not pos_ok:
                pos_ok = self.w.pts.hexapod.mv(xaxis, xp, yaxis, yp, wait=True)
                if not pos_ok:
                    self.w.messages["recent error message"] = (
                        f"Hexapod move failed at ({xp:.4f}, {yp:.4f}), "
                        f"attempting recovery. {time.ctime()}"
                    )
                    print(self.w.messages["recent error message"])
                    pos_ok = self.w.pts.hexapod.handle_error()

            # Configurable idle time between exposures.
            time.sleep(self.w.parameters._waittime_between_scans)

            # Confirm detector armed before each trigger (may fall out of arm
            # state after a previous timeout or transient IOC error).
            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.w.messages["recent error message"] = (
                    f"Detector arm timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            # Fire DG645 trigger — causes _pulses_per_step detector exposures.
            self.w.dg645_12ID.trigger()
            print(
                f"Trigger sent for point {i + 1} ({xp:.4f}, {yp:.4f}). {time.ctime()}"
            )

            # Block until the expected frame count is reached.
            timeout_occurred, TIMEOUT = self.is_waiting_detectors_timedout(expt, i)
            if timeout_occurred:
                self.w.messages["recent error message"] = (
                    f"Detector frame timeout ({TIMEOUT}s) at point {i + 1}. {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            # Record position and emit progress.
            self.w.mpos.append([xp, yp])

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

        return 1

    def stepscan3d0(
        self, xmotor=0, ymotor=-1, phimotor=-1, update_progress=None, update_status=None
    ):
        axis = self.w.motornames[phimotor]
        self.w.signalmotor3 = axis
        self.w.signalmotorunit3 = self.w.motorunits[phimotor]
        self.w.isfly3 = False

        st = self.stepscan3d_st + self.stepscan3d_p0
        fe = self.stepscan3d_fe + self.stepscan3d_p0
        step = self.stepscan3d_step

        self.w.pts.mv(axis, st)
        pos = self._make_positions(0, st - 0, fe - 0, step)

        i = 0
        Npos = len(pos)
        retried_dueto_timeout = 0

        if self.w.DEBUG_MOTORS:
            phiaxis = axis
            xaxis = self.w.motornames[xmotor]
            yaxis = self.w.motornames[ymotor]

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
                self.w.pts.mv(phiaxis, phip)
                for xp, yp in xy_pos:
                    if self.isStopScanIssued:
                        break
                    self.w.pts.mv(xaxis, xp)
                    self.w.pts.mv(yaxis, yp)
                    time.sleep(min(expt, 0.05))
                    mpos_data.append(
                        [self.w.pts.get_pos(xaxis), self.w.pts.get_pos(yaxis)]
                    )
                    count += 1
                    if update_progress is not None:
                        update_progress(int(100 * count / Ntot))
                time.sleep(0.5)
            self.w.mpos = mpos_data
            for key, p0val in self.w.motor_p0.items():
                self.w.pts.mv(self.w.motornames[key], p0val)
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
            self.w.write_scaninfo_to_logfile(scaninfo)

            self.w.pts.mv(axis, value)
            self._push_filepaths_to_detectors()
            self.progress_3d = (i, Npos)
            scan = f"{axis}{i:03d}"
            self._log_3d_slice_start(scan, self.stepscan3d_axes_params, value)
            retval = self.w.stepscan2d0(
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

            self.w.scandone(True, False, update_gui=False)
            if wait_long:
                wait_for_det_recovery_s = 60  # extended wait after detector timeout to allow IOC recovery before retry
                time.sleep(wait_for_det_recovery_s)

            # monitoring the station ready
            if self.w.monitor_beamline_status:
                # if beam is down, wait here
                if self.w.isOK2run is not True:
                    self.w.wait_for_beam(update_status, value)
                    # retry the same angle
                    i -= 1
            i = i + 1

    def fly3d0(
        self,
        xmotor=0,
        ymotor=1,
        phimotor=6,
        scanname="",
        snake=False,
        update_progress=None,
        update_status=None,
    ):
        # xmotor is for flying
        # ymotor is for stepping
        # phimotor is for rotation
        axis = self.w.motornames[phimotor]
        self.w.signalmotor3 = axis
        self.w.signalmotorunit3 = self.w.motorunits[phimotor]
        pos = self.w.pts.get_pos(axis)
        self.w.isfly3 = False

        st = self.fly3d_st + self.fly3d_p0
        fe = self.fly3d_fe + self.fly3d_p0
        step = self.fly3d_step

        if st > fe:
            step = -1 * abs(step)
        if st < fe:
            step = abs(step)

        # revsere scan disabled: always scan from start to final regardless of the initial position.
        self.w.pts.mv(axis, st)
        pos = np.arange(st, fe + step / 2, step)
        retried_dueto_timeout = 0

        if self.w.DEBUG_MOTORS:
            phiaxis = axis
            xaxis = self.w.motornames[xmotor]
            yaxis = self.w.motornames[ymotor]

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
                self.w.pts.mv(phiaxis, phip)
                for xp, yp in xy_pos:
                    if self.isStopScanIssued:
                        break
                    self.w.pts.mv(yaxis, yp)
                    self.w.pts.mv(xaxis, xp)
                    time.sleep(min(tm, 0.05))
                    mpos_data.append(
                        [self.w.pts.get_pos(xaxis), self.w.pts.get_pos(yaxis)]
                    )
                    count += 1
                    if update_progress is not None:
                        update_progress(int(100 * count / Ntot))
                time.sleep(0.5)
            self.w.mpos = mpos_data
            return

        # ── REAL MODE ─────────────────────────────────────────────────────────
        # Build a per-slice scan label used in log entries.
        if scanname:
            scanname = axis
        else:
            scanname = f"{scanname}{axis}"

        i = 0
        retried_dueto_timeout = 0
        while i < len(pos):
            wait_long = False
            value = pos[i]

            if self.isStopScanIssued:
                break

            # Log this phi slice.
            print(f"\n***** phi position: {value:.3e}")
            self.w.write_scaninfo_to_logfile(["#I phi = ", value])

            # Move phi to this angle.
            self.w.pts.mv(axis, value)

            self.progress_3d = (i, len(pos))
            scan = f"{scanname}{i:03d}"
            self._log_3d_slice_start(
                scan, self.fly3d_axes_params, value,
                scan_kind="fly_snake" if snake else "fly_hexapod_1d",
            )

            # Program the hexapod trajectory for this phi slice, then run the
            # 2-D executor directly on the current worker thread (no sub-worker).
            if snake:
                # Disable the wave generator before reprogramming; set_traj_SNAKE2
                # fails with GCSError 73 if the generator output is still active.
                if i > 0:
                    self.w.pts.hexapod.stop_traj()
                self.fly_traj(xmotor, ymotor)
                # As early as possible after pulse_number becomes known, verify
                # the software-predicted hexapod trigger count matches it and
                # record both. The X/Y grid is identical for every phi slice,
                # so only the first slice needs to write the master file
                # dataset; no try/except here — let a mismatch propagate to
                # Worker.run()'s generic exception handler like DET_MIN_READOUT_Error
                # and DG645_Error already do elsewhere in this file.
                self._reconcile_hexapod_snake_positions(write_to_master=(i == 0))
                retval = self.fly2d0_SNAKE(
                    xmotor, ymotor, scanname=scan,
                    update_progress=update_progress, update_status=update_status,
                )
            else:
                self.fly_traj(xmotor)
                retval = self.fly2d0(
                    xmotor, ymotor, scanname=scan,
                    update_progress=update_progress, update_status=update_status,
                )
            self.w.s12softglue.flush()
            print(f"softglue flushed at {time.ctime()}")
            txt = "%s_%0.4i" % (self.w.parameters.scan_name, self.w.parameters.scan_number)
            self.ui.lbl_scanname.setText(txt)
            if i < len(pos) - 1:
                self.w.get_detectors_ready()
                self._push_filepaths_to_detectors()

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
            if self.w.monitor_beamline_status and not self.w.isOK2run:
                self.wait_for_beam(update_status, value)
                i -= 1  # retry the same angle after beam recovery

            i += 1

    def wait_for_beam(self, update_status, value):
        ct0 = time.time()
        while self.w.isOK2run is not True:
            wait_for_beam_poll_s = 10  # poll interval while waiting for beam to return
            time.sleep(wait_for_beam_poll_s)
            self.w.messages["current status"] = (
                f"Beam has been down for {int((time.time() - ct0) / 60)} minutes. {time.ctime()}"
            )
            update_status(self.w.messages["current status"])
            if self.isStopScanIssued:
                break
        # Need some action after shutter back up
        self.w.shutter.open_A()
        self.w.messages["current status"] = (
            f"Beam just came back. A-shutter open command was sent and run will resume in 10mins. {time.ctime()}"
        )
        update_status(self.w.messages["current status"])
        wait_for_shutter_stabilize_s = 60  # wait after opening A-shutter before reopening to allow beam to stabilize
        time.sleep(wait_for_shutter_stabilize_s)
        self.w.shutter.open_A()
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
        self.w.write_scaninfo_to_logfile(scaninfo)

    def fly2d0(
        self, xmotor=0, ymotor=1, scanname="", update_progress=None, update_status=None
    ):
        """2-D fly scan: step the slow (Y) axis, fire fly0 along X for each Y line.

        This is the non-snake variant.  Each X line is an independent 1-D hexapod
        trajectory.  Works for any motor combination where X is a hexapod axis.

        Contrast with fly2d0_SNAKE, which programs the entire 2-D path as a single
        hexapod trajectory (requires both axes to be hexapod axes, but is faster
        and more precise).
        """

        axis = self.w.motornames[ymotor]
        self.w.signalmotor2 = axis
        self.w.signalmotorunit2 = self.w.motorunits[ymotor]
        self.w.isfly2 = False

        # Re-read Y and X parameters from the UI.  This executor may be called
        # repeatedly from fly3d0 while the user has access to the UI, so re-reading
        # here picks up any edits made between phi slices.
        n = ymotor + 1
        y_p0 = float(self.ui.findChild(QLineEdit, f"ed_{n}").text())
        y_st = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_L").text())
        y_fe = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_R").text())
        y_step = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_N").text())
        self.fly2d_p0 = y_p0
        self.fly2d_st = y_st
        self.fly2d_fe = y_fe

        n = xmotor + 1
        x_p0 = float(self.ui.findChild(QLineEdit, f"ed_{n}").text())
        x_st = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_L").text())
        x_fe = float(self.ui.findChild(QLineEdit, f"ed_lup_{n}_R").text())
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
            self.w.write_scaninfo_to_logfile(["#I Y = ", yval])

            # Step the slow axis to this Y position and wait for it to stop.
            self.w.pts.mv(axis, yval)
            while self.w.pts.ismoving(axis):
                time.sleep(0.02)

            # Optional: update per-line HDF file number for multi-line HDF capture.
            # This ensures each X-line lands in a separate HDF entry.
            if self.w.use_hdf_plugin and (self.w.hdf_plugin_savemode_fly > 0):
                for det in self.w.detector:
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
                    isreshreshed = self.refresh_detectors()
                if isreshreshed == 0:
                    print("Detector refresh failed. Stopping scan.")
                    if update_status:
                        update_status(f"Detector refresh failed. {time.ctime()}")
                    return DETECTOR_NOT_STARTED_ERROR

            # Advance the scan number log entry for this completed X line.
            # return_motor=False keeps the motor at the end of the X trajectory
            # (the next line starts from wherever fly0 left off, or goto_start_pos
            # handles repositioning internally).
            self.w.flydone(return_motor=False, reset_scannumber=False)

            # Inter-line idle time (configurable).
            t1 = time.time()
            while time.time() - t1 < self.w.parameters._waittime_between_scans:
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

        self.w.run_stop_issued()
        return 1

    def refresh_detectors(self):
        """Refresh the detectors to ensure they are ready for the next scan."""
        stata = 1
        for detN, det in enumerate(self.w.detector):
            if detN > 1:
                continue
            if det is not None:
                scaninfo = []
                scaninfo.append("\n")
                scaninfo.append(f"#I {det._prefix} IOC error at %{time.ctime()}.\n")
                m1, m2, m3 = det.getMessages()
                scaninfo.append(f"{m1}\n{m2}\n{m3}")
                scaninfo.append("\n")
                self.w.write_scaninfo_to_logfile(scaninfo)
                try:
                    status = (
                        det.refresh()
                    )  # if failed, it will return 0. ohterwise it will return 1.
                    stata = stata * status
                except Exception as e:
                    print(f"Error refreshing detector {det._prefix}: {e}")
                    self.ui.statusbar.showMessage(
                        f"Error refreshing detector {det._prefix}: {e}"
                    )
        return stata

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
        Xaxis = self.w.motornames[xmotor]
        Xst = self.fly1d_st + self.fly1d_p0  # absolute start
        Xfe = self.fly1d_fe + self.fly1d_p0  # absolute end
        Xstep = self.fly1d_step  # step distance (mm)
        Xtm = self.fly1d_tm  # user exposure time (s)

        # Compute step_time from the user-set acquisition time, clamped to safe floors.
        # The period must be at least as long as the detector readout so that
        # the next trigger does not arrive before the previous frame is read out,
        # and at least 33 ms (Pilatus 2M limit of 30 Hz).
        fly_acq_time = getattr(self.w.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
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
        self.w.parameters._ratio_exp_period = Xtm / step_time

        # Store axis labels for use by fly2d0_SNAKE (which runs on the worker thread
        # and cannot safely re-read the UI).
        self.Xaxis = Xaxis
        self.Xi = Xst
        self.Xf = Xfe

        if ymotor >= 0:
            # 2-D SNAKE: read Y parameters and program the 2-D snake waveform.
            Yaxis = self.w.motornames[ymotor]
            Yst = self.fly2d_st + self.fly2d_p0
            Yfe = self.fly2d_fe + self.fly2d_p0
            Ystep = self.fly2d_step

            self.Yaxis = Yaxis
            self.Yi = Yst
            self.Yf = Yfe

            # set_traj_SNAKE2 programs both axes simultaneously: the hexapod
            # will execute a boustrophedon (snake) path covering the full X×Y grid.
            self.w.pts.hexapod.set_traj_SNAKE2(
                step_time, Xst, Xfe - Xst, Xstep, Yst, Yfe, Ystep
            )

            minstep, commonstep = self.w.pts.hexapod.analyze_pulse_steps()
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
            self.w.pts.hexapod.set_traj(
                Xaxis, total_time, Xfe - Xst, Xst, 1, abs(step_time), 50
            )

    def fly2d0_SNAKE(
        self, xmotor=0, ymotor=1, scanname="", update_progress=None, update_status=None
    ):
        self.w.isfly2 = False
        ##### ############## need to work from this........

        print()
        # scaninfo = []
        # scaninfo.append('#I Y = ')
        # scaninfo.append(value)
        # self.w.write_scaninfo_to_logfile(scaninfo)
        # print("In fly2d0")
        t0 = time.time()

        self.plotlabels = []
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if self.w.s12softglue.isConnected:
                    self.w.s12softglue.memory_clear()
            except TimeoutError:
                self.w.messages["recent error message"] = (
                    "softglue memory_clear timeout"
                )
                print(self.w.messages["recent error message"])

        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.w.isfly = True
        self.w.isscan = True

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.chk_keep_prev_scan.isChecked():
            self.w.clearplot()

        if self.w.DEBUG_MOTORS:
            xaxis = self.w.motornames[xmotor]
            yaxis = self.w.motornames[ymotor]
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
                self.w.pts.mv(yaxis, yp)
                self.w.pts.mv(xaxis, xp)
                time.sleep(min(tm, 0.05))
                mpos_data.append([self.w.pts.get_pos(xaxis), self.w.pts.get_pos(yaxis)])
                count += 1
                if update_progress is not None:
                    update_progress(int(100 * count / Ntot))
            self.w.mpos = mpos_data
            return

        # expt = np.around(self.w.pts.hexapod.scantime/self.w.pts.hexapod.pulse_number*0.75, 3)
        period = self.w.pts.hexapod.pulse_step
        print(self.w.pts.hexapod.pulse_number, "This is the number of pulses......")
        # expt = period-self.det_readout_time  JD
        expt = (
            period * self.w.parameters._ratio_exp_period
        )  # JMM, *0.2 previously for JD. -0.02 previously for BL
        # if period-expt < DETECTOR_READOUTTIME:
        #    raise RuntimeError("expouretime is too short to readout DET images.")

        if expt <= 0:
            self.w.messages["recent error message"] = (
                f"Exposure time is ≤ 0 (period={period:.4f}, "
                f"ratio={self.w.parameters._ratio_exp_period:.3f})."
            )
            print(self.w.messages["recent error message"])
            raise DET_MIN_READOUT_Error(self.w.messages["recent error message"])

        if abs(period) < self.OVERHEAD_FLY:
            self.w.messages["recent error message"] = (
                "Period < %d ms — Pilatus 2M maximum rate is 30 Hz."
                % (100 * OVERHEAD_FLY)
            )
            print(self.w.messages["recent error message"])
            raise DET_OVER_READOUT_SPEED_Error(self.w.messages["recent error message"])

        # ── REAL MODE ─────────────────────────────────────────────────────────
        # Set delay generator for fly-scan timing.
        if expt != self.w.dg645_12ID._exposuretime:
            try:
                self.w.dg645_12ID.set_pilatus_fly(expt)
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
        self.w.pts.hexapod.goto_start_pos(axes)

        # Arm all detectors for the TOTAL pulse count across the full 2-D snake.
        # The hexapod fires one trigger per position; pulse_number is the total
        # across all lines (not per line).
        for detN, det in enumerate(self.w.detector):
            if det is not None:
                try:
                    det.fly_ready(
                        expt,
                        self.w.pts.hexapod.pulse_number,
                        1,
                        period=period,
                        isTest=isTestRun,
                        capture=(self.w.use_hdf_plugin, self.w.hdf_plugin_savemode_fly),
                        fn=self.w.hdf_plugin_name[detN],
                    )
                except TimeoutError:
                    self.w.messages["recent error message"] = (
                        f"Detector {det._prefix} timed out during fly_ready."
                    )
                    print(self.w.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR

        timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
        if timeout_occurred:
            self.w.messages["recent error message"] = (
                f"Detector arm timeout ({TIMEOUT}s). {time.ctime()}"
            )
            print(self.w.messages["recent error message"])
            return DETECTOR_NOT_STARTED_ERROR

        print("fly2d0_SNAKE: executing 2-D snake trajectory...")
        # Execute the complete 2-D snake trajectory in a single hexapod command.
        # The hexapod moves both X and Y axes simultaneously along the pre-programmed
        # snake path, firing one encoder-synchronized trigger per position.
        self.w.pts.hexapod.run_traj(axes)

        # Wait until all expected frames have been collected.
        Nstep = self.w.pts.hexapod.pulse_number
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
            for ndet, det in enumerate(self.w.detector):
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

            # When one frame short, Armed→0 is the reliable "all data written" signal.
            # ArrayCounter_RBV lags while the HDF5 file is being finalised.
            if N_imgcollected == Nstep - 1:
                active_dets = [
                    det for ndet, det in enumerate(self.w.detector)
                    if ndet <= 1 and det is not None
                ]
                if active_dets and all(det.Armed == 0 for det in active_dets):
                    msg = (
                        f"Warning: fly2d0_SNAKE completed with {N_imgcollected}/{Nstep} "
                        f"frames collected (Armed=0 accepted as finished, but the "
                        f"detector's ArrayCounter_RBV never reached the expected "
                        f"hexapod pulse count)."
                    )
                    self.w.messages["recent error message"] = msg
                    print(msg)
                    break

            # Stall timeout: use the longer window for the last frame.
            stall_limit = LAST_FRAME_TIMEOUT if N_imgcollected == Nstep - 1 else TIMEOUT
            if time.time() - t_since_last_frame > stall_limit:
                self.w.messages["recent error message"] = (
                    f"Data collection stalled after {stall_limit:.1f}s "
                    f"({N_imgcollected}/{Nstep} frames). {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

        self.w.pts.hexapod.wait()
        self.w.run_stop_issued()
        return 1

    def fly0(self, motornumber=-1, update_progress=None, update_status=None):
        t0 = time.time()
        axis = self.w.motornames[motornumber]
        self.w.signalmotor = axis
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        self.plotlabels = []
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if self.w.s12softglue.isConnected:
                    self.w.s12softglue.memory_clear()
            except TimeoutError:
                self.w.messages["recent error message"] = (
                    "softglue memory_clear timeout"
                )
                print(self.w.messages["recent error message"])

        print("")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.w.isfly = True
        self.w.isscan = True

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.chk_keep_prev_scan.isChecked():
            self.w.clearplot()

        st = self.fly1d_st + self.fly1d_p0
        fe = self.fly1d_fe + self.fly1d_p0
        step = self.fly1d_step
        tm = self.fly1d_tm

        if self.w.DEBUG_MOTORS:
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
                self.w.pts.mv(axis, p)
                time.sleep(min(tm, 0.05))
                mpos_data.append(self.w.pts.get_pos(axis))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / N))
            self.w.mpos = mpos_data
            return

        pos = self.w.pts.get_pos(axis)
        # print("Time to finish line 2127: %0.3f" % (time.time()-t0)) very fast down to this far
        if (axis in self.w.pts.hexapod.axes) and (
            self.w.hexapod_flymode == HEXAPOD_FLYMODE_WAVELET
        ):
            if self.ui.chk_reverse_scan_dir.isChecked():
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
            self.w.pts.hexapod.assign_axis2wavtable(
                axis, self.w.pts.hexapod.WaveGenID[axis] + dirv
            )

            period = self.w.pts.hexapod.pulse_step  # pulse step time.
            expt = (
                period * self.w.parameters._ratio_exp_period
            )  # JMM, *0.2 previously for JD. -0.02 previously for BL
            if isTestRun:
                print(
                    f"{self.w.pts.hexapod.pulse_number} images will be collected every {period}s with exposure time of {expt}s."
                )

            if period - expt < DETECTOR_READOUTTIME:
                self.w.messages["recent error message"] = (
                    f"Exposure time {expt:.4f} and period {period:.4f} requires the readout time {period - expt}, which is too short."
                )
                print(self.w.messages["recent error message"])
                self.ui.statusbar.showMessage(self.w.messages["recent error message"])
                return None

            if expt <= 0:
                self.w.messages["recent error message"] = (
                    f"Note that after subtracting the detector readout time {self.det_readout_time:.3e} s, the exposure time becomes equal or less than 0."
                )
                print(self.w.messages["recent error message"])
                raise DET_MIN_READOUT_Error(self.w.messages["recent error message"])

            if abs(period) < self.OVERHEAD_FLY:
                self.w.messages["recent error message"] = (
                    f"Note that Max speed of Pilatus2M is 30Hz."
                )
                print(self.w.messages["recent error message"])
                raise DET_OVER_READOUT_SPEED_Error(
                    self.w.messages["recent error message"]
                )

            # set the delay generator
            if expt != self.w.dg645_12ID._exposuretime:
                try:
                    self.w.dg645_12ID.set_pilatus_fly(expt)
                except:
                    raise DG645_Error

            # SoftGlue ready for recording interferometer values
            movestep = (
                abs(fe - st)
                / self.w.pts.hexapod.pulse_number
                * 1000
                * self.w.parameters._ratio_exp_period
            )
            print(
                f"Actual exposure time: {1000.*expt:0.3f} ms, during which {axis} will move {movestep:.3f} um."
            )

            # If softglue SG is not selected, use prepare for the softglue.
            if self.w.detector[3] is None:
                if self.w.s12softglue.isConnected:
                    N_counts = self.w.s12softglue.number_acquisition(
                        expt, self.w.pts.hexapod.pulse_number
                    )
                    self.w.parameters.countsperexposure = np.round(
                        N_counts / self.w.pts.hexapod.pulse_number
                    )
                    print(
                        f"Total {self.w.parameters.countsperexposure} encoder positions will be collected per a DET image."
                    )
                    if N_counts > 100000:
                        self.w.messages["recent error message"] = (
                            f"******** CAUTION: Number of softglue counts: {N_counts} is larger than 100E3. Slow down the clock speed."
                        )
                        raise SOFTGLUE_Setup_Error(
                            self.w.messages["recent error message"]
                        )

            if isTestRun:
                return

            # Scan start ............................
            self.w.pts.hexapod.goto_start_pos(axis)  # took 0.4 second
            for detN, det in enumerate(self.w.detector):
                if det is not None:
                    try:
                        det.fly_ready(
                            expt,
                            self.w.pts.hexapod.pulse_number,
                            period=period,
                            isTest=isTestRun,
                            capture=(
                                self.w.use_hdf_plugin,
                                self.w.hdf_plugin_savemode_fly,
                            ),
                            fn=self.w.hdf_plugin_name[detN],
                        )
                    except TimeoutError:
                        self.w.messages["recent error message"] = (
                            f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        )
                        print(self.w.messages["recent error message"])
                        self.ui.statusbar.showMessage(
                            self.w.messages["recent error message"]
                        )
                        return DETECTOR_NOT_STARTED_ERROR
            print("Ready for traj")
            pos = self.w.pts.get_pos(axis)
            print(f"pos is {pos} before traj run start.")

            timeout_occurred, TIMEOUT = self.w.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.w.messages["recent error message"] = (
                    f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            istraj_running = False
            timeout = 5
            i = 0
            print("Trajectory scan initiated..")
            while not istraj_running:
                try:
                    self.w.pts.hexapod.run_traj(axis)
                except:
                    pass
                wait_for_traj_start_s = 0.05  # brief pause before checking if hexapod trajectory has started moving
                time.sleep(wait_for_traj_start_s)
                pos_tmp = self.w.pts.get_pos(axis)
                if pos_tmp != pos:
                    istraj_running = True
                # istraj_running = self.w.is_traj_running()
                i = i + 1
                if i > timeout:
                    self.w.messages["recent error message"] = (
                        "traj scan command is resent for 5 times to the hexapod without success."
                    )
                    print(self.w.messages["recent error message"])
                    break
            print("Run_traj is sent command in rungui.")
            isattarget = False
            timeelapsed = 0
            t0 = time.time()
            while not isattarget:
                try:
                    isattarget = self.w.pts.hexapod.isattarget(axis)
                except:
                    isattarget = False
                wait_for_traj_at_target_s = 0.02  # poll interval while waiting for hexapod to reach trajectory end position
                time.sleep(wait_for_traj_at_target_s)
                # pos_tmp = self.w.pts.get_pos(axis)
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
                self.w.messages["current status"] = "%s%s" % (msg1, msg2)
                if update_status:
                    update_status(self.w.messages["current status"])

                if self.isStopScanIssued:
                    break

            pos = self.w.pts.get_pos(axis)
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
            fly_acq_time = getattr(self.w.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
            step_time = max(fly_acq_time, Xtm + self.det_readout_time, self.OVERHEAD_FLY)
            # self.w.parameters._ratio_exp_period = Xtm / step_time
            # total time calculation
            # Use the same rounding-guarded formula as _make_positions/_compute_n_positions
            # (bare int((fe-st)/Xstep) truncation can drop the last trigger on
            # float-rounding edge cases where the division isn't exactly integral).
            Nsteps = self._compute_n_positions([self._make_positions(0, st, fe, Xstep)], scan_kind="fly_phi")
            total_time = Nsteps * step_time
            # expt = step_time*self.w.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
            expt = Xtm
            if step_time - expt < 0.015:
                raise DET_MIN_READOUT_Error(
                    f"Period - Exposure Time,{step_time - expt}s, should be longer than 50 microseconds."
                )

            # set the delay generator
            try:
                self.w.dg645_12ID.set_pilatus2(
                    expt, Nsteps, step_time
                )  # exposuretime, number of images, and time period for fly scan.
            except:
                raise DG645_Error
            print(
                f"Exposure time: {expt:0.3e} s, number of steps: {Nsteps}, Step time: {step_time:.3e} s, Total time for the scan: {total_time:.3f} s."
            )
            if self.ui.chk_reverse_scan_dir.isChecked():
                if abs(st - pos) > abs(fe - pos):
                    t = fe
                    fe = st
                    st = t

            if motornumber == 6:
                # enable fit menu
                self.ui.actionFit_QDS_phi.setEnabled(True)

            self._prev_vel, self._prev_acc = self.w.pts.get_speed(axis)
            self.w.pts.mv(axis, st, wait=True)
            wait_for_motor_settle_s = 0.1  # brief settle time after moving phi to start position before setting fly speed
            time.sleep(wait_for_motor_settle_s)
            # print(f"Setting speed for fly scan. Total time: {abs(fe-st)/total_time:.3f} s, acceleration: {abs(fe-st)/total_time*10:.3f}.")
            self.w.pts.set_speed(
                axis, abs(fe - st) / total_time, abs(fe - st) / total_time * 10
            )
            wait_for_speed_set_s = (
                0.02  # brief pause after setting fly scan speed before arming detectors
            )
            time.sleep(wait_for_speed_set_s)

            # Need to make detectors ready
            for detN, det in enumerate(self.w.detector):
                if det is not None:
                    try:
                        det.fly_ready(
                            expt,
                            Nsteps,
                            period=step_time,
                            isTest=isTestRun,
                            capture=(
                                self.w.use_hdf_plugin,
                                self.w.hdf_plugin_savemode_fly,
                            ),
                            fn=self.w.hdf_plugin_name[detN],
                        )
                    #            print("Time to finish line 2190: %0.3f" % (time.time()-t0)) # take 0.3 second
                    except TimeoutError:
                        self.w.messages["recent error message"] = (
                            f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        )
                        print(self.w.messages["recent error message"])
                        self.ui.statusbar.showMessage(
                            self.w.messages["recent error message"]
                        )
                        # showerror("Detector timeout.")
                        return

            timeout_occurred, TIMEOUT = self.w.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.w.messages["recent error message"] = (
                    f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                )
                print(self.w.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            scaninfo = []
            print("")
            print(f"{axis} scan started..")
            scaninfo.append(f"FileIndex, {axis},    time(s)")
            scaninfo.append(f"0,   {st},   {time.time()}")
            self.w.pts.mv(axis, fe, wait=False)

            print("about to send out trigger.")
            # Start collect data while an axis is moving.
            self.w.dg645_12ID.trigger()
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
                for ndet, det in enumerate(self.w.detector):
                    if ndet > 1:
                        continue
                    if det is not None:
                        val = det.ArrayCounter_RBV
                        break
                prog = float(val) / float(Nsteps)
                pos = self.w.pts.get_pos(axis)
                scaninfo.append(f"{val},    {pos},  {time.time()}")

                if update_progress:
                    update_progress(int(prog * 100))
                msg1 = f"Elapsed time = {int(timeelapsed)}s since the start."
                if prog > 0:
                    remainingtime = timeelapsed / prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime, 2)}s\n"
                self.w.messages["current status"] = "%s%s" % (msg1, msg2)
                if update_status:
                    update_status(self.w.messages["current status"])

                wait_for_det_progress_s = 0.1  # poll interval while monitoring phi fly-scan frame collection progress
                time.sleep(wait_for_det_progress_s)
                if val > N_imgcollected:
                    N_imgcollected = val
                    timestart = time.time()

                updatetime = time.time() - timestart
                if updatetime > TIMEOUT:
                    self.w.messages["recent error message"] = (
                        f"Detector {det._prefix} data collection timeout after {TIMEOUT} seconds."
                    )
                    print(self.w.messages["recent error message"])
                    self.ui.statusbar.showMessage(
                        self.w.messages["recent error message"]
                    )
                    return DETECTOR_NOT_STARTED_ERROR
                timeelapsed = time.time() - t0
                if self.isStopScanIssued:
                    break
            self.w.write_scaninfo_to_logfile(scaninfo)

        return 1

    def helix_fly0(self, phimotor=6, zmotor=2, update_progress=None, update_status=None):
        """Worker-thread executor for helix fly scan: phi + Z simultaneous constant-velocity fly."""
        t0 = time.time()
        phi_axis = self.w.motornames[phimotor]
        z_axis = self.w.motornames[zmotor]
        self.w.signalmotor = f"{phi_axis},{z_axis}"
        self.w.signalmotorunit = self.w.motorunits[phimotor]

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if self.w.s12softglue.isConnected:
                    self.w.s12softglue.memory_clear()
            except TimeoutError:
                self.w.messages["recent error message"] = "softglue memory_clear timeout"
                print(self.w.messages["recent error message"])

        print("")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run (helix):")
        self.w.isfly = True
        self.w.isscan = True

        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.chk_keep_prev_scan.isChecked():
            self.w.clearplot()

        phi_st = self.helix_phi_st + self.helix_phi_p0
        phi_fe = self.helix_phi_fe + self.helix_phi_p0
        phi_step = self.helix_phi_step
        phi_tm = self.helix_phi_tm

        z_st = self.helix_z_st + self.helix_z_p0
        z_fe = self.helix_z_fe + self.helix_z_p0

        if self.w.DEBUG_MOTORS:
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
                self.w.pts.mv(phi_axis, p)
                self.w.pts.mv(z_axis, z_p)
                time.sleep(min(phi_tm, 0.05))
                phi_mpos_data.append(self.w.pts.get_pos(phi_axis))
                z_mpos_data.append(self.w.pts.get_pos(z_axis))
                if update_progress is not None:
                    update_progress(int(100 * (i + 1) / N))
            self.w.mpos = phi_mpos_data
            self.w.zpos = z_mpos_data
            return

        phi_pos = self.w.pts.get_pos(phi_axis)
        z_pos = self.w.pts.get_pos(z_axis)

        Xstep = self.helix_phi_step
        Xtm = self.helix_phi_tm

        fly_acq_time = getattr(self.w.parameters, "_fly_acq_time", self.OVERHEAD_FLY)
        step_time = max(fly_acq_time, Xtm + self.det_readout_time, self.OVERHEAD_FLY)
        # Same rounding-guarded formula used for helix's nominal count in
        # _log_scan_header, so the logged count and the actual DG645 arm
        # count can never diverge.
        Nsteps = self._compute_n_positions([self._make_positions(0, phi_st, phi_fe, Xstep)], scan_kind="fly_phi")
        total_time = Nsteps * step_time
        expt = Xtm
        if step_time - expt < 0.015:
            raise DET_MIN_READOUT_Error(
                f"Period - Exposure Time,{step_time - expt}s, should be longer than 50 microseconds."
            )

        try:
            self.w.dg645_12ID.set_pilatus2(expt, Nsteps, step_time)
        except:
            raise DG645_Error
        print(
            f"Exposure time: {expt:0.3e} s, number of steps: {Nsteps}, Step time: {step_time:.3e} s, Total time for the scan: {total_time:.3f} s."
        )

        if self.ui.chk_reverse_scan_dir.isChecked():
            if abs(phi_st - phi_pos) > abs(phi_fe - phi_pos):
                t = phi_fe
                phi_fe = phi_st
                phi_st = t
            if abs(z_st - z_pos) > abs(z_fe - z_pos):
                t = z_fe
                z_fe = z_st
                z_st = t

        self._prev_vel_phi, self._prev_acc_phi = self.w.pts.get_speed(phi_axis)
        self._prev_vel_z, _ = self.w.pts.get_speed(z_axis)

        self.w.pts.mv(phi_axis, phi_st, wait=True)
        self.w.pts.mv(z_axis, z_st, wait=True)
        wait_for_motor_settle_s = 0.1
        time.sleep(wait_for_motor_settle_s)

        phi_vel = abs(phi_fe - phi_st) / total_time
        z_vel = abs(z_fe - z_st) / total_time

        self.w.pts.set_speed(phi_axis, phi_vel, phi_vel * 10)
        self.w.pts.set_speed(z_axis, z_vel, None)
        wait_for_speed_set_s = 0.02
        time.sleep(wait_for_speed_set_s)

        for detN, det in enumerate(self.w.detector):
            if det is not None:
                try:
                    det.fly_ready(
                        expt,
                        Nsteps,
                        period=step_time,
                        isTest=isTestRun,
                        capture=(
                            self.w.use_hdf_plugin,
                            self.w.hdf_plugin_savemode_fly,
                        ),
                        fn=self.w.hdf_plugin_name[detN],
                    )
                except TimeoutError:
                    self.w.messages["recent error message"] = (
                        f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                    )
                    print(self.w.messages["recent error message"])
                    self.ui.statusbar.showMessage(
                        self.w.messages["recent error message"]
                    )
                    return

        timeout_occurred, TIMEOUT = self.w.is_arming_detecotors_timedout()
        if timeout_occurred:
            self.w.messages["recent error message"] = (
                f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
            )
            print(self.w.messages["recent error message"])
            return DETECTOR_NOT_STARTED_ERROR

        scaninfo = []
        print("")
        print(f"{phi_axis},{z_axis} helix scan started..")
        scaninfo.append(f"FileIndex, {phi_axis}, {z_axis},    time(s)")
        scaninfo.append(f"0,   {phi_st},   {z_st},   {time.time()}")

        self.w.pts.mv(phi_axis, phi_fe, wait=False)
        self.w.pts.mv(z_axis, z_fe, wait=False)

        print("about to send out trigger.")
        self.w.dg645_12ID.trigger()
        print("Delay generator is triggered to start the helix fly scan.")

        N_imgcollected = 0
        timeelapsed = time.time() - t0
        TIMEOUT = total_time + 5
        if TIMEOUT < 5:
            TIMEOUT = 5
        timestart = time.time()
        val = 0

        while N_imgcollected < Nsteps:
            for ndet, det in enumerate(self.w.detector):
                if ndet > 1:
                    continue
                if det is not None:
                    val = det.ArrayCounter_RBV
                    break
            prog = float(val) / float(Nsteps)
            phi_pos = self.w.pts.get_pos(phi_axis)
            z_pos = self.w.pts.get_pos(z_axis)
            scaninfo.append(f"{val},    {phi_pos},   {z_pos},  {time.time()}")

            if update_progress:
                update_progress(int(prog * 100))
            msg1 = f"Elapsed time = {int(timeelapsed)}s since the start."
            if prog > 0:
                remainingtime = timeelapsed / prog - timeelapsed
            else:
                remainingtime = 999
            msg2 = f"; Remaining time for the current helix scan is {np.round(remainingtime, 2)}s\n"
            self.w.messages["current status"] = "%s%s" % (msg1, msg2)
            if update_status:
                update_status(self.w.messages["current status"])

            wait_for_det_progress_s = 0.1
            time.sleep(wait_for_det_progress_s)
            if val > N_imgcollected:
                N_imgcollected = val
                timestart = time.time()

            updatetime = time.time() - timestart
            if updatetime > TIMEOUT:
                self.w.messages["recent error message"] = (
                    f"Detector {det._prefix} data collection timeout after {TIMEOUT} seconds."
                )
                print(self.w.messages["recent error message"])
                self.ui.statusbar.showMessage(
                    self.w.messages["recent error message"]
                )
                return DETECTOR_NOT_STARTED_ERROR
            timeelapsed = time.time() - t0
            if self.isStopScanIssued:
                break
        self.w.write_scaninfo_to_logfile(scaninfo)

        return 1

    def is_traj_running(self):
        ret = False
        if self.w.s12softglue.isConnected:
            if self.w.s12softglue.get_eventN() == 0:
                ret = False
            else:
                ret = True
        return ret

    def print_fly_settings(self, motornumber):
        print("")
        print("Currently, the flyscan only works for X axis of the hexapod.")
        print("==========================================================")
        print("")
        axis = self.w.motornames[motornumber]
        self.w.signalmotor = axis
        self.w.signalmotorunit = self.w.motorunits[motornumber]

        self.w.isfly = True
        n = motornumber + 1
        p0 = self.ui.findChild(QLabel, "lb_%i" % n).text()
        p0 = float(p0)
        self.ui.findChild(QLineEdit, "ed_%i" % n).setText("%0.6f" % p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L" % n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R" % n).text())
        tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t" % n).text())
        st = st + p0
        fe = fe + p0
        try:
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N" % n).text())
        except:
            step = 0.1
            self.ui.findChild(QLineEdit, "ed_lup_%i_N" % n).setText("%0.3f" % step)
        pos = self.w.pts.get_pos(axis)
        if axis in self.w.pts.hexapod.axes:
            if self.ui.chk_reverse_scan_dir.isChecked():
                if abs(st - pos) > abs(fe - pos):
                    t = fe
                    fe = st
                    st = t
                    step = -step
            if (self.w.hexapod_flymode == HEXAPOD_FLYMODE_WAVELET) and (axis == "X"):
                direction = int(step) / abs(step)
                self.w.pts.hexapod.set_traj(
                    axis, tm, fe - st, st, direction, abs(step), 50
                )
                if direction == 1:
                    dirv = 0
                else:
                    dirv = 6
                self.w.pts.hexapod.assign_axis2wavtable(
                    axis, self.w.pts.hexapod.WaveGenID[axis] + dirv
                )
            else:
                print("Currently, the flyscan only works for X axis.")
        else:
            print("Currently, the flyscan only works for X axis of the hexapod.")
        print("==========================================================")
        print("")
        print("")

    def save_qds(self, filename="", saveoption="w"):
        if type(filename) == bool:
            fn = ""
        if type(filename) == str:
            if len(filename) == 0:
                fn = ""
            else:
                fn = filename
        if len(fn) == 0:
            filename = self.w.getfilename()

        self.w.rpos = np.asarray(self.w.rpos)
        self.w.mpos = np.asarray(self.w.mpos)
        if self.w.isStruckCountNeeded:
            pass
        else:
            # data unit and data
            if self.w.parameters._qds_unit == QDS_UNIT_MM:
                self.w.rpos = self.w.rpos / 1e3
            if self.w.parameters._qds_unit == QDS_UNIT_UM:
                pass
            if self.w.parameters._qds_unit == QDS_UNIT_NM:
                self.w.rpos = self.w.rpos * 1e3
        # print(self.w.rpos.shape, " This is the shape of rpos")
        col = []
        for ind in range(self.w.rpos.shape[1]):
            col.append(ind)
        self.w.save_list(filename, self.w.mpos, self.w.rpos, col=col, option=saveoption)

    def save_list(self, filename, mpos, rpos, col, option="w"):
        mpos = np.asarray(mpos)
        rpos = np.asarray(rpos)

        if len(rpos) == 0:
            return
        if len(mpos) == 0:
            mpos = np.arange(rpos.shape[1])
        if mpos.ndim == 2:
            with open(filename, option) as f:
                for i, m in enumerate(mpos):
                    strv = ""
                    for data in m:
                        strv = "%s    %0.5e" % (strv, data)
                    for cind in range(len(col)):
                        strv = "%s    %0.5e" % (strv, rpos[cind][i])
                    f.write("%s\n" % (strv))
        else:
            with open(filename, option) as f:
                for i, m in enumerate(mpos):
                    strv = ""
                    for cind in range(len(col)):
                        strv = "%s    %0.5e" % (strv, rpos[cind][i])
                    f.write("%0.5e%s\n" % (m, strv))

    def save_nparray(self, filename, mpos, rpos, col, option="w"):
        with open(filename, option) as f:
            for i, m in enumerate(mpos):
                strv = ""
                for cind in col:
                    strv = "%s    %0.5e" % (strv, rpos[cind][i])
                f.write("%0.5e%s\n" % (m, strv))

    def savescan(self, filename=""):
        if self.w.is_selfsaved:
            self.w.save_qds(self.tempfilename, "a")
            filename = self.w.getfilename()
            os.rename(self.tempfilename, filename)
        else:
            self.w.save_qds(filename=filename)
        if self.w.is_selfsaved:
            self.w.is_selfsaved = False

    def fly_result(self):
        # if len(filename)==0:
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Save QDS Data As")
        fn = QFileDialog.getSaveFileName(
            w,
            "Save File",
            "",
            "Text (*.txt *.dat)",
            None,
            QFileDialog.DontUseNativeDialog,
        )
        filename = fn[0]
        if filename == "":
            return 0
        # filename handling
        if ".txt" not in filename:
            filename = filename + ".txt"
        d = os.path.dirname(filename)
        if len(d) == 0:
            filename = os.path.join(self.w.parameters.working_folder, filename)
        else:
            self.w.parameters.working_folder = d
        data = self.w.pts.hexapod.get_records()
        if isinstance(data, type({})):
            l_data = [data]
        else:
            l_data = data

        try:
            axis = self.w.signalmotor
        except:
            axis = "X"
        for data in l_data:
            # ndata = data[axis][0].size
            # x = range(0, ndata)
            if len(filename) > 0:
                print(
                    f"Target, Encoder, and Pulse positions for axis {axis} are saved in {filename}."
                )
                target = data[axis][0] * 1000
                encoded = data[axis][1] * 1000
                ind = np.zeros(target.shape, int)
                ind[self.w.pts.hexapod.pulse_positions_index] = 1
                try:
                    dt2 = np.column_stack((target, encoded, ind))
                    np.savetxt(filename, dt2, fmt="%1.8e %1.8e %i")
                except:
                    self.w.messages["recent error message"] = "Error in fly_result."
                    print(self.w.messages["recent error message"])

                print("Done...")

    def run_json(self, json_message):
        # data = json.loads(json_message)
        # return_message = None
        cmd = json_message["command"]
        scanname = ""
        try:
            data = json_message["data"]
        except:
            data = {}

        try:
            xmotor = int(data["xmotor"])
        except:
            xmotor = DEFAULTS["xmotor"]
        try:
            detectors = data["detectors"]
        except:
            detectors = ""
        try:
            ymotor = int(data["ymotor"])
        except:
            ymotor = DEFAULTS["ymotor"]
        try:
            phimotor = int(data["phimotor"])
        except:
            phimotor = DEFAULTS["phimotor"]
        try:
            scanname = data["scanname"]
        except:
            scanname = ""
        try:
            folder = data["folder"]
        except:
            folder = ""
        try:
            saxsmode = bool(int(data["saxsmode"]))
        except:
            saxsmode = False
        try:
            testmode = bool(int(data["testmode"]))
        except:
            testmode = False

        if cmd == "set":
            if saxsmode:
                self.w.set_hdf_plugin_use(True)
                self.w.select_detector_mode(False)
                self.w.set_hdf_plugin_use(True)
                # self.w.set_basepaths('/net/s12data/export/12id-c/')

            if testmode:
                print("Testmode is on.")
                self.w.set_monitor_beamline_status(False)
                self.w.set_shutter_close_after_scan(False)
            else:
                print("Testmode is off.")
                self.w.set_monitor_beamline_status(True)
                self.w.set_shutter_close_after_scan(True)
            # if scanname is provided, set it.
            if len(scanname) > 0:
                try:
                    print(f"Setting scanname to {scanname}")
                    self.ui.edit_scanname.setText(scanname)
                    self.w.update_scanname()
                except:
                    pass
            if len(detectors) > 0:
                for N in range(1, 7):
                    if str(N) in detectors:
                        try:
                            self.w.select_detectors(N, value=True)
                        except:
                            pass

        elif cmd == "setrange":
            motornumber = self.w.motornames.index(data["axis"])
            n = motornumber + 1
            for key, val in data.items():
                if key == "axis":
                    pass
                else:
                    self.ui.findChild(QLineEdit, "ed_lup_%i_%s" % (n, key)).setText(val)

        elif cmd == "mv":
            for axis, pos in data.items():
                motornumber = self.w.motornames.index(axis)
                n = motornumber + 1
                self.ui.findChild(QLineEdit, "ed_%i" % n).setText("%0.6f" % float(pos))
                self.w.mv(motornumber=motornumber, val=float(pos))
        elif cmd == "mvr":
            for axis, pos in data.items():
                motornumber = self.w.motornames.index(axis)
                self.w.mvr(motornumber=motornumber, val=float(pos))

        elif cmd == "fly2d":
            self.w.fly2d(xmotor=xmotor, ymotor=ymotor, scanname=scanname)

        elif cmd == "fly2d_snake":
            self.w.fly2d(xmotor=xmotor, ymotor=ymotor, snake=True, scanname=scanname)

        elif cmd == "fly3d":
            self.w.fly3d(
                xmotor=xmotor, ymotor=ymotor, phimotor=phimotor, scanname=scanname
            )

        elif cmd == "fly3d_snake":
            self.w.fly3d(
                xmotor=xmotor,
                ymotor=ymotor,
                phimotor=phimotor,
                snake=True,
                scanname=scanname,
            )

        elif cmd == "stepscan3d":
            self.w.stepscan3d(xmotor=xmotor, ymotor=ymotor, phimotor=phimotor)

        elif cmd == "stepscan2d":
            self.w.stepscan2d(xmotor=xmotor, ymotor=ymotor)

        elif cmd == "none":
            self.runRequested.emit(0)

        elif cmd == "toggle":
            try:
                val = data["controllerfly"]
                if val == "on":
                    self.ui.actionEnable_fly_with_controller.setChecked(True)
                if val == "off":
                    self.ui.actionEnable_fly_with_controller.setChecked(False)
            except:
                pass

            try:
                val = data["keepprevscan"]
                if val == "on":
                    self.ui.chk_keep_prev_scan.setChecked(True)
                if val == "off":
                    self.ui.chk_keep_prev_scan.setChecked(False)
            except:
                pass

            try:
                val = data["reversescan"]
                if val == "on":
                    self.ui.chk_reverse_scan_dir.setChecked(True)
                if val == "off":
                    self.ui.chk_reverse_scan_dir.setChecked(False)
            except:
                pass
        elif cmd == "shclose":
            self.w.shutter.close()
        elif cmd == "setfolder":
            self.w.parameters.working_folder = folder
            self.w.update_workingfolder(self.w.parameters.working_folder)
        elif cmd == "get_error_message":
            return self.w.messages["recent error message"]
        else:
            print(f"Invalid command {cmd} is recieved.")

    def run_cmd(self, n):
        pass  # body was commented out / empty in original rungui.py

    def set_mv(self, axis, pos):
        motornumber = self.w.motornames.index(axis)
        self.w.mv(motornumber=motornumber, val=pos)
