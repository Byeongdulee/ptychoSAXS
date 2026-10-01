"""Simplified optics motor panel.

Everything hardware-specific lives in the three tables at the top of this
file -- MOTORS, AXIS_BLOCKS and SINGLE_ROWS. Remapping which physical axis a
dpad or a row drives is a one-line edit there; no widget name, .ini key or
signal connection needs to change with it.

The full-featured original panel is kept alongside as optics_motors_full.py.
Layout for both lives in gui/ui/.
"""

from PyQt5 import uic, QtCore
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtCore import QObject, QSettings, QTimer
from threading import Lock
from collections import OrderedDict
from datetime import datetime
import argparse
import configparser
import csv
import json
import os
import sys

from font_utils import apply_font_size_to_tree, apply_saved_font_size, DEFAULT_FONT_SIZE
from ini_utils import INI_DIR, ensure_ini_defaults
from resize_utils import ProportionalResizer
from xray_eye import EYE_CMD_PV, XrayEye

_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(_GUI_DIR, "ui")
_OPTICS_INI = os.path.join(INI_DIR, "optics_motors.ini")

DEFAULT_SNAPSHOT_NAME = "ZP_optics_log.csv"

try:
    from epics import PV
except ImportError:
    PV = None  # replaced by FakePV in debug mode

_repo_root = os.path.dirname(_GUI_DIR)
sys.path.append(os.path.join(_repo_root, "debug"))
if _repo_root not in sys.path:
    sys.path.append(_repo_root)

try:
    from ptychosaxs.optics import opticsbox, OSA, camera, beamstop

    MotorControlAvailable = True
except Exception:
    MotorControlAvailable = False
    print("Optics motor control is NOT available.")


# ---------------------------------------------------------------------------
# Hardware map -- the only place motors are named.
#
# Each entry is display_name -> (controller, index of the axis within that
# controller's PV list). The controllers come from ptychosaxs.optics:
#   opticsbox  12idc:m10, m11, m12, m13, m14
#   OSA        12idc:m9, m15, m16
#   camera     12idc:m2
#   beamstop   12ideSFT:m4, m5
#
# The CL slits are deliberately absent: this panel neither drives nor stops
# them, so Stop All must leave them alone.
# ---------------------------------------------------------------------------
MOTORS = OrderedDict([
    ("BS_ver",      ("opticsbox", 0)),
    ("BS_hor",      ("opticsbox", 1)),
    ("ZP_ver",      ("opticsbox", 2)),
    ("ZP_hor",      ("opticsbox", 3)),
    ("BSZP_Ztrans", ("opticsbox", 4)),
    ("OSA_X",       ("OSA", 0)),
    ("OSA_Z",       ("OSA", 1)),
    ("OSA_Y",       ("OSA", 2)),
    ("SAXS_Z",      ("camera", 0)),
    ("SAXSBS_hor",  ("beamstop", 0)),
    ("SAXSBS_ver",  ("beamstop", 1)),
])

# PV record base for each motor, for tooltips only -- nothing here is used to
# talk to hardware (that goes through MOTORS/the controllers above). Must be
# kept in step with the pvlist order in ptychosaxs/optics.py's opticsbox,
# OSA, camera and beamstop classes. Readback reads "<base>.RBV"; Move to
# writes "<base>.VAL" (ptychosaxs/epicsmotor.py's get_pos/mv).
PV_BASES = {
    "BS_ver":      "12idc:m10",
    "BS_hor":      "12idc:m11",
    "ZP_ver":      "12idc:m12",
    "ZP_hor":      "12idc:m13",
    "BSZP_Ztrans": "12idc:m14",
    "OSA_X":       "12idc:m9",
    "OSA_Z":       "12idc:m15",
    "OSA_Y":       "12idc:m16",
    "SAXS_Z":      "12idc:m2",
    "SAXSBS_hor":  "12ideSFT:m4",
    "SAXSBS_ver":  "12ideSFT:m5",
}


class AxisBlock:
    """One dpad + in/out registry column.

    `prefix` selects the widget family in optics_motors.ui (pb_<prefix>_up,
    lbl_<prefix>_in_0, ...). `section` is the optics_motors.ini section the
    saved positions go in -- changing `prefix` without changing `section`
    keeps an existing installation's saved positions.
    """

    def __init__(self, prefix, title, ver, hor, section, has_list=False):
        self.prefix = prefix
        self.title = title
        self.ver = ver
        self.hor = hor
        self.section = section
        self.has_list = has_list

    @property
    def motors(self):
        return [self.ver, self.hor]


AXIS_BLOCKS = OrderedDict((b.prefix, b) for b in [
    AxisBlock("bs",     "BS",      ver="BS_ver",     hor="BS_hor",     section="bs"),
    AxisBlock("zp",     "ZP",      ver="ZP_ver",     hor="ZP_hor",     section="zp",
              has_list=True),
    AxisBlock("osa",    "OSA",     ver="OSA_Y",      hor="OSA_X",      section="osa"),
    AxisBlock("saxsbs", "SAXS BS", ver="SAXSBS_ver", hor="SAXSBS_hor", section="SAXSbs"),
])

