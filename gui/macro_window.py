# -*- coding: utf-8 -*-
"""
Scan macro: a user-configurable sequence of motor moves, scans, and waits.

Opened from the main panel's pb_writeMacro button (see rungui.py's
_open_macro_window). Every "command" in the macro is executed by calling the
exact same function the corresponding main-panel button/line-edit already
uses (mv/mvr for motor moves, QPushButton.click() for scans, QLineEdit.setText
for staging From/To/Step/Exposure values) -- nothing here re-implements scan
or motor logic.
"""
import json
import os
import re
import threading
import time

from PyQt5.QtCore import Qt, QObject, pyqtSignal, QSettings
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QFileDialog,
)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_SAVE_PATH = os.path.join(_MODULE_DIR, "macro_commands.json")

# QSettings key for the last directory used by Export/Import/Append Macro --
# same persistence mechanism (and organization/app name) rungui.py already
# uses for remembered filepaths, e.g. _attr_filepaths()'s
# "layout/ptychoAttributesPath".
MACRO_DIALOG_DIR_KEY = "macro/importExportDir"

# Motor slot numbers (1-based, n = motor list index + 1) that have real 1D
# fly/step buttons on the main panel -- see rungui.py's motor wiring loop
# (`if n in (1, 2, 3, 7, 8, 9): pb_lup_%i / pb_SAXSscan_%i`).
PER_MOTOR_SCAN_SLOTS = (1, 2, 3, 7, 8, 9)

# Slot numbers the fixed 2D/3D/helix scan buttons actually read from --
# matches rungui.py's DEFAULTS (xmotor=0 -> slot1, ymotor=2 -> slot3,
# phimotor=6 -> slot7). Used only for time estimation.
SCAN_ESTIMATE_SLOTS = {"x": 1, "z": 3, "phi": 7}

# Static (non-per-motor) commands, keyed by a short token used in
# COMMAND_ORDER below. pb_timeSeries is intentionally not offered here: it
# opens a modal setup dialog and can't run unattended in a macro.
STATIC_COMMANDS = {
    "wait": {
        "display": "Wait",
        "kind": "wait",
        "needs_param": True,
        "param_label": "seconds",
    },
    "fly2d_snake": {
        "display": "Fly2D snake (X-Z)",
        "kind": "click",
        "button_name": "pb_SAXSscan_fly2d",
    },
    "step2d": {
        "display": "Step2D (X-Z)",
        "kind": "click",
        "button_name": "pb_lup_step2d",
    },
    "fly3d_snake": {
        "display": "Fly3D snake (X-Z-phi)",
        "kind": "click",
        "button_name": "pb_SAXSscan_fly3d",
    },
    "step3d": {
        "display": "Step3D (X-Z-phi)",
        "kind": "click",
        "button_name": "pb_lup_step3d",
    },
    # pb_helix_scan's real click handler (helix_fly_choose_axis) pops a modal
    # "X or Z?" QInputDialog -- can't run unattended, so the macro instead
    # calls scan_handler.helix_fly(...) directly with the axis this command's
    # own lineEdit specifies, bypassing that dialog entirely. button_name is
    # kept (not clicked) purely so the button still gets disabled while the
    # macro window is open, like every other scan button.
    "helix": {
        "display": "Helix Scan",
        "kind": "helix",
        "needs_param": True,
        "param_label": "axis: X or Z",
        "param_kind": "axis_letter",
        "button_name": "pb_helix_scan",
    },
    "takeshot": {
        "display": "Take Shot",
        "kind": "click",
        "button_name": "pb_takeshot",
    },
    # Exposure time is a single shared field (ed_lup_1_t) -- not per-motor,
    # unlike From/To/Step below (see scan_handler.py's _read_motor_params).
    "set_expt": {
        "display": "Set Exposure Time",
        "kind": "set_field",
        "needs_param": True,
        "param_label": "seconds",
        "widget_name": "ed_lup_1_t",
    },
}

# ed_lup_<n>_<suffix> widgets used by scan setup (see scan_handler.py's
# _read_motor_params): L=start offset ("From"), R=end offset ("To"),
# N=step size. Only exist for motor slots in PER_MOTOR_SCAN_SLOTS. A
# "set_*" command just writes a value into the named line edit -- no button
# is pressed and no motor moves; it stages a value for a scan command added
# later in the macro.
SET_FIELD_SUFFIXES = {
    "set_from": ("L", "From"),
    "set_to": ("R", "To"),
    "set_step": ("N", "Step"),
}

# Per-motor group tokens -- each expands, in place, into one combo-box entry
# per connected motor (in self.main.motornames order). "fly_1d"/"step_1d"/
# "set_from"/"set_to"/"set_step" only produce an entry for motor slots in
# PER_MOTOR_SCAN_SLOTS (the ones that actually have those widgets).
PER_MOTOR_GROUPS = (
    "move_abs",
    "move_rel",
    "set_from",
    "set_to",
    "set_step",
    "fly_1d",
    "step_1d",
)

# Group-picker (first comboBox) label for each per-motor token. Static
# tokens don't need an entry here -- their own STATIC_COMMANDS["display"]
# is used as the group label directly, since there's only ever one command
# in that group.
GROUP_LABELS = {
    "move_abs": "Move Absolute",
    "move_rel": "Move Relative",
    "set_from": "Set From",
    "set_to": "Set To",
    "set_step": "Set Step",
    "fly_1d": "Fly (1D)",
    "step_1d": "Step (1D)",
}

# ---------------------------------------------------------------------------
# Edit this list BY HAND to control the comboBox order. Freely mix tokens
# from STATIC_COMMANDS with the PER_MOTOR_GROUPS tokens above -- each
# per-motor token expands in place into one entry per connected motor.
# ---------------------------------------------------------------------------
COMMAND_ORDER = [
    "wait",
    "move_abs",
    "move_rel",
    "set_from",
    "set_to",
    "set_step",
    "set_expt",
    "fly2d_snake",
    "step2d",
    "fly3d_snake",
    "step3d",
    "fly_1d",
    "step_1d",
    "helix",
    "takeshot",
]


