from PyQt5 import uic, QtCore, QtGui
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QFileDialog
from PyQt5.QtWidgets import (
    QLabel,
    QLineEdit,
    QMessageBox,
    QInputDialog,
    QDialog,
    QDialogButtonBox,
    QSlider,
    QComboBox,
)
from PyQt5.QtCore import (
    QTimer,
    QObject,
    pyqtSlot,
    pyqtSignal,
    QRunnable,
    QThreadPool,
    QSize,
)
from threading import Lock
import argparse
import configparser
import json
import sys
import re
import os

# INI file that persists in/out block positions across sessions.
# Stored next to this script so it travels with the GUI directory.
_OPTICS_INI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "optics_motors.ini"
)
try:
    from epics import PV
except ImportError:
    PV = None  # replaced by FakePV in debug mode
# try:
#     import ptychosaxs_v2.tw_galil as gl
#     MotorControlAvailable = True
# except:
#     MotorControlAvailable = False
#     print("Galil is not working")

# MotorControlAvailable = False
_optics_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_optics_dir)
sys.path.append(os.path.join(_repo_root, "debug"))
if _repo_root not in sys.path:
    sys.path.append(_repo_root)
# import tw_galil as gl
MotorControlAvailable = True
try:
    from ptychosaxs.optics import (
        ptyoptics,
        opticsbox,
        OSA,
        camera,
        beamstop,
        slit,
        slit_CRL,
        gentry,
    )

    # from newport_piezo import newport
    MotorControlAvailable = True
except:
    MotorControlAvailable = False
    print("Piezo is NOT available.")


