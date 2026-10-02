"""Guided sample-alignment workflow, opened from the main panel's
pb_sample_alignment button (see rungui.py's _open_sample_alignment_window).

One window, one QStackedWidget, one page per step, plus a footer of
Unlock all / Return to previous page / Cancel that is present on every page.
Every routing, gating and tooltip decision lives in alignment_flow, which is
Qt-free and unit-tested; this module only renders that state and drives
motors. Motion reuses rungui's own move/mover QRunnables and the main
window's threadpool -- nothing here re-implements motor control.

The .ui uses real Qt layouts, so this window (unlike the rest of the suite)
needs no ProportionalResizer; it reflows on its own and the shared font-size
setting still applies through font_utils.

The window is a controller, not a widget: self.ui (loaded from
sample_alignment.ui) is the real window, matching the convention in rungui.py
and CRL_3dprint.py.
"""

import configparser
import os
from html import escape

from PyQt5 import uic
from PyQt5.QtCore import QObject, QSettings, QTimer
from PyQt5.QtGui import QDoubleValidator
from PyQt5.QtWidgets import QMessageBox

from alignment_flow import (
    CLOSE,
    PHI_HIGH_DEFAULT,
    PHI_LOW_DEFAULT,
    PHI_ZERO_DEFAULT,
    Action,
    AlignmentFlow,
    Page,
    iterative_script_lines,
    window_title,
)
from font_utils import apply_saved_font_size
from ini_utils import INI_DIR
from motor_rows import MotorRow, MotorRowTable
from xray_eye import XrayEye

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_UI_PATH = os.path.join(_MODULE_DIR, "ui", "sample_alignment.ui")

# ---------------------------------------------------------------------------
# Tunables -- deliberately code-only, not exposed in the GUI
# ---------------------------------------------------------------------------

# Hexapod X is parked here when a fresh start resets the tomography centre of
# rotation. Change this one value if the nominal COR position moves.
HEXAPOD_X_COR_DEFAULT = 1.3  # mm

STEP_MOVEMENT_SAFE = 0.25  # mm, fixed
STEP_SET_ROTATION = 2.0  # deg, fixed
STEP_ROTATION_SAFE = 10.0  # deg
# Half-range the "Full 360 deg OK" shortcut opens either side of the zero
# angle, for a stage that can turn all the way round.
FULL_ROTATION_ARC_DEG = 270.0  # deg
STEP_TRANS_PHI = 180.0  # deg, fixed -- the flip the trans alignment needs
STEP_SMALL_ANGLE_PHI = 10.0  # deg
STEP_LINEAR_DEFAULT = 0.1  # mm, editable starting step for X/Z/trans rows

# The optics GUI's .ini is the source of truth for every saved optics
# position; this window only reads it.
OPTICS_INI_PATH = os.path.join(INI_DIR, "optics_motors.ini")

# Zone plate optics. ZP_ver / ZP_hor, matching scan_handler._US_OPTICS_PVS.
ZP_PVS = ("12idc:m12", "12idc:m13")
ZP_OUT_THRESH = 0.005  # same tolerance as optics_motors.MotorPresetBlock

# SAXS beamstop, in (vertical, horizontal) order to match the optics GUI's
# [SAXSbs] in_0 / in_1 convention. 12ideSFT:m4 is the horizontal axis, and is
# the one whose readback this window displays.
SAXS_BS_PVS = ("12ideSFT:m5", "12ideSFT:m4")
SAXS_BS_SHOWN_PV = "12ideSFT:m4"

# Mono energy, used only to decide which buttons the SAXS-beamstop reminder
# dialog offers (see _prompt_saxs_beamstop_reminder). At or below this energy
# an expert may dismiss the reminder without inserting the beamstop; above it
# the dialog only offers an acknowledgement.
MONO_ENERGY_PV = "12ida2:EnCalc"
SAXS_BS_LOW_ENERGY_KEV = 12.0

POS_POLL_MS = 250

# Status pill styling, matching optics_motors' in/out markers.
_PILL_IN = "background-color: #00cc00; color: white;"
_PILL_OUT = "background-color: #cc0000; color: white;"
_PILL_UNKNOWN = ""

# ---------------------------------------------------------------------------
# QSettings
# ---------------------------------------------------------------------------

GEOMETRY_KEY = "sampleAlignmentWindow/geometry"
EXPERT_KEY = "alignment/expertMode"
PHI_LOW_KEY = "alignment/phiSoftLow"
PHI_HIGH_KEY = "alignment/phiSoftHigh"
PHI_ZERO_KEY = "alignment/phiZero"


def _settings():
    return QSettings("ptychoSAXS", "ptychoSAXS")


def expert_mode_enabled():
    """Read the shared expert-mode flag.

    Written by both the "Expert User" checkbox on this window's first page and
    the Setup window's "Expert alignment" checkbox -- they are two views of
    this one setting.
    """
    return _settings().value(EXPERT_KEY, False, type=bool)


def set_expert_mode(enabled):
    _settings().setValue(EXPERT_KEY, bool(enabled))