class MacroExecutionEngine(QObject):
    """Runs a macro command list on a background thread.

    Scan/motor calls are marshalled onto the GUI thread via queued signals
    (mirrors gui/network/server.py's cross-thread pattern to ptyco_main_control):
    this object is created on, and never moved off, the GUI thread, so PyQt
    auto-queues delivery of _invokeCommand/_invokeStopScan whenever they are
    emitted from the background thread started by start().
    """

    stepStarted = pyqtSignal(int)
    stepFinished = pyqtSignal(int, bool)
    macroStarted = pyqtSignal()
    macroFinished = pyqtSignal(bool)
    _invokeCommand = pyqtSignal(dict)
    _invokeStopScan = pyqtSignal()

    # A scan step is considered "never started" (and fails) if self.main.isscan
    # doesn't go True within this many seconds of clicking its button.
    SCAN_START_TIMEOUT_S = 30.0
    # Overall safety-net cap for a single step (move or scan), used only
    # while NOT stopping. Needed because self.main.isscan is not reliably
    # reset to False if a scan's worker thread errors out (a pre-existing
    # gap in rungui.py's _on_worker_error) -- without this cap a hardware
    # error mid-scan could hang the macro forever.
    MAX_STEP_WAIT_S = 3600.0
    # Once Stop Macro has pressed the real Stop Scan button, how long to
    # wait for self.main.isscan to actually clear before giving up. Short on
    # purpose: MAX_STEP_WAIT_S is the right safety net for a scan quietly
    # running long, but once the user has explicitly asked to stop, hanging
    # onto the same hour-long budget would leave "Stop Macro" doing nothing
    # visible and the window unclosable for that whole time.
    STOP_GRACE_PERIOD_S = 20.0
    SCAN_POLL_INTERVAL_S = 0.05
    # Mandatory settle time after any scan (kind == "click" or "helix")
    # completes -- not applied after moves, field-writes, or Wait commands.
    POST_SCAN_SLEEP_S = 5.0

    def __init__(self, main_controller, parent=None):
        super().__init__(parent)
        self.main = main_controller
        self._running = False
        self._stop_requested = threading.Event()
        self._action_done_event = threading.Event()
        self._action_error = None
        self._invokeCommand.connect(self._on_invoke_command)
        self._invokeStopScan.connect(self._on_invoke_stop_scan)

    @property
    def is_running(self):
        return self._running

    def start(self, commands):
        """GUI thread. commands is a snapshot list of plain command dicts."""
        if self._running:
            return
        self._running = True
        self._stop_requested.clear()
        self.macroStarted.emit()
        threading.Thread(
            target=self._run_loop, args=(list(commands),), daemon=True
        ).start()

    def stop(self):
        """GUI thread. Cuts off remaining steps. The current step is allowed
        to finish naturally if it's a motor move; if it's a running scan,
        the real Stop Scan button is pressed to cut it short cooperatively.
        """
        self._stop_requested.set()

    # ---- background thread -------------------------------------------------
    def _run_loop(self, commands):
        self.main.macro_running = True
        completed_clean = True
        try:
            for idx, cmd in enumerate(commands):
                if self._stop_requested.is_set():
                    completed_clean = False
                    break
                time.sleep(0.01)  # mandated default settle delay per step
                self.stepStarted.emit(idx)
                ok = self._execute_one(cmd)
                self.stepFinished.emit(idx, ok)
                if not ok:
                    completed_clean = False
                    break
        finally:
            self.main.macro_running = False
            self._running = False
            self.macroFinished.emit(completed_clean)

    def _execute_one(self, cmd):
        kind = cmd.get("kind")
        try:
            if kind == "wait":
                time.sleep(max(0.0, float(cmd.get("param") or 0.0)))
                return True
            if kind in ("move_abs", "move_rel", "set_field", "scan_param_snapshot"):
                return self._run_gui_action(cmd)
            if kind in ("click", "helix"):
                ok = self._run_click(cmd)
                if not self._stop_requested.is_set():
                    time.sleep(self.POST_SCAN_SLEEP_S)
                return ok
        except Exception as e:
            print(f"Macro step failed: {e}")
            return False
        print(f"Macro: unknown command kind {kind!r}")
        return False

    def _run_gui_action(self, cmd):
        """Runs move_abs/move_rel/set_field/scan_param_snapshot: emit to the
        GUI thread and block until it reports done (immediately for
        set_field/scan_param_snapshot, or once the move worker's
        finished/error signal fires for move_abs/move_rel)."""
        self._action_done_event.clear()
        self._action_error = None
        self._invokeCommand.emit(cmd)
        finished = self._action_done_event.wait(self.MAX_STEP_WAIT_S)
        if not finished:
            return False
        return self._action_error is None

    def _run_click(self, cmd):
        self._invokeCommand.emit(cmd)
        return self._wait_for_scan_completion()

    def _wait_for_scan_completion(self):
        # Detection is by polling self.main.isscan (True while a scan runs,
        # reset False on completion uniformly by _launch_worker/scandone) --
        # this codebase has no dedicated "scan finished" signal. A very fast
        # scan could in principle flip True->False between two polls;
        # SCAN_POLL_INTERVAL_S is kept small to minimize that window.
        started = False
        t0 = time.time()
        stop_requested_at = None
        while True:
            if self.main.isscan:
                started = True
            elif started:
                return True
            elif self._stop_requested.is_set():
                # Stopped before the scan ever started (isscan never went
                # True) -- nothing running to wait on, so give up now
                # instead of waiting out SCAN_START_TIMEOUT_S.
                return False
            now = time.time()
            if not started and now - t0 > self.SCAN_START_TIMEOUT_S:
                return False
            if self._stop_requested.is_set() and started:
                if stop_requested_at is None:
                    stop_requested_at = now
                    self._invokeStopScan.emit()
                elif now - stop_requested_at > self.STOP_GRACE_PERIOD_S:
                    # Stop Macro was pressed but the scan never actually wound
                    # down (e.g. the isscan-not-reset-on-error gap noted
                    # above) -- give up waiting rather than hold the whole
                    # window hostage for up to MAX_STEP_WAIT_S.
                    return False
            elif now - t0 > self.MAX_STEP_WAIT_S:
                return False
            time.sleep(self.SCAN_POLL_INTERVAL_S)

    # ---- GUI-thread slots (invoked via queued connection) -------------------
    def _on_invoke_command(self, cmd):
        kind = cmd.get("kind")
        if kind == "click":
            button_name = cmd.get("button_name")
            btn = (
                self.main.ui.findChild(QPushButton, button_name)
                if button_name
                else None
            )
            if btn is None:
                print(f"Macro: button '{button_name}' not found")
                return
            # QAbstractButton.click() is a no-op while the button is disabled
            # -- and MacroWindow disables every scan button on the main panel
            # while it's open (see showEvent/hideEvent), to keep the user
            # from starting a conflicting scan by hand. Flip it on just long
            # enough to fire the click, then restore whatever state it had.
            was_enabled = btn.isEnabled()
            btn.setEnabled(True)
            btn.click()
            btn.setEnabled(was_enabled)
        elif kind == "helix":
            # Bypasses pb_helix_scan's real click handler (helix_fly_choose_axis),
            # which pops a modal "X or Z?" dialog -- calls helix_fly directly
            # with the axis this command's own lineEdit chose instead.
            axis_letter = (cmd.get("param") or "").strip().upper()
            motor_index = (
                self.main.motornames.index(axis_letter)
                if axis_letter in self.main.motornames
                else None
            )
            if motor_index is None or not self.main.motorconnected[motor_index]:
                print(f"Macro: helix axis '{axis_letter}' not connected")
                return
            self.main.scan_handler.helix_fly(motor_index, SCAN_ESTIMATE_SLOTS["phi"] - 1)
        elif kind == "move_abs":
            n = cmd.get("motor_index") + 1
            self._set_line_edit_text("ed_%i" % n, cmd.get("param"))
            w = self.main.mv(motornumber=cmd.get("motor_index"), val=cmd.get("param"))
            self._wire_move_worker(w)
        elif kind == "move_rel":
            n = cmd.get("motor_index") + 1
            param = cmd.get("param")
            magnitude = abs(param)
            # Tweak Step always shows a positive magnitude, matching what a
            # person sees after typing a step size and pressing one of the
            # two arrow (pb_tweak%iL/pb_tweak%iR) buttons; direction comes
            # from sign, not from the line edit's sign.
            self._set_line_edit_text("ed_%i_tweak" % n, magnitude)
            sign = 1 if param > 0 else -1
            w = self.main.mvr(
                motornumber=cmd.get("motor_index"), sign=sign, val=magnitude
            )
            self._wire_move_worker(w)
        elif kind == "set_field":
            widget_name = cmd.get("widget_name")
            if self._set_line_edit_text(widget_name, cmd.get("param")):
                self._action_error = None
            else:
                self._action_error = f"field '{widget_name}' not found"
            self._action_done_event.set()
        elif kind == "scan_param_snapshot":
            missing = [
                name
                for name, value in (cmd.get("field_values") or {}).items()
                if not self._set_line_edit_text(name, value)
            ]
            self._action_error = f"fields not found: {missing}" if missing else None
            self._action_done_event.set()

    def _set_line_edit_text(self, widget_name, value):
        """GUI thread. Writes value into the named QLineEdit, mirroring what
        a person would see after typing it in by hand. Returns False (and
        does nothing) if the widget doesn't exist."""
        ed = self.main.ui.findChild(QLineEdit, widget_name) if widget_name else None
        if ed is None:
            print(f"Macro: line edit '{widget_name}' not found")
            return False
        ed.setText("%0.4f" % float(value))
        return True

    def _on_invoke_stop_scan(self):
        self.main.stopscan()

    def _wire_move_worker(self, w):
        if w is None:
            self._action_error = "move did not start (no worker returned)"
            self._action_done_event.set()
            return
        w.signal.finished.connect(self._on_action_finished)
        w.signal.error.connect(self._on_action_error)

    def _on_action_finished(self, _ok):
        self._action_error = None
        self._action_done_event.set()

    def _on_action_error(self, msg):
        self._action_error = msg
        self._action_done_event.set()