class MotorPresetBlock:
    """Reusable enable/disable block for saving and recalling motor in/out positions.

    To add a new block, construct one more instance with the appropriate prefix
    and motor label indices, then call update_status() inside updatepos().
    """

    THRESH = 0.005  # position comparison tolerance — adjust here if needed
    DISABLED_TEXT_COLOR = "#606060"  # gray applied to labels when block is disabled

    def __init__(
        self,
        parent,
        prefix,
        pos_lbl_indices,
        in_lbl_names,
        out_lbl_names,
        extra_labels=None,
    ):
        """
        parent          – the motor_control instance
        prefix          – widget name prefix, e.g. 'osa'
        pos_lbl_indices – list of 1-based motor label indices, e.g. [6, 8]
        in_lbl_names    – label widget names for saved "In" positions
        out_lbl_names   – label widget names for saved "Out" positions
        extra_labels    – additional label names to gray out when disabled
        """
        self._parent = parent
        self._ui = parent.ui
        self._prefix = prefix
        self._pos_lbl_indices = pos_lbl_indices
        self._in_lbl_names = in_lbl_names
        self._out_lbl_names = out_lbl_names
        self._extra_labels = extra_labels or []
        self._enabled = False
        self._setup()

    def _w(self, cls, name):
        return self._ui.findChild(cls, name)

    def _setup(self):
        p = self._prefix
        btn = self._w(QPushButton, f"pushButton_{p}Enable")
        if btn:
            btn.setText("Enable")
            btn.setStyleSheet("background-color: #ffcccc;")
            btn.clicked.connect(self._toggle)
        btn_in = self._w(QPushButton, f"pushButton_{p}In")
        if btn_in:
            btn_in.clicked.connect(self._on_in)
        btn_out = self._w(QPushButton, f"pushButton_{p}Out")
        if btn_out:
            btn_out.clicked.connect(self._on_out)
        slider = self._w(QSlider, f"horizontalSlider_{p}MoveSet")
        if slider:
            self._install_slider_toggle(slider)
        self._apply_enabled(False)
        self._load_ini()

    @staticmethod
    def _install_slider_toggle(slider):
        """Make a 2-state QSlider toggle on any click instead of seeking."""

        class _ToggleFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QtCore.QEvent.MouseButtonPress:
                    obj.setValue(1 - obj.value())
                    return True  # consume — suppress Qt's own seek behaviour
                return False

        slider.installEventFilter(_ToggleFilter(slider))

    def _apply_enabled(self, enabled):
        p = self._prefix

        # enable/disable interactive widgets
        for cls, name in [
            (QPushButton, f"pushButton_{p}In"),
            (QPushButton, f"pushButton_{p}Out"),
            (QSlider, f"horizontalSlider_{p}MoveSet"),
            (QLabel, f"label_{p}Status"),
        ]:
            w = self._w(cls, name)
            if w:
                w.setEnabled(enabled)

        # toggle-button label and color (green = block enabled, red = block disabled)
        btn = self._w(QPushButton, f"pushButton_{p}Enable")
        if btn:
            if enabled:
                btn.setText("Disable")
                btn.setStyleSheet("background-color: #ffcccc;")
            else:
                btn.setText("Enable")
                btn.setStyleSheet("background-color: #ccffcc;")

        # gray out / restore all descriptive labels
        text_color = "black" if enabled else self.DISABLED_TEXT_COLOR
        all_text_labels = (
            self._in_lbl_names
            + self._out_lbl_names
            + self._extra_labels
            + [f"label_{p}Status"]
        )
        for name in all_text_labels:
            lbl = self._w(QLabel, name)
            if lbl:
                lbl.setStyleSheet(f"color: {text_color};")

        self._enabled = enabled

    def _toggle(self):
        self._apply_enabled(not self._enabled)

    def _slider_mode(self):
        s = self._w(QSlider, f"horizontalSlider_{self._prefix}MoveSet")
        return s.value() if s else 0

    def _move_motor(self, label_idx, target):
        i = label_idx - 1
        p = self._parent
        ctrl = p.control[p.controller[i]]
        axis = ctrl.motornames[p.motorindices[i]]
        with p.lock:
            ctrl.mv(axis, target, wait=False)

    def _on_in(self):
        if self._slider_mode() == 0:
            for idx, lbl_name in zip(self._pos_lbl_indices, self._in_lbl_names):
                lbl = self._w(QLabel, lbl_name)
                if lbl and lbl.text():
                    try:
                        self._move_motor(idx, float(lbl.text()))
                    except ValueError:
                        pass
        else:
            for idx, lbl_name in zip(self._pos_lbl_indices, self._in_lbl_names):
                src = self._w(QLabel, f"lbl_pos_{idx}")
                dst = self._w(QLabel, lbl_name)
                if src and dst:
                    dst.setText(src.text())
            self._save_ini()

    def _on_out(self):
        if self._slider_mode() == 0:
            for idx, lbl_name in zip(self._pos_lbl_indices, self._out_lbl_names):
                lbl = self._w(QLabel, lbl_name)
                if lbl and lbl.text():
                    try:
                        self._move_motor(idx, float(lbl.text()))
                    except ValueError:
                        pass
        else:
            for idx, lbl_name in zip(self._pos_lbl_indices, self._out_lbl_names):
                src = self._w(QLabel, f"lbl_pos_{idx}")
                dst = self._w(QLabel, lbl_name)
                if src and dst:
                    dst.setText(src.text())
            self._save_ini()

    def _load_ini(self):
        """Restore saved in/out positions from optics_motors.ini into the UI labels."""
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)
        sec = self._prefix
        if sec not in cfg:
            return
        for i, lbl_name in enumerate(self._in_lbl_names):
            val = cfg[sec].get(f"in_{i}", "").strip()
            lbl = self._w(QLabel, lbl_name)
            if lbl and val:
                lbl.setText(val)
        for i, lbl_name in enumerate(self._out_lbl_names):
            val = cfg[sec].get(f"out_{i}", "").strip()
            lbl = self._w(QLabel, lbl_name)
            if lbl and val:
                lbl.setText(val)

    def _save_ini(self):
        """Persist current in/out label values to optics_motors.ini."""
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)  # preserve other sections
        sec = self._prefix
        if sec not in cfg:
            cfg[sec] = {}
        for i, lbl_name in enumerate(self._in_lbl_names):
            lbl = self._w(QLabel, lbl_name)
            cfg[sec][f"in_{i}"] = lbl.text() if lbl else ""
        for i, lbl_name in enumerate(self._out_lbl_names):
            lbl = self._w(QLabel, lbl_name)
            cfg[sec][f"out_{i}"] = lbl.text() if lbl else ""
        with open(_OPTICS_INI, "w") as f:
            cfg.write(f)

    def update_status(self):
        """Call from updatepos to refresh the In/Out status label."""
        if not self._enabled:
            return

        def read(name):
            lbl = self._w(QLabel, name)
            if not lbl:
                return None
            try:
                return float(lbl.text())
            except ValueError:
                return None

        current = [read(f"lbl_pos_{idx}") for idx in self._pos_lbl_indices]
        in_vals = [read(n) for n in self._in_lbl_names]
        out_vals = [read(n) for n in self._out_lbl_names]

        lbl_status = self._w(QLabel, f"label_{self._prefix}Status")
        if not lbl_status:
            return

        def near(a, b):
            return a is not None and b is not None and abs(a - b) <= self.THRESH

        is_in = all(near(c, v) for c, v in zip(current, in_vals))
        is_out = all(near(c, v) for c, v in zip(current, out_vals))

        if is_in:
            lbl_status.setText("In")
            lbl_status.setStyleSheet("background-color: #00cc00; color: white;")
        elif is_out:
            lbl_status.setText("Out")
            lbl_status.setStyleSheet("background-color: #cc0000; color: white;")
        else:
            lbl_status.setText("----")
            lbl_status.setStyleSheet("")


