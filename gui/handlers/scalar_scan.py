"""
scalar_scan.py — live EPICS-scalar monitor + 1-D step scan window for
CRL_3dprint.py.

Watches the CRL rig's X/Y stage position against all four scaler channels
in two live-updating pyqtgraph plots (a combo box picks which one is
displayed), and can run a "lup"-style single-motor step scan (move-wait-read,
no areaDetector) that pops up its own result plot when finished. Non-modal:
opened with .show(), never .exec_(), so the main CRL_3dprint window stays
usable.
"""

import configparser
import os
import time
from collections import deque

import h5py
import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QByteArray, QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from font_utils import DEFAULT_FONT_SIZE, apply_font_size_to_tree

# Scaler channels this codebase already treats as "the scalars" for NeXus
# master files (see gui/handlers/nexus_metadata_config.py SHARED_METADATA_MAP).
SCALAR_PVS = {
    "IC": "12idc:3820:scaler1.S2",
    "BS2": "12idc:3820:scaler1.S3",
    "BS": "12idc:3820:scaler1.S4",
    "IfCRL": "12idc:3820:scaler1.S5",
}
SCALER_TP_PV = "12idc:3820:scaler1.TP"  # scaler preset (exposure) time

# Preamplifier sensitivity (unit, value) PV pairs per scalar - read once at
# save time (not polled at 5 Hz like SCALAR_PVS) and stored alongside each
# scalar's data.
PREAMP_PVS = {
    "IC": ("12idc:A1sens_unit.VAL", "12idc:A1sens_num.VAL"),
    "BS2": ("12idc:A4sens_unit.VAL", "12idc:A4sens_num.VAL"),
    "BS": ("12idc:A3sens_unit.VAL", "12idc:A3sens_num.VAL"),
    "IfCRL": ("12idc:A5sens_unit.VAL", "12idc:A5sens_num.VAL"),
}

POLL_INTERVAL_MS = 200  # 5 Hz
DEFAULT_BUFLEN = 200
DEFAULT_EXPTIME_S = "0.001"
MOVE_PRIME_DELAY_S = 0.02  # let the controller's moving-status flag catch up before polling it
MOVE_POLL_INTERVAL_S = 0.01
POSITION_SETTLE_S = 0.1  # fixed mechanical-settle wait after each move, before the exposure
ACCUMULATE_WAIT_S = 0.1  # wait between repeated measurements at the same position (N accumulate > 1)

# This module lives in gui/handlers/, but CRL_3dprint.ini lives in gui/ -
# go up two directory levels (handlers/ -> gui/) to find it.
_GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INI_PATH = os.path.join(_GUI_DIR, "CRL_3dprint.ini")
_INI_SECTION = "scalar_scan"


def _ini_get(key, fallback=""):
    cfg = configparser.ConfigParser()
    cfg.read(_INI_PATH)
    return cfg.get(_INI_SECTION, key, fallback=fallback)


def _ini_set(key, value):
    cfg = configparser.ConfigParser()
    cfg.read(_INI_PATH)
    if _INI_SECTION not in cfg:
        cfg[_INI_SECTION] = {}
    cfg[_INI_SECTION][key] = str(value)
    with open(_INI_PATH, "w") as f:
        cfg.write(f)


def _named(widget, name):
    """setObjectName(...) and return the widget, so font_utils.apply_font_size_to_tree
    (which skips unnamed widgets) reaches everything we build here."""
    widget.setObjectName(name)
    return widget


def _write_preamp_attrs(scalars_group, pv_class):
    """One-shot read of each scalar's preamp unit/value PVs and store them as
    attrs on that scalar's dataset. Called at save time in both save paths."""
    for name, (unit_pv, value_pv) in PREAMP_PVS.items():
        if name not in scalars_group:
            continue
        unit = pv_class()(unit_pv).get()
        value = pv_class()(value_pv).get()
        ds = scalars_group[name]
        ds.attrs["preamp_unit"] = "" if unit is None else str(unit)
        ds.attrs["preamp_value"] = "" if value is None else str(value)


