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

from font_scaling import FontScaler
from font_utils import apply_font_size_to_tree, apply_saved_font_size, DEFAULT_FONT_SIZE
from ini_utils import INI_DIR, ensure_ini_defaults
from optics_modes import fmt_seconds, OpticsModes
from optics_status import (
    BLOCKS as _STATUS_BLOCKS,
    MOTORS,
    PV_BASES,
    THRESH as _STATUS_THRESH,
    classify_block,
    IN as _IN,
    OUT as _OUT,
)
from pv_lineedit import MemoryPV, PVLineEdit
from resize_utils import ProportionalResizer
from ui_levels import LevelSwitcher
from xray_eye import EYE_CMD_PV, EYE_EXPOSURE_PV, EYE_EXPOSURE_RBV_PV, XrayEye

_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(_GUI_DIR, "ui")
_OPTICS_INI = os.path.join(INI_DIR, "optics_motors.ini")

DEFAULT_SNAPSHOT_NAME = "ZP_optics_log.csv"

# Font size follows the window size, like a caqtdm screen, instead of being
# picked by hand. Set this to False to go back to fixed, designed-in sizes
# (no spin box exists in any of the three .ui levels to pick a size with
# any more): nothing else needs changing. font_scaling.py can then be
# deleted along with its one import.
DYNAMIC_FONTS = True

# The More/Less ladder, most detailed first. pushButton_More / pushButton_Less
# in each .ui step one level toward the front / back of this list -- see
# ui_levels.LevelSwitcher. Widening or narrowing the ladder is a one-line
# edit here; nothing else references the file list directly.
#
# The user view is the last entry but is not part of the ladder: it carries
# neither More nor Less, and the tiny view above it carries no Less either,
# so the only ways in and out of it are the two File menu entries wired in
# _connect_menus. It is in this list so that it still gets LevelSwitcher's
# per-level geometry memory and its "which view was open last" restore.
FULL_VIEW = "optics_motors.ui"
USER_VIEW = "optics_motors_user.ui"
VIEW_LEVELS = [FULL_VIEW, "optics_motors_small.ui", "optics_motors_tiny.ui",
               USER_VIEW]

try:
    from epics import PV
except ImportError:
    PV = None  # replaced by FakePV in debug mode

_repo_root = os.path.dirname(_GUI_DIR)
sys.path.append(os.path.join(_repo_root, "debug"))
if _repo_root not in sys.path:
    sys.path.append(_repo_root)

try:
    from ptychosaxs.optics import opticsbox, OSA, saxs_z, beamstop

    MotorControlAvailable = True
except Exception:
    MotorControlAvailable = False
    print("Optics motor control is NOT available.")


# ---------------------------------------------------------------------------
# Hardware map
#
# MOTORS and PV_BASES now live in optics_status.py, imported above, because
# rungui's "Check Upstream Optics" report and the NeXus/CSV scan logging read
# the same motors from a separate process. Remapping an axis is still a
# one-line edit -- it just happens there. The controllers come from
# ptychosaxs.optics:
#   opticsbox  12idc:m10, m11, m12, m13, m14
#   OSA        12idc:m9, m15, m16
#   saxs_z     12idcACS1:m1
#   beamstop   12ideSFT:m4, m5
#
# The CL slits are deliberately absent: this panel neither drives nor stops
# them, so Stop All must leave them alone.
# ---------------------------------------------------------------------------


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


# Derived from optics_status.BLOCKS so the panel and rungui's optics report
# can never disagree about which motor or ini section a block owns. Only the
# two-axis blocks become dpads here; "ztrans" is a SINGLE_ROWS entry below.
AXIS_BLOCKS = OrderedDict(
    (spec.key, AxisBlock(spec.key, spec.title, ver=spec.motors[0],
                         hor=spec.motors[1], section=spec.section,
                         has_list=spec.has_list))
    for spec in _STATUS_BLOCKS.values() if len(spec.motors) == 2
)

# Single-axis rows: widget prefix -> motor. Each is a plain readback / drive
# box / nudge row -- none of them owns a position registry. BSZP_Ztrans used
# to, and the registry (and its [ztrans] ini section) has been removed; the
# motor itself is still driven from here, and still logged per scan via
# optics_status.US_OPTICS_MOTORS.
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

# Motors shown to whole mm instead of the panel's usual 1 um. The SAXS
# detector Z runs metres down a rail and is positioned to the millimetre;
# its sub-mm digits are noise that only makes the number harder to read.
# Display only -- a typed target is still passed through as typed, and the
# record keeps whatever precision it has, exactly as a caqtdm field with
# PREC 0 would.
MM_RESOLUTION_MOTORS = {"SAXS_Z"}