class ZPPresetBlock(MotorPresetBlock):
    """Extends MotorPresetBlock with a named list of ZP 'In' positions.

    _pos_store is a flat dict {name: [z_val, x_val]}. listWidget_zpList shows
    all names; selecting one loads its z/x into the In display labels so
    update_status() proximity checking works without modification.
    """

    # Initial data: {position_name: [z_val, x_val]}  None = not yet saved.
    _DEFAULT_DATA = {
        "test position 1": [None, None],
        "test position 2": [None, None],
    }

    def __init__(
        self,
        parent,
        prefix,
        pos_lbl_indices,
        in_lbl_names,
        out_lbl_names,
        extra_labels=None,
    ):
        self._pos_store = {k: list(v) for k, v in self._DEFAULT_DATA.items()}
        self._listw = None
        super().__init__(
            parent, prefix, pos_lbl_indices, in_lbl_names, out_lbl_names, extra_labels
        )
        self._setup_zp()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_zp(self):
        from PyQt5.QtWidgets import QWidget, QAbstractItemView

        # QWidget base used because QListWidget.bool() is False while hidden
        self._listw = self._ui.findChild(QWidget, "listWidget_zpList")
        btn_add_zp = self._w(QPushButton, "pushButton_addZp")

        if self._listw is not None:
            self._listw.setSelectionMode(QAbstractItemView.SingleSelection)
            self._listw.currentItemChanged.connect(self._on_position_changed)

        if btn_add_zp:
            btn_add_zp.clicked.connect(self._on_add_zp_button)

        QTimer.singleShot(0, self._populate_list)

    # ------------------------------------------------------------------
    # List management
    # ------------------------------------------------------------------

    def _populate_list(self):
        if self._listw is None:
            return
        self._listw.blockSignals(True)
        self._listw.clear()
        for pos_name in self._pos_store:
            self._listw.addItem(pos_name)
        self._listw.blockSignals(False)
        if self._listw.count() > 0:
            self._listw.setCurrentRow(0)
            self._load_position(self._listw.item(0).text())

    def _on_position_changed(self, current, _):
        if current is None:
            return
        self._load_position(current.text())

    def _load_position(self, pos_name):
        """Push stored z/x values into the In display labels."""
        if pos_name not in self._pos_store:
            return
        vals = self._pos_store[pos_name]
        for lbl_name, val in zip(self._in_lbl_names, vals):
            lbl = self._w(QLabel, lbl_name)
            if lbl:
                lbl.setText("" if val is None else "%.3f" % val)

    def _current_pos_name(self):
        """Return the currently selected position name, or None."""
        if self._listw is None:
            return None
        item = self._listw.currentItem()
        return item.text() if item else None

    # ------------------------------------------------------------------
    # INI persistence (overrides base — flat positions JSON, no in labels)
    # ------------------------------------------------------------------

    def _load_ini(self):
        """Restore ZP out positions and position store from the INI file.

        In positions are not stored as flat values; they are always derived
        from _pos_store when a list item is selected.
        """
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)
        sec = self._prefix
        if sec not in cfg:
            return
        for i, lbl_name in enumerate(self._out_lbl_names):
            val = cfg[sec].get(f"out_{i}", "").strip()
            lbl = self._w(QLabel, lbl_name)
            if lbl and val:
                lbl.setText(val)
        raw = cfg[sec].get("positions", "").strip()
        if raw:
            try:
                self._pos_store = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass  # keep _DEFAULT_DATA on corrupt entry

    def _save_ini(self):
        """Persist ZP out positions and position store to the INI file."""
        cfg = configparser.ConfigParser()
        cfg.read(_OPTICS_INI)
        sec = self._prefix
        if sec not in cfg:
            cfg[sec] = {}
        for i, lbl_name in enumerate(self._out_lbl_names):
            lbl = self._w(QLabel, lbl_name)
            cfg[sec][f"out_{i}"] = lbl.text() if lbl else ""
        cfg[sec]["positions"] = json.dumps(self._pos_store)
        with open(_OPTICS_INI, "w") as f:
            cfg.write(f)

    # ------------------------------------------------------------------
    # Enable/disable — pushButton_addZp stays always active
    # ------------------------------------------------------------------

    def _apply_enabled(self, enabled):
        super()._apply_enabled(enabled)

    # ------------------------------------------------------------------
    # In-button override (reads/writes internal store instead of labels)
    # ------------------------------------------------------------------

    def _on_in(self):
        pos_name = self._current_pos_name()
        if self._slider_mode() == 0:
            # Move — use internally stored values (labels are display-only)
            if pos_name and pos_name in self._pos_store:
                vals = self._pos_store[pos_name]
                for idx, val in zip(self._pos_lbl_indices, vals):
                    if val is not None:
                        self._move_motor(idx, val)
        else:
            # Save — write current motor positions into store, then refresh labels
            if pos_name:
                new_vals = []
                for idx in self._pos_lbl_indices:
                    src = self._w(QLabel, f"lbl_pos_{idx}")
                    try:
                        new_vals.append(float(src.text()) if src else None)
                    except ValueError:
                        new_vals.append(None)
                self._pos_store[pos_name] = new_vals
                self._load_position(pos_name)
                self._save_ini()

    # ------------------------------------------------------------------
    # Add / Remove — launched from the choice dialog on pushButton_addZp
    # ------------------------------------------------------------------

    def _on_add_zp_button(self):
        from PyQt5.QtWidgets import QVBoxLayout

        dlg = QDialog()
        dlg.setWindowTitle("ZP Positions")
        layout = QVBoxLayout(dlg)
        btn_add = QPushButton("Add ZP")
        btn_remove = QPushButton("Remove ZP")
        layout.addWidget(btn_add)
        layout.addWidget(btn_remove)
        result = [None]

        def _pick_add():
            result[0] = "add"
            dlg.accept()

        def _pick_remove():
            result[0] = "remove"
            dlg.accept()

        btn_add.clicked.connect(_pick_add)
        btn_remove.clicked.connect(_pick_remove)
        dlg.exec_()
        if result[0] == "add":
            self._add_position()
        elif result[0] == "remove":
            self._remove_position()

    def _add_position(self):
        name, ok = QInputDialog.getText(None, "Add ZP Position", "Position name:")
        if not (ok and name.strip()):
            return
        name = name.strip()
        if name not in self._pos_store:
            self._pos_store[name] = [None, None]
        if self._listw is not None:
            existing = [self._listw.item(i).text() for i in range(self._listw.count())]
            if name not in existing:
                self._listw.addItem(name)
            for i in range(self._listw.count()):
                if self._listw.item(i).text() == name:
                    self._listw.setCurrentRow(i)
                    break
        self._save_ini()

    def _remove_position(self):
        if not self._pos_store:
            return
        from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout

        dlg = QDialog()
        dlg.setWindowTitle("Remove ZP Position")
        layout = QVBoxLayout(dlg)
        combo = QComboBox()
        for name in self._pos_store:
            combo.addItem(name)
        layout.addWidget(combo)
        btn_row = QHBoxLayout()
        btn_ok = QPushButton("Remove")
        btn_cancel = QPushButton("Cancel")
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        if dlg.exec_() != QDialog.Accepted:
            return
        name = combo.currentText()
        self._pos_store.pop(name, None)
        if self._listw is not None:
            for i in range(self._listw.count()):
                if self._listw.item(i).text() == name:
                    self._listw.takeItem(i)
                    break
        self._save_ini()


