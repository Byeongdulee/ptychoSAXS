"""Guided sample-alignment workflow, opened from the main panel's
pb_sample_alignment button (see rungui.py's _open_sample_alignment_window).

One window, one QStackedWidget, one page per step. Every routing, gating and
tooltip decision lives in alignment_flow.AlignmentFlow, which is Qt-free and
unit-tested; this module only renders that state and drives motors. Motion
reuses rungui's own move/mover QRunnables and the main window's threadpool --
nothing here re-implements motor control.

The window is a controller, not a widget: self.ui (loaded from
sample_alignment.ui) is the real window, matching the convention in rungui.py
and CRL_3dprint.py.
"""

import configparser
import os
from html import escape

from PyQt5 import uic
from PyQt5.QtCore import QObject, QSettings, QTimer
from PyQt5.QtWidgets import QMessageBox

from alignment_flow import (
    CLOSE,
    PAGE_TITLES,
    Action,
    AlignmentFlow,
    Page,
    iterative_script_lines,
)
from font_utils import apply_saved_font_size
from ini_utils import INI_DIR
from motor_rows import MotorRow, MotorRowTable
from resize_utils import ProportionalResizer

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_UI_PATH = os.path.join(_MODULE_DIR, "sample_alignment.ui")

# ---------------------------------------------------------------------------
# Tunables -- deliberately code-only, not exposed in the GUI
# ---------------------------------------------------------------------------

# Hexapod X is parked here when a fresh start resets the tomography centre of
# rotation. Change this one value if the nominal COR position moves.
HEXAPOD_X_COR_DEFAULT = 1.3  # mm

STEP_MOVEMENT_SAFE = 0.25  # mm, fixed
STEP_SET_ROTATION = 2.0  # deg, fixed
STEP_ROTATION_SAFE = 10.0  # deg
STEP_TRANS_PHI = 180.0  # deg, fixed -- the flip the trans alignment needs
STEP_SMALL_ANGLE_PHI = 10.0  # deg
STEP_LINEAR_DEFAULT = 0.1  # mm, editable starting step for X/Z/trans rows

# Zone plate optics. ZP_ver / ZP_hor, matching scan_handler._US_OPTICS_PVS.
ZP_PVS = ("12idc:m12", "12idc:m13")
ZP_OUT_THRESH = 0.005  # same tolerance as optics_motors.MotorPresetBlock
ZP_INI_PATH = os.path.join(INI_DIR, "optics_motors.ini")

# X-ray eye. STATUS.VAL == 0 means the eye is OUT of the beam.
EYE_STATUS_PV = "usxRIO:Galil2Bo0_STATUS.VAL"
EYE_CMD_PV = "usxRIO:Galil2Bo0_CMD"  # put(1) = in, put(0) = out

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

PHI_LOW_DEFAULT = -540.0
PHI_HIGH_DEFAULT = 540.0
PHI_ZERO_DEFAULT = 0.0


def _settings():
    return QSettings("ptychoSAXS", "ptychoSAXS")


def expert_mode_enabled():
    """Read the setup window's expert-mode checkbox state."""
    return _settings().value(EXPERT_KEY, False, type=bool)


# Page -> the object name of its QStackedWidget page, so indices are resolved
# by name rather than hardcoded (reordering pages in Designer stays safe).
PAGE_WIDGETS = {
    Page.RADIOGRAPHY: "page_radiography",
    Page.MOVEMENT_SAFE: "page_movement_safe",
    Page.ROUGH_CENTER: "page_rough_center",
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

_CAMERA_ISSUE_TEXT = """Camera issues!
- Try toggling X-ray eye in/out once
- Check camera IOC:
-- Is it running?
-- Is the exposure time enough?"""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly -- no Qt, no hardware)
# ---------------------------------------------------------------------------

def eye_is_out(status_value):
    """True when the X-ray eye is retracted out of the beam.

    usxRIO:Galil2Bo0_STATUS.VAL == 0 means OUT. This matches
    optics_motors._is_xrayeye_out, which is the reading the optics GUI's
    move guard has always used.
    """
    return status_value == 0


