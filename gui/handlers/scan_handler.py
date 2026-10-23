"""
gui/handlers/scan_handler.py
Scan execution, detector management, data saving, and network command handling.
Extracted from ptyco_main_control in rungui.py.
"""

import sys
import time
import os
import csv
import json
import math
import numpy as np
import re
import traceback
import datetime
import pathlib
import inspect
from collections import deque
from PyQt5.QtWidgets import QMessageBox, QInputDialog, QLabel, QLineEdit, QFileDialog, QPushButton, QCheckBox, QDialog, QVBoxLayout

# The upstream-optics motor/PV table, shared with the optics panel and
# rungui's Check Upstream Optics report so the three cannot disagree.
from gui import optics_status as _optics_status
from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal, QSettings
import pyqtgraph as pg

from ptychosaxs.api import guards as guard_checks
from ptychosaxs.api.scan_exec import ScanExecutorsMixin
from ptychosaxs.api.scans import ScanContext
from ptychosaxs.api import scanplan
from ptychosaxs.hexapod_traj import TrajectoryUnavailable

# tools.detectors / tools.dg645 / tools.softglue each import `epics` at module
# level just to expose these three plain Exception subclasses. `epics` drags
# in `pkg_resources`, `pyvisa`, and `pandas` -- multiple seconds of import
# time -- so skip the real modules in debug-devices mode (mirrors the same
# DEBUG_DEVICES gating rungui.py already applies to these imports) and use
# equivalent local stand-ins instead.
_DEBUG_MODE = "--debug" in sys.argv or os.environ.get("PTYCHOSAXS_DEBUG") == "1"
_DEBUG_LEVEL = 0
if _DEBUG_MODE:
    _idx = sys.argv.index("--debug") if "--debug" in sys.argv else -1
    if _idx >= 0 and _idx + 1 < len(sys.argv) and sys.argv[_idx + 1].isdigit():
        _DEBUG_LEVEL = int(sys.argv[_idx + 1])
    _DEBUG_LEVEL = int(os.environ.get("PTYCHOSAXS_DEBUG_LEVEL", _DEBUG_LEVEL))
_DEBUG_DEVICES = _DEBUG_MODE and _DEBUG_LEVEL in (0, 1)

# tools/dg645.py also imports InstrumentKit (`instruments`), which the other
# device modules do not, so it has a flag of its own - rungui exports it
# before importing this module. That is what lets the GUI run with real
# detectors and softglue in an environment without InstrumentKit.
_DEBUG_DG645 = _DEBUG_DEVICES or os.environ.get("PTYCHOSAXS_DEBUG_DG645") == "1"

# Defined once in the api so this file and ptychosaxs/api/scan_exec.py
# catch the same class objects. Two separate definitions would each make
# their own class, and an `except` in one would quietly miss what the
# other raised.
from ptychosaxs.api.scan_errors import (  # noqa: E402,F401
    DET_MIN_READOUT_Error,
    DET_OVER_READOUT_SPEED_Error,
    DG645_Error,
    SOFTGLUE_Setup_Error,
)



# Constants mirrored from rungui.py
HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
FRACTION_EXPOSURE_PERIOD = 0.2
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = 1
STRUCK_CHANNELS = [2, 3, 4, 5]

# Record names and timing now live in ptychosaxs/api/scan_config.py, so a
# headless scan runner can read them without importing this Qt handler.
# Re-exported here because this file names them in many places.
from ptychosaxs.api.scan_config import (  # noqa: E402
    COUNT_DONE_TIMEOUT_MARGIN_S,
    COUNT_POLL_INTERVAL_S,
    DETECTOR_NOT_STARTED_ERROR,
    DETECTOR_READOUTTIME,
    SCALER_CNT_PV,
    SCALER_CONT_PV,
    SCALER_TP_PV,
    SCAN_SCALAR_PVS,
)


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


