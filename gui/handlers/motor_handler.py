"""
gui/handlers/motor_handler.py
Handles all motor movement, position reading, hardware connect/disconnect,
and motor tweak button interactions for the ptychoSAXS GUI.
"""
from __future__ import annotations
import re
import time
from typing import TYPE_CHECKING

from PyQt5.QtWidgets import QLabel, QLineEdit, QMenu
from PyQt5.QtCore import Qt, QPoint

if TYPE_CHECKING:
    from rungui import ptyco_main_control  # avoid circular import at runtime


class MotorHandler:
    def __init__(self, window: "ptyco_main_control"):
        self.w = window          # reference to the main window
        self.ui = window.ui      # shortcut to the loaded .ui widgets
        self._connect_signals()

    def _connect_signals(self):
        """Connect push-button signals to this handler's slots.

        Note: The dynamic per-motor signal connections (tweak buttons,
        lup buttons, SAXS scan buttons, and line-edit returnPressed) are
        wired in ptyco_main_control.__init__ because they depend on the
        motornames list that is built there.  Only static signals that do
        not depend on that loop are wired here.
        """
        pass

    # --- extracted methods below ---

    def _on_motor_context_menu(self, n, pos: QPoint):
        menu = QMenu(self.ui)
        set_zero_action = menu.addAction("Set to 0")
        set_zero_action.triggered.connect(lambda chosen, wn=n: self._on_set_to_zero(chosen, wn))

        # Map the position from the label to global screen coordinates
        global_pos = self.ui.findChild(QLabel, "lb_%i" % n).mapToGlobal(pos)
        menu.exec_(global_pos)

    def _on_set_to_zero(self, checked=False, n=0):
        self.w.pts.set_pos(self.w.motornames[n - 1], 0)

    def handle_hexapod_error(self):
        self.w.pts.hexapod.handle_error()

    def write_motor_scan_range(self):
        import numpy as np
        numbers = np.random.rand(len(self.w.motornames), 6)
        for i, name in enumerate(self.w.motornames):
            n = i + 1
            line_edit_suffixes = ["pos", "tweak", "L", "R", "N", "t"]
            arr = []

            for suffix in line_edit_suffixes:
                if len(suffix) == 1:
                    line_edit_name = f"ed_lup_{n}_{suffix}"
                else:
                    if suffix == "tweak":
                        line_edit_name = f"ed_{n}_{suffix}"
                    if suffix == "pos":
                        line_edit_name = f"ed_{n}"
                try:
                    value = float(self.ui.findChild(QLineEdit, line_edit_name).text())
                except ValueError:  # More specific exception for parsing errors
                    value = -999999
                arr.append(value)

            numbers[i] = arr

            # Save the array to a file
        import numpy as np
        np.save('_numbers.npy', numbers)

    def read_motor_scan_range(self):
        import numpy as np
        # Load the array from the file
        numbers = np.load('_numbers.npy')

        for i, name in enumerate(self.w.motornames):
            n = i + 1
            if numbers.shape[1] == 5:
                line_edit_suffixes = ["tweak", "L", "R", "N", "t"]
            if numbers.shape[1] == 6:
                line_edit_suffixes = ["pos", "tweak", "L", "R", "N", "t"]
            try:
                for j, suffix in enumerate(line_edit_suffixes):
                    value = '' if numbers[i, j] == -999999 else str(numbers[i, j])
                    if len(suffix) == 1:
                        line_edit_name = f"ed_lup_{n}_{suffix}"
                    else:
                        if suffix == "tweak":
                            line_edit_name = f"ed_{n}_{suffix}"
                        if suffix == "pos":
                            line_edit_name = f"ed_{n}"
                            if len(value) > 0:
                                value = "%0.6f" % float(value)
                    self.ui.findChild(QLineEdit, line_edit_name).setText(value)
            except:
                pass

    def get_motorpos(self, axis):
        # get motor position from the label
        # i.e. axis = 'X'
        i = self.w.motornames.index(axis)
        return float(self.ui.findChild(QLabel, "lb_%i" % (i + 1)).text())

    def get_pos_all(self):
        motors = {}
        for name in self.w.motornames:
            motors[name] = self.w.pts.get_pos(name)
        return motors

    def updatepos(self, axis="", val=None):
        if len(axis) == 0:
            for i, name in enumerate(self.w.motornames):
                if val is None:
                    val = self.w.pts.get_pos(name)
                self.ui.findChild(QLabel, "lb_%i" % (i + 1)).setText("%0.6f" % val)
                val = None
        else:
            if val is None:
                val = self.w.pts.get_pos(axis)
            i = self.w.motornames.index(axis)
            self.ui.findChild(QLabel, "lb_%i" % (i + 1)).setText("%0.6f" % val)

    def update_motorpos(self, value):
        self.updatepos(self.w.signalmotor, value)

    def update_motorname(self, axis):
        self.w.signalmotor = axis

    def smaract_set_defaultspeed(self):
        for i, connected in enumerate(self.w.pts.gonio.connected):
            if connected:
                self.w.pts.gonio.set_speed(i)

    def smaract_calibrate(self):
        for i, connected in enumerate(self.w.pts.gonio.connected):
            if connected:
                self.w.pts.gonio.calibrate(i)
        print("MCS2 calibration done..")

    def smaract_findreference(self):
        for i, connected in enumerate(self.w.pts.gonio.connected):
            if connected:
                self.w.pts.gonio.findReference(i)
        print("MCS2 finding references done..")

    def setphivel_default(self):
        self.w.pts.set_speed('phi', 36, 360)

    def sethexapodvel_default(self):
        self.w.pts.set_speed(self.w.pts.hexapod.axes[0], 5, None)

    def mv(self, motornumber=-1, val=None):
        from rungui import move
        if motornumber < 0:
            pb = self.w.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n - 1

        axis = self.w.motornames[motornumber]
        self.w.signalmotor = axis
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        if type(val) == type(None):
            try:
                val = float(val_text)
            except:
                from rungui import showerror
                showerror('Text box is empty.')
                return
        w = move(self.w.pts, axis, val)
        self.w.threadpool.start(w)
        self.updatepos(axis)

    def mvr(self, motornumber=-1, sign=1, val=0):
        from rungui import mover
        if motornumber == -1:
            pb = self.w.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n - 1
        axis = self.w.motornames[motornumber]
        self.w.signalmotor = axis
        self.w.signalmotorunit = self.w.motorunits[motornumber]
        if val == 0:
            val = float(self.ui.findChild(QLineEdit, "ed_%i_tweak" % n).text())

        w = mover(self.w.pts, axis, sign * val)
        self.w.threadpool.start(w)
        self.updatepos(axis)
