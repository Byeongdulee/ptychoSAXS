"""
gui/handlers/user_view.py
Wiring for ptychoSAXS_user.ui, the reduced level of the main panel.

The user view is loaded into a LayeredMainWindow backed by the large view
(gui/widgets/layered_window.py), so the scan engine still finds every
setting it reads. What lives here is only what the user view adds or does
differently:

  * the scan-trajectory form (frame_scan_traj) and the run panel
    (frame_run_scan), driving TrajHandler;
  * the sample-positioning panel: a dpad on Z and trans1/trans2, drive
    boxes, readbacks, and a phi tweak;
  * the per-panel "Enabled" checkboxes;
  * the status message list.

Everything shared with the large view (scan name/number, Setup, optics,
stop/reset, detector align/scan, macros, extra scans, ...) is wired by
ptyco_main_control._wire_common_view so the two views cannot drift apart.
"""

from PyQt5.QtCore import QObject, QSettings
from PyQt5.QtGui import QBrush, QColor, QIntValidator
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QWidget,
)

from ptychosaxs.api.scan_traj import FLY, STEP, TAKESHOT, TrajectoryError
from gui.handlers.traj_handler import (
    DEFAULT_DIM_TEXT,
    DIM_CHOICES,
    KIND_CHOICES,
    MOTORS_2D,
    MOTORS_3D,
)

SETTINGS_PREFIX = "userView/"
# Motors the positioning panel shows a readback and a drive box for.
POSITIONING_MOTORS = ("Z", "trans1", "trans2", "phi")
# The status list keeps this many lines; older ones scroll away.
MAX_STATUS_LINES = 200

# (frame, its "Enabled" checkbox, hide instead of grey out)
ENABLE_PANELS = (
    ("frame_positioning", "checkBox_enable_positioning", False),
    ("frame_scan_traj", "checkBox_enable_scan_traj", False),
    ("frame_run_scan", "checkBox_enable_run_scan", False),
    ("frame_advanced", "checkBox_enable_advanced", True),
)
# Never greyed out with its panel: a scan must always be stoppable.
ALWAYS_ENABLED = ("pushButton_stopScan",)
# Kept visible when the advanced panel hides its buttons.
ADVANCED_TITLE = "label_44"


def _settings():
    return QSettings("ptychoSAXS", "ptychoSAXS")