_DEFAULT_ZP_POSITIONS = {"test position 1": [None, None]}


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
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        cfg.write(f)
    os.replace(tmp, path)   # atomic, so a concurrent reader never sees a
                            # half-written file -- see _save_ini


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

    Every one of those widgets is optional. The user view carries only the
    status pill (and, for the ZP, the position list): it has no In/Out
    buttons to gate, so a block built there is permanently enabled and is
    moved by the mode sequences in optics_modes rather than by a click.
    """

    THRESH = _STATUS_THRESH  # position comparison tolerance (shared)
    DISABLED_TEXT_COLOR = "#606060"
    # Clearing the status pill back to "no colour" still has to say
    # transparent, or the column stylesheet's grey shows instead of the
    # registry's pale yellow.
    NEUTRAL_STYLE = "background-color: transparent;"

    def __init__(self, panel, prefix, section, motors, has_list=False,
                 default_positions=None, initial_state=None):
        self.panel = panel
        self.ui = panel.ui
        self.prefix = prefix
        self.section = section
        self.motors = list(motors)
        self.has_list = has_list
        self.positions = {k: list(v) for k, v in (default_positions or {}).items()}
        self.enabled = False
        # Cached by update_status so status_text() works even in a view
        # with no status-pill widget; see update_status/status_text.
        self.state = None
        self.status_name = None

        # True stored positions, always in mm regardless of the display unit
        # setting -- the labels only ever show a formatted view of these.
        self._in_values = [None] * len(self.motors)
        self._out_values = [None] * len(self.motors)

        self._list = self.ui.findChild(QWidget, "listWidget_%sList" % prefix)
        self.enable_button = self._w(QPushButton, "pb_%s_enable" % prefix)
        self._connect()
        self._load_ini()
        self.refresh_display()

        # Carried over from this block's last incarnation in a different
        # view (More/Less), if any -- see motor_control._snapshot_block_state.
        state = initial_state or {}
        # No enable button means nothing to gate: the view has no In/Out
        # buttons either, so the gate would only be stopping the mode
        # sequences, which do their own asking.
        self._apply_enabled(True if self.enable_button is None
                            else state.get("enabled", False))
        slider = self._w(QSlider, "slider_%s_moveSet" % prefix)
        if slider is not None and "mode" in state:
            slider.setValue(state["mode"])
        if self.has_list:
            self._populate_list(select=state.get("selected"))

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
        if self.enable_button is not None:
            self.enable_button.clicked.connect(self._toggle)
        for name, slot in (("pb_%s_in" % p, self.on_in),
                           ("pb_%s_out" % p, self.on_out)):
            button = self._w(QPushButton, name)
            if button is not None:
                button.clicked.connect(slot)
        slider = self._w(QSlider, "slider_%s_moveSet" % p)
        # Reduced views can drop the move/set slider (the user view has no
        # registry controls at all) and fall back to Out always moving
        # motors -- see _move_set_mode.
        if slider is not None:
            # Unstyled, it paints the enclosing column's grey instead of the
            # registry's pale yellow -- same fix as the status pill below.
            slider.setStyleSheet("background-color: transparent;")
            slider.installEventFilter(_SliderToggle(slider))
        if self.has_list and self._list is not None:
            self._list.setSelectionMode(QAbstractItemView.SingleSelection)
            self._list.currentItemChanged.connect(self._on_selection_changed)
            add_pos = self._w(QPushButton, "pb_%s_addPos" % p)
            if add_pos is not None:
                add_pos.clicked.connect(self._edit_positions)

    def _apply_enabled(self, enabled):
        p = self.prefix
        for cls, name in [(QPushButton, "pb_%s_in" % p),
                          (QPushButton, "pb_%s_out" % p),
                          (QSlider, "slider_%s_moveSet" % p),
                          (QLabel, "lbl_%s_status" % p)]:
            widget = self._w(cls, name)
            if widget is not None:
                widget.setEnabled(enabled)

        if self.enable_button is not None:
            self.enable_button.setText("Yes" if enabled else "No")
            self.enable_button.setStyleSheet(
                "background-color: %s;" % ("#ccffcc" if enabled else "#ffcccc"))

        # Grey the whole registry out, status pill excepted -- it keeps its
        # own In/Out colouring. The explicit transparent background overrides
        # the grey the enclosing column's stylesheet cascades onto every
        # child, so the registry's pale yellow shows through.
        status = self._w(QLabel, "lbl_%s_status" % p)
        colour = "black" if enabled else self.DISABLED_TEXT_COLOR
        reg = self._w(QWidget, "frame_%s_reg" % p)
        if reg is not None:
            for lbl in reg.findChildren(QLabel):
                if lbl is not status:
                    lbl.setStyleSheet(
                        "color: %s; background-color: transparent;" % colour)

        self.enabled = enabled
        self.panel.update_all_buttons()

    def _toggle(self):
        self._apply_enabled(not self.enabled)

    def _move_set_mode(self):
        """0 = the In/Out buttons move motors, 1 = they save positions.

        A view that drops the slider (the user view) gets no choice in the
        matter -- Out always moves, same as mode 0, which is what the mode
        sequences need.
        """
        slider = self._w(QSlider, "slider_%s_moveSet" % self.prefix)
        return slider.value() if slider is not None else 0

    # -- In / Out ----------------------------------------------------------

    def will_move_on_out(self):
        """True when Out moves motors rather than saving their positions."""
        return self._move_set_mode() == 0

    def on_in(self):
        if self._move_set_mode() == 0:
            if self.has_list:
                name = self._selected_name()
                values = self.positions.get(name, []) if name else []
            else:
                values = self._stored("in")
            for motor, value in zip(self.motors, values):
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

    def has_target(self, kind):
        """Whether on_in/on_out would actually move anything.

        Both of those quietly skip a motor with no saved position, which is
        right for a button press but wrong for a mode sequence -- see
        optics_modes.OpticsModes._missing_targets, which uses this to refuse
        a sequence up front instead of half-performing it.
        """
        if kind == "in" and self.has_list:
            name = self._selected_name()
            values = self.positions.get(name, []) if name else []
        else:
            values = self._stored(kind)
        return any(value is not None for value in values)

    # -- display -------------------------------------------------------

    def refresh_display(self):
        """Redraw the in/out labels from the stored mm values, in whichever
        unit the panel is currently showing positions in.

        A reduced view can drop the in/out registry labels entirely (bs/zp/
        osa in the tiny layout) while keeping the rest of the block --
        _in_label/_out_label return None there, and setting nothing is the
        right degraded behaviour.
        """
        fmt = self.panel._fmt_pos
        if not self.has_list:
            for i in range(len(self.motors)):
                label = self._in_label(i)
                if label is not None:
                    label.setText(fmt(self._in_values[i]))
        for i in range(len(self.motors)):
            label = self._out_label(i)
            if label is not None:
                label.setText(fmt(self._out_values[i]))
        if self.has_list:
            name = self._selected_name()
            if name:
                self._show_position(name)

    # -- named position list ----------------------------------------------

    def _selected_name(self):
        if self._list is None:
            return None
        item = self._list.currentItem()
        return item.text() if item else None

    def _populate_list(self, select=None):
        if self._list is None:
            return
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
        """Push a stored named position into the In labels (display only).

        The user view keeps the ZP's position list but drops the In labels
        (lbl_zp_in_0 does not exist there) -- nothing to push to in that
        case.
        """
        for i, value in enumerate(self.positions.get(name, [])):
            label = self._in_label(i)
            if label is not None:
                label.setText(self.panel._fmt_pos(value))

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
        # Write-then-rename: rungui reads this file from another process to
        # build its optics report, and a plain truncate-then-write lets it
        # see a half-written file (and a truncated "positions" JSON).
        tmp = _OPTICS_INI + ".tmp"
        with open(tmp, "w") as f:
            cfg.write(f)
        os.replace(tmp, _OPTICS_INI)

    # -- status ------------------------------------------------------------

    def _status_spec(self):
        """This block's optics_status spec, for the shared classifier."""
        return _STATUS_BLOCKS[self.prefix]

    def _status_defs(self):
        """This block's in-memory state in optics_status' definitions shape.

        Read from memory rather than from the .ini so the pill still tracks
        an in/out position the operator has just captured but not yet saved.
        """
        return {self.prefix: {"in": self._stored("in"),
                              "out": self._stored("out"),
                              "positions": self.positions}}

    def update_status(self, readbacks):
        # Tracks the live readbacks regardless of whether this block is
        # enabled -- disabling only blocks the In/Out/move-set controls,
        # not the status pill reporting where the motors actually are.
        #
        # The matching itself lives in optics_status.classify_block, shared
        # with rungui's optics report: two copies of this comparison would
        # drift, and the two windows would then disagree about "In".
        status = classify_block(self._status_spec(), readbacks, self._status_defs())
        # Cached regardless of whether this view has a status pill to
        # paint, so status_text() (read by _update_all_status) keeps
        # working in a view that has no pill widget at all.
        self.state = status.state
        self.status_name = status.name

        label = self._w(QLabel, "lbl_%s_status" % self.prefix)
        if label is None:
            return

        if status.state == _IN:
            label.setText("In")
            label.setStyleSheet("background-color: #00cc00; color: white;")
        elif status.state == _OUT:
            label.setText("Out")
            label.setStyleSheet("background-color: #cc0000; color: white;")
        else:
            label.setText("----")
            label.setStyleSheet(self.NEUTRAL_STYLE)

    def status_text(self):
        """"In"/"Out"/"----", from the state update_status last classified.

        Reads the cached classification rather than a status pill's text, so
        it keeps working in a view that has no pill widget at all.
        """
        state = getattr(self, "state", None)
        if state == _IN:
            return "In"
        if state == _OUT:
            return "Out"
        return "----"


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
        self.lock = Lock()

        # Everything above this point, plus what follows to the end of
        # __init__, is one-time process setup: hardware, saved settings,
        # the X-ray eye. self.ui itself -- and everything built from it --
        # belongs to _build_view instead, since More/Less rebuilds that
        # from a different .ui file on every switch.
        self.control = self._build_controllers()
        self.invert = self._load_inverts()
        self._units_um = bool(QSettings("ptychoSAXS", "ptychoSAXS")
                              .value(self.UNITS_SETTINGS_KEY, False, type=bool))
        self._saxsbs_in_all = read_saxsbs_in_all()

        # Drive and tweak boxes mirror the motor records' .VAL and .TWV, so
        # the panel has to talk channel access directly for those two fields
        # -- the ptychosaxs controllers only expose readback and move. The
        # cache is keyed by PV name and outlives any one view, so a
        # More/Less switch reuses the same channels instead of reconnecting.
        self._pv_cache = {}

        if debug_mode:
            from debug_stubs import FakePV as _PV
        else:
            _PV = PV
        self.xray_eye = XrayEye(_PV, debug=debug_mode)

        # Carried across a view switch: a block the incoming view does not
        # show (the SAXS beamstop outside the full and user views) keeps
        # whatever state it last had instead of resetting to
        # disabled/move-mode/no selection.
        # Snapshotted from the live blocks just before each rebuild
        # (_snapshot_block_state) and merged in, never replaced, so a block
        # already missing before that snapshot keeps its last-known state.
        self._block_state = {}

        # Ptycho / X-ray Eye / SAXS mode. Panel-lifetime rather than
        # per-view: it holds no widgets, only a reference to this panel.
        self.modes = OpticsModes(self)

        self.timer = QTimer()
        self.timer.timeout.connect(self.updatepos)

        self._levels = LevelSwitcher(
            [os.path.join(UI_DIR, name) for name in VIEW_LEVELS],
            self._build_view, "opticsMotorsWindow")
        self._levels.start()

    # -- widget helpers ------------------------------------------------

    def _w(self, cls, name):
        """The named widget in the current view, or None if this view omits
        it -- every reduced view drops some widgets (see the table in
        ui_levels.py's caller, optics_motors.VIEW_LEVELS), so this is the
        one idiom used everywhere instead of scattered presence checks."""
        return self.ui.findChild(cls, name)

    def _connect(self, cls, name, signal_name, slot):
        """Wire `slot` to the named widget's `signal_name`, if present.

        Returns the widget (or None) so a caller that also needs to touch
        it (set a tooltip, say) does not have to look it up twice.
        """
        widget = self._w(cls, name)
        if widget is not None:
            getattr(widget, signal_name).connect(slot)
        return widget

    # -- per-view construction ------------------------------------------

    def _build_view(self, path):
        """Load, wire up and show one level's .ui -- everything tied to a
        particular window rather than to the panel's whole lifetime.

        Called by self._levels both at startup and on every More/Less
        switch, so it has to leave the panel in a fully working state on
        its own: fresh widget bindings, fresh blocks (carrying over
        whatever state the outgoing view's blocks had), fresh resize/font
        scaling snapshotted against *this* view's designed size.
        """
        # _show_view() below pumps the event loop (QApplication.processEvents)
        # before the new window's blocks/bindings exist; stopping the timer
        # for the duration keeps updatepos from running against that
        # half-built window in between.
        self.timer.stop()
        self._snapshot_block_state()

        self._bindings = []
        self._unitless_bindings = []
        self._tweak = {}
        self.ui = uic.loadUi(path)

        self._building = True
        self._build_blocks()
        self._building = False

        self._connect_motor_widgets()
        self._connect_exposure()
        self._connect_menus()
        self._connect_buttons()
        self._refresh_xrayeye_ui()
        self.update_all_buttons()
        self._update_units_display()

        self._show_view()
        self.timer.start(100)
        return self.ui

    def _snapshot_block_state(self):
        """Remember enable/mode/selection for the outgoing view's blocks.

        Merged into self._block_state rather than replacing it: a block
        that view did not show either (already missing before this call)
        keeps whatever it was last remembered as, so state survives a
        two-step switch (full -> tiny, which drops saxsbs, -> small, which
        also does not show saxsbs) rather than being forgotten partway.
        """
        for prefix, block in getattr(self, "blocks", {}).items():
            state = {
                "mode": block._move_set_mode(),
                "selected": block._selected_name() if block.has_list else None,
            }
            # Not recorded for a view with no enable button, where "enabled"
            # only ever means "there was nothing to gate" -- carrying that
            # into the full view would hand a staff screen live In/Out
            # buttons that nobody unlocked.
            if block.enable_button is not None:
                state["enabled"] = block.enabled
            self._block_state.setdefault(prefix, {}).update(state)

    def _show_view(self):
        """Show self.ui, size it to its designed layout, then apply this
        level's remembered geometry.

        LevelSwitcher owns closing the outgoing window and persisting
        geometry; this only has to get the new one on screen, correctly
        scaled, before handing it back.
        """
        self.ui.show()

        # QMainWindow only resolves its central widget's real size once the
        # window is shown, so force that before snapshotting the as-designed
        # layout the proportional rescaling is measured against.
        QApplication.processEvents()
        self._main_resizer = ProportionalResizer(self.ui.centralWidget())
        self.ui.setMinimumSize(int(self._main_resizer.orig_size.width() * 0.4),
                               int(self._main_resizer.orig_size.height() * 0.4))

        # Snapshot the as-designed point sizes before any geometry is
        # restored, for the same reason ProportionalResizer snapshots
        # geometry here: both scale factors are measured against the
        # designed size, not against whatever size the window opens at.
        if DYNAMIC_FONTS:
            apply_font_size_to_tree(self.ui, DEFAULT_FONT_SIZE)
            self._update_title_fonts(DEFAULT_FONT_SIZE)
            self._font_scaler = FontScaler(self.ui.centralWidget())

        # This level's own saved geometry if there is one, anchored to the
        # window being replaced on a More/Less switch, or Qt's own default
        # placement the first time this level is ever opened.
        self._levels.restore_geometry(self.ui)
        self._main_resizer.rescale()

        if DYNAMIC_FONTS:
            self._font_scaler.rescale()

    # -- hardware ----------------------------------------------------------

    def _build_controllers(self):
        if self.debug_mode:
            from debug_stubs import (
                DebugBeamstop, DebugOpticsbox, DebugOSA, DebugSaxsZ)
            return {"opticsbox": DebugOpticsbox(), "OSA": DebugOSA(),
                    "saxs_z": DebugSaxsZ(), "beamstop": DebugBeamstop()}
        if not MotorControlAvailable:
            raise RuntimeError(
                "Motor control hardware (optics) is not available. "
                "Run with --debug_mode to use stubs.")
        return {"opticsbox": opticsbox(), "OSA": OSA(), "saxs_z": saxs_z(),
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

    def _pv(self, pvname):
        """A channel for `pvname`, cached. None when EPICS is unavailable.

        A None channel is not an error: the bound boxes simply stop showing
        the record and go back to being plain entry boxes, which is what the
        panel did before they were bound at all.
        """
        if pvname in self._pv_cache:
            return self._pv_cache[pvname]
        if self.debug_mode:
            channel = MemoryPV(pvname)
        elif PV is None:
            channel = None
        else:
            try:
                channel = PV(pvname)
            except Exception as exc:
                print("Could not connect %s: %s" % (pvname, exc))
                channel = None
        self._pv_cache[pvname] = channel
        return channel

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

    def _fmt_step(self, value_mm):
        """Format a tweak step, which _fmt_pos' 1 um resolution would lose.

        Positions are shown at 1 um because that is as finely as the stages
        are read, but a step of a tenth of that is still a legitimate thing
        to ask a dpad for -- and the x0.1 button makes one in two clicks --
        so steps keep whatever precision the record holds.
        """
        if value_mm is None:
            return ""
        if self._units_um:
            return "%g" % (value_mm / self.MM_PER_UM)
        return "%g" % value_mm

    def _fmt_mm_only(self, value_mm):
        """Whole-mm display, for the MM_RESOLUTION_MOTORS.

        In um display mode that is still whole mm -- 3000, not 3000.4 --
        since the point is the resolution the number is meaningful to, not
        the unit it is written in.
        """
        if value_mm is None:
            return ""
        whole = round(value_mm)
        return "%d" % (round(whole / self.MM_PER_UM) if self._units_um else whole)

    def _fmt_for(self, motor):
        """The display formatter this motor's *positions* use.

        Tweak steps deliberately do not go through here: a 0.4 mm step
        rounded to "0" for display would be read straight back out of the
        box by _step and move the motor nowhere.
        """
        return self._fmt_mm_only if motor in MM_RESOLUTION_MOTORS else self._fmt_pos

    def _parse_pos(self, text):
        """Inverse of _fmt_pos: user-typed text in the current display unit
        to an internal mm float. Raises ValueError on bad input."""
        value = float(text)
        return value * self.MM_PER_UM if self._units_um else value

    # -- construction ------------------------------------------------------

    # A view "has" a block if it shows any of these for it. The enable button
    # is what small and tiny use to drop the SAXS beamstop column entirely;
    # the status pill is what the user view keeps when it drops every control
    # but still has to say where the optic is, and be driven by a mode button.
    BLOCK_MARKERS = [(QPushButton, "pb_%s_enable"),
                     (QLabel, "lbl_%s_status"),
                     (QWidget, "listWidget_%sList")]

    def _build_blocks(self):
        """One PresetBlock per axis block this view actually shows.

        self.blocks then holds exactly the blocks this view shows, which is
        what _active_blocks relies on for All In / All Out and what the mode
        sequences in optics_modes check before moving anything.
        """
        self.blocks = OrderedDict()
        for prefix, spec in AXIS_BLOCKS.items():
            if not self._shows_block(prefix):
                continue
            self.blocks[prefix] = PresetBlock(
                self, prefix, spec.section, spec.motors, has_list=spec.has_list,
                default_positions=_DEFAULT_ZP_POSITIONS if spec.has_list else None,
                initial_state=self._block_state.get(prefix))

    def _shows_block(self, prefix):
        return any(self._w(cls, name % prefix) is not None
                   for cls, name in self.BLOCK_MARKERS)

    def _connect_motor_widgets(self):
        """Label, move-to box and nudge buttons for every mapped motor.

        Every lookup here tolerates absence: a reduced view can drop a
        whole row (osaz/saxsz outside the full view, ztrans outside full and
        small) or just its drive box (bs/zp/osa's registry-only tiny
        layout) or just its tweak box and +/- buttons.
        """
        for prefix, spec in AXIS_BLOCKS.items():
            for axis, motor in (("ver", spec.ver), ("hor", spec.hor)):
                self._connect_move_to("ed_mv_%s_%s" % (prefix, axis), motor)
                self._set_readback_tooltip("lbl_rb_%s_%s" % (prefix, axis), motor)
            title = self._w(QLabel, "lbl_%s_title" % prefix)
            if title is not None:
                title.setText(spec.title)
            # One tweak box per dpad, two motors behind it, so both motors'
            # .TWV follow it -- otherwise the box would agree with the
            # vertical axis' record and silently disagree with the
            # horizontal one.
            self._connect_tweak("ed_%s_step" % prefix, spec.motors)
            self._connect_dpad(prefix, spec)

        for prefix, motor in SINGLE_ROWS.items():
            name_label = self._w(QLabel, "lbl_name_%s" % prefix)
            if name_label is not None:
                name_label.setText(motor)
            self._connect_move_to("ed_mv_%s" % prefix, motor)
            self._set_readback_tooltip("lbl_rb_%s" % prefix, motor)
            step_box = "ed_%s_step" % prefix
            self._connect_tweak(step_box, [motor])
            self._connect(QPushButton, "pb_%s_minus" % prefix, "clicked",
                lambda _c=False, m=motor, s=step_box: self.move_rel(m, -self._step(s)))
            self._connect(QPushButton, "pb_%s_plus" % prefix, "clicked",
                lambda _c=False, m=motor, s=step_box: self.move_rel(m, self._step(s)))

    def _connect_exposure(self):
        """The X-ray eye camera's exposure time: ed_xeExp, its RBV label and
        the x0.1 / x10 buttons.

        Deliberately not routed through _fmt_pos / _parse_pos. Those carry
        the panel's mm/um conversion, and an exposure is seconds -- bound
        through them, switching the panel to um display would multiply the
        camera's exposure time by a thousand.

        Which widgets exist varies: the full and user views have the x0.1 /
        x10 buttons, small and tiny have the box and RBV only.
        """
        self._exposure = None
        edit = self._w(QLineEdit, "ed_xeExp")
        if edit is not None:
            edit.setToolTip("Exposure: %s" % EYE_EXPOSURE_PV)
            self._exposure = PVLineEdit(
                edit, [self._pv(EYE_EXPOSURE_PV)], fmt_seconds, float,
                on_error=lambda text: QMessageBox.warning(
                    self.ui, "Exposure time", "%r is not a number." % text))
            # Kept out of self._bindings: that list is what a units change
            # rewrites, and this box has no units to change.
            self._unitless_bindings.append(self._exposure)

        rbv = self._w(QLabel, "lbl_xeExp_RBV")
        if rbv is not None:
            rbv.setToolTip("Readback: %s" % EYE_EXPOSURE_RBV_PV)

        if self._exposure is None:
            return   # no box for the x0.1 / x10 buttons to scale
        for suffix, factor in (("stepDown", 0.1), ("stepUp", 10.0)):
            self._connect(QPushButton, "pb_xeExp_%s" % suffix, "clicked",
                          lambda _c=False, f=factor: self._exposure.scale(f))

    def _update_exposure_rbv(self):
        label = self._w(QLabel, "lbl_xeExp_RBV")
        if label is None:
            return
        value = self.pv_value(EYE_EXPOSURE_RBV_PV)
        if value is not None:
            # Left alone when unreadable, same as PVLineEdit.poll: a dropped
            # connection should not blank a readback every tick.
            label.setText(fmt_seconds(value))

    def _set_readback_tooltip(self, widget_name, motor):
        label = self._w(QLabel, widget_name)
        if label is not None:
            label.setToolTip("Readback: %s.RBV" % PV_BASES[motor])

    def _connect_move_to(self, widget_name, motor):
        """Drive box: shows <motor>.VAL, typing in it moves the motor.

        The move still goes through the ptychosaxs controller rather than a
        raw .VAL put, so everything that already hangs off move_abs keeps
        working; only the display side is the record's. Some views have no
        drive box at all for this motor (tiny drops it everywhere).
        """
        edit = self._w(QLineEdit, widget_name)
        if edit is None:
            return
        edit.setToolTip("Drive: %s.VAL" % PV_BASES[motor])
        self._bindings.append(PVLineEdit(
            edit, [self._pv(PV_BASES[motor] + ".VAL")],
            self._fmt_for(motor), self._parse_pos,
            on_commit=lambda target, m=motor: self.move_abs(m, target),
            on_error=lambda text: QMessageBox.warning(
                self.ui, "Move", "%r is not a number." % text)))

    def _connect_tweak(self, widget_name, motors):
        """Tweak box: shows the first motor's .TWV, writes all of theirs.

        Returns None, and leaves nothing in self._tweak, when this view has
        no step box here (every view that drops a row entirely) -- callers
        that need it back (_connect_dpad's x10/x0.1 buttons) always pair it
        with a dpad that only exists where the tweak box does too.
        """
        edit = self._w(QLineEdit, widget_name)
        if edit is None:
            return None
        bases = [PV_BASES[m] for m in motors]
        edit.setToolTip("Tweak step: %s"
                        % ", ".join("%s.TWV" % base for base in bases))
        binding = PVLineEdit(
            edit, [self._pv(base + ".TWV") for base in bases],
            self._fmt_step, self._parse_pos)
        self._bindings.append(binding)
        self._tweak[widget_name] = binding
        return binding

    def _connect_dpad(self, prefix, spec):
        step_box = "ed_%s_step" % prefix
        for button, axis, motor, sign in [
                ("up", "ver", spec.ver, +1), ("down", "ver", spec.ver, -1),
                ("right", "hor", spec.hor, +1), ("left", "hor", spec.hor, -1)]:
            self._connect(QPushButton, "pb_%s_%s" % (prefix, button), "clicked",
                lambda _c=False, p=prefix, a=axis, m=motor, s=sign, box=step_box:
                self.move_rel(m, s * self._direction(p, a) * self._step(box)))

        for suffix, factor in (("stepDown", 0.1), ("stepUp", 10.0)):
            self._connect(QPushButton, "pb_%s_%s" % (prefix, suffix), "clicked",
                lambda _c=False, box=step_box, f=factor: self._scale_step(box, f))

    def _step(self, widget_name):
        """The dpad/tweak step in mm, 0 when the box is missing or does not
        hold a number.

        The typed number is in the current display unit, same as the move-to
        boxes -- so a tweak box showing "10" means 10 um once um display is
        selected, not 10 mm.
        """
        edit = self._w(QLineEdit, widget_name)
        if edit is None:
            return 0.0
        try:
            return self._parse_pos(edit.text())
        except ValueError:
            return 0.0

    def _scale_step(self, widget_name, factor):
        """The x10 / x0.1 buttons, which write .TWV like a typed step does."""
        binding = self._tweak.get(widget_name)
        if binding is not None:
            binding.scale(factor)

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
        old_units_um = self._units_um
        self._units_um = not self._units_um
        QSettings("ptychoSAXS", "ptychoSAXS").setValue(
            self.UNITS_SETTINGS_KEY, self._units_um)
        self._update_units_display()
        for block in self.blocks.values():
            block.refresh_display()
        self._rescale_free_text_widgets(old_units_um)

    def _rescale_free_text_widgets(self, old_units_um):
        """Re-display the move-to and tweak boxes in the new unit.

        A bound box just redraws its record in the new unit. One that has
        never read a record (no EPICS) is still free-typed text, so toggling
        units has to reinterpret whatever the user typed (in the old unit)
        and rewrite it in the new one -- otherwise a "10" typed as mm
        silently became "10" um underneath the user.
        """
        for binding in self._bindings:
            if not binding.refresh():
                self._rescale_lineedit(binding.edit, old_units_um, binding.fmt)

    def _rescale_lineedit(self, edit, old_units_um, fmt):
        text = edit.text().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return
        mm = value * self.MM_PER_UM if old_units_um else value
        edit.setText(fmt(mm))

    def _update_units_display(self):
        """self._units_action only exists in the full view -- small and
        tiny have no Edit menu to flip units from -- so it may be None."""
        label = self._w(QLabel, "lbl_pos_units")
        text = "All pos in um" if self._units_um else "All pos in mm"
        if label is not None:
            label.setText(text)
        if self._units_action is not None:
            self._units_action.setText(
                "Change units to mm" if self._units_um else "Change units to um")

    # -- menus and standalone buttons --------------------------------------

    def _connect_menus(self):
        """Wires the File/Edit menu, when this view has one at all -- small
        and tiny have no menu bar, so every lookup here can come back None
        and most of this method just does nothing.
        """
        self._connect(QAction, "actionSnapshotMotors", "triggered", self.snapshot_motors)
        self._connect(QAction, "actionExportPositions", "triggered", self.export_positions)
        self._connect(QAction, "actionImportPositions", "triggered", self.import_positions)
        self._connect(QAction, "actionRedefineMotor", "triggered", self.redefine_motor)

        # The user view is reached and left by name rather than by stepping
        # the More/Less ladder -- see the note on VIEW_LEVELS.
        self._connect(QAction, "actionOpen_user_GUI", "triggered",
                      lambda: self._open_view(USER_VIEW))
        self._connect(QAction, "actionOpen_full_GUI", "triggered",
                      lambda: self._open_view(FULL_VIEW))
        self._connect_zp_choice()

        # Label flips between the two phrasings in _update_units_display;
        # self._units_action is None wherever this view has no Edit menu.
        self._units_action = self._connect(QAction, "actionChangeUnits", "triggered",
                                           self._toggle_units)
        self._update_units_display()

        saxsbs_action = self._w(QAction, "actionSAXSbs_in_all")
        if saxsbs_action is not None:
            saxsbs_action.setChecked(self._saxsbs_in_all)
            saxsbs_action.toggled.connect(self._on_saxsbs_in_all)

        menu_edit = self._w(QMenu, "menuEdit")
        redefine_action = self._w(QAction, "actionRedefineMotor")
        if menu_edit is None or redefine_action is None:
            return  # small/tiny: nothing below has anywhere to attach to

        # Built here rather than in the .ui so that remapping AXIS_BLOCKS
        # keeps the menu in step with the dpads it controls.
        invert_menu = QMenu("Invert dpad direction", self.ui)
        menu_edit.insertMenu(redefine_action, invert_menu)
        menu_edit.insertSeparator(redefine_action)
        for prefix, spec in AXIS_BLOCKS.items():
            for axis in ("ver", "hor"):
                action = invert_menu.addAction("%s %s" % (spec.title, axis))
                action.setCheckable(True)
                action.setChecked(self.invert[(prefix, axis)])
                action.toggled.connect(
                    lambda checked, p=prefix, a=axis: self._set_invert(p, a, checked))

    def _open_view(self, filename):
        """Switch to the named level. Qt is still inside the menu action's
        own signal when this runs, which is why LevelSwitcher defers the
        outgoing window's destruction (deleteLater) rather than deleting it."""
        self._levels.switch_to(VIEW_LEVELS.index(filename))

    def _connect_zp_choice(self):
        """The user view's zone plate list is read-only until unlocked.

        A user picking the wrong zone plate out of the list would send the
        ZP somewhere nobody intended on the next Ptycho Mode, so the list
        starts disabled and the File menu is what arms it. The action does
        not exist in the staff views, where the whole registry is editable
        anyway and there is nothing to unlock.
        """
        listing = self._w(QWidget, "listWidget_zpList")
        action = self._w(QAction, "actionEnable_zone_plate_choice")
        if action is None:
            return
        action.setCheckable(True)
        action.setChecked(False)
        if listing is not None:
            listing.setEnabled(False)
            action.toggled.connect(listing.setEnabled)

    def _connect_buttons(self):
        eye_in = self._connect(QPushButton, "pushButton_xrayEyeIn", "clicked",
                               self.put_xrayeye_in)
        if eye_in is not None:
            eye_in.setToolTip("Writes 1 to %s" % EYE_CMD_PV)
        eye_out = self._connect(QPushButton, "pushButton_xrayEyeOut", "clicked",
                                self.put_xrayeye_out)
        if eye_out is not None:
            eye_out.setToolTip("Writes 0 to %s" % EYE_CMD_PV)
        self._connect(QPushButton, "pushButton_stopAll", "clicked", self.stop_all)
        # close(), not QApplication.quit() -- quit() exits the event loop
        # directly without running closeEvent, so the Exit button would skip
        # the geometry save that LevelSwitcher's own close handler does.
        # Closing the last visible window still quits the app (Qt's default
        # quitOnLastWindowClosed) once no other level's window is open.
        self._connect(QPushButton, "pushButton_exit", "clicked", self.ui.close)
        self._connect(QPushButton, "pushButton_allIn", "clicked", self.all_in)
        self._connect(QPushButton, "pushButton_allOut", "clicked", self.all_out)

        # The user view's three one-button configurations. Each runs a
        # sequence from optics_modes, which does its own confirming.
        self._connect(QPushButton, "pushButton_ptychoMode", "clicked",
                      self.modes.ptycho_mode)
        self._connect(QPushButton, "pushButton_xrayEyeMode", "clicked",
                      self.modes.xray_eye_mode)
        self._connect(QPushButton, "pushButton_saxsMode", "clicked",
                      self.modes.saxs_mode)

    def _update_title_fonts(self, size):
        """Re-apply the sizes that sit above the panel-wide font size.

        apply_font_size_to_tree puts every widget on `size`, so anything
        meant to stand out has to be pushed back up afterwards. Small and
        tiny drop the saxsbs column (no lbl_saxsbs_title there) even though
        AXIS_BLOCKS still lists it, so each lookup here can come back None.
        """
        for prefix in AXIS_BLOCKS:
            label = self._w(QLabel, "lbl_%s_title" % prefix)
            if label is None:
                continue
            f = label.font()
            f.setPointSize(size + 4)
            f.setBold(True)
            label.setFont(f)

        stop = self._w(QPushButton, "pushButton_stopAll")
        if stop is not None:
            f = stop.font()
            f.setPointSize(size + 2)
            stop.setFont(f)

    # -- X-ray eye ---------------------------------------------------------

    def _refresh_xrayeye_ui(self):
        """Offer whichever direction the eye is not already in.

        State comes from the command record, so an eye moved from the main
        panel or the sample alignment window shows up here too.
        """
        state = self.xray_eye.is_in()
        eye_in = self._w(QPushButton, "pushButton_xrayEyeIn")
        if eye_in is not None:
            eye_in.setEnabled(state is not True)
        eye_out = self._w(QPushButton, "pushButton_xrayEyeOut")
        if eye_out is not None:
            eye_out.setEnabled(state is not False)

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

    # -- interface for optics_modes.OpticsModes ----------------------------
    #
    # The mode sequences reach the hardware only through these five, so they
    # stay independent of which .ui is loaded and of how a block is built.

    def has_block(self, key):
        """Whether this view carries the block at all.

        Deliberately separate from block_state: a block whose status has not
        been classified yet (no updatepos tick since the view was built)
        also reports None, and "we do not know where it is yet" must not be
        mistaken for "this screen cannot move it".
        """
        return key in self.blocks

    def block_state(self, key):
        """optics_status IN/OUT/BETWEEN/UNDEFINED for a block. None when this
        view does not carry it, or has not yet classified it."""
        block = self.blocks.get(key)
        return None if block is None else block.state

    def block_has_target(self, key, kind):
        block = self.blocks.get(key)
        return block is not None and block.has_target(kind)

    def move_block(self, key, into):
        """Command one block in or out, bypassing the X-ray eye guard.

        The guard exists to catch an operator retracting optics with no eye
        to watch the beam on; a mode sequence has already asked about the
        eye in its own terms, and a second, differently worded question
        about the same thing is how an operator learns to click through
        both.
        """
        block = self.blocks.get(key)
        if block is None:
            return False
        self._eye_guard_suppressed = True
        try:
            block.on_in() if into else block.on_out()
        finally:
            self._eye_guard_suppressed = False
        return True

    def pv_value(self, name):
        """A PV as a float, or None when it cannot be read."""
        channel = self._pv(name)
        if channel is None:
            return None
        try:
            value = channel.get()
        except Exception:
            return None
        return None if value is None else float(value)

    def pv_factory(self, name):
        """A PV object for PVLineEdit, from the panel's shared cache."""
        return self._pv(name)

    # -- All In / All Out --------------------------------------------------

    def _active_blocks(self):
        """The blocks All In / All Out drive in *this* view.

        Filtered to self.blocks (what this view actually shows) rather than
        assumed: small and tiny have no saxsbs column and no Edit menu to
        turn "SAXSbs in All" on or off, so that option simply does not
        apply there, same as it did not exist before this block was added.
        """
        prefixes = list(ALL_IN_OUT_BLOCKS)
        if self._saxsbs_in_all and OPTIONAL_ALL_BLOCK in self.blocks:
            prefixes.append(OPTIONAL_ALL_BLOCK)
        return [self.blocks[p] for p in prefixes if p in self.blocks]

    def _on_saxsbs_in_all(self, checked):
        self._saxsbs_in_all = checked
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
        if getattr(self, "_building", False):
            return  # _build_blocks is still constructing this view's blocks
        ready = all(block.enabled for block in self._active_blocks())
        all_in = self._w(QPushButton, "pushButton_allIn")
        if all_in is not None:
            all_in.setEnabled(ready)
        all_out = self._w(QPushButton, "pushButton_allOut")
        if all_out is not None:
            all_out.setEnabled(ready)

    def _update_all_status(self):
        texts = [block.status_text() for block in self._active_blocks()]
        label = self._w(QLabel, "label_allStatus")
        if label is None:
            return
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
            motor = dlg.comboBox_motor.currentText()
            dlg.label_currentPos.setText(self._fmt_for(motor)(self.read(motor)))

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
        # Every motor is read regardless of whether this view shows its row
        # -- PresetBlock.update_status for a block this view *does* show may
        # need any of them -- but the label write is skipped where the view
        # has no such label (osaz/saxsz rows do not exist outside the full
        # view).
        readbacks = {}
        for prefix, spec in AXIS_BLOCKS.items():
            for axis, motor in (("ver", spec.ver), ("hor", spec.hor)):
                readbacks[motor] = self.read(motor)
                label = self._w(QLabel, "lbl_rb_%s_%s" % (prefix, axis))
                if label is not None:
                    label.setText(self._fmt_for(motor)(readbacks[motor]))
        for prefix, motor in SINGLE_ROWS.items():
            readbacks[motor] = self.read(motor)
            label = self._w(QLabel, "lbl_rb_%s" % prefix)
            if label is not None:
                label.setText(self._fmt_for(motor)(readbacks[motor]))
        for block in self.blocks.values():
            block.update_status(readbacks)
        self._update_all_status()
        # Drive/tweak boxes track .VAL/.TWV, so a move or a tweak-step
        # change made anywhere else shows up here within one tick. The
        # exposure box is in the second list for the reason given in
        # _connect_exposure; it is polled exactly the same way.
        for binding in self._bindings + self._unitless_bindings:
            binding.poll()
        self._update_exposure_rbv()
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
    # Bound to a name and kept for the app's lifetime: More/Less now
    # destroys and rebuilds windows under this object, so the window
    # staying alive only via the signal closures that happened to capture
    # `self` is no longer something to rely on.
    panel = motor_control(debug_mode=args.debug_mode)  # noqa: F841
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