def confirm_beamline_staff(parent):
    """Gate expert mode behind an explicit staff acknowledgement.

    Shared by both checkboxes, so ticking either one asks the same question.
    """
    reply = QMessageBox.question(
        parent,
        "Expert mode",
        "Expert mode merges alignment steps and skips the step-by-step "
        "safety unlocking.\n\nIt is intended for beamline staff. Confirm "
        "you are beamline staff?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return reply == QMessageBox.Yes


# Page -> the object name of its QStackedWidget page, so indices are resolved
# by name rather than hardcoded (reordering pages in Designer stays safe).
PAGE_WIDGETS = {
    Page.RADIOGRAPHY: "page_radiography",
    Page.MOVEMENT_SAFE: "page_movement_safe",
    Page.ROUGH_CENTER: "page_rough_center",
    Page.ROTATION_AXES: "page_rotation_axes",
    Page.SET_ROTATION: "page_set_rotation",
    Page.ROTATION_SAFE: "page_rotation_safe",
    Page.POSITION_AND_ROTATION: "page_position_and_rotation",
    Page.FIRST_TRANS: "page_first_trans",
    Page.SECOND_TRANS: "page_second_trans",
    Page.TRANS_BOTH: "page_trans_both",
    Page.COR_KNOWN: "page_cor_known",
    Page.SMALL_ANGLE_TRANS: "page_small_angle_trans",
    Page.ITERATIVE: "page_iterative",
    Page.CHANGE_SAMPLE: "page_change_sample",
}

# Rich text (lb_radio_instructions is set to Qt::RichText in the .ui). It has
# to switch between these two messages at runtime, so -- unlike every other
# page's title label -- whatever is typed into this label in Designer is
# overwritten the instant the page renders; edit these two constants instead.
_RADIOGRAPHY_TEXT = (
    "<b>Change to radiography mode:</b><br>"
    "- Put X-ray eye in<br>"
    "- Verify X-rays are on camera<br>"
    "- Remove ZP optics<br>"
    "- If energy &gt; 12 keV, insert SAXS beamstop"
)

_CAMERA_ISSUE_TEXT = (
    "<b>Camera issues!</b><br>"
    "- Try toggling X-ray eye in/out once<br>"
    "- Check camera IOC (usually open on Workspace 8):<br>"
    "--- Is it running?<br>"
    "--- Is the exposure time enough?"
    "- Else call beamline staff"
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly -- no Qt, no hardware)
# ---------------------------------------------------------------------------

def read_block_positions(ini_path, section, kind):
    """(value_0, value_1) for one optics preset block's saved positions.

    `kind` is "in" or "out"; the keys are `<kind>_0` / `<kind>_1`, in the
    (vertical, horizontal) order the optics GUI writes them.

    Returns (None, None) when the file, the section, or either value is
    missing -- optics_motors seeds these as empty strings, meaning "no
    position has been saved yet".
    """
    parser = configparser.ConfigParser()
    try:
        parser.read(ini_path)
    except (configparser.Error, OSError):
        return (None, None)
    if not parser.has_section(section):
        return (None, None)
    values = []
    for index in (0, 1):
        key = "%s_%d" % (kind, index)
        try:
            values.append(float(parser.get(section, key, fallback="").strip()))
        except (ValueError, AttributeError):
            return (None, None)
    return (values[0], values[1])


def read_zp_out_positions(ini_path=OPTICS_INI_PATH):
    """(ZP_ver, ZP_hor) saved Out positions from the optics GUI's .ini."""
    return read_block_positions(ini_path, "zp", "out")


def read_saxs_bs_in_positions(ini_path=OPTICS_INI_PATH):
    """(SAXS BS vertical, horizontal) saved In positions."""
    return read_block_positions(ini_path, "SAXSbs", "in")


def zp_is_out(readback, target, thresh=ZP_OUT_THRESH):
    """True when a ZP motor has reached its saved Out position."""
    if readback is None or target is None:
        return False
    return abs(float(readback) - float(target)) <= thresh


def script_html(lines):
    """Render iterative_script_lines() output as the label's rich text.

    A line may carry a "<title>\\n<body>" split (ITER_PREAMBLE's heading) --
    the title renders bold with a break after it. Everything is escaped first
    so literal markup characters in the instruction text (e.g. step 4's
    "<= 90 deg") never get parsed as tags.
    """
    parts = []
    for text, colour in lines:
        colour_hex = "#000000" if colour == "black" else "#888888"
        if "\n" in text:
            title, body = text.split("\n", 1)
            inner = "<b>%s</b><br>%s" % (escape(title), escape(body))
        else:
            inner = escape(text)
        parts.append('<span style="color:%s;">%s</span>' % (colour_hex, inner))
    return "<br>".join(parts)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class SampleAlignmentWindow(QObject):
    """Controller for sample_alignment.ui."""

    def __init__(self, main):
        super(SampleAlignmentWindow, self).__init__()
        self.main = main
        self.ui = uic.loadUi(_UI_PATH)
        self.flow = AlignmentFlow(expert=expert_mode_enabled())
        self._load_phi_settings()

        self._tables = {}
        self._nav_buttons = {}  # Page -> [(button, action)]
        self._closing_confirmed = False
        self._polling = False
        self._syncing_expert = False
        self._pv_cache = {}
        # Rows the operator unlocked on the current page/sub-step visit.
        self._unlocked = set()
        self._render_key = None
        self._zp_targets = read_zp_out_positions()
        self._zp_out_commanded = False
        self._saxs_bs_targets = read_saxs_bs_in_positions()

        self.eye = XrayEye(self._pv_class(), debug=self._debug_devices())

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_live)

        self._build_tables()
        self._wire_buttons()

        geometry = _settings().value(GEOMETRY_KEY)
        if geometry is not None:
            self.ui.restoreGeometry(geometry)

        self.ui.closeEvent = self._close_event
        apply_saved_font_size(self.ui)

        self._render_page()

    # -- settings ----------------------------------------------------------

    def _load_phi_settings(self):
        store = _settings()
        self.flow.phi_low = store.value(PHI_LOW_KEY, PHI_LOW_DEFAULT, type=float)
        self.flow.phi_high = store.value(PHI_HIGH_KEY, PHI_HIGH_DEFAULT, type=float)
        self.flow.phi_zero = store.value(PHI_ZERO_KEY, PHI_ZERO_DEFAULT, type=float)

    def _save_phi_settings(self):
        store = _settings()
        store.setValue(PHI_LOW_KEY, self.flow.phi_low)
        store.setValue(PHI_HIGH_KEY, self.flow.phi_high)
        store.setValue(PHI_ZERO_KEY, self.flow.phi_zero)

    # -- PVs ---------------------------------------------------------------

    def _debug_devices(self):
        return bool(getattr(self.main, "DEBUG_DEVICES", False))

    def _pv_class(self):
        if self._debug_devices():
            from debug_stubs import FakePV

            return FakePV
        from epics import PV

        return PV

    def _pv(self, name):
        """A cached PV handle. The status rows poll at POS_POLL_MS, and
        constructing a fresh epics.PV each tick would open a new channel."""
        pv = self._pv_cache.get(name)
        if pv is None:
            pv = self._pv_class()(name)
            self._pv_cache[name] = pv
        return pv

    # -- motors ------------------------------------------------------------

    def _axis(self, key):
        """Resolve a row key to the motor name `pts` knows.

        transH/transD are display roles chosen at runtime; every other key is
        already a real motor name.
        """
        if key == "transH":
            return self.flow.trans_h
        if key == "transD":
            return self.flow.trans_d
        return key

    def _is_connected(self, key):
        axis = self._axis(key)
        if axis is None:
            return False
        try:
            index = self.main.motornames.index(axis)
        except (ValueError, AttributeError):
            return False
        return bool(self.main.motorconnected[index])

    def _limits_enforced(self):
        """Soft limits are set on the rotation-safety pages, so they cannot be
        enforced there -- the operator has to rotate past them to find them."""
        return self.flow.page not in (Page.ROTATION_SAFE, Page.POSITION_AND_ROTATION)

    def _dispatch_move(self, key, value, relative):
        """The one place any motor move leaves this window.

        Reuses rungui's move/mover QRunnables and the main window's threadpool
        so motion behaves exactly as it does from the main panel. They come off
        the main controller rather than from `import rungui`, which would
        re-execute that module and build a second GUI -- see the note in
        handlers/status_handler.py.
        """
        axis = self._axis(key)
        if axis is None or not self._is_connected(key):
            return
        table = self._tables.get(self.flow.page)

        if axis == "phi" and self._limits_enforced():
            try:
                current = self.main.pts.get_pos("phi")
            except Exception:
                current = 0.0
            target = current + value if relative else value
            if not self.flow.phi_in_limits(target):
                if table is not None:
                    table.flag_invalid(key, True)
                QMessageBox.warning(
                    self.ui,
                    "Outside soft limits",
                    "That move would take phi to %.3f deg, outside the soft "
                    "limits [%g, %g].\n\nThe move was not sent."
                    % (target, self.flow.phi_low, self.flow.phi_high),
                )
                return
        if table is not None:
            table.flag_invalid(key, False)

        self.flow.record_move(axis)
        factory = (self.main.MoveRelRunnable if relative
                   else self.main.MoveRunnable)
        self.main.threadpool.start(factory(self.main.pts, axis, value))
        self._refresh_enabled()

    def _on_tweak(self, key, sign, step):
        self._dispatch_move(key, sign * step, relative=True)

    def _on_moveto(self, key, value):
        self._dispatch_move(key, value, relative=False)

    def _phi_position(self):
        try:
            return float(self.main.pts.get_pos("phi"))
        except Exception:
            return None

    # -- table construction ------------------------------------------------

    def _add_table(self, page, container_name, rows, columns, prefix):
        container = getattr(self.ui, container_name)
        table = MotorRowTable(container, rows, columns=columns, prefix=prefix)
        table.on_tweak(self._on_tweak)
        table.on_moveto(self._on_moveto)
        self._tables[page] = table
        return table

    def _build_tables(self):
        fixed = ("name", "current", "tweak")
        full = ("name", "current", "moveto", "tweak")

        def linear(key, step=STEP_LINEAR_DEFAULT, tweak="edit"):
            return MotorRow(key, step=step, tweak=tweak)

        self._add_table(
            Page.MOVEMENT_SAFE, "container_rows_movesafe",
            [MotorRow(key, step=STEP_MOVEMENT_SAFE, tweak="label")
             for key in ("X", "Z", "trans1", "trans2")],
            fixed, "movesafe")

        self._add_table(
            Page.ROUGH_CENTER, "container_rows_rough",
            [linear(key) for key in ("X", "Z", "trans1", "trans2")],
            full, "rough")

        # The rotation-axes detour: phi plus both trans stages, every step
        # editable, so the operator can line the trans axes up with the beam.
        self._add_table(
            Page.ROTATION_AXES, "container_rows_rotaxes",
            [MotorRow("phi", step=STEP_SMALL_ANGLE_PHI),
             linear("trans1"), linear("trans2")],
            fixed, "rotaxes")

        self._add_table(
            Page.SET_ROTATION, "container_rows_setrot",
            [MotorRow("phi", step=STEP_SET_ROTATION, tweak="label")],
            fixed, "setrot")

        self._add_table(
            Page.ROTATION_SAFE, "container_rows_rotsafe",
            [MotorRow("phi", step=STEP_ROTATION_SAFE)],
            fixed, "rotsafe")

        self._add_table(
            Page.POSITION_AND_ROTATION, "container_rows_posrot",
            [linear(key) for key in ("X", "Z", "trans1", "trans2")]
            + [MotorRow("phi", step=STEP_ROTATION_SAFE)],
            full, "posrot")

        self._add_table(
            Page.FIRST_TRANS, "container_rows_firsttrans",
            [MotorRow("phi", step=STEP_TRANS_PHI, tweak="label"),
             linear("trans1"), linear("trans2")],
            fixed, "firsttrans")

        # Only the trans stage NOT chosen as horizontal appears here, so the
        # row is keyed by the transD role and labelled at page-entry time.
        self._add_table(
            Page.SECOND_TRANS, "container_rows_secondtrans",
            [MotorRow("phi", step=STEP_SMALL_ANGLE_PHI), linear("transD")],
            fixed, "secondtrans")

        self._add_table(
            Page.TRANS_BOTH, "container_rows_transboth",
            [MotorRow("phi", step=STEP_TRANS_PHI),
             linear("trans1"), linear("trans2")],
            fixed, "transboth")

        self._add_table(
            Page.SMALL_ANGLE_TRANS, "container_rows_smallangle",
            [MotorRow("phi", step=STEP_SMALL_ANGLE_PHI),
             linear("trans1"), linear("trans2")],
            fixed, "smallangle")

        # phi is swappable: a fixed 180 deg for the step 2a/2c flips, becoming
        # an editable 10 deg for the small step-4 rotations.
        self._add_table(
            Page.ITERATIVE, "container_rows_iter",
            [MotorRow("phi", step=STEP_TRANS_PHI, tweak="label", swappable=True),
             linear("trans1"), linear("trans2"), linear("X")],
            fixed, "iter")

    # -- wiring ------------------------------------------------------------

    def _register_nav(self, page, button, action):
        button.clicked.connect(lambda _checked=False, a=action: self._do(a))
        self._nav_buttons.setdefault(page, []).append((button, action))

    def _wire_buttons(self):
        ui = self.ui

        # -- footer, shared by every page
        ui.pb_cancel.clicked.connect(self._cancel)
        ui.pb_unlock_all.clicked.connect(self._on_unlock_all)
        ui.pb_back.clicked.connect(lambda: self._do(Action.GO_BACK))

        # -- radiography
        ui.pb_eye_in.clicked.connect(lambda: self._put_eye(True))
        ui.pb_eye_out.clicked.connect(lambda: self._put_eye(False))
        ui.pb_xrays_ok.clicked.connect(self._on_xrays_ok)
        ui.pb_xrays_no.clicked.connect(self._on_xrays_no)
        ui.pb_zp_out.clicked.connect(self._pull_zp_out)
        ui.pb_saxs_bs_in.clicked.connect(self._insert_saxs_bs)
        ui.checkBox_expertUser.toggled.connect(self._on_expert_toggled)
        self._register_nav(Page.RADIOGRAPHY, ui.pb_radio_continue,
                           Action.RADIO_CONTINUE)

        # -- movement safe. Fresh start goes through its own handler so the
        # "call beamline staff" warning can offer its three outcomes.
        ui.pb_fresh_start.clicked.connect(self._on_fresh_start)
        self._nav_buttons[Page.MOVEMENT_SAFE] = [
            (ui.pb_fresh_start, Action.MOVE_FRESH_START)]
        self._register_nav(Page.MOVEMENT_SAFE, ui.pb_sample_change,
                           Action.MOVE_SAMPLE_CHANGE)

        # -- rough centre: the transH picker, the detour, then Continue
        self._register_nav(Page.ROUGH_CENTER, ui.pb_rough_continue,
                           Action.ROUGH_CONTINUE)
        self._register_nav(Page.ROUGH_CENTER, ui.pb_need_rotation,
                           Action.NEED_ROTATION)
        ui.pb_rough_trans1.clicked.connect(
            lambda: self._do(Action.TRANS1_HORIZONTAL))
        ui.pb_rough_trans2.clicked.connect(
            lambda: self._do(Action.TRANS2_HORIZONTAL))
        ui.pb_posrot_trans1.clicked.connect(
            lambda: self._do(Action.TRANS1_HORIZONTAL))
        ui.pb_posrot_trans2.clicked.connect(
            lambda: self._do(Action.TRANS2_HORIZONTAL))

        # -- rotation-axes detour. Picking the horizontal stage is what ends
        # the detour, so the picker doubles as this page's exit; the .ui
        # tooltips say so, and no nav tooltip is registered over them.
        ui.pb_rotaxes_trans1.clicked.connect(
            lambda: self._on_rotaxes_pick(Action.TRANS1_HORIZONTAL))
        ui.pb_rotaxes_trans2.clicked.connect(
            lambda: self._on_rotaxes_pick(Action.TRANS2_HORIZONTAL))

        # -- set rotation
        self._register_nav(Page.SET_ROTATION, ui.pb_setrot_continue,
                           Action.ROT_CONTINUE)

        # -- rotation safety: the same strip appears on the user page and on
        # the expert merge, under an "x_" name prefix.
        for prefix in ("", "x_"):
            getattr(ui, "pb_%sset_low" % prefix).clicked.connect(self._set_phi_low)
            getattr(ui, "pb_%sset_high" % prefix).clicked.connect(self._set_phi_high)
            getattr(ui, "pb_%sset_zero" % prefix).clicked.connect(self._set_phi_zero)
        ui.pb_phi_full360.clicked.connect(self._set_phi_full_360)
        self._wire_limit_edits()
        ui.pb_rotsafe_continue.clicked.connect(self._on_rotsafe_continue)
        ui.pb_posrot_continue.clicked.connect(self._on_rotsafe_continue)
        self._nav_buttons[Page.ROTATION_SAFE] = [
            (ui.pb_rotsafe_continue, Action.ROTSAFE_CONTINUE),
        ]
        self._nav_buttons[Page.POSITION_AND_ROTATION] = [
            (ui.pb_posrot_continue, Action.ROTSAFE_CONTINUE),
        ]

        # -- trans pages
        self._register_nav(Page.FIRST_TRANS, ui.pb_first_continue,
                           Action.FIRST_TRANS_CONTINUE)
        self._register_nav(Page.SECOND_TRANS, ui.pb_second_finish,
                           Action.TRANS_FINISH)
        self._register_nav(Page.TRANS_BOTH, ui.pb_both_finish,
                           Action.TRANS_FINISH)
        self._register_nav(Page.SMALL_ANGLE_TRANS, ui.pb_small_finish,
                           Action.TRANS_FINISH)

        # -- centre-of-rotation question
        self._register_nav(Page.COR_KNOWN, ui.pb_cor_yes, Action.COR_YES)
        self._register_nav(Page.COR_KNOWN, ui.pb_cor_no, Action.COR_NO)

        # -- iterative: pb_iter_a is re-purposed per sub-step, so it is
        # connected once to a dispatcher that reads the current action rather
        # than being disconnected and reconnected on every step change.
        ui.pb_iter_a.clicked.connect(self._on_iter_step_button)
        self._register_nav(Page.ITERATIVE, ui.pb_iter_back, Action.ITER_BACK)
        self._register_nav(Page.ITERATIVE, ui.pb_iter_finish, Action.ITER_FINISH)
        self._nav_buttons[Page.ITERATIVE].append((ui.pb_iter_a, None))

        # -- change sample
        self._register_nav(Page.CHANGE_SAMPLE, ui.pb_start_over, Action.START_OVER)

    # -- flow driving ------------------------------------------------------

    def _do(self, action, rotation_safe=None):
        if not self.flow.can(action):
            return
        target = self.flow.advance(action, rotation_safe=rotation_safe)
        if target == CLOSE:
            self._finish()
            return
        self._render_page()
        if self.flow.pending_hexapod_x_prompt:
            self.flow.pending_hexapod_x_prompt = False
            self._offer_hexapod_x_reset()

    def _on_rotaxes_pick(self, action):
        """The rotation-axes detour's exit.

        Choosing the horizontal stage is the whole point of the detour, so
        the pick records the choice and then retraces the one step back to
        the rough-centre page -- the same hop the old "Done" button made.
        """
        self._do(action)
        self._do(Action.ROTAXES_DONE)

    def _on_iter_step_button(self):
        state = self.flow.iterative_state()
        spec = state["btn_a"]
        if spec["action"] is None or not spec["enabled"]:
            return
        self._do(spec["action"])

    def _cancel(self):
        """The footer's Cancel. The confirmation prompt lives in
        _close_event, so the title-bar X behaves identically."""
        self.ui.close()

    def _finish(self):
        """Finished alignment / Finished! -- close without confirming."""
        self._closing_confirmed = True
        self.ui.close()

    # -- unlocking ---------------------------------------------------------

    def _enabled_keys(self, table):
        """Row keys that should be live on the current page.

        The flow decides the baseline; anything the operator unlocked on this
        visit is added on top.
        """
        page = self.flow.page
        if page == Page.ITERATIVE:
            base = set(self.flow.iterative_state()["motors"])
        else:
            base = set(table.keys()) - set(self.flow.locked_rows(page))
        return base | self._unlocked

    def _locked_keys(self, table):
        return set(table.keys()) - self._enabled_keys(table)

    def _on_unlock_all(self):
        """Footer button: enable whatever this step has disabled.

        Scoped to this visit -- the enable map is recomputed from the flow on
        the next page or sub-step change, which re-locks everything.
        """
        table = self._tables.get(self.flow.page)
        if table is None:
            return
        locked = self._locked_keys(table)
        if not locked:
            return
        if "X" in locked:
            reply = QMessageBox.question(
                self.ui,
                "Unlock X?",
                "Moving X moves the sample off the center of rotation.\n\n"
                "Unlock it for this step?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self._unlocked |= locked
        self._apply_enables(table)
        self._refresh_footer()

    # -- rotation-safety handlers -----------------------------------------

    _LIMIT_FIELDS = ("low", "high", "zero")

    def _limit_edits(self, which):
        """Both copies of one soft-limit field: the user page and the expert
        merge show the same value."""
        return [getattr(self.ui, "ed_%sphi_%s" % (prefix, which))
                for prefix in ("", "x_")]

    def _wire_limit_edits(self):
        for which in self._LIMIT_FIELDS:
            for edit in self._limit_edits(which):
                edit.setValidator(QDoubleValidator(-100000.0, 100000.0, 4, edit))
                edit.editingFinished.connect(
                    lambda e=edit, w=which: self._on_limit_edited(w, e))

    def _on_limit_edited(self, which, edit):
        """Apply a typed soft limit, or put the old value back if it will not
        parse (the validator allows a partial entry like "-")."""
        try:
            value = float(edit.text())
        except (TypeError, ValueError):
            self._render_limit_edits()
            return
        if which == "low":
            self.flow.set_phi_low(value)
        elif which == "high":
            self.flow.set_phi_high(value)
        else:
            self.flow.set_phi_zero(value)
        self._save_phi_settings()
        self._render_limit_edits()
        self._refresh_enabled()

    def _set_phi_from_current(self, which):
        value = self._phi_position()
        if value is None:
            return
        if which == "low":
            self.flow.set_phi_low(value)
        elif which == "high":
            self.flow.set_phi_high(value)
        else:
            self.flow.set_phi_zero(value)
        self._save_phi_settings()
        self._render_limit_edits()
        self._refresh_enabled()

    def _set_phi_low(self):
        self._set_phi_from_current("low")

    def _set_phi_high(self):
        self._set_phi_from_current("high")

    def _set_phi_zero(self):
        self._set_phi_from_current("zero")

    def _set_phi_full_360(self):
        """Shortcut for a stage that turns all the way round: put the soft
        limits a full 270 deg either side of the zero angle, which clears the
        180 deg arc both ways."""
        zero = self.flow.phi_zero
        self.flow.set_phi_low(zero - FULL_ROTATION_ARC_DEG)
        self.flow.set_phi_high(zero + FULL_ROTATION_ARC_DEG)
        self._save_phi_settings()
        self._render_limit_edits()
        self._refresh_enabled()

    # The only destinations that assume the sample starts at the zero angle:
    # the trans alignment opens by flipping phi 0 -> 180 deg. TRANS_BOTH is
    # the expert-mode merge of FIRST_TRANS and SECOND_TRANS, so it counts too.
    # Everywhere else -- the iterative page, and both "rotation not safe"
    # landing pages -- phi is left exactly where the operator left it.
    _PHI_ZERO_PAGES = frozenset({Page.FIRST_TRANS, Page.TRANS_BOTH})

    def _offer_phi_zero_move(self):
        """Ask whether to park phi at the zero angle before continuing.

        Only called on the way to the trans alignment, which starts from that
        angle -- see _PHI_ZERO_PAGES.
        """
        if not self._is_connected("phi"):
            return
        if not self.flow.phi_in_limits(self.flow.phi_zero):
            # Every page after this one enforces the limits, so parking phi
            # outside them would immediately lock the operator out.
            QMessageBox.warning(
                self.ui,
                "Angle 0 outside soft limits",
                "Angle 0 (%g deg) lies outside the soft limits [%g, %g], so "
                "phi was not moved there.\n\nEvery later step enforces those "
                "limits - fix one or the other before continuing."
                % (self.flow.phi_zero, self.flow.phi_low, self.flow.phi_high),
            )
            return
        reply = QMessageBox.question(
            self.ui,
            "Set phi to angle 0?",
            "Move phi to the zero angle (%g deg) before continuing?\n\n"
            "The next steps assume the sample starts at that angle."
            % self.flow.phi_zero,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._dispatch_move("phi", self.flow.phi_zero, relative=False)

    def _on_rotsafe_continue(self):
        """Confirm the range, then leave the rotation-safety page.

        The arc decision is computed from the limits just entered; the middle
        button is the override, so it always offers the opposite verdict to
        the one shown. Cancel dismisses the dialog and changes nothing.
        """
        if not self.flow.can(Action.ROTSAFE_CONTINUE):
            return
        auto_safe = self.flow.rotation_arc_ok()
        box = QMessageBox(self.ui)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Rotation range")
        box.setText(
            "Range is set to: [%g, %g, %g].\n"
            "This range counts as %s for 180 deg rotation."
            % (self.flow.phi_low, self.flow.phi_zero, self.flow.phi_high,
               "SAFE" if auto_safe else "NOT SAFE"))
        proceed = box.addButton("Continue", QMessageBox.AcceptRole)
        box.addButton("180 deg rotation not safe" if auto_safe
                      else "180 deg rotation is safe", QMessageBox.NoRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(proceed)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is None or clicked is cancel:
            return
        safe = auto_safe if clicked is proceed else not auto_safe
        if self.flow.destination(Action.ROTSAFE_CONTINUE,
                                 rotation_safe=safe) in self._PHI_ZERO_PAGES:
            self._offer_phi_zero_move()
        self._do(Action.ROTSAFE_CONTINUE, rotation_safe=safe)

    # -- fresh start / hexapod X reset -------------------------------------

    def _on_fresh_start(self):
        """A fresh start re-establishes the X-ray alignment, which is not an
        operator task -- warn before committing to that branch."""
        if not self.flow.can(Action.MOVE_FRESH_START):
            return
        box = QMessageBox(self.ui)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Fresh start")
        box.setText("Call beamline staff to realign X-rays.")
        box.setInformativeText(
            "A fresh start assumes the X-ray beam path itself is being "
            "re-established, not just the sample.")
        proceed = box.addButton("Continue as expert", QMessageBox.AcceptRole)
        box.addButton("Return", QMessageBox.RejectRole)
        cancel = box.addButton("Cancel", QMessageBox.DestructiveRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is cancel:
            self._cancel()
            return
        if clicked is not proceed:
            return  # Return: stay on this page, nothing changes
        self._do(Action.MOVE_FRESH_START)

    def _offer_hexapod_x_reset(self):
        """A fresh start means the tomography centre of rotation is lost, so
        hexapod X goes back to its nominal value before re-centring."""
        if not self._is_connected("X"):
            return
        reply = QMessageBox.question(
            self.ui,
            "Reset hexapod X?",
            "The center of rotation is being re-established.\n\n"
            "Move the hexapod X stage to %g mm now?"
            % HEXAPOD_X_COR_DEFAULT,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            # X is locked on this page, so the move has to bypass the row's
            # enable state -- the operator has just authorised this one.
            self._unlocked.add("X")
            table = self._tables.get(self.flow.page)
            if table is not None:
                self._apply_enables(table)
            self._dispatch_move("X", HEXAPOD_X_COR_DEFAULT, relative=False)
            self._refresh_footer()

    # -- expert mode -------------------------------------------------------

    def refresh_expert_mode(self):
        """Re-read the shared expert flag after the Setup window changed it.

        On the first page the change can be applied immediately (the run has
        not started); anywhere else it is left for the next run rather than
        reshuffling pages underneath the operator.
        """
        enabled = expert_mode_enabled()
        if enabled == self.flow.expert:
            return
        if self.flow.page == Page.RADIOGRAPHY:
            self.flow.start(expert=enabled)
        self._render_page()

    def _on_expert_toggled(self, checked):
        if self._syncing_expert:
            return
        if checked and not confirm_beamline_staff(self.ui):
            self._syncing_expert = True
            self.ui.checkBox_expertUser.setChecked(False)
            self._syncing_expert = False
            return
        set_expert_mode(checked)
        # Expert mode changes which pages exist, so the run restarts. Nothing
        # is lost: this checkbox only appears on the first page.
        self.flow.start(expert=checked)
        self._render_page()

    # -- radiography page --------------------------------------------------

    def _put_eye(self, inserted):
        try:
            self.eye.set_in(inserted)
        except Exception as exc:
            QMessageBox.warning(self.ui, "X-ray eye",
                                "Could not command the X-ray eye:\n%s" % exc)
        self._refresh_eye_buttons()

    def _refresh_eye_buttons(self):
        """Offer whichever direction the eye is not already in.

        State comes from the shared command readback, so a command issued in
        the optics GUI shows up here too. Both buttons stay live while the
        state is unknown.
        """
        state = self.eye.is_in()
        self.ui.pb_eye_in.setEnabled(state is not True)
        self.ui.pb_eye_out.setEnabled(state is not False)

    def _on_xrays_ok(self):
        self.flow.xrays_confirmed = True
        self.flow.camera_issue = False
        self._render_radiography_text()
        self._refresh_zp_status()
        self._refresh_enabled()

    def _on_xrays_no(self):
        # Seeing no X-rays invalidates an earlier confirmation, so the page
        # re-locks until the operator confirms again.
        self.flow.xrays_confirmed = False
        self.flow.camera_issue = True
        self._render_radiography_text()
        self._refresh_zp_status()
        self._refresh_enabled()

    def _read_mono_energy(self):
        try:
            return float(self._pv(MONO_ENERGY_PV).get())
        except Exception:
            return None

    def _prompt_saxs_beamstop_reminder(self):
        """Remind the operator to insert the SAXS beamstop after pulling ZP.

        Purely informational: pressing either button here moves no motors --
        use the "Insert SAXS beamstop" button below for that. Skipped
        entirely once expert mode is already on; the "I'm an expert" button
        does NOT turn expert mode on, it only lets this one reminder be
        dismissed without the move. An unreadable energy is treated as the
        mandatory case (no skip option) rather than assumed safe to bypass.
        """
        if self.flow.expert:
            return
        energy = self._read_mono_energy()
        low_energy = energy is not None and energy <= SAXS_BS_LOW_ENERGY_KEV

        box = QMessageBox(self.ui)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("SAXS beamstop")
        box.setText("Insert the SAXS beamstop before continuing.")
        box.setInformativeText(
            "Mono energy: %.3f keV" % energy if energy is not None
            else "Could not read the mono energy (%s)." % MONO_ENERGY_PV)
        box.addButton(QMessageBox.Ok)
        if low_energy:
            box.addButton("I'm an expert", QMessageBox.NoRole)
        box.exec_()

    def _pull_zp_out(self):
        ver, hor = self._zp_targets
        if ver is None or hor is None:
            return
        try:
            for pv_name, target in zip(ZP_PVS, (ver, hor)):
                self._pv("%s.VAL" % pv_name).put(target)
        except Exception as exc:
            QMessageBox.warning(self.ui, "ZP optics",
                                "Could not move the ZP optics:\n%s" % exc)
            return
        if self._debug_devices():
            # optics_motors.py runs in its own process with its own fake
            # motor state, and FakePV.get() always returns 0 -- the RBV poll
            # below could never show the optics arriving. Track the
            # commanded state instead.
            self._zp_out_commanded = True
        self._prompt_saxs_beamstop_reminder()

    def _refresh_zp_status(self):
        ver, hor = self._zp_targets
        label = self.ui.lb_zp_status
        if ver is None or hor is None:
            # No saved Out position to move to or compare against. Rather than
            # locking Continue forever, skip the gate and say why.
            self.ui.pb_zp_out.setEnabled(False)
            self.ui.lb_zp_note.setText(
                "No saved ZP Out position in optics_motors.ini - pull the ZP "
                "optics out from the optics GUI, then continue.")
            label.setText("----")
            label.setStyleSheet(_PILL_UNKNOWN)
            self.flow.zp_out = True
            return

        if not self.flow.xrays_confirmed:
            # Pulling ZP optics only makes sense once X-rays are confirmed on
            # the camera -- lock the button until then.
            self.ui.pb_zp_out.setEnabled(False)
            self.ui.lb_zp_note.setText(
                "Confirm X-rays are on the camera before pulling the ZP "
                "optics out.")
        else:
            self.ui.pb_zp_out.setEnabled(True)
            self.ui.lb_zp_note.setText("")

        if self._debug_devices():
            is_out = self._zp_out_commanded
            self.flow.zp_out = is_out
            label.setText("Out" if is_out else "In")
            label.setStyleSheet(_PILL_OUT if is_out else _PILL_IN)
            return

        try:
            readbacks = [self._pv("%s.RBV" % name).get() for name in ZP_PVS]
        except Exception:
            readbacks = [None, None]
        is_out = all(zp_is_out(rbv, target)
                     for rbv, target in zip(readbacks, (ver, hor)))
        self.flow.zp_out = is_out
        if is_out:
            label.setText("Out")
            label.setStyleSheet(_PILL_OUT)
        elif any(rbv is None for rbv in readbacks):
            label.setText("----")
            label.setStyleSheet(_PILL_UNKNOWN)
        else:
            label.setText("In")
            label.setStyleSheet(_PILL_IN)

    def _insert_saxs_bs(self):
        """Move both SAXS beamstop axes to their saved In positions.

        Optional -- this never gates Continue. The In positions come from the
        optics GUI's [SAXSbs] block.
        """
        ver, hor = self._saxs_bs_targets
        if ver is None or hor is None:
            return
        try:
            for pv_name, target in zip(SAXS_BS_PVS, (ver, hor)):
                self._pv("%s.VAL" % pv_name).put(target)
        except Exception as exc:
            QMessageBox.warning(self.ui, "SAXS beamstop",
                                "Could not move the SAXS beamstop:\n%s" % exc)

    def _refresh_saxs_bs(self):
        """Show 12ideSFT:m4's live position next to its saved In position."""
        _ver, hor = self._saxs_bs_targets
        if hor is None:
            self.ui.pb_saxs_bs_in.setEnabled(False)
            self.ui.pb_saxs_bs_in.setToolTip(
                "No saved SAXS beamstop In position - set one in the optics "
                "GUI first.")
            self.ui.lb_saxs_bs_in_pos.setText("----")
        else:
            self.ui.pb_saxs_bs_in.setEnabled(True)
            self.ui.lb_saxs_bs_in_pos.setText("%0.4f" % hor)
        try:
            readback = self._pv("%s.RBV" % SAXS_BS_SHOWN_PV).get()
        except Exception:
            readback = None
        self.ui.lb_saxs_bs_rbv.setText(
            "----" if readback is None else "%0.4f" % float(readback))

    def _render_radiography_text(self):
        self.ui.lb_radio_instructions.setText(
            _CAMERA_ISSUE_TEXT if self.flow.camera_issue else _RADIOGRAPHY_TEXT)

    def _render_radiography(self):
        self._render_radiography_text()
        self._syncing_expert = True
        self.ui.checkBox_expertUser.setChecked(self.flow.expert)
        self._syncing_expert = False
        self._refresh_eye_buttons()
        # The ZP pill and the SAXS readback are refreshed by the poll timer.

    # -- iterative page ----------------------------------------------------

    def _render_iterative(self):
        state = self.flow.iterative_state()
        table = self._tables[Page.ITERATIVE]

        table.set_step("phi", state["phi_step"])
        table.set_tweak_editable("phi", state["phi_editable"])
        self._apply_enables(table)

        spec = state["btn_a"]
        # Qt reads a lone "&" in button text as a mnemonic marker.
        self.ui.pb_iter_a.setText(spec["text"].replace("&", "&&"))
        self.ui.pb_iter_a.setEnabled(spec["enabled"])
        self.ui.pb_iter_back.setEnabled(state["back"]["enabled"])
        self.ui.pb_iter_finish.setEnabled(state["finish"]["enabled"])
        self.ui.lb_iter_script.setText(
            script_html(iterative_script_lines(state["step"])))

    # -- rendering ---------------------------------------------------------

    def _apply_enables(self, table):
        """Enable the flow's baseline plus this visit's unlocks, minus
        anything the hardware cannot drive."""
        keys = self._enabled_keys(table)
        for key in table.keys():
            table.set_row_enabled(key, key in keys and self._is_connected(key))

    def _apply_trans_labels(self):
        """Show transH / transD everywhere once the choice has been made.

        The row keys never change -- only the displayed name -- so `pts` is
        still addressed by the real motor name.
        """
        horizontal = self.flow.trans_h
        downstream = self.flow.trans_d
        for table in self._tables.values():
            keys = table.keys()
            if "transD" in keys:
                # Named by its role, like every other page; the real stage
                # is on the tooltip for anyone who needs to know.
                table.set_label("transD", "transD")
                name_cell = table.widget("transD", "name")
                if name_cell is not None:
                    name_cell.setToolTip(
                        "The trans stage not chosen as horizontal%s"
                        % (" (%s)" % downstream if downstream else ""))
            for key in ("trans1", "trans2"):
                if key not in keys:
                    continue
                if horizontal is None:
                    table.set_label(key, key)
                else:
                    table.set_label(
                        key, "transH" if key == horizontal else "transD")

    def _render_limit_edits(self):
        for which, value in (("low", self.flow.phi_low),
                             ("high", self.flow.phi_high),
                             ("zero", self.flow.phi_zero)):
            for edit in self._limit_edits(which):
                text = "%g" % value
                if edit.text() != text:
                    edit.setText(text)

    def _render_picker(self):
        """All three transH pickers -- rough centre, the rotation-axes detour
        and the expert merge -- show the same choice."""
        horizontal = self.flow.trans_h
        text = ("Horizontal motor is: %s" % horizontal if horizontal
                else "Horizontal motor is:")
        self.ui.lb_transh_prompt.setText(text)
        self.ui.lb_rotaxes_transh_prompt.setText(text)
        self.ui.lb_x_transh_prompt.setText(text)

    def _refresh_enabled(self):
        ui = self.ui
        ui.pb_radio_continue.setEnabled(self.flow.can(Action.RADIO_CONTINUE))
        ui.pb_fresh_start.setEnabled(self.flow.can(Action.MOVE_FRESH_START))
        ui.pb_sample_change.setEnabled(self.flow.can(Action.MOVE_SAMPLE_CHANGE))
        ui.pb_rough_continue.setEnabled(self.flow.can(Action.ROUGH_CONTINUE))
        ui.pb_setrot_continue.setEnabled(self.flow.can(Action.ROT_CONTINUE))
        # Both rotation-safety exits, on the user page and the expert merge.
        # The expert page also gates on transH, which it absorbed from the
        # rough-centre step.
        can_continue = self.flow.can(Action.ROTSAFE_CONTINUE)
        ui.pb_rotsafe_continue.setEnabled(can_continue)
        ui.pb_posrot_continue.setEnabled(can_continue)

    def _refresh_footer(self):
        table = self._tables.get(self.flow.page)
        locked = self._locked_keys(table) if table is not None else set()
        self.ui.pb_unlock_all.setEnabled(bool(locked))
        self.ui.pb_unlock_all.setToolTip(
            "Enable %s for this step only." % ", ".join(sorted(locked))
            if locked else "Nothing is locked on this step.")

        can_back = self.flow.can(Action.GO_BACK)
        self.ui.pb_back.setEnabled(can_back)
        self.ui.pb_back.setToolTip(
            self.flow.destination_label(Action.GO_BACK) if can_back
            else "This is the first step.")

    def _refresh_tooltips(self):
        """Point every navigating button at the page it currently leads to.

        Recomputed per page change because several exits are branch-dependent.
        """
        for button, action in self._nav_buttons.get(self.flow.page, []):
            if action is None:
                continue
            button.setToolTip(self.flow.destination_label(action))
        if self.flow.page == Page.ITERATIVE:
            action = self.flow.iterative_state()["btn_a"]["action"]
            self.ui.pb_iter_a.setToolTip(
                self.flow.destination_label(action) if action else "")

    def _render_page(self):
        page = self.flow.page
        widget = getattr(self.ui, PAGE_WIDGETS[page])
        self.ui.stackedWidget.setCurrentIndex(
            self.ui.stackedWidget.indexOf(widget))
        self.ui.setWindowTitle(window_title(page))

        # "Unlock all" is scoped to one visit of one step, so any change of
        # either re-locks whatever it opened up.
        render_key = (page, self.flow.iter_step)
        if render_key != self._render_key:
            self._unlocked = set()
            self._render_key = render_key

        self._render_limit_edits()
        self._render_picker()
        self._apply_trans_labels()

        if page == Page.RADIOGRAPHY:
            self._render_radiography()
        elif page == Page.FIRST_TRANS:
            self.ui.lb_firsttrans_which.setText(
                "Horizontal stage: %s" % (self.flow.trans_h or "not chosen"))
        elif page == Page.ITERATIVE:
            self._render_iterative()

        table = self._tables.get(page)
        if table is not None and page != Page.ITERATIVE:
            self._apply_enables(table)

        self._refresh_enabled()
        self._refresh_footer()
        self._refresh_tooltips()
        self._refresh_live()

    def _refresh_live(self):
        """Poll whatever the visible page displays live.

        Only the current page's motors are read. Guarded against re-entry: on
        real hardware each get_pos() is a network round-trip, and a slow read
        must not let timer ticks pile up behind it.
        """
        if self._polling:
            return
        self._polling = True
        try:
            if self.flow.page == Page.RADIOGRAPHY:
                # No motor rows here, but the ZP pill gates Continue, the
                # SAXS readback is live, and the eye state can change in
                # another process.
                self._refresh_zp_status()
                self._refresh_saxs_bs()
                self._refresh_eye_buttons()
                self._refresh_enabled()
                return
            table = self._tables.get(self.flow.page)
            if table is None:
                return
            for key in table.keys():
                axis = self._axis(key)
                if axis is None or not self._is_connected(key):
                    continue
                try:
                    table.set_current(key, self.main.pts.get_pos(axis))
                except Exception:
                    pass  # transient read failure; try again next tick
        finally:
            self._polling = False

    # -- window lifecycle --------------------------------------------------

    def show(self):
        """Show or raise the window, restarting the flow on a fresh open."""
        if not self.ui.isVisible():
            self.flow.start(expert=expert_mode_enabled())
            self._load_phi_settings()
            self._zp_targets = read_zp_out_positions()
            self._saxs_bs_targets = read_saxs_bs_in_positions()
            self._zp_out_commanded = False
            self._render_key = None
            self._render_page()
        self.ui.show()
        self.ui.raise_()
        self.ui.activateWindow()
        self._timer.start(POS_POLL_MS)

    def _close_event(self, event):
        if not self._closing_confirmed and self.flow.page != Page.RADIOGRAPHY:
            reply = QMessageBox.question(
                self.ui,
                "Abandon alignment?",
                "Close the sample alignment window?\n\n"
                "Progress through the workflow is discarded. Nothing moves - "
                "the sample stays exactly where it is.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        self._closing_confirmed = False
        self._timer.stop()
        _settings().setValue(GEOMETRY_KEY, self.ui.saveGeometry())
        event.accept()