def _make_positions(frm: float, to: float, step: float) -> np.ndarray:
    """Inclusive position array from frm to to, with step's sign corrected
    to point from frm toward to (mirrors scan_handler.py's _make_positions)."""
    if step == 0 or frm == to:
        return np.array([frm])
    signed_step = abs(step) if to >= frm else -abs(step)
    n = int(round(abs(to - frm) / abs(step))) + 1
    return frm + signed_step * np.arange(n)


class _WorkerSignals(QObject):
    finished = pyqtSignal(str, list, dict)  # axis, positions, {scalar_name: [values]}
    progress = pyqtSignal(int, int)  # point index (1-based), total points
    error = pyqtSignal(str)


class _ScanWorker(QRunnable):
    """Moves `axis` through `positions` one at a time, reading every scalar
    in `pvs` after each move completes and settles. When n_accumulate > 1,
    takes that many repeated readings at the same position (ACCUMULATE_WAIT_S
    apart) and sums them per scalar into the single value logged for that
    position. Runs off the GUI thread via QThreadPool.

    Uses mv(..., wait=False) plus our own is_moving() poll loop rather than
    mv(..., wait=True): the jog/move-to buttons in CRL_3dprint.py only ever
    call mv() with wait=False, so that is the one exercised, known-working
    code path against the real SmarAct controller. A short priming delay is
    inserted before the first is_moving() check to give the controller's
    status flag time to flip to "moving" before we start polling it."""

    def __init__(self, controller, lock, axis, positions, pvs, exp_time, n_accumulate=1):
        super().__init__()
        self.controller = controller
        self.lock = lock
        self.axis = axis
        self.positions = positions
        self.pvs = pvs  # {scalar_name: PV/FakePV}
        self.exp_time = exp_time
        self.n_accumulate = max(1, n_accumulate)
        self.signals = _WorkerSignals()
        self._stop = False

    def stop(self):
        self._stop = True

    def _move_and_wait(self, target: float):
        with self.lock:
            self.controller.mv(self.axis, target, wait=False)
        time.sleep(MOVE_PRIME_DELAY_S)
        while True:
            with self.lock:
                moving = self.controller.is_moving(self.axis)
            if not moving or self._stop:
                break
            time.sleep(MOVE_POLL_INTERVAL_S)

    @pyqtSlot()
    def run(self):
        positions_out = []
        values_out = {name: [] for name in self.pvs}
        try:
            n = len(self.positions)
            for i, value in enumerate(self.positions):
                if self._stop:
                    break
                self._move_and_wait(float(value))
                time.sleep(POSITION_SETTLE_S)  # mechanical settle, once per position

                accum = {name: 0.0 for name in self.pvs}
                for k in range(self.n_accumulate):
                    time.sleep(self.exp_time)  # exposure/integration time
                    for name, pv in self.pvs.items():
                        accum[name] += pv.get()
                    if k < self.n_accumulate - 1:
                        time.sleep(ACCUMULATE_WAIT_S)  # wait between repeated measurements
                for name in self.pvs:
                    values_out[name].append(accum[name])

                positions_out.append(float(value))
                self.signals.progress.emit(i + 1, n)
        except Exception:
            import traceback

            self.signals.error.emit(traceback.format_exc())
            return
        self.signals.finished.emit(self.axis, positions_out, values_out)