class motor_control(QMainWindow):
    #    resized = QtCore.pyqtSignal()

    MOTOR_PREC = "%0.3f"

    # Direction button → (1-based motor label index, step sign, tweak QLineEdit name)
    # Step value is read from the named QLineEdit and treated as MICRONS.
    # To remap a button, change its motor index or tweak widget here.
    DIR_BUTTON_MAP = {
        "pb_osa_left":  (6, -1, "ed_osa_tweak"),
        "pb_osa_right": (6, +1, "ed_osa_tweak"),
        "pb_osa_down":  (7, -1, "ed_osa_tweak"),
        "pb_osa_up":    (7, +1, "ed_osa_tweak"),
        "pb_bs_left":   (2, -1, "ed_bs_tweak"),
        "pb_bs_right":  (2, +1, "ed_bs_tweak"),
        "pb_bs_down":   (1, -1, "ed_bs_tweak"),
        "pb_bs_up":     (1, +1, "ed_bs_tweak"),
        "pb_zp_left":   (4, -1, "ed_zp_tweak"),
        "pb_zp_right":  (4, +1, "ed_zp_tweak"),
        "pb_zp_down":   (3, -1, "ed_zp_tweak"),
        "pb_zp_up":     (3, +1, "ed_zp_tweak"),
    }

    def __init__(self, debug_mode=False):
        super(motor_control, self).__init__()
        self.debug_mode = debug_mode
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        guiName = "motorGUI.ui"
        self.ui = uic.loadUi(guiName)

        # list all possible motors
        # this should came from the pts.
        # controller = ['galil', 'smarAct', 'newport']
        self.control = {}
        if self.debug_mode:
            from debug_stubs import (
                DebugOpticsbox,
                DebugOSA,
                DebugCamera,
                DebugBeamstop,
                DebugSlit,
            )

            self.control["opticsbox"] = DebugOpticsbox()
            self.control["OSA"] = DebugOSA()
            self.control["camera"] = DebugCamera()
            self.control["beamstop"] = DebugBeamstop()
            self.control["slit"] = DebugSlit()
        elif MotorControlAvailable:
            self.control["opticsbox"] = opticsbox()
            self.control["OSA"] = OSA()
            self.control["camera"] = camera()
            self.control["beamstop"] = beamstop()
            self.control["slit"] = slit()
        else:
            raise RuntimeError(
                "Motor control hardware (optics) is not available. Run with --debug_mode to use stubs."
            )
        # self.control["slit_CRL"] = slit_CRL()
        self.motornames = []
        self.motorunits = []
        self.motorindices = []
        self.controller = []
        for i, m in enumerate(self.control["opticsbox"].motors):
            self.motornames.append(m.DESC)
            self.motorunits.append(m.EGU)
            self.controller.append("opticsbox")
            self.motorindices.append(i)

        for i, m in enumerate(self.control["OSA"].motors):
            self.motornames.append(m.DESC)
            self.motorunits.append(m.EGU)
            self.controller.append("OSA")
            self.motorindices.append(i)

        for i, m in enumerate(self.control["camera"].motors):
            self.motornames.append(m.DESC)
            self.motorunits.append(m.EGU)
            self.controller.append("camera")
            self.motorindices.append(i)

        for i, m in enumerate(self.control["beamstop"].motors):
            self.motornames.append(m.DESC)
            self.motorunits.append(m.EGU)
            self.controller.append("beamstop")
            self.motorindices.append(i)

        for i, m in enumerate(self.control["slit"].motors):
            self.motornames.append(m.name)
            self.motorunits.append(m.units)
            self.controller.append("slit")
            self.motorindices.append(i)

        # for i, m in enumerate(self.control["slit_CRL"].motors):
        #     self.motornames.append(m.name)
        #     self.motorunits.append(m.units)
        #     self.controller.append('slit_CRL')
        #     self.motorindices.append(i)

        print(self.motornames)
        self.lock = Lock()
        self.threadpool = QThreadPool.globalInstance()
        enable = True
        for i, name in enumerate(self.motornames):
            n = i + 1
            self.enable_motors(n, enable)

        # update GUI
        for i, name in enumerate(self.motornames):
            n = i + 1
            controller = self.control[self.controller[i]]
            axisname = controller.motornames[self.motorindices[i]]
            lbl_name = self.ui.findChild(QLabel, "lbl_motor_%i" % n)
            lbl_pos = self.ui.findChild(QLabel, "lbl_pos_%i" % n)
            btn_tweakL = self.ui.findChild(QPushButton, "btn_tweak%iL" % n)
            btn_tweakR = self.ui.findChild(QPushButton, "btn_tweak%iR" % n)
            ed_mv = self.ui.findChild(QLineEdit, "edit_%i" % n)
            ed_reset = self.ui.findChild(QLineEdit, "edit_reset_%i" % n)
            btn_stop = self.ui.findChild(QPushButton, "btn_lup_%i" % n)
            if lbl_name:
                lbl_name.setText(name)
            if lbl_pos:
                lbl_pos.setText(str(controller.get_pos(axisname)))
            if btn_tweakL:
                btn_tweakL.clicked.connect(lambda: self.mvr(-1, -1))
            if btn_tweakR:
                btn_tweakR.clicked.connect(lambda: self.mvr(-1, 1))
            if ed_mv:
                ed_mv.returnPressed.connect(lambda: self.mv(-1, None))
            if ed_reset:
                ed_reset.returnPressed.connect(lambda: self.reset(-1))
            if btn_stop:
                btn_stop.setText("Stop")
                btn_stop.clicked.connect(lambda: self.stop(-1))

        # menu
        self.ui.actionSmarAct_3.triggered.connect(self.enable_ptyoptics)
        self.ui.actionNewport.triggered.connect(self.enable_galil)
        self.ui.actionNewport_Piezo.triggered.connect(self.enable_newport)
        self.ui.actionIn.triggered.connect(self.put_xrayeye_in)
        self.ui.actionOut.triggered.connect(self.put_xrayeye_out)
        if self.debug_mode:
            from debug_stubs import FakePV as _PV
        else:
            _PV = PV
        status = _PV("usxRIO:Galil2Bo0_STATUS.VAL")
        if status.get() == 0:
            self.ui.actionOut.setEnabled(False)
            self.ui.actionIn.setEnabled(True)
            self._set_xrayeye_buttons(eye_in=True)
        else:
            self.ui.actionOut.setEnabled(True)
            self.ui.actionIn.setEnabled(False)
            self._set_xrayeye_buttons(eye_in=False)

        # pushButton_xrayEyeIn / Out track the menu items
        btn_eye_in = self.ui.findChild(QPushButton, "pushButton_xrayEyeIn")
        btn_eye_out = self.ui.findChild(QPushButton, "pushButton_xrayEyeOut")
        if btn_eye_in:
            btn_eye_in.clicked.connect(self.put_xrayeye_in)
        if btn_eye_out:
            btn_eye_out.clicked.connect(self.put_xrayeye_out)

        # Copy current positions into move-to boxes
        btn_copy = self.ui.findChild(QPushButton, "pushButton_copyCurrent")
        if btn_copy:
            btn_copy.clicked.connect(self.copy_current_positions)

        # OSA preset block: OSA_X = lbl_pos_6, OSA_Z = lbl_pos_7
        self.osa_block = MotorPresetBlock(
            self,
            "osa",
            pos_lbl_indices=[7, 6],
            in_lbl_names=["lbl_osazIn", "lbl_osaxIn"],
            out_lbl_names=["lbl_osazOut", "lbl_osaxOut"],
            extra_labels=[
                "label_osaxInName",
                "label_osaxOutName",
                "label_osazInName",
                "label_osazOutName",
                "label_3",
                "label_2",
            ],
        )

        # Beamstop preset block: BS_ver = lbl_pos_1, BS_hor = lbl_pos_2
        self.bs_block = MotorPresetBlock(
            self,
            "bs",
            pos_lbl_indices=[1, 2],
            in_lbl_names=["lbl_bszIn", "lbl_bsxIn"],
            out_lbl_names=["lbl_bszOut", "lbl_bsxOut"],
            extra_labels=[
                "label_bsxInName",
                "label_bsxOutName",
                "label_bszInName",
                "label_bszOutName",
                "label_bsMoveSet",
                "label_14",
                "label_bsStatus",
            ],
        )

        # Zone plate preset block: ZP_ver = lbl_pos_3, ZP_hor = lbl_pos_4
        self.zp_block = ZPPresetBlock(
            self,
            "zp",
            pos_lbl_indices=[3, 4],
            in_lbl_names=["lbl_zpzIn", "lbl_zpxIn"],
            out_lbl_names=["lbl_zpzOut", "lbl_zpxOut"],
            extra_labels=[
                "label_zpxInName",
                "label_zpxOutName",
                "label_zpzInName",
                "label_zpzOutName",
                "label_zpMoveSet",
                "label_16",
                "label_zpStatus",
                "pushButton_addZp",
            ],
        )

        # Export / Import positions buttons
        btn_export = self.ui.findChild(QPushButton, "pushButton_exportPos")
        if btn_export:
            btn_export.clicked.connect(self.export_positions)
        btn_import = self.ui.findChild(QPushButton, "pushButton_importPos")
        if btn_import:
            btn_import.clicked.connect(self.import_positions)

        # Exit button
        btn_exit = self.ui.findChild(QPushButton, "pushButton_exit")
        if btn_exit:
            btn_exit.clicked.connect(QApplication.instance().quit)

        # Direction buttons (micron-step nudge)
        self._connect_dir_buttons()

        # All-In / All-Out buttons
        btn_all_in = self.ui.findChild(QPushButton, "pushButton_allIn")
        btn_all_out = self.ui.findChild(QPushButton, "pushButton_allOut")
        if btn_all_in:
            btn_all_in.clicked.connect(self._all_in)
        if btn_all_out:
            btn_all_out.clicked.connect(self._all_out)

        if os.name == "nt":
            self.timer = QTimer()
            self.timer.timeout.connect(self.updatepos)
            self.timer.start(100)
        self.ui.show()
        # self.resized.connect(self.resizeFunction)

    def _set_xrayeye_buttons(self, eye_in: bool):
        """Sync pushButton_xrayEyeIn/Out enabled state to match the menu items."""
        btn_in = self.ui.findChild(QPushButton, "pushButton_xrayEyeIn")
        btn_out = self.ui.findChild(QPushButton, "pushButton_xrayEyeOut")
        if btn_in:
            btn_in.setEnabled(eye_in)
        if btn_out:
            btn_out.setEnabled(not eye_in)

    def put_xrayeye_in(self):
        self.ui.actionIn.setEnabled(False)
        self.ui.actionOut.setEnabled(True)
        self._set_xrayeye_buttons(eye_in=False)
        self.put_xrayeye(True)

    def put_xrayeye_out(self):
        self.ui.actionOut.setEnabled(False)
        self.ui.actionIn.setEnabled(True)
        self._set_xrayeye_buttons(eye_in=True)
        self.put_xrayeye(False)

    def put_xrayeye(self, ins=True):
        if self.debug_mode:
            from debug_stubs import FakePV as _PV
        else:
            _PV = PV
        pvs = _PV("usxRIO:Galil2Bo0_CMD")
        pvs.put(1 if ins else 0)

    def copy_current_positions(self):
        """Copy each motor's current position label into its Move-to edit box."""
        for i in range(len(self.motornames)):
            n = i + 1
            lbl = self.ui.findChild(QLabel, "lbl_pos_%i" % n)
            ed = self.ui.findChild(QLineEdit, "edit_%i" % n)
            if lbl and ed:
                ed.setText(lbl.text())

    def export_positions(self):
        """Save all in/out block positions and ZP store to a user-chosen JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self.ui, "Export Positions", "optics_positions.json", "JSON Files (*.json)"
        )
        if not path:
            return

        def read_lbl(block, lbl_name):
            lbl = block._w(QLabel, lbl_name)
            if lbl:
                try:
                    return float(lbl.text())
                except ValueError:
                    pass
            return None

        def motor_name(block, idx_in_block):
            list_idx = block._pos_lbl_indices[idx_in_block] - 1
            return (
                self.motornames[list_idx]
                if list_idx < len(self.motornames)
                else f"motor_{idx_in_block}"
            )

        def block_in_out(block):
            return {
                "in": {
                    motor_name(block, i): read_lbl(block, n)
                    for i, n in enumerate(block._in_lbl_names)
                },
                "out": {
                    motor_name(block, i): read_lbl(block, n)
                    for i, n in enumerate(block._out_lbl_names)
                },
            }

        data = {
            "osa": block_in_out(self.osa_block),
            "bs": block_in_out(self.bs_block),
            "zp": {
                "out": {
                    motor_name(self.zp_block, i): read_lbl(self.zp_block, n)
                    for i, n in enumerate(self.zp_block._out_lbl_names)
                },
                "positions": self.zp_block._pos_store,
            },
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def import_positions(self):
        """Load positions from a JSON file produced by export_positions."""
        path, _ = QFileDialog.getOpenFileName(
            self.ui, "Import Positions", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.warning(self.ui, "Import Error", str(e))
            return

        def motor_name(block, idx_in_block):
            list_idx = block._pos_lbl_indices[idx_in_block] - 1
            return (
                self.motornames[list_idx]
                if list_idx < len(self.motornames)
                else f"motor_{idx_in_block}"
            )

        def write_lbl(block, lbl_name, val):
            lbl = block._w(QLabel, lbl_name)
            if lbl and val is not None:
                lbl.setText("%.3f" % val)

        def apply_block(block, section):
            sec = data.get(section, {})
            for i, lbl_name in enumerate(block._in_lbl_names):
                mname = motor_name(block, i)
                write_lbl(block, lbl_name, sec.get("in", {}).get(mname))
            for i, lbl_name in enumerate(block._out_lbl_names):
                mname = motor_name(block, i)
                write_lbl(block, lbl_name, sec.get("out", {}).get(mname))
            block._save_ini()

        apply_block(self.osa_block, "osa")
        apply_block(self.bs_block, "bs")

        zp_sec = data.get("zp", {})
        for i, lbl_name in enumerate(self.zp_block._out_lbl_names):
            mname = motor_name(self.zp_block, i)
            write_lbl(self.zp_block, lbl_name, zp_sec.get("out", {}).get(mname))
        positions = zp_sec.get("positions")
        if isinstance(positions, dict):
            self.zp_block._pos_store = positions
            self.zp_block._populate_list()
        self.zp_block._save_ini()

    def _all_in(self):
        for block in (self.osa_block, self.bs_block, self.zp_block):
            block._on_in()

    def _all_out(self):
        for block in (self.osa_block, self.bs_block, self.zp_block):
            block._on_out()

    def _update_all_status(self):
        """Aggregate OSA / BS / ZP status labels into label_allStatus."""
        lbl_all = self.ui.findChild(QLabel, "label_allStatus")
        if not lbl_all:
            return
        status_names = ["label_osaStatus", "label_bsStatus", "label_zpStatus"]
        texts = []
        for name in status_names:
            lbl = self.ui.findChild(QLabel, name)
            texts.append(lbl.text() if lbl else "")
        if all(t == "In" for t in texts):
            src = self.ui.findChild(QLabel, "label_osaStatus")
            lbl_all.setText("In")
            lbl_all.setStyleSheet(
                src.styleSheet() if src else "background-color: #00cc00; color: white;"
            )
        elif all(t == "Out" for t in texts):
            src = self.ui.findChild(QLabel, "label_osaStatus")
            lbl_all.setText("Out")
            lbl_all.setStyleSheet(
                src.styleSheet() if src else "background-color: #cc0000; color: white;"
            )
        else:
            lbl_all.setText("----")
            lbl_all.setStyleSheet("")

    def _connect_dir_buttons(self):
        """Wire each direction button from DIR_BUTTON_MAP to _dir_step."""
        for btn_name, (motor_1based, sign, tweak_widget) in self.DIR_BUTTON_MAP.items():
            btn = self.ui.findChild(QPushButton, btn_name)
            if btn:
                btn.clicked.connect(
                    lambda checked=False, m=motor_1based, s=sign, t=tweak_widget: (
                        self._dir_step(m, s, t)
                    )
                )

    def _dir_step(self, motor_1based, sign, tweak_widget):
        """Nudge a motor by sign * tweak-box value (microns → mm)."""
        ed = self.ui.findChild(QLineEdit, tweak_widget)
        try:
            step_um = float(ed.text()) if ed else 1.0
        except ValueError:
            step_um = 1.0
        step_mm = step_um / 1000.0
        motor_idx = motor_1based - 1
        controller = self.control[self.controller[motor_idx]]
        axis = controller.motornames[self.motorindices[motor_idx]]
        with self.lock:
            controller.mvr(axis, sign * step_mm, wait=False)

    def enable_motors(self, n, enable):
        def _set(w, val):
            if w:
                w.setEnabled(val)

        _set(self.ui.findChild(QLabel, "lbl_motor_%i" % n), enable)
        _set(self.ui.findChild(QLabel, "lbl_pos_%i" % n), enable)
        _set(self.ui.findChild(QPushButton, "btn_tweak%iL" % n), enable)
        _set(self.ui.findChild(QPushButton, "btn_tweak%iR" % n), enable)
        _set(self.ui.findChild(QPushButton, "btn_lup_%i" % n), enable)
        _set(self.ui.findChild(QPushButton, "btn_SAXSscan_%i" % n), False)
        _set(self.ui.findChild(QLineEdit, "edit_%i" % n), enable)
        _set(self.ui.findChild(QLineEdit, "edit_%i_tweak" % n), enable)
        _set(self.ui.findChild(QLineEdit, "edit_reset_%i" % n), enable)

    def set_ui_enability(self, controller="smarAct", enable=True):
        for i, con in enumerate(self.controller):
            if con == controller:
                self.enable_motors(i + 1, enable)

    # def enable_smarAct(self):
    #     if self.ui.actionSmarAct_3.isChecked():
    #         enable = True
    #     else:
    #         enable = False
    #     self.set_ui_enability('smarAct', enable=enable)

    def enable_galil(self):
        if self.ui.actionGalil.isChecked():
            enable = True
        else:
            enable = False
        self.set_ui_enability("galil", enable=enable)

    def enable_newport(self):
        if self.ui.actionNewport_Piezo.isChecked():
            enable = True
        else:
            enable = False
        self.set_ui_enability("newport", enable=enable)

    def enable_ptyoptics(self):
        if self.ui.actionOptics.isChecked():
            enable = True
        else:
            enable = False
        self.set_ui_enability("ptyoptics", enable=enable)

    def stop(self, motornumber=-1):
        if motornumber < 0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r"\d+", objname)[0])
            # n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n - 1

        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        controller.stop(axis)

    def reset(self, motornumber=-1):
        if motornumber < 0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r"\d+", objname)[0])
            # n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n - 1

        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        try:
            val = int(val_text)
        except ValueError:
            print("Invalid input for reset value.")
            return
        with self.lock:
            controller.set_pos(axis, val)

    def mv(self, motornumber=-1, val=None):
        if motornumber < 0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r"\d+", objname)[0])
            motornumber = n - 1

        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        self.signalmotor = axis
        self.signalmotorunit = controller.motorunits[self.motorindices[motornumber]]
        self.set_ui_enability(controller, False)
        if type(val) == type(None):
            try:
                val = float(val_text)
            except:
                print("Text box is empty.")
                return
        with self.lock:
            controller.mv(axis, val, wait=False)
        self.set_ui_enability(controller, True)

    def mvr(self, motornumber=-1, sign=1, val=0):
        if motornumber == -1:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r"\d+", objname)[0])
            # n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n - 1
        # print("motornumber is ", motornumber)
        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        self.signalmotor = axis
        # print("axis is ", axis)
        # print("sign is ", sign)
        self.signalmotorunit = controller.motorunits[self.motorindices[motornumber]]
        self.set_ui_enability(controller, False)
        if val == 0:
            val = float(self.ui.findChild(QLineEdit, "edit_%i_tweak" % n).text())
        # print(f"Move {axis} by {sign*val}")

        controller.mvr(axis, sign * val, wait=False)
        self.set_ui_enability(controller, True)

    def updatepos(self, axis="", val=None):
        # done = False
        # timeout = 10
        # ct0 = time.time()
        if len(axis) == 0:
            for i, name in enumerate(self.motornames):
                controller = self.control[self.controller[i]]
                axis = controller.motornames[self.motorindices[i]]
                if val is None:
                    with self.lock:
                        val = controller.get_pos(axis)
                        # print(val)
                lbl = self.ui.findChild(QLabel, "lbl_pos_%i" % (i + 1))
                if lbl:
                    lbl.setText(self.MOTOR_PREC % val)
                val = None
            self.osa_block.update_status()
            self.bs_block.update_status()
            self.zp_block.update_status()
            self._update_all_status()
        else:
            motornumber = self.motornames.index(axis)
            controller = self.control[self.controller[motornumber]]
            axis = controller.motornames[self.motorindices[motornumber]]
            if val is None:
                with self.lock:
                    print(axis, " This is in line 3042")
                    val = controller.get_pos(axis)
            i = motornumber
            lbl = self.ui.findChild(QLabel, "lbl_pos_%i" % (i + 1))
            if lbl:
                lbl.setText("%0.6f" % val)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug_mode",
        action="store_true",
        help="Run without connecting to motors or EPICS PVs",
    )
    args, _ = parser.parse_known_args()  # parse_known_args so Qt args pass through

    app = QApplication(sys.argv)
    motor_panel = motor_control(debug_mode=args.debug_mode)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
