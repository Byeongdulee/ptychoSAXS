"""
gui/handlers/scan_handler.py
Scan execution, detector management, data saving, and network command handling.
Extracted from ptyco_main_control in rungui.py.
"""

import time
import os
import json
import numpy as np
import re
import traceback
import datetime
import pathlib
from PyQt5.QtWidgets import QMessageBox, QInputDialog, QLabel, QLineEdit, QFileDialog
import pyqtgraph as pg


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


class ScanHandler:
    # Per-point overhead added to exposure time when estimating scan duration.
    # Fly scan overhead accounts for detector readout + SoftGlue latency.
    # Step scan overhead accounts for motor settle + readout.
    OVERHEAD_FLY = 0.04  # seconds # 0.033 is the Pilatus 2M limit (30 Hz)
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

        Nx = (lup_1_R - lup_1_L) / lup_1_N + 1
        Ny = (lup_3_R - lup_3_L) / lup_3_N + 1
        Ntot = Nx * Ny
        est_time = Ntot * (lup_1_t + overhead)

        Overhead is OVERHEAD_FLY for fly scans, OVERHEAD_STEP for step scans.
        The scan type is determined by whether pushButton_flyscan is checked.
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

        Nx = (lup_1_R - lup_1_L) / lup_1_N + 1
        Ny = (lup_3_R - lup_3_L) / lup_3_N + 1
        Ntot = Nx * Ny

        step_est = Ntot * (lup_1_t + self.OVERHEAD_STEP)
        fly_est = Ntot * (lup_1_t + self.OVERHEAD_FLY)

        def _set_label(name, text):
            lbl = self.ui.findChild(QLabel, name)
            if lbl is not None:
                lbl.setText(text)

        _set_label("label_Nx", "Nx\n%d" % int(round(Nx)))
        _set_label("label_Ny", "Ny\n%d" % int(round(Ny)))
        _set_label("label_Ntot", "Ntot\n%d" % int(round(Ntot)))

        def _fmt_time(t):
            return "%.1f min" % (t / 60) if t > 300 else "%.1f s" % t

        _set_label("label_estT", "%s\n%s" % (_fmt_time(step_est), _fmt_time(fly_est)))

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

    def _make_positions(
        self, p0: float, st: float, fe: float, step: float
    ) -> np.ndarray:
        """Return absolute scan positions as a 1-D numpy array.

        p0+st is the scan start; p0+fe is the scan end.  The sign of *step* is
        corrected automatically so the direction matches st→fe.  If step==0 it is
        replaced by (fe-st) so a two-element array is still produced.
        When arange yields only one element (st==fe), returns [p0+st, p0+fe]
        so callers always get at least two positions.
        """
        ast = p0 + st
        afe = p0 + fe
        if step == 0:
            step = (afe - ast) if (afe != ast) else 1.0
        step = -abs(step) if ast > afe else abs(step)
        pos = np.arange(ast, afe + step / 2, step)
        return pos if len(pos) > 1 else np.array([ast, afe])

    def _pre_scan(self, scan_name: str) -> None:
        """Common setup called at the start of every scan entry point (GUI thread).

        Resets detector file/frame counters, refreshes scan name and file paths,
        logs current motor positions, and clears the stop flag so a previous scan's
        stop signal does not immediately abort the new one.
        """
        self.w.update_scanname()
        self.w.get_detectors_ready()
        self.w.prepare_scan_files()
        self.w.write_motor_scan_range()
        self.isStopScanIssued = False
        print(f"\n\n{scan_name} starting")

    def _log_scan_header(self, scan_name: str, axes_params: list) -> None:
        """Write the SPEC-style #S header line for this scan to the log file.

        axes_params is a list of dicts from _read_motor_params, in axis order
        (X first, then Y if 2-D, then phi if 3-D).
        """
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

        title = "2d scan positions using current lup scan parameters"
        win = pg.PlotWidget(title=title)
        win.setWindowTitle(title)
        win.resize(600, 500)
        plot = win.getPlotItem()
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
        win.show()
        # Keep reference so the window is not garbage-collected
        self._scan_pos_window = win

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

    def set_fly_idletime(self):
        val, ok = QInputDialog().getDouble(
            self,
            "Flyscan step time-exptime",
            "Time (s)",
            self.w.parameters._fly_idletime,
            decimals=3,
        )
        self.w.parameters._fly_idletime = val
        self.w.parameters.writeini()

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
                        # time.sleep(5)
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
                            if str(fnum - 1) not in fn:
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
        if self.isStopScanIssued:
            self.w.set_scan_status("Stopped")
            self.ui.statusbar.showMessage("Scan stopped by user \u2014 motors returned.")
        else:
            self.w.set_scan_status("No Scan")
            self.ui.statusbar.showMessage("Fly scan complete.")
        self.w.s12softglue.flush()
        print(f"softglue flushed at {time.ctime()}")

        self.w.update_scanname()

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

    def flydone2d(self, value=0):
        for key in self.w.motor_p0:
            self.w.mv(key, self.w.motor_p0[key])
        self.w.isscan = False
        self.w.isfly = False
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

    def check_start_position(self, n):
        # Compare p0 and p0_move_to at 4 digits
        p0_move_to = self.ui.findChild(QLineEdit, "ed_%i" % n).text()
        p0 = self.ui.findChild(QLabel, "lb_%i" % n).text()
        if len(p0_move_to) > 0:
            try:
                p0_float = float(p0)
                p0_move_to_float = float(p0_move_to)
            except Exception:
                p0_float = p0
                p0_move_to_float = p0_move_to
            if round(p0_float, 4) != round(p0_move_to_float, 4):
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
        scan_name = "fly2d_SNAKE" if snake else "fly2d"
        self._pre_scan(scan_name)

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

        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

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

        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        if not self._confirm_large_scan(
            len(xpos) * len(ypos), xax["expt"], self.OVERHEAD_FLY
        ):
            return

        self._log_scan_header(scan_name, [xax, yax])

        if snake:
            # Program the full 2-D snake trajectory on the hexapod controller
            # before the worker starts.  fly2d0_SNAKE then just triggers it.
            self.w.fly_traj(xmotor, ymotor)
            self._launch_worker(
                self.fly2d0_SNAKE,
                xmotor,
                ymotor,
                done_signal=self.w.flydone,
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
        scan_name = "fly3d_SNAKE" if snake else "fly3d"
        self._pre_scan(scan_name)

        self.w.switch_SGstream(snake)

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()

        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
            phiax = self._read_motor_params(phimotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

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

        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        phipos = self._make_positions(
            phiax["p0"], phiax["st"], phiax["fe"], phiax["step"]
        )
        if not self._confirm_large_scan(
            len(xpos) * len(ypos) * len(phipos), xax["expt"], self.OVERHEAD_FLY
        ):
            return

        self._log_scan_header(scan_name, [xax, yax, phiax])

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
        self._pre_scan("fly")

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        if motornumber < 0:
            motornumber = self._motor_from_sender()

        try:
            ax = self._read_motor_params(motornumber)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        self.fly1d_p0 = ax["p0"]
        self.fly1d_st = ax["st"]
        self.fly1d_fe = ax["fe"]
        self.fly1d_tm = ax["expt"]
        self.fly1d_step = ax["step"]

        self.w.signalmotor = ax["name"]
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        self.w.motor_p0 = {motornumber: ax["p0"]}
        self.time_scanstart = time.time()

        pos = self._make_positions(ax["p0"], ax["st"], ax["fe"], ax["step"])
        if not self._confirm_large_scan(len(pos), ax["expt"], self.OVERHEAD_FLY):
            return

        self._log_scan_header("fly", [ax])

        # Program the hexapod trajectory waveform before the worker starts.
        # fly_traj sets self.Xaxis and _ratio_exp_period, which fly0 needs.
        if ax["name"] in self.w.pts.hexapod.axes:
            self.w.fly_traj(motornumber)

        self._launch_worker(self.fly0, motornumber, done_signal=self.w.flydone)

        # Advance the scan number immediately after launch.  Preserves the
        # original fly() behaviour; other scan types advance on completion.
        self.w.run_stop_issued()
        self.w.update_status_scan_time()

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
        if not self._check_hdf_for_multi_pulse():
            return
        self._pre_scan("stepscan")

        # Resolve the motor index: negative means the call came from a UI button
        # whose object name encodes the motor number (e.g. 'pushButton_step_3').
        if motornumber < 0:
            motornumber = self._motor_from_sender()

        try:
            ax = self._read_motor_params(motornumber)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

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

        pos = self._make_positions(ax["p0"], ax["st"], ax["fe"], ax["step"])
        if not self._confirm_large_scan(len(pos), ax["expt"], self.OVERHEAD_STEP):
            return

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
        if not self._check_hdf_for_multi_pulse():
            return
        self._pre_scan("stepscan2d")

        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

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

        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        if not self._confirm_large_scan(
            len(xpos) * len(ypos), xax["expt"], self.OVERHEAD_STEP
        ):
            return

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
        if not self._check_hdf_for_multi_pulse():
            return
        self._pre_scan("stepscan3d")

        self.isMCS_ready = False
        if self.w.detector[2] is not None:
            self.w.detector[2].mcs_init()

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if self.w.s12softglue.isConnected:
                self.w.s12softglue.ckTime_reset()

        try:
            xax = self._read_motor_params(xmotor)
            yax = self._read_motor_params(ymotor)
            phiax = self._read_motor_params(phimotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

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

        xpos = self._make_positions(xax["p0"], xax["st"], xax["fe"], xax["step"])
        ypos = self._make_positions(yax["p0"], yax["st"], yax["fe"], yax["step"])
        phipos = self._make_positions(
            phiax["p0"], phiax["st"], phiax["fe"], phiax["step"]
        )
        if not self._confirm_large_scan(
            len(xpos) * len(ypos) * len(phipos), xax["expt"], self.OVERHEAD_STEP
        ):
            return

        self._log_scan_header("stepscan3d", [xax, yax, phiax])

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
        if st > fe:
            step = -1 * abs(step)
        if st < fe:
            step = abs(step)
        if self.ui.chk_reverse_scan_dir.isChecked():
            if abs(st - pos) > abs(fe - pos):
                t = fe
                fe = st
                st = t
                step = -step

        # start scan..
        self.w.pts.mv(axis, st)
        pos = np.arange(st, fe + step / 2, step)
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
                isDET_selected = True
                print(
                    f"Arming detector {detN} ({det._prefix}) for {len(pos)} positions."
                )
                try:
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

        ## prepare zig-zag positions ................
        # build Y range (absolute)
        yst = self.stepscan2d_st + self.stepscan2d_p0
        yfe = self.stepscan2d_fe + self.stepscan2d_p0
        ystep = self.stepscan2d_step

        # build X range (absolute)
        xst = self.stepscan1d_st + self.stepscan1d_p0
        xfe = self.stepscan1d_fe + self.stepscan1d_p0
        xstep = self.stepscan1d_step

        if xstep == 0:
            xstep = (xfe - xst) if (xfe != xst) else 1.0
        xstep = -abs(xstep) if xst > xfe else abs(xstep)
        x_coords = np.arange(xst, xfe + 0.5 * xstep, xstep)

        # build Y range (absolute) from st, fe, step already computed above
        ystep = ystep if ystep != 0 else ((yfe - yst) if (yfe != yst) else 1.0)
        ystep = -abs(ystep) if yst > yfe else abs(ystep)
        y_coords = np.arange(yst, yfe + 0.5 * ystep, ystep)

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

        if st > fe:
            step = -1 * abs(step)
        if st < fe:
            step = abs(step)

        # revsere scan disabled: always scan from start to final regardless of the initial position.
        self.w.pts.mv(axis, st)
        pos = np.arange(st, fe + step / 2, step)

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
            x_step = -abs(x_step) if x_st > x_fe else abs(x_step)
            x_coords = np.arange(x_st, x_fe + x_step / 2, x_step)
            if len(x_coords) == 1:
                x_coords = np.array([x_st, x_fe])

            y_st = self.stepscan2d_st + self.stepscan2d_p0
            y_fe = self.stepscan2d_fe + self.stepscan2d_p0
            y_step = self.stepscan2d_step
            y_step = -abs(y_step) if y_st > y_fe else abs(y_step)
            y_coords = np.arange(y_st, y_fe + y_step / 2, y_step)
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

            # Program the hexapod trajectory for this phi slice, then run the
            # 2-D executor directly on the current worker thread (no sub-worker).
            if snake:
                # Disable the wave generator before reprogramming; set_traj_SNAKE2
                # fails with GCSError 73 if the generator output is still active.
                if i > 0:
                    self.w.pts.hexapod.stop_traj()
                self.fly_traj(xmotor, ymotor)
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

        # Compute step_time = exposure + idle.
        # The idle time must be at least as long as the detector readout so that
        # the next trigger does not arrive before the previous frame is read out.
        flyidletime = getattr(self.w.parameters, "_fly_idletime", DETECTOR_READOUTTIME)
        if flyidletime < DETECTOR_READOUTTIME:
            flyidletime = DETECTOR_READOUTTIME
        if Xtm + flyidletime < self.OVERHEAD_FLY:
            # Enforce a minimum period of 33 ms (Pilatus 2M limit of 30 Hz).
            flyidletime = self.OVERHEAD_FLY - Xtm
        step_time = Xtm + flyidletime
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
            Nsteps = int((Xfe - Xst) / Xstep) + 1
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
        TIMEOUT = period * Nstep + 2  # generous: total scan time + 2 s buffer
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

            # Abort if no new frames arrived within the timeout window.
            if time.time() - t_since_last_frame > TIMEOUT:
                self.w.messages["recent error message"] = (
                    f"Data collection stalled after {TIMEOUT:.1f}s "
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
                f"Actual exposure time: {expt:0.3e} s, during which {axis} will move {movestep:.3e} um."
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
            step_time = Xtm + self.det_readout_time
            if step_time < self.OVERHEAD_FLY:
                step_time = self.OVERHEAD_FLY
            # self.w.parameters._ratio_exp_period = Xtm / step_time
            # total time calculation
            Nsteps = int((fe - st) / Xstep)
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