# Single-axis rows: widget prefix -> motor. "ztrans" additionally owns a
# one-value position registry (frame_ztrans_reg + listWidget_ztransList).
SINGLE_ROWS = OrderedDict([
    ("osaz",   "OSA_Z"),
    ("ztrans", "BSZP_Ztrans"),
    ("saxsz",  "SAXS_Z"),
])

# Blocks All In / All Out drives. The SAXS beamstop only joins when the Edit
# menu says so -- All Out retracting a beamstop an experiment relies on is a
# worse surprise than having to move it by hand.
ALL_IN_OUT_BLOCKS = ["osa", "bs", "zp"]
OPTIONAL_ALL_BLOCK = "saxsbs"

_DEFAULT_ZP_POSITIONS = {"test position 1": [None, None]}
_DEFAULT_ZTRANS_POSITIONS = {"test position 1": [None]}


def _position_defaults():
    """{section: {key: default}} for every block's saved in/out positions.

    In/out positions default to empty rather than 0: a blank label means "no
    saved position", while a 0 would advertise the origin as a real in/out
    target for the Move buttons.
    """
    defaults = {}
    for block in AXIS_BLOCKS.values():
        entries = {"out_0": "", "out_1": ""}
        if block.has_list:
            entries["positions"] = json.dumps(_DEFAULT_ZP_POSITIONS)
        else:
            entries["in_0"] = ""
            entries["in_1"] = ""
        defaults[block.section] = entries
    return defaults


INI_DEFAULTS = dict(
    _position_defaults(),
    ztrans={"out_0": "", "positions": json.dumps(_DEFAULT_ZTRANS_POSITIONS)},
    invert={"%s_%s" % (p, axis): "0"
            for p in AXIS_BLOCKS for axis in ("ver", "hor")},
    options={"saxsbs_in_all": "0"},
)


def ensure_default_ini(path=_OPTICS_INI):
    """Create optics_motors.ini from INI_DEFAULTS if it does not exist, and
    add any individual entry missing from an existing file."""
    ensure_ini_defaults(path, INI_DEFAULTS)