class UserViewHandler:
    def __init__(self, window) -> None:
        self.w = window
        self._trans_group = None

    @property
    def ui(self):
        return self.w.ui

    @property
    def traj(self):
        return self.w.traj_handler

    def _own(self, cls, name):
        """This view's own widget, never one borrowed from the large view."""
        return QObject.findChild(self.ui, cls, name)

    def _connect(self, cls, name, signal, slot):
        widget = self._own(cls, name)
        if widget is None:
            print(f"[user view] {name} not found in ptychoSAXS_user.ui")
            return None
        getattr(widget, signal).connect(slot)
        return widget

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def wire(self):
        """Wire a freshly loaded user view. Called before it is shown and
        before its ProportionalResizer snapshots the layout."""
        self._wire_enable_panels()
        self._wire_scan_form()
        self._wire_run_panel()
        self._wire_positioning()
        self._connect(QPushButton, "btn_reset_qds", "clicked", self.w.reset_qds_all)
        self.show_working_folder()

    # ------------------------------------------------------------------
    # "Enabled" checkboxes
    # ------------------------------------------------------------------

    def _wire_enable_panels(self):
        """Give each panel a content layer that the checkbox switches.

        Everything in the frame except its checkbox moves onto one child
        widget, and that widget is disabled (or hidden) as a whole. Toggling
        the layer rather than each widget leaves the widgets' own enabled
        states alone -- Stop Scan's, the SAXS buttons', the greyed rows of
        the scan form -- so switching a panel back on restores exactly what
        was there, and nothing that later enables a widget can poke a hole
        in a disabled panel.
        """
        for frame_name, box_name, hide in ENABLE_PANELS:
            frame = self._own(QFrame, frame_name)
            box = self._own(QCheckBox, box_name)
            if frame is None or box is None:
                continue
            keep = {box}
            for name in ALWAYS_ENABLED + ((ADVANCED_TITLE,) if hide else ()):
                widget = self._own(QWidget, name)
                if widget is not None and widget.parent() is frame:
                    keep.add(widget)
            layer = QWidget(frame)
            layer.setObjectName(frame_name + "_content")
            layer.setGeometry(0, 0, frame.width(), frame.height())
            for child in list(frame.children()):
                if isinstance(child, QWidget) and child is not layer and child not in keep:
                    visible = not child.isHidden()
                    child.setParent(layer)
                    child.setVisible(visible)
            layer.show()
            for widget in keep:
                widget.raise_()

            if hide:
                apply = layer.setVisible
            else:
                apply = layer.setEnabled
            box.toggled.connect(apply)
            apply(box.isChecked())

    # ------------------------------------------------------------------
    # Scan trajectory form
    # ------------------------------------------------------------------

    def _wire_scan_form(self):
        ui = self.ui
        dim = self._own(QComboBox, "comboBox_scan_dim")
        kind = self._own(QComboBox, "comboBox_scan_type")
        motors = self._own(QComboBox, "comboBox_scan_motors")
        if dim is None or kind is None or motors is None:
            return
        s = _settings()
        for combo, items, key, default in (
                (dim, [t for t, _ in DIM_CHOICES], "scanDim", DEFAULT_DIM_TEXT),
                (kind, list(KIND_CHOICES), "scanType", FLY)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(items)
            saved = s.value(SETTINGS_PREFIX + key, default, type=str)
            combo.setCurrentIndex(max(combo.findText(saved), 0))
            combo.blockSignals(False)

        for name in ("lineEdit_expt", *("lineEdit_%s_%d" % (f, i)
                                        for f in ("from", "to", "step") for i in (1, 2, 3))):
            edit = self._own(QLineEdit, name)
            if edit is None:
                continue
            saved = s.value(SETTINGS_PREFIX + name, None, type=str)
            if saved:
                edit.setText(saved)
            edit.textChanged.connect(self.refresh_form_feedback)
            edit.editingFinished.connect(
                lambda e=edit, n=name: _settings().setValue(SETTINGS_PREFIX + n, e.text()))

        dim.currentIndexChanged.connect(self._on_scan_shape_changed)
        kind.currentIndexChanged.connect(self._on_scan_shape_changed)
        motors.currentIndexChanged.connect(self._on_motors_changed)
        self._on_scan_shape_changed()

        self._connect(QPushButton, "pushButton_write_scan_traj", "clicked", self.write_trajectory)
        self._connect(QPushButton, "pb_takeshot", "clicked", self.traj.takeshot_now)
        self._connect(QPushButton, "pushButton_checkFlyBlur", "clicked", self.check_fly_blur)

    def _dim(self):
        text = self._own(QComboBox, "comboBox_scan_dim").currentText()
        return dict(DIM_CHOICES).get(text, 2)

    def _kind(self):
        if self._dim() == 0:
            return TAKESHOT
        return self._own(QComboBox, "comboBox_scan_type").currentText() or STEP

    def _motors(self):
        text = self._own(QComboBox, "comboBox_scan_motors").currentText()
        return [m.strip() for m in text.split(",") if m.strip()]

    def _on_scan_shape_changed(self, *_):
        """Re-offer motors and re-shape the from/to/step rows for the new
        dimension. 1-D offers every connected motor; 2-D and 3-D one fixed
        set each for now."""
        dim = self._dim()
        s = _settings()
        s.setValue(SETTINGS_PREFIX + "scanDim",
                   self._own(QComboBox, "comboBox_scan_dim").currentText())
        s.setValue(SETTINGS_PREFIX + "scanType",
                   self._own(QComboBox, "comboBox_scan_type").currentText())

        motors = self._own(QComboBox, "comboBox_scan_motors")
        motors.blockSignals(True)
        motors.clear()
        if dim == 1:
            names = [n for n, ok in zip(self.w.motornames, self.w.motorconnected) if ok]
            motors.addItems(names)
            saved = s.value(SETTINGS_PREFIX + "scanMotor1d", "X", type=str)
            motors.setCurrentIndex(max(motors.findText(saved), 0))
        elif dim == 2:
            motors.addItem(", ".join(MOTORS_2D))
        elif dim == 3:
            motors.addItem(", ".join(MOTORS_3D))
        motors.blockSignals(False)

        takeshot = dim == 0
        self._own(QComboBox, "comboBox_scan_type").setEnabled(not takeshot)
        motors.setEnabled(not takeshot and motors.count() > 1)
        flyblur = self._own(QPushButton, "pushButton_checkFlyBlur")
        if flyblur is not None:
            flyblur.setEnabled(not takeshot)
        self._on_motors_changed()

    def _on_motors_changed(self, *_):
        if self._dim() == 1:
            _settings().setValue(SETTINGS_PREFIX + "scanMotor1d",
                                 self._own(QComboBox, "comboBox_scan_motors").currentText())
        motors = self._motors() if self._dim() else []
        for i in (1, 2, 3):
            used = i <= len(motors)
            label = self._own(QLabel, "label_scan_ax%d" % i)
            if label is not None:
                label.setText(motors[i - 1] if used else " ")
                label.setEnabled(used)
            for field in ("from", "to", "step"):
                edit = self._own(QLineEdit, "lineEdit_%s_%d" % (field, i))
                if edit is not None:
                    edit.setEnabled(used)
        for name in ("label_from", "label_to", "label_step"):
            label = self._own(QLabel, name)
            if label is not None:
                label.setEnabled(bool(motors))
        self.refresh_form_feedback()

    def exposure_seconds(self):
        """lineEdit_expt in seconds. The unit is whatever label_18 says --
        "(ms)" or "(s)" -- so relabelling the field in Designer is enough to
        change it. Raises ValueError on a non-number."""
        value = float(self._own(QLineEdit, "lineEdit_expt").text())
        label = self._own(QLabel, "label_18")
        if label is not None and "(ms)" in label.text():
            return value / 1000.0
        return value

    def _form_axes(self, at_current_position):
        axes = []
        for i, name in enumerate(self._motors() if self._dim() else [], start=1):
            values = []
            for field in ("from", "to", "step"):
                values.append(float(self._own(QLineEdit, "lineEdit_%s_%d" % (field, i)).text()))
            p0 = self.traj._current_pos(name) if at_current_position else 0.0
            axes.append({"name": name, "p0": p0, "st": values[0], "fe": values[1],
                         "step": values[2]})
        return axes

    def trajectory_from_form(self, at_current_position=True):
        """The form as a ScanTrajectory. Raises ValueError/TrajectoryError."""
        missing = [m for m in (self._motors() if self._dim() else [])
                   if not self.traj._connected(m)]
        if missing:
            raise TrajectoryError("Not connected: %s." % ", ".join(missing))
        return self.traj.trajectory_from_params(
            self._kind(), self._form_axes(at_current_position), self.exposure_seconds())

    def refresh_form_feedback(self, *_):
        """Live point counts and time estimates for the form as typed.
        Motor positions are not read here -- counts do not depend on them."""
        try:
            traj = self.trajectory_from_form(at_current_position=False)
            traj.sample_name = traj.sample_name or "-"
            traj.validate()
        except (ValueError, TrajectoryError):
            traj = None
        self.traj.show_feedback("sj", traj)

    def write_trajectory(self):
        """pushButton_write_scan_traj."""
        self.w.update_scanname()
        try:
            traj = self.trajectory_from_form()
        except ValueError:
            QMessageBox.warning(self.ui, "Scan trajectory",
                                "Check the exposure time and the from/to/step values.")
            return
        except TrajectoryError as exc:
            QMessageBox.warning(self.ui, "Scan trajectory", str(exc))
            return
        path = self.traj.write(traj, advance=True)
        if path is None:
            return
        box = self._own(QCheckBox, "checkBox_load_scan_current")
        if box is None or box.isChecked():
            self.traj.load(path)

    def check_fly_blur(self):
        """pushButton_checkFlyBlur: blur for the fast axis of the form's
        trajectory -- the positions that would be written."""
        try:
            traj = self.trajectory_from_form(at_current_position=False)
        except (ValueError, TrajectoryError) as exc:
            QMessageBox.warning(self.ui, "Fly Blur", f"Check the scan parameters.\n\n{exc}")
            return
        if not traj.axes or traj.expt <= 0 or traj.axes[0].step == 0:
            QMessageBox.warning(self.ui, "Fly Blur",
                                "Exposure time and step size must both be > 0.")
            return
        fast = traj.axes[0]
        angular = fast.unit == "deg"
        text = self.w.scan_handler.fly_blur_text(traj.expt, fast.step, angular=angular)
        title = "Fly Blur Estimate (%s)" % fast.name
        QMessageBox.information(self.ui, title, text)

    # ------------------------------------------------------------------
    # Run panel
    # ------------------------------------------------------------------

    def _wire_run_panel(self):
        self._connect(QPushButton, "pb_load_scan_browse", "clicked", self.traj.browse)
        self._connect(QPushButton, "pushButton_run_scan", "clicked", self.traj.run_loaded)
        # The scan number of the loaded trajectory: type one to load it, or
        # step to the previous/next scan that has a trajectory.
        edit = self._connect(QLineEdit, "edit_load_scannumber", "returnPressed",
                             self.traj.load_number_from_edit)
        if edit is not None:
            edit.setValidator(QIntValidator(0, 99999, edit))
            edit.setToolTip("Scan number of the loaded trajectory. Type one and press "
                            "Enter to load it.")
        self._connect(QPushButton, "pushButton_load_scanNm1", "clicked",
                      lambda: self.traj.step_loaded_number(-1))
        self._connect(QPushButton, "pushButton_load_scanNp1", "clicked",
                      lambda: self.traj.step_loaded_number(+1))
        self.traj.show_loaded_number()
        self.traj.refresh_run_button()
        self.traj.show_feedback("rs", None)
        # Coming back from the large view: show what was loaded before.
        self.traj._loaded_plot = None
        self.traj.refresh_loaded()

    # ------------------------------------------------------------------
    # Sample positioning
    # ------------------------------------------------------------------

    def _wire_positioning(self):
        r1 = self._own(QRadioButton, "radioButton_trans1")
        r2 = self._own(QRadioButton, "radioButton_trans2")
        if r1 is not None and r2 is not None:
            group = QButtonGroup(self.ui)
            group.setExclusive(True)
            group.addButton(r1)
            group.addButton(r2)
            self._trans_group = group
            saved = _settings().value(SETTINGS_PREFIX + "dpadTrans", "trans2", type=str)
            (r2 if saved == "trans2" else r1).setChecked(True)
            r1.toggled.connect(lambda on: on and _settings().setValue(
                SETTINGS_PREFIX + "dpadTrans", "trans1"))
            r2.toggled.connect(lambda on: on and _settings().setValue(
                SETTINGS_PREFIX + "dpadTrans", "trans2"))

        for button, motor, sign in (("up", "Z", +1), ("down", "Z", -1),
                                    ("right", None, +1), ("left", None, -1)):
            self._connect(QPushButton, "pb_pos_%s" % button, "clicked",
                          lambda _c=False, m=motor, s=sign: self._dpad(m, s))
        for suffix, factor in (("stepDown", 0.1), ("stepUp", 10.0)):
            self._connect(QPushButton, "pb_pos_%s" % suffix, "clicked",
                          lambda _c=False, f=factor: self._scale_step(f))

        for name in POSITIONING_MOTORS:
            self._connect(QLineEdit, "lineEdit_drive_%s" % name, "returnPressed",
                          lambda n=name: self._drive(n))
            if not self.traj._connected(name):
                for widget_name in ("label_RBV_%s" % name, "lineEdit_drive_%s" % name):
                    widget = self._own(QWidget, widget_name)
                    if widget is not None:
                        widget.setEnabled(False)
        self._connect(QPushButton, "pb_tweakL_phi", "clicked", lambda: self._tweak_phi(-1))
        self._connect(QPushButton, "pb_tweakR_phi", "clicked", lambda: self._tweak_phi(+1))
        if not self.traj._connected("phi"):
            for name in ("pb_tweakL_phi", "pb_tweakR_phi", "lineEdit_tweak_phi"):
                widget = self._own(QWidget, name)
                if widget is not None:
                    widget.setEnabled(False)
        tweak = self._own(QLineEdit, "lineEdit_tweak_phi")
        if tweak is not None:
            tweak.setToolTip("phi tweak step, deg")

        for name in POSITIONING_MOTORS:
            if self.traj._connected(name):
                try:
                    self.show_rbv(name, self.w.pts.get_pos(name))
                except Exception:
                    pass

    def _selected_trans(self):
        r1 = self._own(QRadioButton, "radioButton_trans1")
        return "trans1" if r1 is not None and r1.isChecked() else "trans2"

    def _step(self, name):
        edit = self._own(QLineEdit, name)
        try:
            return float(edit.text()) if edit is not None else 0.0
        except ValueError:
            return 0.0

    def _move_rel(self, motor, delta):
        if delta == 0 or not self.traj._connected(motor):
            return
        self.w.mvr(self.w.motornames.index(motor), 1, delta)

    def _dpad(self, motor, sign):
        """Up/down drive Z; left/right drive the selected trans stage."""
        self._move_rel(motor or self._selected_trans(), sign * self._step("ed_pos_step"))

    def _scale_step(self, factor):
        edit = self._own(QLineEdit, "ed_pos_step")
        step = self._step("ed_pos_step")
        if edit is not None and step:
            edit.setText("%g" % (step * factor))

    def _tweak_phi(self, sign):
        self._move_rel("phi", sign * self._step("lineEdit_tweak_phi"))

    def _drive(self, name):
        edit = self._own(QLineEdit, "lineEdit_drive_%s" % name)
        if edit is None or not self.traj._connected(name):
            return
        try:
            target = float(edit.text())
        except ValueError:
            QMessageBox.warning(self.ui, "Move", f"{name}: not a number.")
            return
        self.w.mv(self.w.motornames.index(name), target)

    def show_rbv(self, name, value):
        label = self._own(QLabel, "label_RBV_%s" % name)
        if label is not None and value is not None:
            label.setText("%0.4f" % float(value))

    # ------------------------------------------------------------------
    # Working folder and status messages
    # ------------------------------------------------------------------

    def show_working_folder(self):
        label = self._own(QLabel, "label_workingfolder")
        if label is not None:
            folder = self.w.parameters.working_folder
            label.setText(folder)
            label.setToolTip(folder)

    def post_status(self, text, error=False):
        """Append one line to listWidget_status_messages."""
        lst = self._own(QListWidget, "listWidget_status_messages")
        if lst is None:
            return
        item = QListWidgetItem(text)
        if error:
            item.setForeground(QBrush(QColor(200, 0, 0)))
        item.setToolTip(text)
        lst.addItem(item)
        while lst.count() > MAX_STATUS_LINES:
            lst.takeItem(0)
        lst.scrollToBottom()

    def replay_status(self, lines):
        """Refill the list after a view switch."""
        for text, error in lines:
            self.post_status(text, error)