class ScanResultWindow(QObject):
    """Standalone pyqtgraph popup showing one completed scan's (position,
    scalar) trace for the scalar that was selected when the scan started,
    with its own Save button. Deliberately self-contained - it saves the
    exact positions/values this scan produced, never the live-monitor
    deques (ScalarScanWindow.buf_x/buf_y/buf_scalars), which hold unrelated
    5 Hz polling data and are paused for the whole scan anyway. Kept alive
    by the caller (ScalarScanWindow._result_windows) so it isn't
    garbage-collected while still on screen."""

    def __init__(self, parent, axis, positions, values, display_scalar, font_size, pv_class):
        super().__init__(parent)
        self.axis = axis
        self.positions = positions  # [float] - this scan's own positions, length N
        self.values = values  # {scalar_name: [float]} - this scan's own readings, each length N
        self._pv_class = pv_class

        self.win = _named(QWidget(parent, Qt.Window), f"scalarscan_result_{axis}")
        self.win.setWindowTitle(f"Scan result: {axis}")
        layout = QVBoxLayout(self.win)
        plot = pg.PlotWidget()
        plot.plot(positions, values[display_scalar], pen=pg.mkPen("g"), symbol="o")
        plot.setLabel("bottom", axis, units="mm")
        plot.setLabel("left", display_scalar)
        layout.addWidget(plot)

        btn_save = _named(QPushButton("Save"), f"scalarscan_result_btn_save_{axis}")
        btn_save.clicked.connect(self._save_h5)
        layout.addWidget(btn_save)

        self.win.resize(500, 400)
        apply_font_size_to_tree(self.win, font_size)
        self.win.show()

    def _save_h5(self):
        start_dir = _ini_get("last_scan_save_path", "")
        path, _ = QFileDialog.getSaveFileName(self.win, "Save scan", start_dir, "HDF5 files (*.h5)")
        if not path:
            return
        if not path.endswith(".h5"):
            path += ".h5"

        with h5py.File(path, "w") as f:
            entry = f.create_group("entry")
            entry.attrs["NX_class"] = b"NXentry"

            sample = entry.create_group("sample")
            sample.attrs["NX_class"] = b"NXsample"
            sample.create_dataset("positions", data=np.array(self.positions, dtype=float))
            sample["positions"].attrs["units"] = b"mm"
            sample["positions"].attrs["axis"] = self.axis.encode("utf-8")

            scalars = entry.create_group("scalars")
            scalars.attrs["NX_class"] = b"NXcollection"
            for name, vals in self.values.items():
                arr = np.array(vals, dtype=float)
                scalars.create_dataset(name, data=arr)
                scalars[name].attrs["units"] = b"counts"
            _write_preamp_attrs(scalars, self._pv_class)

        _ini_set("last_scan_save_path", path)
        QMessageBox.information(self.win, "Saved", f"Saved to {path}")