def read_saxsbs_in_all(path=_OPTICS_INI):
    """Whether All In / All Out should drive the SAXS beamstop too."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path)
    except (configparser.Error, OSError):
        return False
    return cfg.getboolean("options", "saxsbs_in_all", fallback=False)


def write_saxsbs_in_all(enabled, path=_OPTICS_INI):
    _write_ini_value("options", "saxsbs_in_all", "1" if enabled else "0", path)


def _write_ini_value(section, key, value, path=_OPTICS_INI):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg[section][key] = value
    with open(path, "w") as f:
        cfg.write(f)


class _SliderToggle(QObject):
    """Makes a 2-state QSlider flip on any click instead of seeking."""

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.MouseButtonPress:
            obj.setValue(1 - obj.value())
            return True  # consume -- suppress Qt's own seek behaviour
        return False


class PresetBlock:
    """Enable toggle plus In/Out recall for one group of motors.

    Every widget it touches is named after `prefix`, so a block needs no
    per-widget configuration: pb_<p>_enable / _in / _out, slider_<p>_moveSet,
    lbl_<p>_status, frame_<p>_reg, and lbl_<p>_in_<i> / lbl_<p>_out_<i> for
    each motor.

    With `has_list`, the In side is a named set of positions (listWidget_
    <p>List) rather than a single saved value, and the In labels mirror
    whichever entry is selected.
    """

    THRESH = 0.005  # position comparison tolerance
    DISABLED_TEXT_COLOR = "#606060"
    # Clearing the status pill back to "no colour" still has to say
    # transparent, or the column stylesheet's grey shows instead of the
    # registry's pale yellow.
    NEUTRAL_STYLE = "background-color: transparent;"

    def __init__(self, panel, prefix, section, motors, has_list=False,
                 default_positions=None, name_indicator=False):
        self.panel = panel
        self.ui = panel.ui
        self.prefix = prefix
        self.section = section
        self.motors = list(motors)
        self.has_list = has_list
        self.positions = {k: list(v) for k, v in (default_positions or {}).items()}
        self.enabled = False
        self.name_indicator = name_indicator

        # True stored positions, always in mm regardless of the display unit
        # setting -- the labels only ever show a formatted view of these.
        self._in_values = [None] * len(self.motors)
        self._out_values = [None] * len(self.motors)

        self._list = self.ui.findChild(QWidget, "listWidget_%sList" % prefix)
        self._connect()
        self._load_ini()
        self.refresh_display()
        self._apply_enabled(False)
        if self.has_list:
            self._populate_list()

    # -- widget helpers ----------------------------------------------------

    def _w(self, cls, name):
        return self.ui.findChild(cls, name)

    def _in_label(self, i):
        return self._w(QLabel, "lbl_%s_in_%d" % (self.prefix, i))

    def _out_label(self, i):
        return self._w(QLabel, "lbl_%s_out_%d" % (self.prefix, i))

    # -- setup -------------------------------------------------------------

    def _connect(self):
        p = self.prefix
        self._w(QPushButton, "pb_%s_enable" % p).clicked.connect(self._toggle)
        self._w(QPushButton, "pb_%s_in" % p).clicked.connect(self.on_in)
        self._w(QPushButton, "pb_%s_out" % p).clicked.connect(self.on_out)
        slider = self._w(QSlider, "slider_%s_moveSet" % p)
        slider.installEventFilter(_SliderToggle(slider))
        if self.has_list:
            self._list.setSelectionMode(QAbstractItemView.SingleSelection)
            self._list.currentItemChanged.connect(self._on_selection_changed)
            self._w(QPushButton, "pb_%s_addPos" % p).clicked.connect(self._edit_positions)

    def _apply_enabled(self, enabled):
        p = self.prefix
        for cls, name in [(QPushButton, "pb_%s_in" % p),
                          (QPushButton, "pb_%s_out" % p),
                          (QSlider, "slider_%s_moveSet" % p),
                          (QLabel, "lbl_%s_status" % p)]:
            self._w(cls, name).setEnabled(enabled)

        btn = self._w(QPushButton, "pb_%s_enable" % p)
        btn.setText("Yes" if enabled else "No")
        btn.setStyleSheet("background-color: %s;"
                          % ("#ccffcc" if enabled else "#ffcccc"))

        # Grey the whole registry out, status pill excepted -- it keeps its
        # own In/Out colouring. The explicit transparent background overrides
        # the grey the enclosing column's stylesheet cascades onto every
        # child, so the registry's pale yellow shows through.
        status = self._w(QLabel, "lbl_%s_status" % p)
        colour = "black" if enabled else self.DISABLED_TEXT_COLOR
        for lbl in self._w(QWidget, "frame_%s_reg" % p).findChildren(QLabel):
            if lbl is not status:
                lbl.setStyleSheet(
                    "color: %s; background-color: transparent;" % colour)

        self.enabled = enabled
        self.panel.update_all_buttons()

    def _toggle(self):
        self._apply_enabled(not self.enabled)

    def _move_set_mode(self):
        """0 = the In/Out buttons move motors, 1 = they save positions."""
        return self._w(QSlider, "slider_%s_moveSet" % self.prefix).value()

    # -- In / Out ----------------------------------------------------------

    def will_move_on_out(self):
        """True when Out moves motors rather than saving their positions."""
        return self._move_set_mode() == 0

    def on_in(self):
        if self._move_set_mode() == 0:
            for motor, value in zip(self.motors, self._stored("in")):
                if value is not None:
                    self.panel.move_abs(motor, value)
        elif self.has_list:
            name = self._selected_name()
            if name:
                self.positions[name] = [self.panel.read(m) for m in self.motors]
                self._show_position(name)
                self._save_ini()
        else:
            self._capture("in")

    def on_out(self):
        # Only a real move needs the eye guard -- saving a position moves
        # nothing and must not interrogate the operator.
        if self.will_move_on_out():
            if self.panel.is_xrayeye_out() and not self.panel.confirm_xrayeye_guard():
                return
            for motor, value in zip(self.motors, self._stored("out")):
                if value is not None:
                    self.panel.move_abs(motor, value)
        else:
            self._capture("out")

    def _capture(self, kind):
        """Copy the live readbacks into this block's saved `kind` positions."""
        values = self._in_values if kind == "in" else self._out_values
        for i, motor in enumerate(self.motors):
            values[i] = self.panel.read(motor)
        self.refresh_display()
        self._save_ini()

    def _stored(self, kind):
        """The saved `kind` positions (mm), one per motor, None where unset."""
        return list(self._in_values if kind == "in" else self._out_values)

    # -- display -------------------------------------------------------

    def refresh_display(self):
        """Redraw the in/out labels from the stored mm values, in whichever
        unit the panel is currently showing positions in."""
        fmt = self.panel._fmt_pos
        if not self.has_list:
            for i in range(len(self.motors)):
                self._in_label(i).setText(fmt(self._in_values[i]))
        for i in range(len(self.motors)):
            self._out_label(i).setText(fmt(self._out_values[i]))
        if self.has_list:
            name = self._selected_name()
            if name:
                self._show_position(name)

    # -- named position list ----------------------------------------------

    def _selected_name(self):
        item = self._list.currentItem()
        return item.text() if item else None

    def _populate_list(self, select=None):
        names = list(self.positions)
        self._list.blockSignals(True)
        self._list.clear()
        self._list.addItems(names)
        self._list.blockSignals(False)
        if names:
            row = names.index(select) if select in names else 0
            self._list.setCurrentRow(row)
            self._show_position(names[row])

    def _on_selection_changed(self, current, _previous):
        if current is not None:
            self._show_position(current.text())

    def _show_position(self, name):
        """Push a stored named position into the In labels (display only)."""
        for i, value in enumerate(self.positions.get(name, [])):
            self._in_label(i).setText(self.panel._fmt_pos(value))

    def _edit_positions(self):
        choice = _choice_dialog(self.ui, "Positions", ["Add", "Remove"])
        if choice == "Add":
            name, ok = QInputDialog.getText(self.ui, "Add position", "Position name:")
            name = name.strip() if ok else ""
            if not name:
                return
            self.positions.setdefault(name, [None] * len(self.motors))
        elif choice == "Remove":
            if not self.positions:
                return
            name = _choice_dialog(self.ui, "Remove position", list(self.positions),
                                  combo=True)
            if name is None:
                return
            self.positions.pop(name, None)
            name = None
        else:
            return
        self._populate_list(select=name)
        self._save_ini()

    # -- persistence -------------------------------------------------------

    def _load_ini(self):
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)
        if not cfg.has_section(self.section):
            return
        section = cfg[self.section]
        for i in range(len(self.motors)):
            if not self.has_list:
                value = section.get("in_%d" % i, "").strip()
                self._in_values[i] = float(value) if value else None
            value = section.get("out_%d" % i, "").strip()
            self._out_values[i] = float(value) if value else None
        if self.has_list:
            try:
                self.positions = json.loads(section.get("positions", "") or "{}")
            except ValueError:
                pass  # keep the defaults on a corrupt entry

    def _save_ini(self):
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)  # preserve the other sections
        if not cfg.has_section(self.section):
            cfg.add_section(self.section)
        for i in range(len(self.motors)):
            if not self.has_list:
                v = self._in_values[i]
                cfg[self.section]["in_%d" % i] = "" if v is None else "%.6f" % v
            v = self._out_values[i]
            cfg[self.section]["out_%d" % i] = "" if v is None else "%.6f" % v
        if self.has_list:
            cfg[self.section]["positions"] = json.dumps(self.positions)
        with open(_OPTICS_INI, "w") as f:
            cfg.write(f)

    # -- status ------------------------------------------------------------

    def update_status(self, readbacks):
        if not self.enabled:
            return
        label = self._w(QLabel, "lbl_%s_status" % self.prefix)
        live = [readbacks[m] for m in self.motors]

        def matches(saved):
            return all(s is not None and abs(c - s) <= self.THRESH
                       for c, s in zip(live, saved))

        if self.name_indicator:
            for name, saved in self.positions.items():
                if matches(saved):
                    label.setText(name if len(name) <= 5 else name[:4] + "…")
                    label.setStyleSheet(self.NEUTRAL_STYLE)
                    return
            label.setText("---")
            label.setStyleSheet(self.NEUTRAL_STYLE)
            return

        if matches(self._stored("in")):
            label.setText("In")
            label.setStyleSheet("background-color: #00cc00; color: white;")
        elif matches(self._stored("out")):
            label.setText("Out")
            label.setStyleSheet("background-color: #cc0000; color: white;")
        else:
            label.setText("----")
            label.setStyleSheet(self.NEUTRAL_STYLE)

    def status_text(self):
        return self._w(QLabel, "lbl_%s_status" % self.prefix).text()


