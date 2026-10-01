"""
CRL_3dprint.py — motor control GUI for the CRL 3D-printing rig.

Controls 4 SmarAct MCS2 stages (X, Y linear in mm; TILT, PITCH rotary in
deg) on one network controller. Run with --debug_mode to use simulated
motors with no hardware/network connection required:

    python CRL_3dprint.py --debug_mode
"""

import argparse
import configparser
import json
import os
import sys
from threading import Lock

from PyQt5 import uic
from PyQt5.QtCore import QByteArray, QEvent, QObject, QTimer
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from font_utils import DEFAULT_FONT_SIZE, apply_font_size_to_tree
from ini_utils import INI_DIR, ensure_ini_defaults
from resize_utils import ProportionalResizer
from handlers.scalar_scan import INI_DEFAULTS as SCALAR_SCAN_INI_DEFAULTS
from handlers.scalar_scan import ScalarScanWindow

_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_GUI_DIR)
for _p in (os.path.join(_REPO_ROOT, "debug"), os.path.join(_REPO_ROOT, "ptychosaxs"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from epics import PV
except ImportError:
    PV = None  # replaced by FakePV in debug mode

try:
    from smaract_crl3dprint import MOTOR_SLOTS as _RAW_MOTOR_SLOTS
    from smaract_crl3dprint import CRLAxisController

    _HARDWARE_AVAILABLE = True
except ImportError:
    _HARDWARE_AVAILABLE = False
    _RAW_MOTOR_SLOTS = [("X", 0, "mm"), ("Y", 1, "mm"), ("TILT", 2, "deg"), ("PITCH", 3, "deg")]

_CRL_INI = os.path.join(INI_DIR, "CRL_3dprint.ini")

# Single source of truth for the 4 motors, in {"name", "unit"} form for readability.
MOTOR_SLOTS = [{"name": name, "unit": unit} for name, _channel, unit in _RAW_MOTOR_SLOTS]

XY_JOG_MAP = {
    "pb_xy_left": ("X", -1),
    "pb_xy_right": ("X", +1),
    "pb_xy_down": ("Y", -1),
    "pb_xy_up": ("Y", +1),
}

TILTPITCH_JOG_MAP = {
    "pb_tp_left": ("TILT", -1),
    "pb_tp_right": ("TILT", +1),
    "pb_tp_down": ("PITCH", -1),
    "pb_tp_up": ("PITCH", +1),
}

# Hand-edit these per motor: (min, max) in the motor's own unit (mm for
# X/Y, deg for TILT/PITCH). A move that would land outside its motor's
# range here is rejected (never sent to the controller) and that motor's
# "move to" field turns pale red until a valid move is made.
SOFT_LIMITS = {
    "X": (-14.0, 14.0),
    "Y": (-14.0, 14.0),
    "TILT": (-5.0, 5.0),
    "PITCH": (-5.0, 5.0),
}

# Hand-edit these per motor: (min, max) velocity in the motor's own unit/s.
# A value entered outside its motor's range here is rejected (never sent to
# the controller) and the velocity field turns pale red until a valid value
# is entered.
VELOCITY_SOFT_LIMITS = {
    "X": (0.001, 5.1),
    "Y": (0.001, 5.1),
    "TILT": (0.001, 1.1),
    "PITCH": (0.001, 1.1),
}

TOOLS_ACTIONS = {
    "actionChangeVelocities": "_open_velocities_dialog",
    "actionCalibrateReference": "_open_calibrate_dialog",
}

EDIT_ACTIONS = {
    "actionRemapXYJog": "_open_remap_xy_dialog",
    "actionRemapTiltPitchJog": "_open_remap_tp_dialog",
}

# Display order/labels for the 4 jog-pad buttons, shared by both jog pads
# (each pad's actual button names are "pb_xy_*" / "pb_tp_*").
JOG_BUTTON_LABELS = ["up", "down", "left", "right"]

# Jog-pad step widgets, and the .ini key each is persisted under (in the
# "tweak_steps" section, alongside each motor's own edit_tweak_{name}).
PAD_STEP_INI_KEYS = {"ed_xy_tweak": "xy_pad", "ed_tp_tweak": "tp_pad"}


def _default_jog_section(jog_map):
    """Flatten a button->(axis, sign) map into its .ini key/value form."""
    section = {}
    for btn_name, (axis, sign) in jog_map.items():
        section[f"{btn_name}_axis"] = axis
        section[f"{btn_name}_sign"] = str(sign)
    return section


# Every section/key this GUI (and its scalar-scan window) reads out of
# CRL_3dprint.ini, with the value used when the file - or just that entry -
# does not exist yet. The .ini is untracked per-installation state, so this
# is the only definition of a fresh one.
INI_DEFAULTS = {
    "xy_preset": {"in_0": "0.0", "in_1": "0.0", "out_0": "0.0", "out_1": "0.0"},
    "ui": {"font_size": str(DEFAULT_FONT_SIZE), "window_geometry": ""},
    "tweak_steps": dict(
        [(slot["name"], "0.001" if slot["unit"] == "mm" else "0.010") for slot in MOTOR_SLOTS]
        + [("xy_pad", "0.001"), ("tp_pad", "0.010")]
    ),
    "jog_remap_xy": _default_jog_section(XY_JOG_MAP),
    "jog_remap_tp": _default_jog_section(TILTPITCH_JOG_MAP),
    **SCALAR_SCAN_INI_DEFAULTS,
}


def _ensure_default_ini(path):
    """Create CRL_3dprint.ini from INI_DEFAULTS if it does not exist, and
    add any individual entry missing from an existing file."""
    ensure_ini_defaults(path, INI_DEFAULTS)


def _load_jog_map(section, default_map):
    """Read a jog-pad button->(axis, sign) map from the .ini, falling back to
    default_map (and to each individual entry) when the section or a key is
    missing or malformed."""
    cfg = configparser.ConfigParser()
    cfg.read(_CRL_INI)
    result = dict(default_map)
    if not cfg.has_section(section):
        return result
    for btn_name, (default_axis, default_sign) in default_map.items():
        axis = cfg.get(section, f"{btn_name}_axis", fallback=default_axis)
        try:
            sign = int(cfg.get(section, f"{btn_name}_sign", fallback=str(default_sign)))
        except ValueError:
            sign = default_sign
        result[btn_name] = (axis, sign)
    return result


def _save_jog_map(section, jog_map):
    cfg = configparser.ConfigParser()
    cfg.read(_CRL_INI)  # preserve other sections
    cfg[section] = {}
    for btn_name, (axis, sign) in jog_map.items():
        cfg[section][f"{btn_name}_axis"] = axis
        cfg[section][f"{btn_name}_sign"] = str(sign)
    with open(_CRL_INI, "w") as f:
        cfg.write(f)


def _near(a, b, thresh=0.005):
    """Proximity check used by XYPresetBlock's status pill logic."""
    return a is not None and b is not None and abs(a - b) <= thresh


class XYPresetBlock:
    """In/out position memory for the X/Y stages only (tilt/pitch excluded).

    Modeled on gui/optics_motors.py's MotorPresetBlock: same enable toggle,
    Move/Save slider-toggle, status pill, and JSON export/import behavior,
    but scoped to exactly X/Y and persisted in CRL_3dprint's own .ini file.
    """

    THRESH = 0.005

    def __init__(self, parent):
        self._parent = parent
        self._ui = parent.ui
        self._enabled = False
        self._setup()

    def _setup(self):
        self._ui.pushButton_xyEnable.clicked.connect(self._toggle)
        self._ui.pushButton_xyIn.clicked.connect(self._on_in)
        self._ui.pushButton_xyOut.clicked.connect(self._on_out)
        self._ui.pushButton_xyExport.clicked.connect(self._export)
        self._ui.pushButton_xyImport.clicked.connect(self._import)
        self._install_slider_toggle(self._ui.horizontalSlider_xyMoveSet)
        self._apply_enabled(False)
        self._load_ini()

    @staticmethod
    def _install_slider_toggle(slider):
        """Make a 2-state QSlider toggle on any click instead of seeking."""

        class _ToggleFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.MouseButtonPress:
                    obj.setValue(1 - obj.value())
                    return True  # consume - suppress Qt's own seek behaviour
                return False

        slider.installEventFilter(_ToggleFilter(slider))

    def _apply_enabled(self, enabled):
        for w in (
            self._ui.pushButton_xyIn,
            self._ui.pushButton_xyOut,
            self._ui.horizontalSlider_xyMoveSet,
            self._ui.label_xyStatus,
        ):
            w.setEnabled(enabled)
        if enabled:
            self._ui.pushButton_xyEnable.setText("Yes")
            self._ui.pushButton_xyEnable.setStyleSheet("background-color: #ccffcc;")
        else:
            self._ui.pushButton_xyEnable.setText("No")
            self._ui.pushButton_xyEnable.setStyleSheet("background-color: #ffcccc;")
        self._enabled = enabled

    def _toggle(self):
        self._apply_enabled(not self._enabled)

    def _slider_mode(self):
        return self._ui.horizontalSlider_xyMoveSet.value()

    def _on_in(self):
        self._in_out_common(self._ui.lbl_xyxIn, self._ui.lbl_xyyIn)

    def _on_out(self):
        self._in_out_common(self._ui.lbl_xyxOut, self._ui.lbl_xyyOut)

    def _in_out_common(self, lbl_x, lbl_y):
        p = self._parent
        if self._slider_mode() == 0:
            # Move mode: send the stored value to hardware.
            for axis, lbl in (("X", lbl_x), ("Y", lbl_y)):
                if lbl.text():
                    try:
                        target = float(lbl.text())
                    except ValueError:
                        continue
                    with p.lock:
                        p.controller.mv(axis, target, wait=False)
        else:
            # Save mode: capture the current position into the label.
            for axis, lbl in (("X", lbl_x), ("Y", lbl_y)):
                cur = self._ui.findChild(QLabel, f"lbl_pos_{axis}")
                if cur:
                    lbl.setText(cur.text())
            self._save_ini()

    def _load_ini(self):
        cfg = configparser.ConfigParser()
        cfg.read(_CRL_INI)
        if "xy_preset" not in cfg:
            return
        sec = cfg["xy_preset"]
        self._ui.lbl_xyxIn.setText(sec.get("in_0", "0.0"))
        self._ui.lbl_xyyIn.setText(sec.get("in_1", "0.0"))
        self._ui.lbl_xyxOut.setText(sec.get("out_0", "0.0"))
        self._ui.lbl_xyyOut.setText(sec.get("out_1", "0.0"))

    def _save_ini(self):
        cfg = configparser.ConfigParser()
        cfg.read(_CRL_INI)  # preserve other sections
        cfg["xy_preset"] = {
            "in_0": self._ui.lbl_xyxIn.text(),
            "in_1": self._ui.lbl_xyyIn.text(),
            "out_0": self._ui.lbl_xyxOut.text(),
            "out_1": self._ui.lbl_xyyOut.text(),
        }
        with open(_CRL_INI, "w") as f:
            cfg.write(f)

    def update_status(self):
        if not self._enabled:
            return

        def rd(w):
            try:
                return float(w.text())
            except ValueError:
                return None

        cur_x = rd(self._ui.findChild(QLabel, "lbl_pos_X"))
        cur_y = rd(self._ui.findChild(QLabel, "lbl_pos_Y"))
        in_x, in_y = rd(self._ui.lbl_xyxIn), rd(self._ui.lbl_xyyIn)
        out_x, out_y = rd(self._ui.lbl_xyxOut), rd(self._ui.lbl_xyyOut)

        if _near(cur_x, in_x, self.THRESH) and _near(cur_y, in_y, self.THRESH):
            self._ui.label_xyStatus.setText("In")
            self._ui.label_xyStatus.setStyleSheet("background-color: #00cc00; color: white;")
        elif _near(cur_x, out_x, self.THRESH) and _near(cur_y, out_y, self.THRESH):
            self._ui.label_xyStatus.setText("Out")
            self._ui.label_xyStatus.setStyleSheet("background-color: #cc0000; color: white;")
        else:
            self._ui.label_xyStatus.setText("----")
            self._ui.label_xyStatus.setStyleSheet("")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self._ui, "Export X/Y Positions", "crl3dprint_xy_positions.json", "JSON Files (*.json)"
        )
        if not path:
            return

        def f(lbl):
            try:
                return float(lbl.text())
            except ValueError:
                return None

        data = {
            "xy": {
                "in": {"X": f(self._ui.lbl_xyxIn), "Y": f(self._ui.lbl_xyyIn)},
                "out": {"X": f(self._ui.lbl_xyxOut), "Y": f(self._ui.lbl_xyyOut)},
            }
        }
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self._ui, "Import X/Y Positions", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.warning(self._ui, "Import Error", str(e))
            return

        xy = data.get("xy", {})

        def w(lbl, val):
            if val is not None:
                lbl.setText("%.4f" % val)

        w(self._ui.lbl_xyxIn, xy.get("in", {}).get("X"))
        w(self._ui.lbl_xyyIn, xy.get("in", {}).get("Y"))
        w(self._ui.lbl_xyxOut, xy.get("out", {}).get("X"))
        w(self._ui.lbl_xyyOut, xy.get("out", {}).get("Y"))
        self._save_ini()


class VelocitiesDialog(QDialog):
    """One row per motor, showing its current velocity and letting the user
    set a new one for any/all of them in a single dialog.

    Session-only: the caller is responsible for pushing accepted values to
    the controller (see CRL3DPrintControl._open_velocities_dialog) - nothing
    here touches the .ini file or is applied automatically at startup.
    """

    def __init__(self, parent, motor_slots, controller):
        super().__init__(parent)
        self.setWindowTitle("Change Velocities")
        self._edits = {}
        self._acc = {}
        self._values = {}

        layout = QVBoxLayout(self)
        grid = QGridLayout()
        layout.addLayout(grid)
        for row, slot in enumerate(motor_slots):
            name, unit = slot["name"], slot["unit"]
            vel, acc = controller.get_speed(name)
            self._acc[name] = acc
            lo, hi = VELOCITY_SOFT_LIMITS[name]

            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(QLabel(f"Current: {vel:g} {unit}/s"), row, 1)
            edit = QLineEdit(f"{vel:g}")
            grid.addWidget(edit, row, 2)
            grid.addWidget(QLabel(f"Range: {lo:g} to {hi:g} {unit}/s"), row, 3)
            self._edits[name] = edit

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        """Only close once every field is a number within its motor's
        VELOCITY_SOFT_LIMITS; otherwise flag the offending field(s) pale red
        and keep the dialog open, mirroring the move-to field's soft-limit
        check."""
        values = {}
        all_valid = True
        for name, edit in self._edits.items():
            lo, hi = VELOCITY_SOFT_LIMITS[name]
            try:
                val = float(edit.text())
            except ValueError:
                val = None
            if val is None or not (lo <= val <= hi):
                edit.setStyleSheet("background-color: #ffcccc;")
                all_valid = False
            else:
                edit.setStyleSheet("")
                values[name] = val
        if not all_valid:
            return
        self._values = values
        super().accept()

    def new_velocities(self):
        """name -> new velocity, for every motor (accept() only succeeds
        once all of them validate, so this always covers every motor)."""
        return dict(self._values)

    def acc_for(self, name):
        return self._acc[name]


class CalibrateReferenceDialog(QDialog):
    """Per-motor Calibrate / Reference controls, wired straight to the
    controller - unlike VelocitiesDialog, these calls happen live as each
    button is pressed rather than being staged for an OK click."""

    def __init__(self, parent, motor_slots, controller):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Calibrate & Reference")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Calibration only needed when cabling is changed"))

        grid = QGridLayout()
        layout.addLayout(grid)
        self._lights = {}
        self._buttons = []
        for row, slot in enumerate(motor_slots):
            name = slot["name"]
            grid.addWidget(QLabel(name), row, 0)

            btn_cal = QPushButton("Calibrate")
            btn_cal.clicked.connect(lambda checked=False, n=name: self._run(n, self.controller.calibrate))
            grid.addWidget(btn_cal, row, 1)
            self._buttons.append(btn_cal)

            btn_ref = QPushButton("Reference")
            btn_ref.clicked.connect(lambda checked=False, n=name: self._run(n, self.controller.find_reference))
            grid.addWidget(btn_ref, row, 2)
            self._buttons.append(btn_ref)

            light = QLabel()
            light.setFixedSize(16, 16)
            grid.addWidget(light, row, 3)
            self._lights[name] = light
            self._set_light(name, False)

        exit_btn = QPushButton("Exit")
        exit_btn.clicked.connect(self.accept)
        layout.addWidget(exit_btn)

    def _set_light(self, name, busy: bool):
        color = "#00cc00" if busy else "#999999"
        self._lights[name].setStyleSheet(f"background-color: {color}; border-radius: 8px;")

    def _run(self, name, func):
        """Disables every Calibrate/Reference button for the duration of a
        (blocking) calibrate/find_reference call, and lights up this row
        while it runs - re-enabling/graying out again once func returns,
        even if it raises."""
        for btn in self._buttons:
            btn.setEnabled(False)
        self._set_light(name, True)
        QApplication.processEvents()
        try:
            func(name)
        finally:
            self._set_light(name, False)
            for btn in self._buttons:
                btn.setEnabled(True)