class ScanHandler(ScanExecutorsMixin):
    # Per-point overhead added to exposure time when estimating scan duration.
    # Fly scan overhead accounts for detector readout + SoftGlue latency.
    # Step scan overhead accounts for motor settle + readout.
    OVERHEAD_FLY = 0.033  # seconds — Pilatus 2M hard minimum (30 Hz)
    OVERHEAD_STEP = 0.5  # seconds
    # Extra settle time between phi slices in a 3-D fly scan, on top of whatever
    # get_detectors_ready()/_push_filepaths_to_detectors() already take. Detectors
    # occasionally need longer than that to be ready for the next slice's trigger.
    OVERHEAD_FLY3D_PHI = 4.0  # seconds
    # Show a confirmation dialog before starting scans larger than this.
    LARGE_SCAN_THRESHOLD = guard_checks.LARGE_SCAN_THRESHOLD
    # 1-indexed motor numbers whose positions are saved/restored by the
    # Save Current / Go To Saved buttons.  Adjust this list as needed.
    SAVED_POSITION_MOTORS = [1, 2, 3, 7, 8, 9]
    # Ceiling on one coordinated two-axis scan move, matching the per-move
    # timeout pts.mv applies on the sequential path.
    COORDINATED_MOVE_TIMEOUT_S = 10
    # Maximum allowed fly velocity per axis (same units as get_speed/set_speed
    # use for that axis: mm/s for hexapod axes, deg/s for phi). Enforced by
    # _check_velocity_limits before a helix fly scan starts moving anything —
    # exceeding the hardware's own limit otherwise makes the motion controller
    # (the hexapod) reject the move command outright mid-scan.
    # Edit these values directly to change the limit.
    # The numbers themselves are in ptychosaxs/api/guards.py. They stop the
    # hexapod being commanded past what it will accept, and having them as
    # attributes of a Qt handler meant they could only be read by importing
    # a GUI. Aliased here because this file names them in several places.
    MAX_VELOCITY = guard_checks.MAX_VELOCITY
    MIN_VELOCITY = guard_checks.MIN_VELOCITY

    def __init__(self, window) -> None:
        self.w = window
        self.ui = window.ui
        # What the moved executors read instead of self.w. Session first,
        # window second, so a name can migrate to BeamlineSession without
        # any executor being edited again.
        self.ctx = ScanContext(window=window)
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
        """See ptychosaxs.api.scanplan.format_duration."""
        return scanplan.format_duration(t)

    def _advance_scan_number_safe(self) -> int:
        """Increment scan_number and persist it (PV put + ini write) without
        touching any Qt widgets. Safe to call from a worker thread.

        Unlike run_stop_issued()/update_scannumber(), this never reads or writes
        a QLineEdit/QLabel; callers must refresh those on the GUI thread separately
        (e.g. via the scanFolderAdvanced signal / on_scan_folder_advanced slot).

        Returns the new scan_number.
        """
        self.w.parameters.scan_number = self.w.parameters.scan_number + 1
        # Each phi slice is the start of its own sub-scan, so the IOC is
        # pointed at it before any frames are taken.
        self.w.write_scan_number_to_ioc()
        self.w.parameters.writeini()
        return self.w.parameters.scan_number

    def _advance_scan_number_thread_safe(self) -> None:
        """Advance scan_number at the end of fly2d0 / fly2d0_SNAKE.

        These run on the worker thread both as standalone 2-D fly scans and,
        synchronously, once per phi slice inside fly3d0's loop. run_stop_issued()
        touches ui.edit_scannumber/lbl_scanname directly, which is only safe to
        call from the GUI thread. For the standalone case that's how it's always
        worked; but for the fly3d0 case, doing this once per slice from the worker
        thread caused the scan-name label to stop updating reliably and, worse, let
        a later update_scanname() call (which reads scan_number back out of the
        QLineEdit) clobber the in-memory scan_number with a stale value -- so a
        later slice's data would land in an already-used scan-number folder.
        When fly3d0 is running (self._notify_folder_advanced_3d set), advance the
        same worker-thread-safe way stepscan3d0 does instead.
        """
        notify = getattr(self, '_notify_folder_advanced_3d', None)
        if notify is not None:
            new_scan_number = self._advance_scan_number_safe()
            notify(new_scan_number)
        else:
            self.w.run_stop_issued()

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

    def _write_scan_summary_line(self, scan_label: str, axes_params: list, n_pos: int) -> None:
        csv_path = self._get_scan_summary_csv_path()
        if csv_path is None:
            return
        if not csv_path.parent.exists():
            QMessageBox.warning(
                self.ui,
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
        gonio_trans_values = getattr(self, '_cached_gonio_trans', None)
        if gonio_trans_values is None:
            gonio_trans_values = self._get_gonio_trans_positions()
        optics_identity = getattr(self, "_cached_optics_identity", None) or {}
        # Order must match _get_scan_summary_header exactly. These first four
        # used to be written as date, time, "no", scan_id under a header
        # reading scan, completed, date, time -- so every one of them was
        # filed under the wrong name, and _update_scan_summary_completed
        # could never find its row (it matches on the "scan" column, which
        # held a date), leaving every scan marked incomplete.
        row = [
            scan_id,
            "no",
            timestamp.strftime("%Y-%m-%d"),
            timestamp.strftime("%H:%M:%S"),
            self.w.parameters.scan_name,
            scan_label,
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
            *gonio_trans_values,
            optics_identity.get("optics_name", ""),
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
                self.ui,
                "CSV Logging",
                f"Could not write scan summary CSV {csv_path}: {exc}",
            )

    def _ask(self, guard) -> bool:
        """Put one Guard to the operator. True if the scan may start.

        The single place this file turns a decision into a dialog. The
        decision itself is made by a pure function in
        ptychosaxs/api/guards.py, so the same check can be run by a macro,
        the CLI or a test, none of which have a screen.
        """
        if guard is None:
            return True
        dlg = QMessageBox(self.w.ui)
        dlg.setWindowTitle(guard.title)
        dlg.setText(guard.text)
        dlg.setIcon(QMessageBox.Question if guard.kind == guard_checks.CONFIRM
                    else QMessageBox.Warning)
        buttons = {dlg.addButton(choice, QMessageBox.AcceptRole): choice
                   for choice in guard.choices}
        dlg.exec_()
        return guard.allows(buttons.get(dlg.clickedButton(), guard.default))

    def _confirm_large_scan(self, Ntot, tm, overhead):
        """Return True to proceed, False to abort."""
        return self._ask(guard_checks.large_scan(
            Ntot, tm, overhead,
            threshold=self.LARGE_SCAN_THRESHOLD,
            # A macro drives scans with nobody there to click Proceed.
            unattended=bool(getattr(self.w, "macro_running", False))))

    # ------------------------------------------------------------------
    # Shared helpers used by all scan entry points and executors
    # ------------------------------------------------------------------

    def _check_hdf_for_multi_pulse(self) -> bool:
        """False when multi-pulse is on without the HDF5 plugin to aggregate
        the frames. See ptychosaxs.api.guards.multi_pulse_needs_hdf."""
        return self._ask(guard_checks.multi_pulse_needs_hdf(
            self.w.parameters._pulses_per_step, self.w.use_hdf_plugin))

    def _check_saxs_det_mode(self) -> bool:
        """False unless the SAXS detector is in scan mode."""
        return self._ask(guard_checks.saxs_detector_mode(
            self.get_saxs_det_mode()))

    def _confirm_scan_name(self) -> bool:
        """Prompt user to confirm/edit the scan name before a scan starts.

        Pushes the confirmed text through the same widget+update_scanname() path
        used everywhere else scan_name is set programmatically (see update_scanname),
        so the edit isn't lost when _pre_scan() calls update_scanname() again.
        Returns False if user cancels, True if confirmed.
        """
        if getattr(self.w, "macro_running", False):
            # A macro is driving scans unattended — accept the current name as-is
            # rather than blocking on a dialog nobody is there to click.
            return True

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
                self.ui.edit_scanname.setText(scan_name)
                self.w.update_scanname()
                return True
            else:
                QMessageBox.warning(self.w.ui, "Empty Name", "Scan name cannot be empty.")
                return False
        return False

    def _pre_scan_guards(self, velocities: dict = None, confirm_name: bool = True,
                         det_mode: bool = True) -> bool:
        """Run all pre-scan guards and return False if any fail.
        Call this at the top of every scan entry point before any other logic.
        Add new guards here so they apply to all scan types.

        velocities, if given, maps axis name -> planned fly velocity for that
        axis (same units as MAX_VELOCITY) and is checked by
        _check_velocity_limits. Only helix_fly passes this today.

        confirm_name and det_mode let the quick-acquisition entry points
        (takeshot, time_series) opt out of the two guards that would get in
        their way, while still inheriting every other guard -- including any
        added later. They used to skip this method entirely, which is why
        they silently missed new guards.

        Order matters: the cheap, certain checks run first, so a scan that
        was going to be rejected on its name never pays for the optics
        check's EPICS round-trips.
        """
        if confirm_name and not self._confirm_scan_name():
            return False
        if not self._check_hdf_for_multi_pulse():
            return False
        if det_mode and not self._check_saxs_det_mode():
            return False
        if velocities is not None and not self._check_velocity_limits(velocities):
            return False
        if not self._check_optics_configuration():
            return False
        return True

    # How many times the optics warning may re-prompt after the operator
    # asks to see the diagram, before it gives up and cancels the scan.
    OPTICS_PROMPT_MAX_ROUNDS = 20

    def _check_optics_configuration(self) -> bool:
        """Warn when the upstream optics are not set up for ptychography.

        Advisory only -- the operator can always proceed, and any failure of
        the check itself passes silently. A guard that can block scanning
        because an .ini moved would be worse than no guard at all.

        Turned off by the "Check upstream optics before scans" checkbox on
        the setup dialog, for non-ptycho measurements.

        Once the operator proceeds past a given set of problems, the same set
        stops asking: operators run many scans in a row, and a dialog on
        every one of them would just get the checkbox switched off for good.
        A *different* problem re-warns, and clearing the fault resets it.
        """
        if not getattr(self.w.parameters, "_check_optics_prescan", True):
            return True

        try:
            from gui import optics_status

            status = self.w.optics_status_report()
            ok, problems = optics_status.check_good_for_ptycho(status)
        except Exception as exc:
            print(f"Warning: upstream optics check failed, continuing: {exc}")
            return True

        acked = getattr(self, "_optics_warning_acked", None)
        if acked is None:
            acked = self._optics_warning_acked = set()
        if ok:
            acked.clear()
            return True

        key = tuple(sorted(problems))
        if key in acked:
            return True

        dlg = QMessageBox(self.w.ui)
        dlg.setWindowTitle("Upstream Optics")
        dlg.setIcon(QMessageBox.Warning)
        dlg.setText("The upstream optics are not set up for ptychography:\n\n- "
                    + "\n- ".join(problems))
        dlg.setInformativeText("Scan anyway?")
        proceed = dlg.addButton("Scan anyway", QMessageBox.AcceptRole)
        show = dlg.addButton("Show diagram", QMessageBox.ActionRole)
        cancel = dlg.addButton("Cancel scan", QMessageBox.RejectRole)
        dlg.setDefaultButton(cancel)

        # "Show diagram" returns to this question afterwards, so the prompt
        # loops. Bounded rather than `while True`: if the diagram cannot be
        # opened at all, an unbounded loop would re-prompt forever with no
        # way for the operator out of it.
        for _ in range(self.OPTICS_PROMPT_MAX_ROUNDS):
            dlg.exec_()
            clicked = dlg.clickedButton()
            if clicked is proceed:
                acked.add(key)
                return True
            if clicked is not show:
                return False
            # Reuse the same report window the Check Upstream Optics button
            # opens, modally, then come back to this question.
            try:
                from gui.optics_status_dialog import OpticsStatusDialog

                report = OpticsStatusDialog(
                    self.w.ui,
                    caget=self.w._optics_caget(),
                    eye_state=lambda: self.w.xray_eye.is_in(),
                    problems=problems,
                )
                report.exec_()
            except Exception as exc:
                print(f"Warning: could not show optics diagram: {exc}")
                return False    # cannot help further; treat as cancel
        return False

    def _scan_motors(self, xmotor=None, ymotor=None, phimotor=None):
        """Fill in whichever scan-axis indices the caller left unset.

        The defaults live in one place - rungui's DEFAULTS, which the axis
        selectors write to - so a signature default here could only
        contradict them, and used to.
        """
        defaults = self.w.DEFAULTS
        return (
            defaults["xmotor"] if xmotor is None else xmotor,
            defaults["ymotor"] if ymotor is None else ymotor,
            defaults["phimotor"] if phimotor is None else phimotor,
        )

    def _fly_guards(self, *axis_names) -> bool:
        """Return False (and explain) if a fly scan cannot run right now.

        Two separate reasons it might not. The trajectory is programmed into
        the hexapod's wave generator, so every flown axis has to be a hexapod
        axis - no other stage here has one. And the wave generator is reached
        over GCS, which needs the pihexapod library and a live connection to
        the C-887; the EPICS motor records that drive step scans expose no
        trajectory interface and cannot substitute.

        Checking here means the failure arrives before the detector is armed,
        with a reason, rather than as an exception part-way through a scan.
        """
        hexapod = self.w.pts.hexapod
        foreign = [name for name in axis_names if name not in hexapod.axes]
        if foreign:
            QMessageBox.warning(
                self.w.ui,
                "Fly scan not available",
                "Fly scans are driven by the hexapod's wave generator, so "
                "every flown axis must be a hexapod axis (%s).\n\nNot a "
                "hexapod axis: %s." % (", ".join(hexapod.axes), ", ".join(foreign)),
            )
            return False
        if not hexapod.can_fly:
            QMessageBox.warning(
                self.w.ui,
                "Fly scan not available",
                "The hexapod's trajectory transport is unavailable (%s).\n\n%s"
                "\n\nStep scans are unaffected."
                % (hexapod.traj_state, hexapod.traj_reason),
            )
            return False
        return True

    def _check_velocity_limits(self, velocities: dict) -> bool:
        """False if any axis would fly outside its configured limits.

        Checked before anything moves: the controller rejecting a move
        command mid-scan leaves a half-collected dataset and a stage
        somewhere nobody chose. See ptychosaxs.api.guards.velocity_limits.
        """
        return self._ask(guard_checks.velocity_limits(
            velocities, self.MAX_VELOCITY, self.MIN_VELOCITY))

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



    def _pre_scan(self, scan_label: str, scan_kind: str = "step", xmotor: int = 0, ymotor: int = 2) -> None:
        """Common setup called at the start of every scan entry point (GUI thread).

        Resets detector file/frame counters, refreshes scan name and file paths,
        logs current motor positions, and clears the stop flag so a previous scan's
        stop signal does not immediately abort the new one.

        scan_kind: one of "step", "fly_snake", "fly_hexapod_1d", "fly_phi", "helix".
        Used to determine how positions are computed for the NeXus master file.

        xmotor, ymotor: 0-based indices of the motors actually driving this scan's
        XY grid (e.g. trans1/trans2 for a piezo scan). Defaults (0, 2) match the
        hexapod X/Z pair used by the "normal" 2-D scans, for callers with no XY
        grid of their own (1-D scans, takeshot, timeseries).
        """
        self.w.update_scanname()
        self.w.get_detectors_ready()
        self.w.prepare_scan_files()
        self.w.write_motor_scan_range()
        self.isStopScanIssued = False
        print(f"\n\n{scan_label} starting")

        # Store the scan number at scan start time so background linking thread uses the
        # same number even if user changes scan_number before linking completes
        self._scan_number_at_start = self.w.parameters.scan_number

        # Query optics PVs once and cache for both CSV and master file (avoid duplicate queries)
        self._cached_us_optics = self._get_us_optics_positions()
        self._cached_zone_plate_optics = self._fetch_zone_plate_optics_metadata()
        # Read once here, on the GUI thread: 3-D scans rewrite a master file
        # per slice from a worker thread, and the installed optic cannot
        # change mid-scan anyway.
        self._cached_optics_identity = self._fetch_optics_identity()
        self._cached_gonio_trans = self._get_gonio_trans_positions()

        # Create NeXus master files before scan starts
        try:
            sample_name = self.w.parameters.scan_name
            scan_positions_dict = self._compute_scan_positions(scan_kind=scan_kind, xmotor=xmotor, ymotor=ymotor)
            # Cache the grid so 3-D fly/step scans can rewrite a per-slice master
            # file from the worker thread without re-reading Qt widgets (the X/Y
            # grid is identical for every phi slice; only the EPICS metadata,
            # incl. theta, needs to be re-fetched per slice).
            self._cached_scan_positions_dict = scan_positions_dict

            # Create master file for each active detector
            detectors_active = []
            if len(self.w.detector) > 0 and self.w.detector[0] is not None:
                detectors_active.append('SAXS')
            if len(self.w.detector) > 1 and self.w.detector[1] is not None:
                detectors_active.append('WAXS')

            master_paths = {}
            for detector_type in detectors_active:
                path = self._write_master_file_metadata(detector_type, scan_positions_dict, sample_name)
                if path:
                    master_paths[detector_type] = path
            # Lets step-scan executors (e.g. stepscan2d0's piezo-position readback)
            # find the just-created master file(s) without recomputing the path.
            # 3-D fly/step scans overwrite this per-slice via _write_slice_master_file.
            self._current_scan_master_paths = master_paths
        except Exception as e:
            print(f"Warning: Failed to create master file: {e}")

    def _log_scan_header(self, scan_label: str, axes_params: list, scan_kind: str = "step") -> None:
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
        scaninfo = ["\n#S", self.w.parameters.scan_number, scan_label]
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
        self._write_scan_summary_line(scan_label, axes_params, n_pos)






    # Derived from optics_status, which the optics panel and the Check
    # Upstream Optics report also read, so the PV strings cannot drift
    # between the panel, the CSV log and the master file. The names here are
    # also the CSV column names -- see _get_scan_summary_header.
    _US_OPTICS_PVS = [
        (_motor, _optics_status.PV_BASES[_motor] + ".RBV")
        for _motor in _optics_status.US_OPTICS_MOTORS
    ]


    _GONIO_TRANS_AXES = ["trans1", "trans2"]


    def check_fly_blur(self):
        """Estimate the sample blur during a fly scan exposure and show it in a dialog.

        Reports three independent estimates when their fields are populated:
        the generic 1-D translation fly (ed_lup_1_*), the 1-D phi angular fly
        (ed_lup_7_*, motor index 6), and the helix scan's Z blur (ed_lup_3_*,
        motor index 2 — "vertical stage is Z", see DEFAULTS in rungui.py),
        computed against the same phi timing that drives the helix trigger.
        """
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
                f"1D Fly (translation):\n"
                f"Exposure time:     {expt * 1000.:.2f} ms\n"
                f"Step size:         {step * 1000.:.3f} µm\n"
                f"Acquisition time:  {step_time * 1000.:.2f} ms\n"
                f"Velocity:          {velocity_mm_s * 1000.:.3f} µm/s\n"
                f"Motion during exposure:  {movestep_um * 1000:.0f} nm"
            )

            # 1D fly phi (angular) blur. Exposure falls back to ed_lup_1_t,
            # matching _read_motor_params's per-axis-widget-or-shared behaviour.
            phi_expt = _read("ed_lup_7_t")
            if phi_expt is None or phi_expt <= 0:
                phi_expt = expt
            phi_step = _read("ed_lup_7_N")
            if phi_step is not None and phi_step > 0:
                phi_step = abs(phi_step)
                phi_step_time = max(fly_acq_time, phi_expt + DETECTOR_READOUTTIME, self.OVERHEAD_FLY)
                phi_step_time = round(phi_step_time * 1000) / 1000
                phi_velocity_deg_s = phi_step / phi_step_time
                phi_move_mdeg = phi_step * 1e3 * phi_expt / phi_step_time
                phi_move_mrad = (1000 * np.pi / 180) * phi_step * phi_expt / phi_step_time
                msg += (
                    f"\n\n1D Fly phi (angular):\n"
                    f"Exposure time:     {phi_expt * 1000.:.2f} ms\n"
                    f"Step size:         {phi_step * 1000.:.3f} mdeg\n"
                    f"Acquisition time:  {phi_step_time * 1000.:.2f} ms\n"
                    f"Velocity:          {phi_velocity_deg_s:.4f} deg/s\n"
                    f"Motion during exposure:  {phi_move_mdeg:.3f} mdeg\n"
                    f"                      :  {phi_move_mrad:.3f} mrad"
                )

                # Helix Z blur: phi and Z fly simultaneously over the same
                # total_time, triggered at phi's step timing (see
                # _compute_helix_total_time / helix_fly). p0 cancels out of
                # that calculation, so the raw L/R offsets stand in for it.
                phi_L = _read("ed_lup_7_L")
                phi_R = _read("ed_lup_7_R")
                z_L = _read("ed_lup_3_L")
                z_R = _read("ed_lup_3_R")
                if None not in (phi_L, phi_R, z_L, z_R):
                    total_time, _, _ = self._compute_helix_total_time(phi_L, phi_R, phi_step, phi_expt)
                    if total_time > 0:
                        z_velocity_mm_s = abs(z_R - z_L) / total_time
                        z_move_um = z_velocity_mm_s * phi_expt * 1000.0
                        msg += (
                            f"\n\nHelix Z blur:\n"
                            f"Total scan time:   {total_time:.2f} s\n"
                            f"Z velocity:        {z_velocity_mm_s * 1000.:.3f} µm/s\n"
                            f"Motion during exposure:  {z_move_um * 1000:.0f} nm"
                        )

            QMessageBox.information(self.w.ui, "Fly Blur Estimate", msg)
        except Exception as e:
            QMessageBox.warning(self.w.ui, "Fly Blur Error", f"Error calculating fly blur: {e}")


    def _adjust_axis_length(self, n: int, axis_index: int, scan_kind: str) -> int:
        """See ptychosaxs.api.scanplan.adjust_axis_length."""
        return scanplan.adjust_axis_length(n, axis_index, scan_kind)










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
        w.signal.scanFolderAdvanced.connect(self.w.on_scan_folder_advanced)
        w.kwargs["update_progress"] = w.signal.progress.emit
        w.kwargs["update_status"] = w.signal.statusmessage.emit
        # Only 3-D fly/step executors (which switch to a new scan folder per phi
        # slice) declare this parameter; skip it for everything else so existing
        # single-folder executors (fly0, stepscan0, fly2d0, ...) are unaffected.
        if 'notify_folder_advanced' in inspect.signature(executor_fn).parameters:
            w.kwargs["notify_folder_advanced"] = w.signal.scanFolderAdvanced.emit
        self.w.set_scan_status("Scanning")
        self.w.isscan = True
        if self.w.monitor_beamline_status:
            self.w.shutter.open()
        self.w.threadpool.start(w)

            # if update_status:
            #     update_status(msg)



    def _motor_from_sender(self) -> int:
        """Extract a 0-based motor index from the name of the button that triggered
        a scan (e.g. 'pushButton_fly_3' → motor index 2).
        """
        pb = self.w.sender()
        objname = pb.objectName()
        n = int(re.findall(r"\d+", objname)[0])
        return n - 1  # 1-based UI index → 0-based motor index


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

    def set_step_acq_time(self):
        if hasattr(self.w.parameters, "_step_acq_time"):
            wtime = self.w.parameters._step_acq_time
        else:
            wtime = 1.0
        value, okPressed = QInputDialog.getDouble(
            self.w.ui, "Set step acquisition time", "Acquisition time (s):", wtime
        )
        if okPressed:
            self.w.parameters._step_acq_time = value
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

    # ------------------------------------------------------------------
    # Output paths
    # ------------------------------------------------------------------
    #
    # One scan folder, two names for it, and which name you need depends on
    # who is doing the opening:
    #
    #   the GUI path    what this process passes to os.makedirs and h5py, to
    #                   create the scan folder and write the NeXus master file.
    #   the EPICS path  what goes into a detector's FilePath PV. The IOCs are
    #                   always on Linux, so this is always a Linux path.
    #
    # Running on Linux the two are identical: the Working folder is already a
    # Linux path, so it is both what the GUI opens and what the IOCs are told.
    # Running on Windows the Working folder is a Windows path (Z:\12IDC\...),
    # and the EPICS path is built by swapping its drive letter for the Linux
    # basepath -- the same folder reached over the mount from the other side.
    #
    # Everything goes through these three functions. They were previously
    # open-coded in four places, which is how the master file came to have its
    # own variant that stripped the leading "/" off an absolute Linux path and
    # joined the rest with backslashes.






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

        # Captured once here so the worker-thread path pushes between 3-D
        # slices, and the master-file path builders, never touch a Qt widget.
        self._scan_working_folder = self._working_folder()

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

            self.w.make_positions_folder(
                self._gui_scan_folder(folder_type, self.scannumberstring)
            )

            hdf_path = self._epics_scan_folder(
                basepath, folder_type, self.scannumberstring
            )
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

            hdf_path = self._epics_scan_folder(
                basepath, folder_type, self.scannumberstring
            )
            det.filePut("FilePath", hdf_path)

    def _iter_detector_gui_paths(self):
        """Yield the GUI-side folder for each active detector at the current scan number.

        GUI-side because the caller (check_scan_folder) stats these folders
        from this process, not through the IOC.
        """
        if not hasattr(self.w, "detector"):
            return
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
            yield self._gui_scan_folder(folder_type, scannumberstring)

    def check_scan_folder(self):
        """Check active detector folders for the current scan number.

        Returns:
            None  — no detectors are selected
            True  — all folders are empty or absent (safe to scan)
            False — at least one folder already contains a file
        """
        found_any = False
        for folder in self._iter_detector_gui_paths():
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
            "Set fly acquisition time",
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

    def _wait_for_primary_detectors_written(self):
        """Wait for SAXS/WAXS to finish writing their own file before touching SG.

        SoftGlue is the hardware trigger source for snake fly scans (see
        switch_SGstream), so force-stopping/flushing it while a primary
        detector's last frame is still being written can cut off an
        in-flight trigger and drop that frame. scandone() (step scans)
        never races here because it waits on each detector's WriteFile_RBV
        in index order, reaching SG last; fly-scan completion has no such
        ordering, so it must wait explicitly.
        """
        if not self.w.use_hdf_plugin:
            return
        for det in self.w.detector[:2]:
            if det is not None:
                while det.fileGet("WriteFile_RBV"):
                    time.sleep(0.01)

    def _stop_sg_detector(self):
        """Force-stop the SoftGlue streaming detector (12idSGSocket IOC) if active.

        Only scandone() used to do this; fly-scan completion handlers (flydone,
        flydone2d, flydone3d, helixdone) skipped it, leaving the IOC's Acquire
        and HDF capture running after fly scans that use the SG stream (snake
        fly2d/fly3d, or any scan with SG manually enabled via the SG action).
        """
        for det in self.w.detector:
            if det is not None and "SG" in det._prefix:
                self.w.s12softglue.flush()
                time.sleep(1)
                det.ForceStop()

    def scandone(self, update_scannumber=True, donedone=True, update_gui=True,
                 direct_gui_updates=True, notify_folder_advanced=None, link_scan_number=None,
                 link_detector_data=True):
        # Return to the initial positions. Per-slice (donedone=False) calls only
        # restore x/y, since 3-D scans re-drive x/y at the start of every slice
        # anyway and restoring phi mid-scan would just be an extra move back and
        # forth. The true end of the scan (donedone=True) restores every motor
        # in motor_p0, including phi for 3-D step scans.
        for i, key in enumerate(self.w.motor_p0):
            if i < 2 or donedone:
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
                        self._stop_sg_detector()
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

            # save Struck as a separate csv file.
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
                    # read_mcs returns one waveform per channel; transpose so the
                    # csv has one row per scan position and one column per channel.
                    counts = np.column_stack(
                        [np.atleast_1d(ch) for ch in self.w.rpos]
                    )
                    np.savetxt(
                        os.path.join(foldername, filename + ".csv"),
                        counts,
                        delimiter=",",
                        fmt="%g",
                        header=",".join(f"ch{n}" for n in STRUCK_CHANNELS),
                        comments="",
                    )

                    # Per-position scaler counts collected by stepscan0. Empty
                    # for scan types that don't collect them.
                    scalar_rows = getattr(self.w, "scan_scalars", [])
                    if len(scalar_rows) > 0:
                        np.savetxt(
                            os.path.join(foldername, filename + "_scalers.csv"),
                            np.asarray(scalar_rows, dtype=float),
                            delimiter=",",
                            fmt="%g",
                            header=",".join(SCAN_SCALAR_PVS.keys()),
                            comments="",
                        )
                    self.w.scan_scalars = []

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

        # Link detector data to master files asynchronously (non-blocking).
        # Skipped when link_detector_data=False (3-D fly/step scans: every slice
        # already linked its own master file in-loop via _link_slice_detector_data,
        # so this single-folder catch-all would only ever hit an already-linked
        # slice -- either the frozen scan-start folder for step scans, or the
        # final slice for fly scans -- and re-attempt the same links.)
        if link_detector_data:
            # Compute full paths NOW while GUI state is frozen, pass to background thread
            sample_name = self.w.parameters.scan_name
            # Use the scan number from when _pre_scan was called, not the current one
            # (user may have changed it since scan started) -- unless the caller
            # explicitly names which slice's folder just finished.
            if link_scan_number is not None:
                scan_number = link_scan_number
            else:
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
        if direct_gui_updates:
            # Default path (all single-folder scans: 1-D/2-D fly, 1-D/2-D step,
            # helix). Always called from the GUI thread (either directly, or via
            # a Qt-queued done_signal), so touching ui.edit_scannumber/lbl_scanname
            # here is safe.
            if update_scannumber:
                self.w.run_stop_issued()
            self.w.update_scanname()
        else:
            # Worker-thread-safe path (3-D fly/step in-loop per-slice calls).
            # Advance scan_number without any direct Qt widget access, and defer
            # the label refresh to a queued signal so it runs on the GUI thread.
            if update_scannumber:
                new_scan_number = self._advance_scan_number_safe()
            else:
                new_scan_number = self.w.parameters.scan_number
            if notify_folder_advanced is not None:
                notify_folder_advanced(new_scan_number)

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

    def _fetch_optics_identity(self) -> dict:
        """Which optic is installed, for the NeXus master file and the log.

        Derived from the name of the saved ZP position the zone plate is
        currently sitting at (optics_motors.ini's [zp] registry), not from a
        PV -- the beamline has no record of which optic is mounted, only of
        where the operator put it.

        Always returns all three keys so the writer never has to special-case
        a missing one: an unmatched ZP gives ("unknown_optic", "", 1).
        Never raises -- a scan must not fail over a metadata lookup.
        """
        from gui import optics_status

        try:
            status = self.w.optics_status_report()
            optics_type, optics_name, optics_ozw = optics_status.classify_optic(
                status.zp_name)
        except Exception as e:
            print(f"Warning: could not determine the installed optic: {e}")
            optics_type = optics_status.UNKNOWN_OPTIC
            optics_name = ""
            optics_ozw = optics_status.UNKNOWN_OZW
        return {"optics_type": optics_type, "optics_name": optics_name,
                "optics_ozw": optics_ozw}



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

        def _p0(n):
            """Start position for motor *n*, without prompting.

            Deliberately not check_start_position. This runs from _pre_scan,
            after _read_motor_params has already resolved p0 for these axes
            and written it into ed_N, so the widget holds exactly the value
            that call returned. Asking again raised the "Move to / Update"
            dialog a second time for the same motor in a single scan launch
            -- and the "Move to" branch leaves ed_N differing from lb_N until
            the motor physically moves, so it re-prompted every time.
            Building master-file metadata must not block on a modal dialog
            anyway.

            ed_N can still be empty, because entry points with no XY grid of
            their own (takeshot, timeseries) reach here without having read
            motors 0/2. Fall back to the current position in lb_N, which is
            what check_start_position returned in that case -- falling back
            to 0.0 would silently write a position grid centred on the origin.
            """
            value = _val(f"ed_{n}", default=None)
            if value is not None:
                return value
            label = self.ui.findChild(QLabel, f"lb_{n}")
            if label is None:
                return 0.0
            try:
                return float(label.text())
            except (TypeError, ValueError):
                return 0.0

        x_n = xmotor + 1
        x_p0 = _p0(x_n)
        x_L = _val(f"ed_lup_{x_n}_L")
        x_R = _val(f"ed_lup_{x_n}_R")
        x_step = _val(f"ed_lup_{x_n}_N", 1.0)

        y_n = ymotor + 1
        y_p0 = _p0(y_n)
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

    def _compute_scan_positions(self, scan_kind: str = "step", xmotor: int = 0, ymotor: int = 2) -> dict:
        """Compute 2D scan position array for master file.

        For snake fly scans, applies hexapod adjustments: one phantom point per
        X line and even-line rounding on Y, mirroring what the hardware will collect.

        Args:
            scan_kind: one of "step", "fly_snake", "fly_hexapod_1d", "fly_phi", "helix".
            xmotor: 0-based X motor index actually used by this scan (default 0 —
                hexapod X — for entry points with no XY grid of their own).
            ymotor: 0-based Y motor index actually used by this scan (default 2 —
                hexapod Z).

        Returns:
            Dictionary with single key 'positions': Nx2 numpy array of (x, y)
            positions in scan traversal order. Empty dict if not a 2D scan.
        """
        pos = self._compute_2d_scan_positions(xmotor=xmotor, ymotor=ymotor, scan_kind=scan_kind)
        if len(pos) > 0:
            pos += -np.mean(pos, axis=0)  # Center positions around (0, 0)
            return {'positions': pos}
        return {}











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

                    # Create external link. Idempotent: 3-D fly/step scans link each
                    # slice's master file once in-loop and may relink it again from
                    # the final scandone()/flydone3d() catch-all, so an existing link
                    # of the same name must be replaced rather than raising.
                    if link_name in entry_data:
                        del entry_data[link_name]
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
                    from ptychosaxs.debug.debug_stubs import PilatusStub

                    self.w.detector[0] = PilatusStub()
                elif not self.detector_is_online(basename, "SAXS Pilatus",
                                                 self.ui.actionSAXS):
                    self.w.detector[0] = None
                else:
                    from ptychosaxs.tools.detectors import pilatus

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
                    from ptychosaxs.debug.debug_stubs import PilatusStub

                    self.w.detector[1] = PilatusStub()
                elif not self.detector_is_online(basename, "WAXS Pilatus",
                                                 self.ui.actionWAXS):
                    self.w.detector[1] = None
                else:
                    from ptychosaxs.tools.detectors import pilatus

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
                    from ptychosaxs.debug.debug_stubs import StruckStub

                    self.w.detector[2] = StruckStub()
                else:
                    from ptychosaxs.tools.struck import struck

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
                    from ptychosaxs.debug.debug_stubs import PilatusStub

                    self.w.detector[4] = PilatusStub()
                else:
                    from ptychosaxs.tools.detectors import dante

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
                    from ptychosaxs.debug.debug_stubs import PilatusStub

                    self.w.detector[4] = PilatusStub()
                else:
                    from ptychosaxs.tools.detectors import XSP

                    self.w.detector[4] = XSP(basename)
            else:
                self.ui.actionXSP3.setChecked(False)
                self.w.detector[4] = None
        self.w.update_scanname()

    # Probe PVs used to decide whether a detector's IOC is actually there.
    # One per detector family, since their record layouts differ.
    DETECTOR_PROBE_PV = {
        "S12-PILATUS1:": "cam1:Acquire_RBV",
        "12idcPIL:": "cam1:Acquire_RBV",
        "12idSGSocket:": "SG1:Acquire_RBV",
    }
    DETECTOR_PROBE_TIMEOUT_S = 1.0

    def detector_is_online(self, prefix, label, action=None):
        """Whether the detector's IOC answers, before we add it to the list.

        A detector whose IOC is down does not fail loudly - every PV read
        blocks for the full channel-access timeout instead, and returns
        None. One unreachable detector was adding ~50 s to the arming phase
        of every fly scan, invisibly. Probe once, cheaply, and leave the
        detector out if nothing answers; unchecking the menu entry keeps the
        setup window honest about what is actually connected.
        """
        from epics import PV

        probe = self.DETECTOR_PROBE_PV.get(prefix)
        if probe is None:
            return True  # no probe defined: do not block the detector
        pvname = prefix + probe
        if PV(pvname).wait_for_connection(timeout=self.DETECTOR_PROBE_TIMEOUT_S):
            return True
        msg = (f"{label} is not reachable ({pvname} did not connect in "
               f"{self.DETECTOR_PROBE_TIMEOUT_S:g}s). Leaving it disabled - "
               f"start its IOC and re-enable it in Setup.")
        print(msg)
        self.w.messages["recent error message"] = msg
        if action is not None:
            action.setChecked(False)
        return False

    def switch_SGstream(self, status=True):
        basename = "12idSGSocket:"
        if status:
            self.ui.actionSG.setChecked(True)
            if self.w.DEBUG_DEVICES:
                from ptychosaxs.debug.debug_stubs import SGStreamStub

                self.w.detector[3] = SGStreamStub()
            elif not self.detector_is_online(basename, "Softglue stream",
                                             self.ui.actionSG):
                self.w.detector[3] = None
            else:
                from ptychosaxs.tools.detectors import SGstream

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
        if self.w.pts.hexapod.can_fly:
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
        # Written by this process (np.savetxt), so the GUI-side path.
        foldername = self._gui_scan_folder(
            "positions", self.scannumberstring, foldername
        )
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


    def flydone(self, return_motor=True, reset_scannumber=True, donedone=True,
                direct_gui_updates=True):
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
        if direct_gui_updates:
            # Safe here because flydone is either invoked as a Qt done_signal
            # (auto-queued onto the GUI thread) or, when called inline from
            # fly2d0's per-line loop on the worker thread, with
            # direct_gui_updates=False so these widget touches are skipped.
            if self.isStopScanIssued:
                self.w.set_scan_status("Stopped")
                self.ui.statusbar.showMessage("Scan stopped by user \u2014 motors returned.")
            else:
                self.w.set_scan_status("No Scan")
                self.ui.statusbar.showMessage("Fly scan complete.")
        self.w.s12softglue.flush()
        print(f"softglue flushed at {time.ctime()}")
        if return_motor:
            # flydone doubles as the per-line completion signal inside fly2d0's
            # Y loop (called there with return_motor=False) -- only force-stop
            # the SG detector on a genuine top-level scan end, not per line.
            self._wait_for_primary_detectors_written()
            self._stop_sg_detector()

        if direct_gui_updates:
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
                else:
                    self.w.sethexapodvel_default()
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
        self._wait_for_primary_detectors_written()
        self._stop_sg_detector()

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
        self._wait_for_primary_detectors_written()
        self._stop_sg_detector()
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
        self._wait_for_primary_detectors_written()
        self._stop_sg_detector()
        self.w.update_scanname()
        self.w.update_status_scan_time()

        # No final data-linking here: every phi slice already linked its own
        # master file in-loop (fly3d0 -> _link_slice_detector_data), including
        # the last one, right after that slice's 2-D sweep finished.

    def check_start_position(self, n):
        """Resolve motor *n*'s start position, prompting if the UI disagrees.

        Call this once per motor per scan launch, from _read_motor_params and
        nowhere else. It is modal, and the "Move to" branch deliberately does
        not move the motor, so lb_N keeps differing from ed_N and any second
        call in the same launch prompts again. Code that needs p0 after
        _read_motor_params has run should read ed_N, which holds the resolved
        value (see _compute_2d_scan_positions).
        """
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
                if getattr(self.w, "macro_running", False):
                    # A macro is driving scans unattended -- automatically
                    # take the "Update the 'Move to' position" branch below
                    # (keep the current position, sync the 'Move to' field
                    # to match it) instead of blocking on this dialog.
                    p0_move_to = p0
                    self.ui.findChild(QLineEdit, "ed_%i" % n).setText(
                        "%0.6f" % float(p0_move_to)
                    )
                    return p0
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

    def fly2d(self, xmotor=None, ymotor=None, snake=False):
        """Entry point for a 2-D fly scan (GUI thread).

        snake=False: steps Y with pts.mv, then flies X with fly0 for each row.
        snake=True:  programs the entire XY boustrophedon path as a single
                     hexapod trajectory via fly_traj / set_traj_SNAKE2, then
                     launches fly2d0_SNAKE which waits for all frames to arrive.

        Why fly2d exists separately from fly2d0 / fly2d0_SNAKE: thread-safety —
        Qt widget reads must happen on the GUI thread.
        """
        xmotor, ymotor, _phi = self._scan_motors(xmotor, ymotor)
        if not self._pre_scan_guards():
            return
        if not self._fly_guards(self.w.motornames[xmotor], self.w.motornames[ymotor]):
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

        scan_label = "fly2d_SNAKE" if snake else "fly2d"
        self._pre_scan(scan_label, scan_kind=scan_kind, xmotor=xmotor, ymotor=ymotor)

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
        # Make sure a leftover 3-D-scan signal emitter can't be picked up by
        # this standalone scan's end-of-scan bookkeeping (see fly3d0 / _advance_scan_number_thread_safe).
        self._notify_folder_advanced_3d = None

        self.w.signalmotor = xax["name"]
        self.w.signalmotorunit = self.w.motorunits[xmotor]
        self.w.motor_p0 = {xmotor: xax["p0"], ymotor: yax["p0"]}
        self.time_scanstart = time.time()

        # Warm up the DG645 so the detector IOC can communicate before the
        # executor configures timing precisely.
        self.w.dg645_12ID.set_pilatus_fly(0.001)

        self._log_scan_header(scan_label, [xax, yax], scan_kind=scan_kind)

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
            self._launch_worker(
                self.fly2d0_SNAKE,
                xmotor,
                ymotor,
                done_signal=self.w.flydone2d,
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
            )

    def updateprogressbar(self, value):
        self.ui.pbar_scan.setValue(value)
        self.w.update_status_scan_time(value)

    def update_status_bar(self, message):
        self.ui.statusbar.showMessage(message)

    def fly3d(self, xmotor=None, ymotor=None, phimotor=None, slice_label_prefix="", snake=False):
        """Entry point for a 3-D fly scan (GUI thread).

        Steps phi in the outer loop; for each phi position fly3d0 calls
        fly2d0 (non-snake) or fly2d0_SNAKE to sweep the 2-D XY grid.

        Note: fly_traj is NOT called here — fly3d0 calls it inside the phi
        loop so the hexapod trajectory is re-programmed fresh for each slice
        (goto_start_pos must be called after each trajectory completes).

        Why fly3d exists separately from fly3d0: thread-safety — Qt widget
        reads must happen on the GUI thread.
        """
        xmotor, ymotor, phimotor = self._scan_motors(xmotor, ymotor, phimotor)
        if not self._pre_scan_guards():
            return
        if not self._fly_guards(self.w.motornames[xmotor], self.w.motornames[ymotor]):
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

        scan_label = "fly3d_SNAKE" if snake else "fly3d"
        self._pre_scan(scan_label, scan_kind=scan_kind, xmotor=xmotor, ymotor=ymotor)

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

        self._log_scan_header(scan_label, [xax, yax, phiax], scan_kind=scan_kind)

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
            slice_label_prefix=slice_label_prefix,
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
        if scan_kind == "fly_hexapod_1d" and not self._fly_guards(ax["name"]):
            return
        n_positions = self._compute_n_positions([pos], scan_kind=scan_kind)
        if not self._confirm_large_scan(n_positions, ax["expt"], self.OVERHEAD_FLY):
            return

        self._pre_scan("fly", scan_kind=scan_kind, xmotor=motornumber)

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

    def helix_fly_choose_axis(self, phimotor=6):
        """Prompt for which axis moves simultaneously with phi, then launch helix_fly."""
        candidates = [
            name for name in ("X", "Z")
            if name in self.w.motornames
            and self.w.motorconnected[self.w.motornames.index(name)]
        ]
        if not candidates:
            QMessageBox.warning(self.w.ui, "Error", "Neither X nor Z motor is connected.")
            return
        selected, ok = QInputDialog.getItem(
            self.w.ui,
            "Helix Fly Scan",
            "Choose which axis to move simultaneously with phi:",
            candidates,
            0,
            False,
        )
        if not ok:
            return
        self.helix_fly(self.w.motornames.index(selected), phimotor)

    def helix_fly(self, zmotor=2, phimotor=6):
        """Entry point for a helix fly scan: phi flies with simultaneous Z constant-velocity motion (GUI thread).

        Reads parameters from UI for both phi and Z axes, validates, logs scan header,
        then launches helix_fly0 on a Worker thread. Both axes move simultaneously at
        constant velocity over the same total_time, with images acquired at DG645-triggered
        intervals synchronized to phi's step timing.
        """
        # Read parameters before the guards so the velocity guard (below) can
        # check the velocities they imply. Also done BEFORE _pre_scan (which
        # creates the master file).
        try:
            phi_ax = self._read_motor_params(phimotor)
            z_ax = self._read_motor_params(zmotor)
        except (ValueError, TypeError):
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return

        phi_st = phi_ax["p0"] + phi_ax["st"]
        phi_fe = phi_ax["p0"] + phi_ax["fe"]
        z_st = z_ax["p0"] + z_ax["st"]
        z_fe = z_ax["p0"] + z_ax["fe"]
        total_time, _, _ = self._compute_helix_total_time(phi_st, phi_fe, phi_ax["step"], phi_ax["expt"])
        planned_velocities = {
            phi_ax["name"]: abs(phi_fe - phi_st) / total_time,
            z_ax["name"]: abs(z_fe - z_st) / total_time,
        }
        print(f"helix_fly planned velocities: {planned_velocities}")
        if not self._pre_scan_guards(velocities=planned_velocities):
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

        # Deliberately opts out of the scan-name prompt: a takeshot is a
        # quick "grab a frame" action and a name dialog would defeat it. It
        # still runs every other guard, so a new one is not silently missed.
        if not self._pre_scan_guards(confirm_name=False):
            return

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

        # Same opt-out as takeshot: the parameters were just confirmed in the
        # dialog above, so only the scan-name prompt is skipped.
        if not self._pre_scan_guards(confirm_name=False):
            return

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

    def stepscan2d(self, xmotor=None, ymotor=None):
        """Entry point for a 2-D step scan in snake (boustrophedon) order (GUI thread).

        Reads X and Y parameters, validates, logs the scan header, then launches
        stepscan2d0 on a Worker thread.  stepscan2d0 re-reads the Y motor position
        from the UI at executor start time (so the user can update Y range between
        phi slices in a 3-D scan if needed).

        Why stepscan2d exists separately from stepscan2d0: same thread-safety reason
        as stepscan/stepscan0 — Qt widget reads must happen on the GUI thread.
        """
        xmotor, ymotor, _phi = self._scan_motors(xmotor, ymotor)
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

        self._pre_scan("stepscan2d", xmotor=xmotor, ymotor=ymotor)

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

    def piezo_step2d(self):
        """2D step scan over the trans1/trans2 ('piezo') stages.

        Kept as a single, argument-free entry point (mirrors stepscan2d(xmotor,
        ymotor)) so it can be invoked directly -- e.g. by a macro/API -- with no
        GUI interaction, exactly as the "Piezo 2D step" button does.
        """
        try:
            xmotor = self.w.motornames.index("trans1")
            ymotor = self.w.motornames.index("trans2")
        except ValueError:
            QMessageBox.warning(
                self.w.ui, "Not Available", "trans1/trans2 are not connected."
            )
            return
        self.stepscan2d(xmotor, ymotor)

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

        self._pre_scan("stepscan3d", xmotor=xmotor, ymotor=ymotor)

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

        # Per-slice scandone(..., direct_gui_updates=False) calls inside stepscan3d0
        # handle per-slice detector cleanup, master-file linking, and scan-number
        # increment -- entirely on the worker thread, with GUI label refresh
        # deferred to a queued scanFolderAdvanced signal (see _launch_worker).
        # The done_signal fires on the GUI thread after the worker exits and
        # handles the final teardown (shutter, status label, scan time) without
        # incrementing the scan number a second time.
        self._launch_worker(
            self.stepscan3d0,
            xmotor,
            ymotor,
            phimotor,
            done_signal=lambda _ok: self.w.scandone(
                update_scannumber=False, donedone=True, link_detector_data=False
            ),
        )

    def run_stop_issued(self):
        """Advance the scan number at the start of a scan.

        This is the one moment fileIndex is written: the number is
        advanced, pushed to the IOC, then shown. Doing it here rather than
        at startup or on completion means the PV names the scan that is
        about to run.
        """
        self.w.parameters.scan_number = self.w.parameters.scan_number + 1
        self.w.write_scan_number_to_ioc()
        self.w.update_scannumber()
        self.w.parameters.writeini()

    def update_scannumber(self):
        """Refresh the scan-number widgets. Does not touch the IOC.

        12idc:data:fileIndex is written only when a scan starts, by
        run_stop_issued and _advance_scan_number_safe, so that the PV names
        the scan that is running. Startup, scan completion and the +/-
        buttons all go through here and leave it alone.
        """
        self.ui.edit_scannumber.setText(str(int(self.w.parameters.scan_number)))
        self.update_label_scanCheck()

    def on_scan_folder_advanced(self, scan_number):
        """GUI-thread slot for workerSignals.scanFolderAdvanced.

        Refreshes the scan-number/scan-name widgets after a 3-D fly/step
        executor advances to a new per-slice folder on the worker thread
        (via _advance_scan_number_safe / scandone(direct_gui_updates=False)).
        Qt auto-queues delivery here because this method is bound to self.w
        (a QObject with GUI-thread affinity), so it is safe even though the
        signal was emitted from the worker thread.
        """
        self.ui.edit_scannumber.setText(str(int(scan_number)))
        self.update_label_scanCheck()
        txt = "%s_%0.4i" % (self.w.parameters.scan_name, scan_number)
        self.ui.lbl_scanname.setText(txt)





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
            xmotor = self.w.DEFAULTS["xmotor"]
        try:
            detectors = data["detectors"]
        except:
            detectors = ""
        try:
            ymotor = int(data["ymotor"])
        except:
            ymotor = self.w.DEFAULTS["ymotor"]
        try:
            phimotor = int(data["phimotor"])
        except:
            phimotor = self.w.DEFAULTS["phimotor"]
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
            self.w.fly2d(xmotor=xmotor, ymotor=ymotor)

        elif cmd == "fly2d_snake":
            self.w.fly2d(xmotor=xmotor, ymotor=ymotor, snake=True)

        elif cmd == "fly3d":
            # scanname here seeds fly3d0's per-phi-slice label prefix, not the
            # sample scan name (set separately via a prior "set" command).
            self.w.fly3d(
                xmotor=xmotor, ymotor=ymotor, phimotor=phimotor, slice_label_prefix=scanname
            )

        elif cmd == "fly3d_snake":
            self.w.fly3d(
                xmotor=xmotor,
                ymotor=ymotor,
                phimotor=phimotor,
                snake=True,
                slice_label_prefix=scanname,
            )

        elif cmd == "stepscan3d":
            self.w.stepscan3d(xmotor=xmotor, ymotor=ymotor, phimotor=phimotor)

        elif cmd == "stepscan2d":
            self.w.stepscan2d(xmotor=xmotor, ymotor=ymotor)

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