def _choice_dialog(parent, title, options, combo=False):
    """Pick one of `options`. Returns the chosen string, or None if cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    layout = QVBoxLayout(dlg)
    picked = [None]

    if combo:
        box = QComboBox()
        box.addItems(options)
        layout.addWidget(box)
        row = QHBoxLayout()
        ok, cancel = QPushButton("OK"), QPushButton("Cancel")
        row.addWidget(ok)
        row.addWidget(cancel)
        layout.addLayout(row)
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return box.currentText()

    for option in options:
        button = QPushButton(option)
        layout.addWidget(button)
        button.clicked.connect(
            lambda _checked=False, o=option: (picked.__setitem__(0, o), dlg.accept()))
    dlg.exec_()
    return picked[0]


class motor_control(QMainWindow):
    """The optics panel window."""

    # Positions are in mm internally and here, at 1 um resolution -- this is
    # also what every saved/exported mm value is formatted with.
    PREC = "%0.3f"
    UNITS_SETTINGS_KEY = "opticsMotors/unitsUm"
    MM_PER_UM = 0.001

    def __init__(self, debug_mode=False):
        super(motor_control, self).__init__()
        self.debug_mode = debug_mode
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        ensure_default_ini()
        self.ui = uic.loadUi(os.path.join(UI_DIR, "optics_motors.ui"))
        self.lock = Lock()

        self.control = self._build_controllers()
        self.invert = self._load_inverts()
        self._units_um = bool(QSettings("ptychoSAXS", "ptychoSAXS")
                              .value(self.UNITS_SETTINGS_KEY, False, type=bool))

        self._build_blocks()
        self._connect_motor_widgets()
        self._connect_menus()
        self._connect_buttons()

        if debug_mode:
            from debug_stubs import FakePV as _PV
        else:
            _PV = PV
        self.xray_eye = XrayEye(_PV, debug=debug_mode)
        self._refresh_xrayeye_ui()
        self.update_all_buttons()

        self.timer = QTimer()
        self.timer.timeout.connect(self.updatepos)
        self.timer.start(100)
        self._restore_window()

    # -- hardware ----------------------------------------------------------

    def _build_controllers(self):
        if self.debug_mode:
            from debug_stubs import (
                DebugBeamstop, DebugCamera, DebugOpticsbox, DebugOSA)
            return {"opticsbox": DebugOpticsbox(), "OSA": DebugOSA(),
                    "camera": DebugCamera(), "beamstop": DebugBeamstop()}
        if not MotorControlAvailable:
            raise RuntimeError(
                "Motor control hardware (optics) is not available. "
                "Run with --debug_mode to use stubs.")
        return {"opticsbox": opticsbox(), "OSA": OSA(), "camera": camera(),
                "beamstop": beamstop()}

    def _axis(self, motor):
        controller_name, index = MOTORS[motor]
        controller = self.control[controller_name]
        return controller, controller.motornames[index]

    def read(self, motor):
        controller, axis = self._axis(motor)
        with self.lock:
            return float(controller.get_pos(axis))

    def move_abs(self, motor, value):
        controller, axis = self._axis(motor)
        with self.lock:
            controller.mv(axis, value, wait=False)

    def move_rel(self, motor, delta):
        controller, axis = self._axis(motor)
        with self.lock:
            controller.mvr(axis, delta, wait=False)

    # -- display units -------------------------------------------------

    def _fmt_pos(self, value_mm):
        """Format an internal mm position for display in the current unit.

        um mode shows integers only -- 1 um is the display resolution limit,
        so a fractional um would be false precision.
        """
        if value_mm is None:
            return ""
        if self._units_um:
            return "%d" % round(value_mm / self.MM_PER_UM)
        return self.PREC % value_mm

    def _parse_pos(self, text):
        """Inverse of _fmt_pos: user-typed text in the current display unit
        to an internal mm float. Raises ValueError on bad input."""
        value = float(text)
        return value * self.MM_PER_UM if self._units_um else value

    # -- construction ------------------------------------------------------

    def _build_blocks(self):
        self.blocks = OrderedDict()
        for prefix, spec in AXIS_BLOCKS.items():
            self.blocks[prefix] = PresetBlock(
                self, prefix, spec.section, spec.motors, has_list=spec.has_list,
                default_positions=_DEFAULT_ZP_POSITIONS if spec.has_list else None)
        self.blocks["ztrans"] = PresetBlock(
            self, "ztrans", "ztrans", [SINGLE_ROWS["ztrans"]], has_list=True,
            default_positions=_DEFAULT_ZTRANS_POSITIONS, name_indicator=True)

    def _connect_motor_widgets(self):
        """Label, move-to box and nudge buttons for every mapped motor."""
        for prefix, spec in AXIS_BLOCKS.items():
            for axis, motor in (("ver", spec.ver), ("hor", spec.hor)):
                self._connect_move_to("ed_mv_%s_%s" % (prefix, axis), motor)
                self._set_readback_tooltip("lbl_rb_%s_%s" % (prefix, axis), motor)
            self.ui.findChild(QLabel, "lbl_%s_title" % prefix).setText(spec.title)
            self._connect_dpad(prefix, spec)

        for prefix, motor in SINGLE_ROWS.items():
            self.ui.findChild(QLabel, "lbl_name_%s" % prefix).setText(motor)
            self._connect_move_to("ed_mv_%s" % prefix, motor)
            self._set_readback_tooltip("lbl_rb_%s" % prefix, motor)
            step_box = "ed_%s_step" % prefix
            self.ui.findChild(QPushButton, "pb_%s_minus" % prefix).clicked.connect(
                lambda _c=False, m=motor, s=step_box: self.move_rel(m, -self._step(s)))
            self.ui.findChild(QPushButton, "pb_%s_plus" % prefix).clicked.connect(
                lambda _c=False, m=motor, s=step_box: self.move_rel(m, self._step(s)))

    def _set_readback_tooltip(self, widget_name, motor):
        self.ui.findChild(QLabel, widget_name).setToolTip(
            "Readback: %s.RBV" % PV_BASES[motor])

    def _connect_move_to(self, widget_name, motor):
        edit = self.ui.findChild(QLineEdit, widget_name)
        edit.setToolTip("Move to: %s.VAL" % PV_BASES[motor])
        edit.returnPressed.connect(
            lambda e=edit, m=motor: self._move_to_typed(e, m))

    def _move_to_typed(self, edit, motor):
        try:
            target = self._parse_pos(edit.text())
        except ValueError:
            QMessageBox.warning(self.ui, "Move",
                                "%r is not a number." % edit.text())
            return
        self.move_abs(motor, target)

    def _connect_dpad(self, prefix, spec):
        step_box = "ed_%s_step" % prefix
        for button, axis, motor, sign in [
                ("up", "ver", spec.ver, +1), ("down", "ver", spec.ver, -1),
                ("right", "hor", spec.hor, +1), ("left", "hor", spec.hor, -1)]:
            self.ui.findChild(QPushButton, "pb_%s_%s" % (prefix, button)).clicked.connect(
                lambda _c=False, p=prefix, a=axis, m=motor, s=sign, box=step_box:
                self.move_rel(m, s * self._direction(p, a) * self._step(box)))

        for suffix, factor in (("stepDown", 0.1), ("stepUp", 10.0)):
            self.ui.findChild(QPushButton, "pb_%s_%s" % (prefix, suffix)).clicked.connect(
                lambda _c=False, box=step_box, f=factor: self._scale_step(box, f))

    def _step(self, widget_name):
        """The dpad/tweak step in mm, 0 when the box does not hold a number."""
        try:
            return float(self.ui.findChild(QLineEdit, widget_name).text())
        except ValueError:
            return 0.0

    def _scale_step(self, widget_name, factor):
        edit = self.ui.findChild(QLineEdit, widget_name)
        try:
            value = float(edit.text())
        except ValueError:
            return
        edit.setText(("%g" % (value * factor)))

    def _direction(self, prefix, axis):
        return -1 if self.invert[(prefix, axis)] else 1

    def _load_inverts(self):
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)
        return {(p, axis): cfg.getboolean("invert", "%s_%s" % (p, axis), fallback=False)
                for p in AXIS_BLOCKS for axis in ("ver", "hor")}

    def _set_invert(self, prefix, axis, inverted):
        self.invert[(prefix, axis)] = inverted
        _write_ini_value("invert", "%s_%s" % (prefix, axis), "1" if inverted else "0")

    # -- display units -------------------------------------------------

    def _toggle_units(self):
        self._units_um = not self._units_um
        QSettings("ptychoSAXS", "ptychoSAXS").setValue(
            self.UNITS_SETTINGS_KEY, self._units_um)
        self._update_units_display()
        for block in self.blocks.values():
            block.refresh_display()

    def _update_units_display(self):
        if self._units_um:
            self.ui.lbl_pos_units.setText("All positions in um")
            self._units_action.setText("Change units to mm")
        else:
            self.ui.lbl_pos_units.setText("All positions in mm")
            self._units_action.setText("Change units to um")

    # -- menus and standalone buttons --------------------------------------

    def _connect_menus(self):
        self.ui.actionIn.triggered.connect(self.put_xrayeye_in)
        self.ui.actionIn.setToolTip("Writes 1 to %s" % EYE_CMD_PV)
        self.ui.actionOut.triggered.connect(self.put_xrayeye_out)
        self.ui.actionOut.setToolTip("Writes 0 to %s" % EYE_CMD_PV)
        self.ui.actionSnapshotMotors.triggered.connect(self.snapshot_motors)
        self.ui.actionExportPositions.triggered.connect(self.export_positions)
        self.ui.actionImportPositions.triggered.connect(self.import_positions)
        self.ui.actionRedefineMotor.triggered.connect(self.redefine_motor)
        self.ui.actionSAXSbs_in_all.setChecked(read_saxsbs_in_all())
        self.ui.actionSAXSbs_in_all.toggled.connect(self._on_saxsbs_in_all)

        # Built here, not in the .ui, so its label can flip between the two
        # phrasings instead of being a checkbox.
        self._units_action = QAction(self.ui)
        self.ui.menuEdit.insertAction(self.ui.actionRedefineMotor, self._units_action)
        self.ui.menuEdit.insertSeparator(self.ui.actionRedefineMotor)
        self._units_action.triggered.connect(self._toggle_units)
        self._update_units_display()

        # Built here rather than in the .ui so that remapping AXIS_BLOCKS
        # keeps the menu in step with the dpads it controls.
        invert_menu = QMenu("Invert dpad direction", self.ui)
        self.ui.menuEdit.insertMenu(self.ui.actionRedefineMotor, invert_menu)
        self.ui.menuEdit.insertSeparator(self.ui.actionRedefineMotor)
        for prefix, spec in AXIS_BLOCKS.items():
            for axis in ("ver", "hor"):
                action = invert_menu.addAction("%s %s" % (spec.title, axis))
                action.setCheckable(True)
                action.setChecked(self.invert[(prefix, axis)])
                action.toggled.connect(
                    lambda checked, p=prefix, a=axis: self._set_invert(p, a, checked))

    def _connect_buttons(self):
        self.ui.pushButton_xrayEyeIn.clicked.connect(self.put_xrayeye_in)
        self.ui.pushButton_xrayEyeIn.setToolTip("Writes 1 to %s" % EYE_CMD_PV)
        self.ui.pushButton_xrayEyeOut.clicked.connect(self.put_xrayeye_out)
        self.ui.pushButton_xrayEyeOut.setToolTip("Writes 0 to %s" % EYE_CMD_PV)
        self.ui.pushButton_stopAll.clicked.connect(self.stop_all)
        self.ui.pushButton_exit.clicked.connect(QApplication.instance().quit)
        self.ui.pushButton_allIn.clicked.connect(self.all_in)
        self.ui.pushButton_allOut.clicked.connect(self.all_out)

    def _restore_window(self):
        settings = QSettings("ptychoSAXS", "ptychoSAXS")

        def save_geometry_and_close(event):
            settings.setValue("opticsMotorsWindow/geometry", self.ui.saveGeometry())
            event.accept()

        self.ui.closeEvent = save_geometry_and_close
        self.ui.show()

        # QMainWindow only resolves its central widget's real size once the
        # window is shown, so force that before snapshotting the as-designed
        # layout the proportional rescaling is measured against.
        QApplication.processEvents()
        self._main_resizer = ProportionalResizer(self.ui.centralWidget())
        self.ui.setMinimumSize(int(self._main_resizer.orig_size.width() * 0.4),
                               int(self._main_resizer.orig_size.height() * 0.4))

        geometry = settings.value("opticsMotorsWindow/geometry")
        if geometry is not None:
            self.ui.restoreGeometry(geometry)
        self._main_resizer.rescale()

        saved_size = settings.value("ui/fontSize", DEFAULT_FONT_SIZE, type=int)
        self.ui.spinBox_fontSize.setValue(saved_size)
        self.ui.spinBox_fontSize.valueChanged.connect(self._on_font_size_changed)
        apply_saved_font_size(self.ui)
        QTimer.singleShot(0, lambda: self._update_title_fonts(saved_size))

    def _update_title_fonts(self, size):
        """Re-apply the sizes that sit above the panel-wide font size.

        apply_font_size_to_tree puts every widget on `size`, so anything
        meant to stand out has to be pushed back up afterwards.
        """
        for prefix in AXIS_BLOCKS:
            label = self.ui.findChild(QLabel, "lbl_%s_title" % prefix)
            f = label.font()
            f.setPointSize(size + 4)
            f.setBold(True)
            label.setFont(f)

        stop = self.ui.pushButton_stopAll
        f = stop.font()
        f.setPointSize(size + 2)
        stop.setFont(f)

    def _on_font_size_changed(self, size):
        apply_font_size_to_tree(self.ui, size)
        self._update_title_fonts(size)
        QSettings("ptychoSAXS", "ptychoSAXS").setValue("ui/fontSize", size)

    # -- X-ray eye ---------------------------------------------------------

    def _refresh_xrayeye_ui(self):
        """Offer whichever direction the eye is not already in.

        State comes from the command record, so an eye moved from the main
        panel or the sample alignment window shows up here too.
        """
        state = self.xray_eye.is_in()
        self.ui.actionIn.setEnabled(state is not True)
        self.ui.actionOut.setEnabled(state is not False)
        self.ui.pushButton_xrayEyeIn.setEnabled(state is not True)
        self.ui.pushButton_xrayEyeOut.setEnabled(state is not False)

    def put_xrayeye_in(self):
        self.put_xrayeye(True)

    def put_xrayeye_out(self):
        self.put_xrayeye(False)

    def put_xrayeye(self, ins=True):
        try:
            self.xray_eye.set_in(ins)
        except Exception as exc:
            QMessageBox.warning(self.ui, "X-ray eye",
                                "Could not command the X-ray eye:\n%s" % exc)
        self._refresh_xrayeye_ui()

    def is_xrayeye_out(self):
        """Unknown counts as out, so the move guard still warns rather than
        silently letting optics move with no idea where the eye is."""
        return self.xray_eye.is_in() is not True

    def confirm_xrayeye_guard(self):
        if getattr(self, "_eye_guard_suppressed", False):
            return True  # All Out already asked once for the whole batch
        reply = QMessageBox.warning(
            self.ui, "X-ray Eye Out",
            "The X-ray eye is currently OUT.\nAre you sure you want to proceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return reply == QMessageBox.Yes

    # -- All In / All Out --------------------------------------------------

    def _active_blocks(self):
        prefixes = list(ALL_IN_OUT_BLOCKS)
        if self.ui.actionSAXSbs_in_all.isChecked():
            prefixes.append(OPTIONAL_ALL_BLOCK)
        return [self.blocks[p] for p in prefixes]

    def _on_saxsbs_in_all(self, checked):
        write_saxsbs_in_all(checked)
        self.update_all_buttons()

    def all_in(self):
        for block in self._active_blocks():
            block.on_in()

    def all_out(self):
        # Ask once for the whole batch, and only if at least one block will
        # actually move. Each block guards itself too, so suppress those.
        movers = [b for b in self._active_blocks() if b.will_move_on_out()]
        if movers and self.is_xrayeye_out() and not self.confirm_xrayeye_guard():
            return
        self._eye_guard_suppressed = True
        try:
            for block in self._active_blocks():
                block.on_out()
        finally:
            self._eye_guard_suppressed = False

    def update_all_buttons(self):
        """All In / All Out only work once every block they drive is enabled."""
        if len(getattr(self, "blocks", {})) < len(AXIS_BLOCKS):
            return  # still constructing
        ready = all(block.enabled for block in self._active_blocks())
        self.ui.pushButton_allIn.setEnabled(ready)
        self.ui.pushButton_allOut.setEnabled(ready)

    def _update_all_status(self):
        texts = [block.status_text() for block in self._active_blocks()]
        label = self.ui.label_allStatus
        if all(t == "In" for t in texts):
            label.setText("In")
            label.setStyleSheet("background-color: #00cc00; color: white;")
        elif all(t == "Out" for t in texts):
            label.setText("Out")
            label.setStyleSheet("background-color: #cc0000; color: white;")
        else:
            label.setText("----")
            label.setStyleSheet("")

    def stop_all(self):
        for motor in MOTORS:
            controller, axis = self._axis(motor)
            try:
                with self.lock:
                    controller.stop(axis)
            except Exception as exc:
                print("Could not stop %s: %s" % (motor, exc))

    # -- Edit menu ---------------------------------------------------------

    def redefine_motor(self):
        """Tell a controller that its current position is some other number."""
        dlg = uic.loadUi(os.path.join(UI_DIR, "redefine_motor.ui"))
        dlg.comboBox_motor.addItems(list(MOTORS))

        def show_current():
            dlg.label_currentPos.setText(
                self._fmt_pos(self.read(dlg.comboBox_motor.currentText())))

        def redefine():
            text = dlg.lineEdit_newPos.text().strip()
            try:
                value = self._parse_pos(text)
            except ValueError:
                QMessageBox.warning(dlg, "Redefine motor",
                                    "%r is not a number." % text)
                return
            motor = dlg.comboBox_motor.currentText()
            controller, axis = self._axis(motor)
            try:
                with self.lock:
                    controller.set_pos(axis, value)
            except Exception as exc:
                QMessageBox.warning(dlg, "Redefine motor",
                                    "Could not redefine %s:\n%s" % (motor, exc))
                return
            dlg.accept()

        dlg.comboBox_motor.currentIndexChanged.connect(show_current)
        dlg.pushButton_redefine.clicked.connect(redefine)
        dlg.pushButton_cancel.clicked.connect(dlg.reject)
        show_current()
        apply_saved_font_size(dlg)
        dlg.exec_()

    # -- File menu ---------------------------------------------------------

    def snapshot_motors(self):
        """Append every motor's position, timestamped, to a CSV log."""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        positions = []
        for motor in MOTORS:
            try:
                positions.append(self.PREC % self.read(motor))
            except Exception as exc:
                print("Could not read %s: %s" % (motor, exc))
                positions.append("")

        settings = QSettings("ptychoSAXS", "ptychoSAXS")
        last = settings.value("opticsMotors/snapshotPath", "")
        path, _ = QFileDialog.getSaveFileName(
            self.ui, "Snapshot motors",
            last or os.path.join(_GUI_DIR, DEFAULT_SNAPSHOT_NAME),
            "CSV Files (*.csv)",
            options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        note, ok = QInputDialog.getText(self.ui, "Snapshot motors",
                                        "Note for this snapshot (optional):")
        if not ok:
            return

        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        try:
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(
                        ["Timestamp"]
                        + ["%s (mm)" % motor for motor in MOTORS]
                        + ["Notes"])
                writer.writerow([stamp] + positions + [note])
        except OSError as exc:
            QMessageBox.warning(self.ui, "Snapshot motors",
                                "Could not write %s:\n%s" % (path, exc))
            return
        settings.setValue("opticsMotors/snapshotPath", path)

    def export_positions(self):
        """Save every block's in/out positions to a user-chosen JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self.ui, "Export Positions", "optics_positions.json",
            "JSON Files (*.json)")
        if not path:
            return
        data = {}
        for prefix, block in self.blocks.items():
            entry = {"out": dict(zip(block.motors, block._stored("out")))}
            if block.has_list:
                entry["positions"] = block.positions
            else:
                entry["in"] = dict(zip(block.motors, block._stored("in")))
            data[prefix] = entry
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def import_positions(self):
        """Load positions from a file produced by export_positions."""
        path, _ = QFileDialog.getOpenFileName(
            self.ui, "Import Positions", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self.ui, "Import Error", str(exc))
            return

        for prefix, block in self.blocks.items():
            entry = data.get(prefix, {})
            for kind in ("in", "out"):
                if kind == "in" and block.has_list:
                    continue  # has_list blocks keep "in" in `positions`, below
                values = block._in_values if kind == "in" else block._out_values
                for i, motor in enumerate(block.motors):
                    value = entry.get(kind, {}).get(motor)
                    if value is not None:
                        values[i] = float(value)
            positions = entry.get("positions")
            if block.has_list and isinstance(positions, dict):
                block.positions = positions
                block._populate_list()
            block.refresh_display()
            block._save_ini()

    # -- periodic refresh --------------------------------------------------

    def updatepos(self):
        readbacks = {}
        for prefix, spec in AXIS_BLOCKS.items():
            for axis, motor in (("ver", spec.ver), ("hor", spec.hor)):
                readbacks[motor] = self.read(motor)
                self.ui.findChild(
                    QLabel, "lbl_rb_%s_%s" % (prefix, axis)
                ).setText(self._fmt_pos(readbacks[motor]))
        for prefix, motor in SINGLE_ROWS.items():
            readbacks[motor] = self.read(motor)
            self.ui.findChild(QLabel, "lbl_rb_%s" % prefix).setText(
                self._fmt_pos(readbacks[motor]))
        for block in self.blocks.values():
            block.update_status(readbacks)
        self._update_all_status()
        # Picks up an eye command issued by the main panel or the sample
        # alignment window, which share this state through the command PV.
        self._refresh_xrayeye_ui()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug_mode", action="store_true",
        help="Run without connecting to motors or EPICS PVs")
    args, _ = parser.parse_known_args()  # so Qt args pass through

    app = QApplication(sys.argv)
    motor_control(debug_mode=args.debug_mode)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