class ScalarScanWindow(QObject):
    """Non-modal live-monitor + 1-D step-scan window for the CRL rig's X/Y
    stage against all four EPICS scalars. QObject wrapping a real QWidget
    (self.win), matching CRL3DPrintControl's own shape, so no stray blank
    window appears."""

    def __init__(self, parent_ui, controller, pv_class, lock, soft_limits):
        super().__init__(parent_ui)
        self._parent_ui = parent_ui
        self.controller = controller
        self._pv_class = pv_class
        self.lock = lock
        self.soft_limits = soft_limits
        self._pv_cache = {}
        self._scan_worker = None
        self._scan_axis = None
        self._scan_display_scalar = None
        self._result_windows = []

        self._build_ui(parent_ui)
        self._init_buffers(self.spin_buflen.value())
        self._wire_signals()
        self._on_exptime_edited()  # push the loaded/default exposure time to the scaler's TP field

        apply_font_size_to_tree(self.win, self._current_font_size())

        self.timer = QTimer(self.win)
        self.timer.timeout.connect(self._poll_live)
        self.timer.start(POLL_INTERVAL_MS)

        self.win.closeEvent = self._on_close

        # show() FIRST (matches CRL_3dprint.py's own window) so the window's
        # real, post-layout size is what we snapshot as the resize floor -
        # snapshotting before show() reads a not-yet-laid-out size.
        self.win.show()
        QApplication.processEvents()
        orig_size = self.win.size()
        self.win.setMaximumSize(16777215, 16777215)
        self.win.setMinimumSize(int(orig_size.width() * 0.4), int(orig_size.height() * 0.4))
        self._restore_window_geometry()

    def _restore_window_geometry(self):
        hexstr = _ini_get("window_geometry", "").strip()
        if hexstr:
            self.win.restoreGeometry(QByteArray.fromHex(hexstr.encode()))

    def _current_font_size(self):
        try:
            return self._parent_ui.spinBox_fontSize.value()
        except AttributeError:
            return DEFAULT_FONT_SIZE

    # -- construction ---------------------------------------------------

    def _build_ui(self, parent_ui):
        self.win = _named(QWidget(parent_ui, Qt.Window), "scalarScanWindow")
        self.win.setWindowTitle("Scalar Scan")
        self.win.resize(900, 500)

        outer = QVBoxLayout(self.win)
        splitter = _named(QSplitter(Qt.Horizontal), "scalarscan_splitter")
        outer.addWidget(splitter)

        # -- left: all controls ---------------------------------------------
        left_widget = _named(QWidget(), "scalarscan_left")
        left = QVBoxLayout(left_widget)

        grid = QGridLayout()
        headers = ["Motor", "from (mm)", "to (mm)", "step (mm)", "N pos", "Scan"]
        for col, text in enumerate(headers):
            grid.addWidget(_named(QLabel(text), f"scalarscan_hdr_{col}"), 0, col)

        self._edit_from = {}
        self._edit_to = {}
        self._edit_step = {}
        self._lbl_npos = {}
        self._btn_scan = {}
        for row, axis in enumerate(("X", "Y"), start=1):
            grid.addWidget(_named(QLabel(axis), f"scalarscan_lbl_axis_{axis}"), row, 0)

            edit_from = _named(QLineEdit(_ini_get(f"{axis.lower()}_from", "0.0")), f"scalarscan_edit_from_{axis}")
            grid.addWidget(edit_from, row, 1)
            edit_from.editingFinished.connect(lambda a=axis, e=edit_from: _ini_set(f"{a.lower()}_from", e.text()))
            self._edit_from[axis] = edit_from

            edit_to = _named(QLineEdit(_ini_get(f"{axis.lower()}_to", "0.0")), f"scalarscan_edit_to_{axis}")
            grid.addWidget(edit_to, row, 2)
            edit_to.editingFinished.connect(lambda a=axis, e=edit_to: _ini_set(f"{a.lower()}_to", e.text()))
            self._edit_to[axis] = edit_to

            edit_step = _named(QLineEdit(_ini_get(f"{axis.lower()}_step", "0.01")), f"scalarscan_edit_step_{axis}")
            grid.addWidget(edit_step, row, 3)
            edit_step.editingFinished.connect(lambda a=axis, e=edit_step: _ini_set(f"{a.lower()}_step", e.text()))
            self._edit_step[axis] = edit_step

            lbl_npos = _named(QLabel("0"), f"scalarscan_lbl_npos_{axis}")
            grid.addWidget(lbl_npos, row, 4)
            self._lbl_npos[axis] = lbl_npos

            btn_scan = _named(QPushButton("Scan"), f"scalarscan_btn_scan_{axis}")
            grid.addWidget(btn_scan, row, 5)
            self._btn_scan[axis] = btn_scan

        left.addLayout(grid)

        exptime_row = QHBoxLayout()
        exptime_row.addWidget(_named(QLabel("exp time (s)"), "scalarscan_lbl_exptime"))
        self.edit_exptime = _named(QLineEdit(_ini_get("exp_time", DEFAULT_EXPTIME_S)), "scalarscan_edit_exptime")
        self.edit_exptime.editingFinished.connect(self._on_exptime_edited)
        exptime_row.addWidget(self.edit_exptime)
        left.addLayout(exptime_row)

        naccum_row = QHBoxLayout()
        naccum_row.addWidget(_named(QLabel("N accumulate"), "scalarscan_lbl_naccum"))
        self.spin_naccum = _named(QSpinBox(), "scalarscan_spin_naccum")
        self.spin_naccum.setMinimum(1)
        self.spin_naccum.setMaximum(10_000)
        try:
            saved_naccum = int(_ini_get("n_accumulate", "1"))
        except ValueError:
            saved_naccum = 1
        self.spin_naccum.setValue(saved_naccum)
        self.spin_naccum.valueChanged.connect(lambda v: _ini_set("n_accumulate", v))
        naccum_row.addWidget(self.spin_naccum)
        left.addLayout(naccum_row)

        scalar_row = QHBoxLayout()
        scalar_row.addWidget(_named(QLabel("Scalar:"), "scalarscan_lbl_scalar"))
        self.combo_scalar = _named(QComboBox(), "scalarscan_combo_scalar")
        self.combo_scalar.addItems(list(SCALAR_PVS.keys()))
        saved_scalar = _ini_get("scalar", next(iter(SCALAR_PVS)))
        if saved_scalar in SCALAR_PVS:
            self.combo_scalar.setCurrentText(saved_scalar)
        scalar_row.addWidget(self.combo_scalar)
        left.addLayout(scalar_row)

        buflen_row = QHBoxLayout()
        buflen_row.addWidget(_named(QLabel("Buffer length:"), "scalarscan_lbl_buflen"))
        self.spin_buflen = _named(QSpinBox(), "scalarscan_spin_buflen")
        self.spin_buflen.setMinimum(1)
        self.spin_buflen.setMaximum(1_000_000)
        try:
            saved_buflen = int(_ini_get("buflen", str(DEFAULT_BUFLEN)))
        except ValueError:
            saved_buflen = DEFAULT_BUFLEN
        self.spin_buflen.setValue(saved_buflen)
        buflen_row.addWidget(self.spin_buflen)
        left.addLayout(buflen_row)

        self.btn_save = _named(QPushButton("Save buffer"), "scalarscan_btn_save")
        left.addWidget(self.btn_save)

        self.lbl_status = _named(QLabel("Idle"), "scalarscan_lbl_status")
        left.addWidget(self.lbl_status)

        left.addStretch(1)
        splitter.addWidget(left_widget)

        # -- right: just the two plots ---------------------------------------
        right_widget = _named(QWidget(), "scalarscan_right")
        right = QVBoxLayout(right_widget)
        self.plot_top = pg.PlotWidget()
        self.plot_bottom = pg.PlotWidget()
        right.addWidget(self.plot_top)
        right.addWidget(self.plot_bottom)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 600])

    def _wire_signals(self):
        for axis in ("X", "Y"):
            for edit in (self._edit_from[axis], self._edit_to[axis], self._edit_step[axis]):
                edit.textChanged.connect(lambda _t, a=axis: self._update_n_pos_label(a))
            self._btn_scan[axis].clicked.connect(lambda _checked=False, a=axis: self._start_scan(a))
            self._update_n_pos_label(axis)

        self.spin_buflen.valueChanged.connect(self._on_buflen_changed)
        self.combo_scalar.currentTextChanged.connect(self._on_scalar_changed)
        self.btn_save.clicked.connect(self._save_h5)

    # -- live monitor -----------------------------------------------------

    def _init_buffers(self, maxlen: int):
        self.buf_x = deque(maxlen=maxlen)
        self.buf_y = deque(maxlen=maxlen)
        self.buf_scalars = {name: deque(maxlen=maxlen) for name in SCALAR_PVS}

    def _on_buflen_changed(self, value: int):
        _ini_set("buflen", value)
        self._init_buffers(value)
        self._redraw_plots()

    def _on_scalar_changed(self, text: str):
        _ini_set("scalar", text)
        self._redraw_plots()

    def _on_exptime_edited(self):
        text = self.edit_exptime.text()
        _ini_set("exp_time", text)
        try:
            self._get_pv(SCALER_TP_PV).put(float(text))
        except ValueError:
            pass

    def _get_pv(self, name: str):
        return self._pv_cache.setdefault(name, self._pv_class()(name))

    def _poll_live(self):
        if self._scan_worker is not None:
            return
        with self.lock:
            x = self.controller.get_pos("X")
            y = self.controller.get_pos("Y")
        self.buf_x.append(x)
        self.buf_y.append(y)
        for name, pv_name in SCALAR_PVS.items():
            self.buf_scalars[name].append(self._get_pv(pv_name).get())
        self._redraw_plots()

    def _redraw_plots(self):
        xs, ys = list(self.buf_x), list(self.buf_y)
        name = self.combo_scalar.currentText()
        ss = list(self.buf_scalars[name])

        self.plot_bottom.clear()
        self.plot_bottom.plot(xs, ss, pen=pg.mkPen("r"))
        self.plot_bottom.setLabel("bottom", "X")
        self.plot_bottom.setLabel("left", name)

        self.plot_top.clear()
        self.plot_top.plot(ys, ss, pen=pg.mkPen("b"))
        self.plot_top.setLabel("bottom", "Y")
        self.plot_top.setLabel("left", name)

    # -- N pos -------------------------------------------------------------

    def _n_pos_for_row(self, axis: str) -> int:
        try:
            frm = float(self._edit_from[axis].text())
            to = float(self._edit_to[axis].text())
            step = float(self._edit_step[axis].text())
        except ValueError:
            return 0
        if step == 0:
            return 0
        return int(round(abs(to - frm) / abs(step))) + 1

    def _update_n_pos_label(self, axis: str):
        self._lbl_npos[axis].setText(str(self._n_pos_for_row(axis)))

    # -- scan ---------------------------------------------------------------

    def _start_scan(self, axis: str):
        if self._scan_worker is not None:
            return
        try:
            frm = float(self._edit_from[axis].text())
            to = float(self._edit_to[axis].text())
            step = float(self._edit_step[axis].text())
            exptime = float(self.edit_exptime.text())
        except ValueError:
            QMessageBox.warning(self.win, "Invalid input", "from/to/step/exp time must be numbers.")
            return
        if step == 0:
            QMessageBox.warning(self.win, "Invalid input", "step must be nonzero.")
            return

        positions = _make_positions(frm, to, step)

        lo, hi = self.soft_limits[axis]
        if positions.min() < lo or positions.max() > hi:
            QMessageBox.warning(
                self.win,
                "Out of range",
                f"{axis} scan range must stay within [{lo}, {hi}] mm.",
            )
            return

        self._get_pv(SCALER_TP_PV).put(exptime)
        pvs = {name: self._get_pv(pv_name) for name, pv_name in SCALAR_PVS.items()}
        self._scan_display_scalar = self.combo_scalar.currentText()

        self._scan_axis = axis
        self._set_scan_controls_enabled(False)
        self.lbl_status.setText(f"Scanning {axis}: 0/{len(positions)}")
        worker = _ScanWorker(
            self.controller, self.lock, axis, positions, pvs, exptime, self.spin_naccum.value()
        )
        worker.signals.finished.connect(self._on_scan_finished)
        worker.signals.error.connect(self._on_scan_error)
        worker.signals.progress.connect(self._on_scan_progress)
        self._scan_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_scan_progress(self, i: int, n: int):
        self.lbl_status.setText(f"Scanning {self._scan_axis}: {i}/{n}")

    def _on_scan_finished(self, axis, positions, values):
        self._scan_worker = None
        self._set_scan_controls_enabled(True)
        self.lbl_status.setText("Idle")
        self._result_windows.append(
            ScanResultWindow(
                self.win,
                axis,
                positions,
                values,
                self._scan_display_scalar,
                self._current_font_size(),
                self._pv_class,
            )
        )

    def _on_scan_error(self, msg: str):
        self._scan_worker = None
        self._set_scan_controls_enabled(True)
        self.lbl_status.setText("Idle")
        QMessageBox.critical(self.win, "Scan error", msg)

    def _set_scan_controls_enabled(self, enabled: bool):
        self._btn_scan["X"].setEnabled(enabled)
        self._btn_scan["Y"].setEnabled(enabled)
        self.btn_save.setEnabled(enabled)

    # -- save ---------------------------------------------------------------

    def _save_h5(self):
        start_dir = _ini_get("last_save_path", "")
        path, _ = QFileDialog.getSaveFileName(self.win, "Save scalar scan", start_dir, "HDF5 files (*.h5)")
        if not path:
            return
        if not path.endswith(".h5"):
            path += ".h5"

        positions = np.array(list(zip(self.buf_x, self.buf_y)), dtype=float)

        with h5py.File(path, "w") as f:
            entry = f.create_group("entry")
            entry.attrs["NX_class"] = b"NXentry"

            sample = entry.create_group("sample")
            sample.attrs["NX_class"] = b"NXsample"
            sample.create_dataset("positions", data=positions)
            sample["positions"].attrs["units"] = b"mm"

            scalars = entry.create_group("scalars")
            scalars.attrs["NX_class"] = b"NXcollection"
            for name in SCALAR_PVS:
                arr = np.array(list(self.buf_scalars[name]), dtype=float)
                scalars.create_dataset(name, data=arr)
                scalars[name].attrs["units"] = b"counts"
            _write_preamp_attrs(scalars, self._pv_class)

        _ini_set("last_save_path", path)
        QMessageBox.information(self.win, "Saved", f"Saved to {path}")

    # -- cleanup --------------------------------------------------------------

    def _on_close(self, event):
        _ini_set("window_geometry", bytes(self.win.saveGeometry().toHex()).decode())
        self.timer.stop()
        if self._scan_worker is not None:
            self._scan_worker.stop()
        event.accept()