class RemapJogDialog(QDialog):
    """Lets the user reassign which axis and direction each of a jog pad's
    4 directional buttons drives - e.g. to match physical stage
    mounting/wiring that doesn't line up with the on-screen arrows.

    Session-only until OK is pressed; the caller (see
    CRL3DPrintControl._open_remap_xy_dialog / _open_remap_tp_dialog) is
    responsible for applying the result to the live jog map and persisting
    it to the .ini."""

    def __init__(self, parent, title, button_prefix, axis_choices, current_map):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._combos = {}  # btn_name -> (axis_combo, sign_combo)

        layout = QVBoxLayout(self)
        grid = QGridLayout()
        layout.addLayout(grid)
        grid.addWidget(QLabel("Button"), 0, 0)
        grid.addWidget(QLabel("Axis"), 0, 1)
        grid.addWidget(QLabel("Direction"), 0, 2)

        for row, label in enumerate(JOG_BUTTON_LABELS, start=1):
            btn_name = f"{button_prefix}{label}"
            axis, sign = current_map[btn_name]
            grid.addWidget(QLabel(label.capitalize()), row, 0)

            axis_combo = QComboBox()
            axis_combo.addItems(axis_choices)
            axis_combo.setCurrentText(axis)
            grid.addWidget(axis_combo, row, 1)

            sign_combo = QComboBox()
            sign_combo.addItems(["+", "-"])
            sign_combo.setCurrentText("+" if sign >= 0 else "-")
            grid.addWidget(sign_combo, row, 2)

            self._combos[btn_name] = (axis_combo, sign_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def new_map(self):
        return {
            btn_name: (axis_combo.currentText(), 1 if sign_combo.currentText() == "+" else -1)
            for btn_name, (axis_combo, sign_combo) in self._combos.items()
        }


class CRL3DPrintControl(QObject):
    """Main controller for the CRL_3dprint GUI. self.ui (loaded from
    CRL_3dprint.ui) is the real visible window; this class only wires it up
    (QObject, not QMainWindow/QWidget, so no redundant blank window appears)."""

    MOTOR_PREC = "%0.4f"
    MOTOR_SLOTS = MOTOR_SLOTS

    def __init__(self, debug_mode=False):
        super().__init__()
        self.debug_mode = debug_mode
        self.lock = Lock()

        _ensure_default_ini(_CRL_INI)

        self.ui = uic.loadUi(os.path.join(_GUI_DIR, "CRL_3dprint.ui"))

        if self.debug_mode:
            from debug_stubs import DebugSmaractCRLController

            self.controller = DebugSmaractCRLController(motor_slots=_RAW_MOTOR_SLOTS)
        elif _HARDWARE_AVAILABLE:
            self.controller = CRLAxisController()
        else:
            raise RuntimeError("smaract.ctl SDK not available. Run with --debug_mode to use stubs.")
        self.controller.connect()

        self._xy_jog_map = _load_jog_map("jog_remap_xy", XY_JOG_MAP)
        self._tp_jog_map = _load_jog_map("jog_remap_tp", TILTPITCH_JOG_MAP)

        self._wire_motor_widgets()
        self._wire_jogpad(self.ui.frame_jogXY, "_xy_jog_map", "ed_xy_tweak", default_step=0.001)
        self._wire_jogpad(self.ui.frame_jogTiltPitch, "_tp_jog_map", "ed_tp_tweak", default_step=0.01)
        self._wire_jogpad_multipliers(self.ui.frame_jogXY, "ed_xy_tweak", "pb_xy_mult_low", "pb_xy_mult_high")
        self._wire_jogpad_multipliers(
            self.ui.frame_jogTiltPitch, "ed_tp_tweak", "pb_tp_mult_low", "pb_tp_mult_high"
        )
        self._wire_tweak_steps()
        self._wire_xray_eye()
        self._wire_font_size()
        self._wire_menu_actions(TOOLS_ACTIONS)
        self._wire_menu_actions(EDIT_ACTIONS)

        self.xy_preset = XYPresetBlock(self)
        self._scalar_scan_window = None

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_positions)
        self.timer.start(200)

        self.ui.pushButton_exit.clicked.connect(self.ui.close)
        self.ui.pushButton_scalarScan.clicked.connect(self._open_scalar_scan)
        self.ui.closeEvent = self._on_close_event

        # show() FIRST (matches optics_motors.py) so centralWidget's real,
        # post-layout size is what ProportionalResizer snapshots as its
        # baseline. Snapshotting before show() reads a not-yet-laid-out
        # size, which throws off every later rescale factor and can push
        # every widget far outside the visible window.
        self.ui.show()

        QApplication.processEvents()
        self._main_resizer = ProportionalResizer(self.ui.centralWidget())
        self.ui.setMaximumSize(16777215, 16777215)
        self.ui.centralWidget().setMaximumSize(16777215, 16777215)
        self.ui.setMinimumSize(
            int(self._main_resizer.orig_size.width() * 0.4),
            int(self._main_resizer.orig_size.height() * 0.4),
        )
        self._restore_window_geometry()
        self._main_resizer.rescale()

    # -- per-motor widget wiring ---------------------------------------------

    def _wire_motor_widgets(self):
        for slot in self.MOTOR_SLOTS:
            name = slot["name"]
            lbl_pos = self.ui.findChild(QLabel, f"lbl_pos_{name}")
            if lbl_pos:
                lbl_pos.setText(self.MOTOR_PREC % self.controller.get_pos(name))

            ed_moveto = self.ui.findChild(QLineEdit, f"edit_moveto_{name}")
            if ed_moveto:
                ed_moveto.returnPressed.connect(lambda n=name: self._move_to(n))

            btn_minus = self.ui.findChild(QPushButton, f"btn_tweak_{name}_minus")
            if btn_minus:
                btn_minus.clicked.connect(lambda checked=False, n=name: self._tweak(n, -1))

            btn_plus = self.ui.findChild(QPushButton, f"btn_tweak_{name}_plus")
            if btn_plus:
                btn_plus.clicked.connect(lambda checked=False, n=name: self._tweak(n, +1))

    def _move_to(self, name):
        ed = self.ui.findChild(QLineEdit, f"edit_moveto_{name}")
        try:
            val = float(ed.text())
        except ValueError:
            return
        self._dispatch_move(name, val, relative=False)

    def _tweak(self, name, sign):
        ed = self.ui.findChild(QLineEdit, f"edit_tweak_{name}")
        try:
            step = float(ed.text()) if ed else 0.0
        except ValueError:
            step = 0.0
        self._dispatch_move(name, sign * step, relative=True)

    # -- soft limits ------------------------------------------------------------

    def _flag_invalid(self, name, invalid: bool):
        """Turn the motor's move-to field pale red when a move was rejected
        for being outside SOFT_LIMITS; clear it on the next valid move."""
        ed = self.ui.findChild(QLineEdit, f"edit_moveto_{name}")
        if ed:
            ed.setStyleSheet("background-color: #ffcccc;" if invalid else "")

    def _dispatch_move(self, name, value, relative: bool):
        """Single choke point for every move path (move-to, tweak, jog):
        checks the intended absolute target against SOFT_LIMITS and only
        forwards the move to the controller if it's in range."""
        if relative:
            with self.lock:
                target = self.controller.get_pos(name) + value
        else:
            target = value

        lo, hi = SOFT_LIMITS[name]
        if not (lo <= target <= hi):
            self._flag_invalid(name, True)
            return
        self._flag_invalid(name, False)

        with self.lock:
            if relative:
                self.controller.mvr(name, value, wait=False)
            else:
                self.controller.mv(name, value, wait=False)

    # -- jog pads (X/Y and Tilt/Pitch share the same wiring shape) -----------

    def _wire_jogpad(self, group_box, jog_map_attr, tweak_widget_name, default_step):
        """jog_map_attr names a self.* dict attribute (e.g. self._xy_jog_map)
        rather than taking the map directly, so that remapping it later (see
        _open_remap_xy_dialog / _open_remap_tp_dialog) takes effect without
        needing to reconnect these signals."""
        for btn_name in getattr(self, jog_map_attr):
            btn = group_box.findChild(QPushButton, btn_name)
            if btn:
                btn.clicked.connect(
                    lambda checked=False, attr=jog_map_attr, b=btn_name: self._jog_step_by_button(
                        attr, b, tweak_widget_name, default_step
                    )
                )

    def _jog_step_by_button(self, jog_map_attr, btn_name, tweak_widget_name, default_step):
        axis, sign = getattr(self, jog_map_attr)[btn_name]
        self._jog_step(axis, sign, tweak_widget_name, default_step)

    def _jog_step(self, axis, sign, tweak_widget_name, default_step):
        ed = self.ui.findChild(QLineEdit, tweak_widget_name)
        try:
            step = float(ed.text()) if ed and ed.text() else default_step
        except ValueError:
            step = default_step
        self._dispatch_move(axis, sign * step, relative=True)

    def _wire_jogpad_multipliers(self, group_box, tweak_widget_name, low_btn_name, high_btn_name):
        btn_low = group_box.findChild(QPushButton, low_btn_name)
        btn_high = group_box.findChild(QPushButton, high_btn_name)
        if btn_low:
            btn_low.clicked.connect(lambda: self._multiply_tweak(tweak_widget_name, 0.1))
        if btn_high:
            btn_high.clicked.connect(lambda: self._multiply_tweak(tweak_widget_name, 10))

    def _multiply_tweak(self, tweak_widget_name, factor):
        ed = self.ui.findChild(QLineEdit, tweak_widget_name)
        if not ed:
            return
        try:
            val = float(ed.text())
        except ValueError:
            return
        ed.setText(f"{val * factor:g}")
        key = PAD_STEP_INI_KEYS.get(tweak_widget_name)
        if key:
            self._save_ini_value("tweak_steps", key, ed.text())

    # -- tweak/pad step persistence --------------------------------------------

    def _wire_tweak_steps(self):
        """Load persisted tweak/jog step sizes from the .ini (falling back to
        the .ui's own default text), and save back to the .ini whenever the
        user edits a tweak or pad step field."""
        cfg = configparser.ConfigParser()
        cfg.read(_CRL_INI)
        sec = cfg["tweak_steps"] if cfg.has_section("tweak_steps") else {}

        def _wire(ed, key):
            if not ed:
                return
            ed.setText(sec.get(key, ed.text()))
            ed.editingFinished.connect(lambda e=ed, k=key: self._save_ini_value("tweak_steps", k, e.text()))

        for slot in self.MOTOR_SLOTS:
            name = slot["name"]
            _wire(self.ui.findChild(QLineEdit, f"edit_tweak_{name}"), name)

        for widget_name, key in PAD_STEP_INI_KEYS.items():
            _wire(self.ui.findChild(QLineEdit, widget_name), key)

    # -- X-ray eye ------------------------------------------------------------

    def _pv_class(self):
        if self.debug_mode:
            from debug_stubs import FakePV

            return FakePV
        return PV

    def _wire_xray_eye(self):
        # Shared with the optics GUI, the main panel and the sample alignment
        # window through the command PV's readback -- see gui/xray_eye.py.
        # This used to read the status PV with the opposite polarity to
        # optics_motors, so the two GUIs showed contradictory states.
        from xray_eye import XrayEye

        self.xray_eye = XrayEye(self._pv_class(), debug=self.debug_mode)
        self._refresh_xrayeye_buttons()
        self.ui.pushButton_xrayEyeIn.clicked.connect(self.put_xrayeye_in)
        self.ui.pushButton_xrayEyeOut.clicked.connect(self.put_xrayeye_out)

    def _refresh_xrayeye_buttons(self):
        """Offer whichever direction the eye is not already in; both stay
        available while the state is unknown."""
        state = self.xray_eye.is_in()
        self.ui.pushButton_xrayEyeIn.setEnabled(state is not True)
        self.ui.pushButton_xrayEyeOut.setEnabled(state is not False)

    def put_xrayeye_in(self):
        self._put_xrayeye(True)

    def put_xrayeye_out(self):
        self._put_xrayeye(False)

    def _put_xrayeye(self, ins: bool):
        self.xray_eye.set_in(ins)
        self._refresh_xrayeye_buttons()

    # -- font size (own .ini, not the shared QSettings key) -------------------

    def _wire_font_size(self):
        cfg = configparser.ConfigParser()
        cfg.read(_CRL_INI)
        size = cfg.getint("ui", "font_size", fallback=DEFAULT_FONT_SIZE)
        self.ui.spinBox_fontSize.setValue(size)

        def _on_font_size_changed(new_size):
            apply_font_size_to_tree(self.ui, new_size)
            self._save_ini_value("ui", "font_size", str(new_size))

        self.ui.spinBox_fontSize.valueChanged.connect(_on_font_size_changed)
        QTimer.singleShot(0, lambda: apply_font_size_to_tree(self.ui, size))

    # -- menu wiring ----------------------------------------------------------

    def _wire_menu_actions(self, actions):
        for action_name, method_name in actions.items():
            action = self.ui.findChild(QAction, action_name)
            if action:
                action.triggered.connect(getattr(self, method_name))

    # -- Tools menu: velocities (session-only) & calibrate/reference --------

    def _open_velocities_dialog(self):
        dlg = VelocitiesDialog(self.ui, self.MOTOR_SLOTS, self.controller)
        if dlg.exec_() == QDialog.Accepted:
            for name, new_vel in dlg.new_velocities().items():
                self.controller.set_speed(name, vel=new_vel, acc=dlg.acc_for(name))

    def _open_calibrate_dialog(self):
        dlg = CalibrateReferenceDialog(self.ui, self.MOTOR_SLOTS, self.controller)
        dlg.exec_()

    # -- Edit menu: remap which axis/direction each jog button drives -------

    def _open_remap_xy_dialog(self):
        dlg = RemapJogDialog(self.ui, "Remap XY Jog", "pb_xy_", ["X", "Y"], self._xy_jog_map)
        if dlg.exec_() == QDialog.Accepted:
            self._xy_jog_map = dlg.new_map()
            _save_jog_map("jog_remap_xy", self._xy_jog_map)

    def _open_remap_tp_dialog(self):
        dlg = RemapJogDialog(self.ui, "Remap Tilt Pitch Jog", "pb_tp_", ["TILT", "PITCH"], self._tp_jog_map)
        if dlg.exec_() == QDialog.Accepted:
            self._tp_jog_map = dlg.new_map()
            _save_jog_map("jog_remap_tp", self._tp_jog_map)

    # -- .ini persistence -------------------------------------------------------

    def _save_ini_value(self, section, key, value):
        cfg = configparser.ConfigParser()
        cfg.read(_CRL_INI)
        if section not in cfg:
            cfg[section] = {}
        cfg[section][key] = value
        with open(_CRL_INI, "w") as f:
            cfg.write(f)

    def _restore_window_geometry(self):
        cfg = configparser.ConfigParser()
        cfg.read(_CRL_INI)
        hexstr = cfg.get("ui", "window_geometry", fallback="").strip()
        if hexstr:
            self.ui.restoreGeometry(QByteArray.fromHex(hexstr.encode()))

    def _on_close_event(self, event):
        if self._scalar_scan_window is not None:
            self._scalar_scan_window.win.close()
        self._save_ini_value("ui", "window_geometry", bytes(self.ui.saveGeometry().toHex()).decode())
        event.accept()

    # -- scalar scan window -------------------------------------------------

    def _open_scalar_scan(self):
        if self._scalar_scan_window is None:
            self._scalar_scan_window = ScalarScanWindow(
                self.ui, self.controller, self._pv_class, self.lock, SOFT_LIMITS
            )
        else:
            self._scalar_scan_window.win.show()
            self._scalar_scan_window.win.raise_()
            self._scalar_scan_window.win.activateWindow()

    # -- periodic update --------------------------------------------------------

    def update_positions(self):
        for slot in self.MOTOR_SLOTS:
            name = slot["name"]
            with self.lock:
                val = self.controller.get_pos(name)
            lbl = self.ui.findChild(QLabel, f"lbl_pos_{name}")
            if lbl:
                lbl.setText(self.MOTOR_PREC % val)
        self.xy_preset.update_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug_mode",
        action="store_true",
        help="Run without connecting to the SmarAct MCS2 controller or EPICS PVs",
    )
    args, _ = parser.parse_known_args()  # parse_known_args so Qt args pass through

    app = QApplication(sys.argv)
    panel = CRL3DPrintControl(debug_mode=args.debug_mode)  # noqa: F841 - keeps GUI alive
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