def read_zp_out_positions(ini_path=ZP_INI_PATH):
    """(ZP_ver, ZP_hor) saved Out positions from the optics GUI's .ini.

    Returns (None, None) when the file, the [zp] section, or either value is
    missing -- optics_motors seeds out_0/out_1 as empty strings, meaning "no
    position has been saved yet".
    """
    parser = configparser.ConfigParser()
    try:
        parser.read(ini_path)
    except (configparser.Error, OSError):
        return (None, None)
    if not parser.has_section("zp"):
        return (None, None)
    values = []
    for key in ("out_0", "out_1"):
        try:
            values.append(float(parser.get("zp", key, fallback="").strip()))
        except (ValueError, AttributeError):
            return (None, None)
    return (values[0], values[1])


def zp_is_out(readback, target, thresh=ZP_OUT_THRESH):
    """True when a ZP motor has reached its saved Out position."""
    if readback is None or target is None:
        return False
    return abs(float(readback) - float(target)) <= thresh


def script_html(lines):
    """Render iterative_script_lines() output as the label's rich text."""
    parts = []
    for text, colour in lines:
        colour_hex = "#000000" if colour == "black" else "#888888"
        parts.append('<span style="color:%s;">%s</span>' % (colour_hex, escape(text)))
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
        self._pv_cache = {}
        self._zp_targets = read_zp_out_positions()

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_positions)

        # Rows must exist before the resizer snapshots geometry, or the
        # generated widgets never scale with the window.
        self._build_tables()
        self._wire_buttons()

        self._resizer = ProportionalResizer(self.ui)
        self.ui.setMinimumSize(
            int(self._resizer.orig_size.width() * 0.4),
            int(self._resizer.orig_size.height() * 0.4),
        )
        geometry = _settings().value(GEOMETRY_KEY)
        if geometry is not None:
            self.ui.restoreGeometry(geometry)
            self._resizer.rescale()

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

        for name in ("pb_radio_cancel", "pb_movesafe_cancel", "pb_rough_cancel",
                     "pb_setrot_cancel", "pb_rotsafe_cancel", "pb_posrot_cancel",
                     "pb_firsttrans_cancel", "pb_secondtrans_cancel",
                     "pb_transboth_cancel", "pb_cor_cancel",
                     "pb_smallangle_cancel", "pb_iter_cancel",
                     "pb_change_cancel"):
            getattr(ui, name).clicked.connect(self._cancel)

        # -- radiography
        ui.pb_eye_in.clicked.connect(lambda: self._put_eye(True))
        ui.pb_eye_out.clicked.connect(lambda: self._put_eye(False))
        ui.pb_xrays_ok.clicked.connect(self._on_xrays_ok)
        ui.pb_xrays_no.clicked.connect(self._on_xrays_no)
        ui.pb_zp_out.clicked.connect(self._pull_zp_out)
        self._register_nav(Page.RADIOGRAPHY, ui.pb_radio_continue,
                           Action.RADIO_CONTINUE)

        # -- movement safe
        self._register_nav(Page.MOVEMENT_SAFE, ui.pb_fresh_start,
                           Action.MOVE_FRESH_START)
        self._register_nav(Page.MOVEMENT_SAFE, ui.pb_sample_change,
                           Action.MOVE_SAMPLE_CHANGE)

        # -- rough centre / set rotation
        self._register_nav(Page.ROUGH_CENTER, ui.pb_rough_continue,
                           Action.ROUGH_CONTINUE)
        self._register_nav(Page.SET_ROTATION, ui.pb_setrot_continue,
                           Action.ROT_CONTINUE)

        # -- rotation safety: the same strip appears on the user page and on
        # the expert merge, under an "x_" name prefix.
        for prefix in ("", "x_"):
            getattr(ui, "pb_%sset_low" % prefix).clicked.connect(self._set_phi_low)
            getattr(ui, "pb_%sset_high" % prefix).clicked.connect(self._set_phi_high)
            getattr(ui, "pb_%sset_zero" % prefix).clicked.connect(self._set_phi_zero)
            getattr(ui, "pb_%srot_360_ok" % prefix).clicked.connect(self._on_360_ok)
        ui.pb_rotsafe_continue.clicked.connect(self._on_rotsafe_continue)
        ui.pb_posrot_continue.clicked.connect(self._on_rotsafe_continue)
        self._nav_buttons[Page.ROTATION_SAFE] = [
            (ui.pb_rot_360_ok, Action.ROTSAFE_360_OK),
            (ui.pb_rotsafe_continue, Action.ROTSAFE_CONTINUE),
        ]
        self._nav_buttons[Page.POSITION_AND_ROTATION] = [
            (ui.pb_x_rot_360_ok, Action.ROTSAFE_360_OK),
            (ui.pb_posrot_continue, Action.ROTSAFE_CONTINUE),
        ]

        # -- trans pages
        self._register_nav(Page.FIRST_TRANS, ui.pb_first_trans1,
                           Action.TRANS1_HORIZONTAL)
        self._register_nav(Page.FIRST_TRANS, ui.pb_first_trans2,
                           Action.TRANS2_HORIZONTAL)
        self._register_nav(Page.SECOND_TRANS, ui.pb_second_finish,
                           Action.TRANS_FINISH)
        self._register_nav(Page.TRANS_BOTH, ui.pb_both_trans1,
                           Action.TRANS1_HORIZONTAL)
        self._register_nav(Page.TRANS_BOTH, ui.pb_both_trans2,
                           Action.TRANS2_HORIZONTAL)
        self._register_nav(Page.TRANS_BOTH, ui.pb_both_finish,
                           Action.TRANS_FINISH)
        self._register_nav(Page.SMALL_ANGLE_TRANS, ui.pb_small_finish,
                           Action.TRANS_FINISH)

        # -- centre-of-rotation question
        self._register_nav(Page.COR_KNOWN, ui.pb_cor_yes, Action.COR_YES)
        self._register_nav(Page.COR_KNOWN, ui.pb_cor_no, Action.COR_NO)

        # -- iterative: buttons a/b are re-purposed per sub-step, so they are
        # connected once to a dispatcher that reads the current action rather
        # than being disconnected and reconnected on every step change.
        ui.pb_iter_a.clicked.connect(lambda: self._iter_button("btn_a"))
        ui.pb_iter_b.clicked.connect(lambda: self._iter_button("btn_b"))
        self._register_nav(Page.ITERATIVE, ui.pb_iter_back, Action.ITER_BACK)
        self._register_nav(Page.ITERATIVE, ui.pb_iter_finish, Action.ITER_FINISH)
        self._nav_buttons[Page.ITERATIVE].extend([
            (ui.pb_iter_a, None), (ui.pb_iter_b, None)])

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

    def _iter_button(self, which):
        state = self.flow.iterative_state()
        action = state[which]["action"]
        if action is None or not state[which]["enabled"]:
            return
        if action == Action.UNLOCK_ALL:
            table = self._tables[Page.ITERATIVE]
            self._apply_enables(table, table.keys())
            return
        self._do(action)

    def _cancel(self):
        """Every page's Cancel button. The confirmation prompt lives in
        _close_event, so the title-bar X behaves identically."""
        self.ui.close()

    def _finish(self):
        """Finished alignment / Finished! -- close without confirming."""
        self._closing_confirmed = True
        self.ui.close()

    # -- rotation-safety handlers -----------------------------------------

    def _set_phi_low(self):
        value = self._phi_position()
        if value is None:
            return
        self.flow.set_phi_low(value)
        self._save_phi_settings()
        self._render_page()

    def _set_phi_high(self):
        value = self._phi_position()
        if value is None:
            return
        self.flow.set_phi_high(value)
        self._save_phi_settings()
        self._render_page()

    def _set_phi_zero(self):
        value = self._phi_position()
        if value is None:
            return
        self.flow.set_phi_zero(value)
        self._save_phi_settings()
        self._render_page()

    def _offer_phi_zero_move(self):
        """Ask whether to park phi at the zero angle before continuing.

        Both downstream paths assume the sample starts there: the trans
        alignment flips 0 -> 180 deg, and the iterative algorithm's step 2a
        does the same.
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

    def _on_360_ok(self):
        self._offer_phi_zero_move()
        self._do(Action.ROTSAFE_360_OK)

    def _on_rotsafe_continue(self):
        if not self.flow.can(Action.ROTSAFE_CONTINUE):
            return
        auto_safe = self.flow.rotation_arc_ok()
        chosen = self.flow.destination(Action.ROTSAFE_CONTINUE,
                                       rotation_safe=auto_safe)
        other = self.flow.destination(Action.ROTSAFE_CONTINUE,
                                      rotation_safe=not auto_safe)
        reply = QMessageBox.question(
            self.ui,
            "Rotation range",
            "Angle 0 is %g deg, soft limits are [%g, %g].\n"
            "  upper - zero = %g deg\n"
            "  zero - lower = %g deg\n\n"
            "A full alignment needs 180 deg from the zero angle, so this "
            "range counts as %s.\n\n"
            "Yes: continue to \"%s\"\n"
            "No:  override, continue to \"%s\" instead"
            % (self.flow.phi_zero, self.flow.phi_low, self.flow.phi_high,
               self.flow.phi_high - self.flow.phi_zero,
               self.flow.phi_zero - self.flow.phi_low,
               "SAFE" if auto_safe else "NOT SAFE",
               PAGE_TITLES[chosen], PAGE_TITLES[other]),
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Cancel:
            return
        safe = auto_safe if reply == QMessageBox.Yes else not auto_safe
        self._offer_phi_zero_move()
        self._do(Action.ROTSAFE_CONTINUE, rotation_safe=safe)

    # -- hexapod X reset ---------------------------------------------------

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
            self._dispatch_move("X", HEXAPOD_X_COR_DEFAULT, relative=False)

    # -- radiography page --------------------------------------------------

    def _pv_class(self):
        if getattr(self.main, "DEBUG_DEVICES", False):
            from debug_stubs import FakePV

            return FakePV
        from epics import PV

        return PV

    def _pv(self, name):
        """A cached PV handle for `name`.

        The ZP status pill polls at POS_POLL_MS, and constructing a fresh
        epics.PV on every tick would open a new channel each time.
        """
        pv = self._pv_cache.get(name)
        if pv is None:
            pv = self._pv_class()(name)
            self._pv_cache[name] = pv
        return pv

    def _put_eye(self, inserted):
        try:
            self._pv(EYE_CMD_PV).put(1 if inserted else 0)
        except Exception as exc:
            QMessageBox.warning(self.ui, "X-ray eye",
                                "Could not command the X-ray eye:\n%s" % exc)
            return
        # Track the commanded state rather than re-reading STATUS, which lags
        # the actual motion (and, in debug mode, never changes at all).
        self._set_eye_buttons(out=not inserted)

    def _set_eye_buttons(self, out):
        self.ui.pb_eye_in.setEnabled(bool(out))
        self.ui.pb_eye_out.setEnabled(not out)

    def _read_eye_state(self):
        try:
            return eye_is_out(self._pv(EYE_STATUS_PV).get())
        except Exception:
            return None

    def _on_xrays_ok(self):
        self.flow.xrays_confirmed = True
        self.flow.camera_issue = False
        self._render_page()

    def _on_xrays_no(self):
        # Seeing no X-rays invalidates an earlier confirmation, so the page
        # re-locks until the operator confirms again.
        self.flow.xrays_confirmed = False
        self.flow.camera_issue = True
        self._render_page()

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

        self.ui.pb_zp_out.setEnabled(True)
        self.ui.lb_zp_note.setText("")
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

    def _render_radiography(self):
        if self.flow.camera_issue:
            self.ui.lb_radio_instructions.setText(_CAMERA_ISSUE_TEXT)
        else:
            self.ui.lb_radio_instructions.setText(
                "Change to radiography mode:\n"
                "- Put X-ray eye in\n"
                "- Verify X-rays are on camera\n"
                "- If energy > 12 keV, insert\n"
                "SAXS beamstop\n"
                "- Remove ZP optics")
        out = self._read_eye_state()
        if out is not None:
            self._set_eye_buttons(out=out)
        # The ZP pill is refreshed by the poll timer (see _refresh_positions),
        # so it tracks the optics arriving at their Out position.

    # -- iterative page ----------------------------------------------------

    def _render_iterative(self):
        state = self.flow.iterative_state()
        table = self._tables[Page.ITERATIVE]

        table.set_step("phi", state["phi_step"])
        table.set_tweak_editable("phi", state["phi_editable"])
        if state["trans_h"]:
            table.set_label(state["trans_h"], "transH")
            table.set_label(state["trans_d"], "transD")
        else:
            table.set_label("trans1", "trans1")
            table.set_label("trans2", "trans2")
        self._apply_enables(table, state["motors"])

        self.ui.lb_iter_prompt.setText(state["prompt"])
        for widget, key in ((self.ui.pb_iter_a, "btn_a"),
                            (self.ui.pb_iter_b, "btn_b")):
            spec = state[key]
            # Qt reads a lone "&" in button text as a mnemonic marker.
            widget.setText(spec["text"].replace("&", "&&"))
            widget.setEnabled(spec["enabled"])
        self.ui.pb_iter_back.setEnabled(state["back"]["enabled"])
        self.ui.pb_iter_finish.setEnabled(state["finish"]["enabled"])
        self.ui.lb_iter_script.setText(
            script_html(iterative_script_lines(state["step"])))

    # -- rendering ---------------------------------------------------------

    def _apply_enables(self, table, keys):
        """Enable exactly `keys`, minus anything the hardware cannot drive."""
        for key in table.keys():
            table.set_row_enabled(key, key in keys and self._is_connected(key))

    def _render_limit_labels(self):
        for prefix in ("", "x_"):
            getattr(self.ui, "lb_%sphi_low" % prefix).setText(
                "%g" % self.flow.phi_low)
            getattr(self.ui, "lb_%sphi_high" % prefix).setText(
                "%g" % self.flow.phi_high)
            getattr(self.ui, "lb_%sphi_zero" % prefix).setText(
                "%g" % self.flow.phi_zero)

    def _refresh_enabled(self):
        ui = self.ui
        ui.pb_radio_continue.setEnabled(self.flow.can(Action.RADIO_CONTINUE))
        ui.pb_fresh_start.setEnabled(self.flow.can(Action.MOVE_FRESH_START))
        ui.pb_sample_change.setEnabled(self.flow.can(Action.MOVE_SAMPLE_CHANGE))
        ui.pb_setrot_continue.setEnabled(self.flow.can(Action.ROT_CONTINUE))
        can_continue = self.flow.can(Action.ROTSAFE_CONTINUE)
        ui.pb_rotsafe_continue.setEnabled(can_continue)
        ui.pb_posrot_continue.setEnabled(can_continue)

    def _refresh_tooltips(self):
        """Point every navigating button at the page it currently leads to.

        Recomputed per page change because several exits are branch-dependent.
        """
        for button, action in self._nav_buttons.get(self.flow.page, []):
            if action is None:
                continue
            button.setToolTip(self.flow.destination_label(action))
        if self.flow.page == Page.ITERATIVE:
            state = self.flow.iterative_state()
            for widget, key in ((self.ui.pb_iter_a, "btn_a"),
                                (self.ui.pb_iter_b, "btn_b")):
                action = state[key]["action"]
                if action == Action.UNLOCK_ALL:
                    widget.setToolTip(
                        "Temporarily enable every motor for this step only.")
                elif action is not None:
                    widget.setToolTip(self.flow.destination_label(action))
                else:
                    widget.setToolTip("")

    def _render_page(self):
        page = self.flow.page
        widget = getattr(self.ui, PAGE_WIDGETS[page])
        self.ui.stackedWidget.setCurrentIndex(
            self.ui.stackedWidget.indexOf(widget))

        self._render_limit_labels()

        if page == Page.RADIOGRAPHY:
            self._render_radiography()
        elif page == Page.SECOND_TRANS:
            self._tables[page].set_label("transD", self.flow.trans_d or "trans")
        elif page == Page.ITERATIVE:
            self._render_iterative()

        table = self._tables.get(page)
        if table is not None and page != Page.ITERATIVE:
            self._apply_enables(table, table.keys())

        self._refresh_enabled()
        self._refresh_tooltips()
        self._refresh_positions()

    def _refresh_positions(self):
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
                # This page shows no motors, but the ZP in/out pill has to
                # track the optics arriving at their Out position, and it
                # gates Continue.
                self._refresh_zp_status()
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