class MacroWindow(QWidget):
    """Non-modal popup: build and run a macro command list."""

    # Flat time estimate for a single motor move (absolute or relative), used
    # by "Estimate Macro Time" -- moves aren't simulated against real
    # hardware speed, just budgeted a fixed cost.
    STAGE_MOVE_ESTIMATE_S = 1.

    def __init__(self, main_controller, parent=None):
        super().__init__(parent)
        self.main = main_controller
        self.setWindowTitle("Scan Macro")
        self.setWindowFlags(Qt.Window)

        self._groups = self._build_command_groups()
        self._scan_button_prev_enabled = {}
        self._build_ui()

        self.engine = MacroExecutionEngine(self.main, parent=self)
        self.engine.stepStarted.connect(self._on_step_started)
        self.engine.stepFinished.connect(self._on_step_finished)
        self.engine.macroStarted.connect(self._on_macro_started)
        self.engine.macroFinished.connect(self._on_macro_finished)

        self._load_macro()

    # ---- command catalog -----------------------------------------------
    def _build_command_groups(self):
        """Build the two-level {group -> [specific commands]} structure the
        two comboBoxes are populated from, by walking COMMAND_ORDER in order
        -- the single source of truth for every selectable command and its
        order. To change what's offered or its order, edit COMMAND_ORDER /
        STATIC_COMMANDS / PER_MOTOR_GROUPS above; nothing else needs to.

        Returns a list of {"group_label": str, "entries": [spec, ...]}, one
        per COMMAND_ORDER token (a static token's group has exactly one
        entry; a per-motor token's group has one entry per connected motor
        that has the relevant widgets -- empty groups are dropped).
        """
        groups = []
        for token in COMMAND_ORDER:
            if token in STATIC_COMMANDS:
                spec = STATIC_COMMANDS[token]
                groups.append(
                    {"group_label": spec["display"], "entries": [self._static_catalog_entry(spec)]}
                )
            elif token in PER_MOTOR_GROUPS:
                entries = self._expand_per_motor_group(token)
                if entries:
                    groups.append({"group_label": GROUP_LABELS[token], "entries": entries})
            else:
                print(f"Macro: unknown COMMAND_ORDER token {token!r}, skipping")
        return groups

    @staticmethod
    def _static_catalog_entry(spec):
        return {
            "display": spec["display"],
            "kind": spec["kind"],
            "needs_param": spec.get("needs_param", False),
            "param_label": spec.get("param_label"),
            "param_kind": spec.get("param_kind", "float"),
            "button_name": spec.get("button_name"),
            "motor_index": None,
            "widget_name": spec.get("widget_name"),
        }

    def _expand_per_motor_group(self, token):
        entries = []
        for i, name in enumerate(self.main.motornames):
            if not self.main.motorconnected[i]:
                continue  # e.g. an unconnected phi placeholder
            n = i + 1
            unit = self.main.motorunits[i] if i < len(self.main.motorunits) else ""
            if token == "move_abs":
                entries.append(
                    {
                        "display": f"Move {name} Absolute",
                        "kind": "move_abs",
                        "needs_param": True,
                        "param_label": f"target ({unit})" if unit else "target",
                        "button_name": None,
                        "motor_index": i,
                        "widget_name": None,
                    }
                )
            elif token == "move_rel":
                entries.append(
                    {
                        "display": f"Move {name} Relative",
                        "kind": "move_rel",
                        "needs_param": True,
                        "param_label": f"step ({unit})" if unit else "step",
                        "button_name": None,
                        "motor_index": i,
                        "widget_name": None,
                    }
                )
            elif token in SET_FIELD_SUFFIXES and n in PER_MOTOR_SCAN_SLOTS:
                suffix, label_word = SET_FIELD_SUFFIXES[token]
                entries.append(
                    {
                        "display": f"Set {label_word} ({name})",
                        "kind": "set_field",
                        "needs_param": True,
                        "param_label": f"{label_word.lower()} ({unit})"
                        if unit
                        else label_word.lower(),
                        "button_name": None,
                        "motor_index": i,
                        "widget_name": "ed_lup_%i_%s" % (n, suffix),
                    }
                )
            elif token == "fly_1d" and n in PER_MOTOR_SCAN_SLOTS:
                entries.append(
                    {
                        "display": f"Fly {name} (1D)",
                        "kind": "click",
                        "needs_param": False,
                        "param_label": None,
                        "button_name": "pb_SAXSscan_%i" % n,
                        "motor_index": None,
                        "widget_name": None,
                    }
                )
            elif token == "step_1d" and n in PER_MOTOR_SCAN_SLOTS:
                entries.append(
                    {
                        "display": f"Step {name} (1D)",
                        "kind": "click",
                        "needs_param": False,
                        "param_label": None,
                        "button_name": "pb_lup_%i" % n,
                        "motor_index": None,
                        "widget_name": None,
                    }
                )
        return entries

    # ---- UI construction --------------------------------------------------
    def _build_ui(self):
        self.resize(520, 480)
        layout = QVBoxLayout(self)

        self.listWidget = QListWidget(self)
        layout.addWidget(self.listWidget)

        add_row = QHBoxLayout()
        combo_col = QVBoxLayout()
        self.combo_commandGroup = QComboBox(self)
        for group in self._groups:
            self.combo_commandGroup.addItem(group["group_label"])
        self.combo_commandGroup.currentIndexChanged.connect(
            self._on_command_group_changed
        )
        combo_col.addWidget(self.combo_commandGroup)

        self.combo_commandType = QComboBox(self)
        self.combo_commandType.currentIndexChanged.connect(
            self._on_command_type_changed
        )
        combo_col.addWidget(self.combo_commandType)
        add_row.addLayout(combo_col, 1)

        self.lineEdit_param = QLineEdit(self)
        self.lineEdit_param.setMaximumWidth(90)
        add_row.addWidget(self.lineEdit_param)

        self.pushButton_add = QPushButton("+", self)
        self.pushButton_add.setToolTip("Add command to macro")
        self.pushButton_add.setFixedWidth(28)
        self.pushButton_add.setStyleSheet(
            "background-color: rgb(150, 230, 150); font-weight: bold;"
        )
        self.pushButton_add.clicked.connect(self._on_add_clicked)
        add_row.addWidget(self.pushButton_add)

        self.pushButton_remove = QPushButton("-", self)
        self.pushButton_remove.setToolTip("Remove selected command")
        self.pushButton_remove.setFixedWidth(28)
        self.pushButton_remove.setStyleSheet(
            "background-color: rgb(230, 150, 150); font-weight: bold;"
        )
        self.pushButton_remove.clicked.connect(self._on_remove_clicked)
        add_row.addWidget(self.pushButton_remove)

        self.pushButton_moveUp = QPushButton("↑", self)
        self.pushButton_moveUp.setToolTip("Move selected command up")
        self.pushButton_moveUp.setFixedWidth(28)
        self.pushButton_moveUp.clicked.connect(self._on_move_up_clicked)
        add_row.addWidget(self.pushButton_moveUp)

        self.pushButton_moveDown = QPushButton("↓", self)
        self.pushButton_moveDown.setToolTip("Move selected command down")
        self.pushButton_moveDown.setFixedWidth(28)
        self.pushButton_moveDown.clicked.connect(self._on_move_down_clicked)
        add_row.addWidget(self.pushButton_moveDown)

        layout.addLayout(add_row)

        io_row = QHBoxLayout()
        self.pushButton_export = QPushButton("Export Macro...", self)
        self.pushButton_export.setToolTip("Save the current macro to a file")
        self.pushButton_export.clicked.connect(self._on_export_clicked)
        io_row.addWidget(self.pushButton_export)

        self.pushButton_import = QPushButton("Import Macro...", self)
        self.pushButton_import.setToolTip("Load a macro from a file (replaces the current list)")
        self.pushButton_import.clicked.connect(self._on_import_clicked)
        io_row.addWidget(self.pushButton_import)

        self.pushButton_append = QPushButton("Append Macro...", self)
        self.pushButton_append.setToolTip(
            "Load a macro from a file and append its commands to the current list"
        )
        self.pushButton_append.clicked.connect(self._on_append_clicked)
        io_row.addWidget(self.pushButton_append)

        self.pushButton_estimate = QPushButton("Estimate Macro Time", self)
        self.pushButton_estimate.setToolTip(
            "Estimate the total time the current macro will take to run"
        )
        self.pushButton_estimate.clicked.connect(self._on_estimate_clicked)
        io_row.addWidget(self.pushButton_estimate)

        layout.addLayout(io_row)

        run_row = QHBoxLayout()
        self.pushButton_run = QPushButton("Run Macro", self)
        self.pushButton_run.setStyleSheet(
            "background-color: rgb(40, 160, 40); color: white; font-weight: bold;"
        )
        self.pushButton_run.clicked.connect(self._on_run_clicked)
        run_row.addWidget(self.pushButton_run)

        self.pushButton_stop = QPushButton("Stop Macro", self)
        self.pushButton_stop.setStyleSheet(
            "background-color: rgb(200, 40, 40); color: white; font-weight: bold;"
        )
        self.pushButton_stop.setEnabled(False)
        self.pushButton_stop.clicked.connect(self._on_stop_clicked)
        run_row.addWidget(self.pushButton_stop)

        self.pushButton_close = QPushButton("Close", self)
        self.pushButton_close.clicked.connect(self.close)
        run_row.addWidget(self.pushButton_close)

        layout.addLayout(run_row)

        self._on_command_group_changed(self.combo_commandGroup.currentIndex())

    def _on_command_group_changed(self, group_idx):
        """First comboBox (command group) changed -- repopulate the second
        comboBox (specific command) from that group's entries."""
        self.combo_commandType.blockSignals(True)
        self.combo_commandType.clear()
        if 0 <= group_idx < len(self._groups):
            for spec in self._groups[group_idx]["entries"]:
                self.combo_commandType.addItem(spec["display"])
        self.combo_commandType.blockSignals(False)
        self._on_command_type_changed(self.combo_commandType.currentIndex())

    def _on_command_type_changed(self, idx):
        spec = self._current_spec(idx)
        if spec is None:
            return
        self.lineEdit_param.setEnabled(spec["needs_param"])
        self.lineEdit_param.setPlaceholderText(spec["param_label"] or "")
        if not spec["needs_param"]:
            self.lineEdit_param.clear()

    def _current_spec(self, type_idx=None):
        """The catalog entry the two comboBoxes currently point at, or None."""
        group_idx = self.combo_commandGroup.currentIndex()
        if not (0 <= group_idx < len(self._groups)):
            return None
        entries = self._groups[group_idx]["entries"]
        if type_idx is None:
            type_idx = self.combo_commandType.currentIndex()
        if not (0 <= type_idx < len(entries)):
            return None
        return entries[type_idx]

    # ---- add / remove -------------------------------------------------
    def _helix_axis_candidates(self):
        """Axis letters helix_fly_choose_axis would offer -- connected
        motors literally named "X"/"Z" (see scan_handler.py)."""
        return [
            name
            for name in ("X", "Z")
            if name in self.main.motornames
            and self.main.motorconnected[self.main.motornames.index(name)]
        ]

    def _on_add_clicked(self):
        spec = self._current_spec()
        if spec is None:
            return

        param = None
        if spec["needs_param"]:
            text = self.lineEdit_param.text().strip()
            param_kind = spec.get("param_kind", "float")

            if param_kind == "axis_letter":
                candidates = self._helix_axis_candidates()
                axis_letter = text.upper()
                if axis_letter not in candidates:
                    QMessageBox.warning(
                        self,
                        "Invalid value",
                        "Enter one of: %s." % (", ".join(candidates) or "(no axis connected)"),
                    )
                    return
                param = axis_letter
            else:
                try:
                    param = float(text)
                except ValueError:
                    QMessageBox.warning(
                        self,
                        "Invalid value",
                        f"Enter a numeric value for {spec['param_label']}.",
                    )
                    return
                if spec["kind"] == "move_rel" and param == 0:
                    # mvr() treats val == 0 as "not given" and falls back to
                    # reading the main panel's own tweak line edit instead of
                    # doing nothing (see rungui.py's mvr()) -- reject here so
                    # a macro row never silently means something else at run
                    # time.
                    QMessageBox.warning(
                        self,
                        "Invalid value",
                        "A relative move of 0 isn't supported here -- it "
                        "would fall back to the main panel's tweak value "
                        "instead of doing nothing.",
                    )
                    return

        cmd = {
            "kind": spec["kind"],
            "button_name": spec["button_name"],
            "motor_index": spec["motor_index"],
            "widget_name": spec.get("widget_name"),
            "param": param,
            "label": self._format_label(spec, param),
        }
        item = QListWidgetItem(cmd["label"])
        item.setData(Qt.UserRole, cmd)
        self.listWidget.addItem(item)
        self._save_macro()

    def _on_remove_clicked(self):
        row = self.listWidget.currentRow()
        if row >= 0:
            self.listWidget.takeItem(row)
            self._save_macro()

    def _on_move_up_clicked(self):
        row = self.listWidget.currentRow()
        if row > 0:
            item = self.listWidget.takeItem(row)
            self.listWidget.insertItem(row - 1, item)
            self.listWidget.setCurrentRow(row - 1)
            self._save_macro()

    def _on_move_down_clicked(self):
        row = self.listWidget.currentRow()
        if 0 <= row < self.listWidget.count() - 1:
            item = self.listWidget.takeItem(row)
            self.listWidget.insertItem(row + 1, item)
            self.listWidget.setCurrentRow(row + 1)
            self._save_macro()

    # ---- scan-param snapshot (pb_sendScanParamToMacro on the main panel) --
    @staticmethod
    def _all_scan_param_widget_names():
        """Every widget a scan reads From/To/Step/Exposure from -- reuses
        PER_MOTOR_SCAN_SLOTS and SET_FIELD_SUFFIXES (the same source of
        truth the individual Set From/To/Step commands are built from) plus
        the shared exposure field's name from STATIC_COMMANDS, so this list
        can't drift out of sync with those."""
        names = [
            "ed_lup_%i_%s" % (n, suffix)
            for n in PER_MOTOR_SCAN_SLOTS
            for suffix, _ in SET_FIELD_SUFFIXES.values()
        ]
        names.append(STATIC_COMMANDS["set_expt"]["widget_name"])
        return names

    def _next_scan_param_snapshot_number(self):
        """1 past the highest snapshot_number already in the list -- reading
        the list itself (rather than keeping a separate running counter) is
        the single source of truth, so numbering stays correct across
        Import/Append/reload instead of risking a desync."""
        numbers = [
            cmd.get("snapshot_number", 0)
            for cmd in self._commands_snapshot()
            if cmd.get("kind") == "scan_param_snapshot"
        ]
        return max(numbers, default=0) + 1

    def add_scan_param_snapshot(self):
        """Called from rungui.py when pb_sendScanParamToMacro is pressed.
        Captures every From/To/Step/Exposure line edit's current value into
        one command that restores them all when it runs later in the macro.
        """
        field_values = {
            name: self._read_line_edit_float(name, 0.0)
            for name in self._all_scan_param_widget_names()
        }
        number = self._next_scan_param_snapshot_number()
        label = f"Set scan param from window {number}"
        cmd = {
            "kind": "scan_param_snapshot",
            "button_name": None,
            "motor_index": None,
            "widget_name": None,
            "param": None,
            "field_values": field_values,
            "snapshot_number": number,
            "label": label,
        }
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, cmd)
        self.listWidget.addItem(item)
        self._save_macro()

    @staticmethod
    def _format_label(spec, param):
        if spec["kind"] == "wait":
            return f"Wait {param:.3f} s"
        if spec["kind"] == "move_abs":
            return f"{spec['display']} -> {param:.4f}"
        if spec["kind"] == "move_rel":
            return f"{spec['display']} by {param:+.4f}"
        if spec["kind"] == "set_field":
            return f"{spec['display']} -> {param:.4f}"
        if spec["kind"] == "helix":
            return f"{spec['display']} ({param})"
        return spec["display"]

    # ---- run / stop / close --------------------------------------------
    def _on_run_clicked(self):
        if self.engine.is_running or self.listWidget.count() == 0:
            return
        # Confirm/set the scan name once, up front -- _pre_scan_guards skips
        # this dialog for every individual scan while macro_running is set,
        # so the macro would otherwise never get a name confirmed at all.
        if not self.main.scan_handler._confirm_scan_name():
            return
        for i in range(self.listWidget.count()):
            item = self.listWidget.item(i)
            item.setBackground(QBrush())
            item.setForeground(QBrush())
        self.engine.start(self._commands_snapshot())

    def _on_stop_clicked(self):
        self.engine.stop()

    def _on_macro_started(self):
        self.pushButton_run.setEnabled(False)
        self.pushButton_add.setEnabled(False)
        self.pushButton_remove.setEnabled(False)
        self.pushButton_moveUp.setEnabled(False)
        self.pushButton_moveDown.setEnabled(False)
        self.pushButton_export.setEnabled(False)
        self.pushButton_import.setEnabled(False)
        self.pushButton_append.setEnabled(False)
        self.pushButton_close.setEnabled(False)
        self.pushButton_stop.setEnabled(True)

    def _on_macro_finished(self, completed_clean):
        self.pushButton_run.setEnabled(True)
        self.pushButton_add.setEnabled(True)
        self.pushButton_remove.setEnabled(True)
        self.pushButton_moveUp.setEnabled(True)
        self.pushButton_moveDown.setEnabled(True)
        self.pushButton_export.setEnabled(True)
        self.pushButton_import.setEnabled(True)
        self.pushButton_append.setEnabled(True)
        self.pushButton_close.setEnabled(True)
        self.pushButton_stop.setEnabled(False)
        self.listWidget.setCurrentRow(-1)
        if completed_clean:
            # A full, unfailed, unstopped run -- clear the "already ran"
            # gray-out so the list looks normal again for the next run.
            for i in range(self.listWidget.count()):
                self.listWidget.item(i).setForeground(QBrush())

    def _on_step_started(self, idx):
        self.listWidget.setCurrentRow(idx)

    def _on_step_finished(self, idx, ok):
        item = self.listWidget.item(idx)
        if item is None:
            return
        if ok:
            # Gray out completed commands so the still-highlighted (selected)
            # row is visibly the one currently running.
            item.setForeground(QBrush(QColor(150, 150, 150)))
        else:
            item.setBackground(QBrush(QColor(255, 190, 190)))

    def closeEvent(self, event):
        if self.engine.is_running:
            event.ignore()
            QMessageBox.information(
                self, "Macro Running", "Stop the macro before closing this window."
            )
        else:
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        btn = self.main.ui.findChild(QPushButton, "pb_sendScanParamToMacro")
        if btn is not None:
            btn.setEnabled(True)
        self._set_scan_buttons_enabled(False)

    def hideEvent(self, event):
        super().hideEvent(event)
        btn = self.main.ui.findChild(QPushButton, "pb_sendScanParamToMacro")
        if btn is not None:
            btn.setEnabled(False)
        self._set_scan_buttons_enabled(True)

    def _scan_button_names(self):
        """Every real scan button the macro touches, one way or another --
        derived directly from self._groups (the same source of truth the
        comboBoxes are built from), so this can never drift out of sync.
        Includes "helix", whose button isn't clicked (its real click handler
        pops a modal dialog -- see helix_fly's STATIC_COMMANDS entry) but
        must still be disabled while the macro window is open."""
        return {
            spec["button_name"]
            for group in self._groups
            for spec in group["entries"]
            if spec.get("button_name")
        }

    def _set_scan_buttons_enabled(self, enabled):
        """Disable every scan button while this window is open, so the user
        can't start a conflicting scan by hand; restore on close. Restoring
        uses each button's own pre-open state (not a blanket True) so a
        button already disabled for an unrelated reason -- e.g. a
        disconnected motor -- doesn't get wrongly re-enabled."""
        if enabled:
            for name, was_enabled in self._scan_button_prev_enabled.items():
                btn = self.main.ui.findChild(QPushButton, name)
                if btn is not None:
                    btn.setEnabled(was_enabled)
            self._scan_button_prev_enabled = {}
        else:
            self._scan_button_prev_enabled = {}
            for name in self._scan_button_names():
                btn = self.main.ui.findChild(QPushButton, name)
                if btn is not None:
                    self._scan_button_prev_enabled[name] = btn.isEnabled()
                    btn.setEnabled(False)

    # ---- persistence ----------------------------------------------------
    def _commands_snapshot(self):
        return [
            self.listWidget.item(i).data(Qt.UserRole)
            for i in range(self.listWidget.count())
        ]

    def _populate_from_commands(self, commands, replace):
        if replace:
            self.listWidget.clear()
        for cmd in commands:
            if not isinstance(cmd, dict):
                continue
            item = QListWidgetItem(cmd.get("label", cmd.get("kind", "?")))
            item.setData(Qt.UserRole, cmd)
            self.listWidget.addItem(item)

    def _save_macro(self):
        try:
            with open(MACRO_SAVE_PATH, "w") as f:
                json.dump(self._commands_snapshot(), f, indent=2)
        except OSError as e:
            print(f"Macro: could not save {MACRO_SAVE_PATH}: {e}")

    def _load_macro(self):
        try:
            with open(MACRO_SAVE_PATH) as f:
                commands = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        self._populate_from_commands(commands, replace=False)

    # ---- export / import (user-chosen file, separate from the session
    # autosave above) --------------------------------------------------
    @staticmethod
    def _macro_dialog_dir():
        return QSettings("ptychoSAXS", "ptychoSAXS").value(
            MACRO_DIALOG_DIR_KEY, "", type=str
        )

    @staticmethod
    def _remember_macro_dialog_dir(path):
        if path:
            QSettings("ptychoSAXS", "ptychoSAXS").setValue(
                MACRO_DIALOG_DIR_KEY, os.path.dirname(path)
            )

    def _on_export_clicked(self):
        if self.listWidget.count() == 0:
            QMessageBox.information(
                self, "Export Macro", "The macro list is empty -- nothing to export."
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Macro", self._macro_dialog_dir(), "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        self._remember_macro_dialog_dir(path)
        try:
            with open(path, "w") as f:
                json.dump(self._commands_snapshot(), f, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "Export Macro", f"Could not save {path}:\n{e}")

    def _prompt_load_commands(self, title):
        """Shared file-open + parse for Import/Append. Returns a list of
        command dicts, or None (after warning the user) if it couldn't."""
        path, _ = QFileDialog.getOpenFileName(
            self, title, self._macro_dialog_dir(), "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return None
        self._remember_macro_dialog_dir(path)
        try:
            with open(path) as f:
                commands = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, title, f"Could not load {path}:\n{e}")
            return None
        if not isinstance(commands, list):
            QMessageBox.warning(
                self, title, "That file doesn't contain a macro command list."
            )
            return None
        return commands

    def _on_import_clicked(self):
        commands = self._prompt_load_commands("Import Macro")
        if commands is None:
            return
        # Replaces the current list (rather than appending) -- importing a
        # saved macro makes it the active one.
        self._populate_from_commands(commands, replace=True)
        self._save_macro()

    def _on_append_clicked(self):
        commands = self._prompt_load_commands("Append Macro")
        if commands is None:
            return
        self._populate_from_commands(commands, replace=False)
        self._save_macro()

    # ---- time estimate ---------------------------------------------------
    def _on_estimate_clicked(self):
        if self.listWidget.count() == 0:
            QMessageBox.information(
                self, "Estimate Macro Time", "The macro list is empty."
            )
            return
        try:
            total = self._estimate_total_time()
        except Exception as e:
            QMessageBox.warning(
                self, "Estimate Macro Time", f"Could not estimate time:\n{e}"
            )
            return
        QMessageBox.information(
            self, "Estimate Macro Time", f"Estimated total time: {self._fmt_duration(total)}"
        )

    @staticmethod
    def _fmt_duration(seconds):
        if seconds >= 3600:
            return "%.2f hr" % (seconds / 3600)
        if seconds >= 300:
            return "%.1f min" % (seconds / 60)
        return "%.1f s" % seconds

    def _estimate_total_time(self):
        """Sum of every row's estimated duration.

        wait -> its own seconds. move_abs/move_rel -> STAGE_MOVE_ESTIMATE_S
        flat. set_field -> 0 (instant; updates the running field_state so
        later scans in the list are estimated with the value this row would
        have written). click (a scan) -> the same Ntot*(exposure+overhead)
        formula scan_handler.py already uses (_confirm_large_scan / the
        label_estT calculation), evaluated against field_state at that point.
        """
        field_state = self._snapshot_scan_fields()
        total = 0.0
        for cmd in self._commands_snapshot():
            kind = cmd.get("kind")
            if kind == "wait":
                total += max(0.0, float(cmd.get("param") or 0.0))
            elif kind in ("move_abs", "move_rel"):
                total += self.STAGE_MOVE_ESTIMATE_S
            elif kind == "set_field":
                widget_name = cmd.get("widget_name")
                param = cmd.get("param")
                if widget_name and param is not None:
                    field_state[widget_name] = float(param)
            elif kind == "scan_param_snapshot":
                for widget_name, value in (cmd.get("field_values") or {}).items():
                    field_state[widget_name] = float(value)
            elif kind in ("click", "helix"):
                total += self._estimate_scan_time(cmd.get("button_name"), field_state)
                # mandated settle after any scan -- lives on the engine (the
                # single source of truth _run_loop itself sleeps for).
                total += self.engine.POST_SCAN_SLEEP_S
        return total

    def _snapshot_scan_fields(self):
        """Current live values of every ed_lup_<n>_{L,R,N,t} field, keyed by
        widget name -- the starting point _estimate_total_time updates as it
        walks past each set_field row."""
        state = {}
        for n in PER_MOTOR_SCAN_SLOTS:
            for suffix, default in (("L", 0.0), ("R", 0.0), ("N", 1.0), ("t", 0.0)):
                name = "ed_lup_%i_%s" % (n, suffix)
                state[name] = self._read_line_edit_float(name, default)
        return state

    def _read_line_edit_float(self, widget_name, default=0.0):
        ed = self.main.ui.findChild(QLineEdit, widget_name)
        if ed is None:
            return default
        try:
            return float(ed.text())
        except ValueError:
            return default

    def _slot_positions(self, field_state, slot):
        L = field_state.get("ed_lup_%i_L" % slot, 0.0)
        R = field_state.get("ed_lup_%i_R" % slot, 0.0)
        N = field_state.get("ed_lup_%i_N" % slot, 1.0)
        if not N:
            return [0.0]
        return self.main.scan_handler._make_positions(0, L, R, N)

    @staticmethod
    def _slot_expt(field_state, slot):
        return field_state.get("ed_lup_%i_t" % slot, 0.0)

    def _fly_period(self, expt):
        """Same per-point time a real fly scan uses (see scan_handler.py's
        update_scan_estimate): the configured fly acquisition time, floored
        by exposure+readout and by the detector's hard minimum period."""
        sh = self.main.scan_handler
        fly_acq_time = getattr(self.main.parameters, "_fly_acq_time", sh.OVERHEAD_FLY)
        return max(fly_acq_time, expt + sh.det_readout_time, sh.OVERHEAD_FLY)

    def _estimate_scan_time(self, button_name, field_state):
        sh = self.main.scan_handler
        x, z, phi = (
            SCAN_ESTIMATE_SLOTS["x"],
            SCAN_ESTIMATE_SLOTS["z"],
            SCAN_ESTIMATE_SLOTS["phi"],
        )

        if button_name == "pb_SAXSscan_fly2d":
            xpos = self._slot_positions(field_state, x)
            zpos = self._slot_positions(field_state, z)
            n = sh._compute_n_positions([xpos, zpos], scan_kind="fly_snake")
            return n * self._fly_period(self._slot_expt(field_state, x))

        if button_name == "pb_lup_step2d":
            xpos = self._slot_positions(field_state, x)
            zpos = self._slot_positions(field_state, z)
            n = len(xpos) * len(zpos)
            return n * (self._slot_expt(field_state, x) + sh.OVERHEAD_STEP)

        if button_name == "pb_SAXSscan_fly3d":
            xpos = self._slot_positions(field_state, x)
            zpos = self._slot_positions(field_state, z)
            phipos = self._slot_positions(field_state, phi)
            xy_n = sh._compute_n_positions([xpos, zpos], scan_kind="fly_snake")
            per_slice = xy_n * self._fly_period(self._slot_expt(field_state, x))
            return len(phipos) * (per_slice + sh.OVERHEAD_FLY3D_PHI)

        if button_name == "pb_lup_step3d":
            xpos = self._slot_positions(field_state, x)
            zpos = self._slot_positions(field_state, z)
            phipos = self._slot_positions(field_state, phi)
            n = len(xpos) * len(zpos) * len(phipos)
            return n * (self._slot_expt(field_state, x) + sh.OVERHEAD_STEP)

        if button_name == "pb_helix_scan":
            L = field_state.get("ed_lup_%i_L" % phi, 0.0)
            R = field_state.get("ed_lup_%i_R" % phi, 0.0)
            N = field_state.get("ed_lup_%i_N" % phi, 1.0)
            t = field_state.get("ed_lup_%i_t" % phi, 0.0)
            if not N:
                return 0.0
            total_time, _, _ = sh._compute_helix_total_time(L, R, N, t)
            return total_time

        if button_name == "pb_takeshot":
            expt = field_state.get("ed_lup_1_t", 0.0)
            return max(expt + sh.det_readout_time, sh.OVERHEAD_FLY)

        m = re.match(r"^pb_SAXSscan_(\d+)$", button_name or "")
        if m:
            n = int(m.group(1))
            pos = self._slot_positions(field_state, n)
            expt = self._slot_expt(field_state, n)
            ax_name = (
                self.main.motornames[n - 1] if n - 1 < len(self.main.motornames) else ""
            )
            hexapod_axes = getattr(getattr(self.main.pts, "hexapod", None), "axes", ())
            scan_kind = "fly_hexapod_1d" if ax_name in hexapod_axes else "fly_phi"
            count = sh._compute_n_positions([pos], scan_kind=scan_kind)
            return count * self._fly_period(expt)

        m = re.match(r"^pb_lup_(\d+)$", button_name or "")
        if m:
            n = int(m.group(1))
            pos = self._slot_positions(field_state, n)
            expt = self._slot_expt(field_state, n)
            return len(pos) * (expt + sh.OVERHEAD_STEP)

        print(f"Macro: no time estimate formula for button '{button_name}', treating as 0 s")
        return 0.0
