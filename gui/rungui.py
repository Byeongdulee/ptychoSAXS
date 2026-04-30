# -*- coding: utf-8 -*-
"""
Created on Thu Oct 27 16:42:18 2016

@author: Byeongdu Lee
@Date: Nov. 1. 2016
"""

import sys
import os

# ==========================================================================
# DEBUG MODE — detected FIRST, before any hardware imports
# ==========================================================================
# Activated by:  python rungui.py --debug   OR   set PTYCHOSAXS_DEBUG=1
#
# In debug mode all hardware libraries (epics, pihexapod, acspy, smaract,
# gclib, pyepics, etc.) are skipped entirely and replaced with stubs from
# debug/debug_stubs.py.  No network or EPICS connections are made.
#
# To add hardware IMPORT testing later (verify the libs are installed
# without actually connecting to instruments):
#   1. Add a second flag, e.g. IMPORT_TEST_MODE = "--import-test" in sys.argv
#   2. Place just the bare `import` lines inside:
#        if not DEBUG_MODE:   # keep this guard so debug still works
#            import epics
#            ...
#      The connection/PV-creation calls remain inside `if not DEBUG_MODE and not IMPORT_TEST_MODE:`.
# ==========================================================================
DEBUG_MODE = "--debug" in sys.argv or os.environ.get("PTYCHOSAXS_DEBUG") == "1"

# Parse optional debug level: --debug [N]  (default 0)
# Level 0 — motors start at 0.0 and respond to mv/mvr; QDS shows random values.
DEBUG_LEVEL = 0
if DEBUG_MODE:
    _debug_idx = sys.argv.index("--debug") if "--debug" in sys.argv else -1
    if _debug_idx >= 0 and _debug_idx + 1 < len(sys.argv):
        _next = sys.argv[_debug_idx + 1]
        if _next.isdigit():
            DEBUG_LEVEL = int(_next)
    DEBUG_LEVEL = int(os.environ.get("PTYCHOSAXS_DEBUG_LEVEL", DEBUG_LEVEL))

# Debug sub-flags derived from level:
#   Level 0 — all debug (motors + devices)
#   Level 1 — real motors, debug devices
#   Level 2 — debug motors, real devices
DEBUG_MOTORS = DEBUG_MODE and DEBUG_LEVEL in (0, 2)  # motors are stubbed
DEBUG_DEVICES = DEBUG_MODE and DEBUG_LEVEL in (0, 1)  # non-motors are stubbed

# Path setup must come before any hardware import (including debug stubs)
_gui_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_gui_dir)
if _repo_root not in sys.path:
    sys.path.append(_repo_root)
sys.path.append(os.path.join(_repo_root, "debug"))

if DEBUG_MODE:
    print(f"[DEBUG MODE] Level {DEBUG_LEVEL}")
    print(f"  Motors:  {'STUB' if DEBUG_MOTORS else 'REAL'}")
    print(f"  Devices: {'STUB' if DEBUG_DEVICES else 'REAL'}")
    from debug_stubs import (
        HexapodStub,
        PhiStub,
        GonioStub,
        PilatusStub,
        ShutterStub,
        DG645Stub,
        SGZStub,
        QDSStub,
        StruckStub,
        BeamStatusStub,
        InstrumentsStub,
    )

# ==========================================================================
# Standard library / GUI imports (always safe — no hardware dependencies)
# ==========================================================================
import asyncio
import json
import datetime
import pathlib
import time
import re
import numpy as np
from typing import List
from threading import Lock

from PyQt5 import uic, QtCore, QtGui
from PyQt5.QtWidgets import QApplication, QPushButton, QFileDialog, QWidget, QFormLayout
from PyQt5.QtWidgets import (
    QLabel,
    QLineEdit,
    QMessageBox,
    QInputDialog,
    QDialog,
    QDialogButtonBox,
    QMenu,
)
from PyQt5.QtGui import QIntValidator
from PyQt5.QtCore import (
    QTimer,
    QObject,
    pyqtSlot,
    pyqtSignal,
    QRunnable,
    QThreadPool,
    Qt,
    QPoint,
)
from asyncqt import QEventLoop

import pyqtgraph as pg

try:
    import requests
except ImportError:
    requests = None  # only used for optional beamline status URL fetch

try:
    import py12inifunc
except ImportError:
    # Minimal stub so the GUI can initialise without the beamline INI library.
    # All values match the defaults that ptyco_main_control.__init__ falls back
    # to when readini() raises an exception.
    class _IniParams:
        _qds_unit = 1
        _qds_x_sensor = 0
        _qds_y_sensor = 1
        countsperexposure = 0
        working_folder = ""
        _ref_X = 0.0
        _ref_Z = 0.0
        _ref_Z2 = 0.0
        _qds_time_interval = 0.1
        _waittime_between_scans = 1
        _qds_R_vert = 10.0
        _qds_th0_vert = -30.0
        _qds_R_cyl = 50.0
        softglue_channels = ["B", "C", "D"]
        logfilename = ""
        scan_number = 0
        scan_name = "debug_scan"
        scan_time = -1
        saxsmode = 0
        _ratio_exp_period = 0.2
        _fly_idletime = 0.0
        _pulses_per_step = 1
        base_linux_datafolder = "/tmp"

        def readini(self):
            pass

        def writeini(self):
            pass

    class _Py12IniFuncStub:
        def ini(self, *a, **kw):
            return _IniParams()

        def read(self, *a, **kw):
            return {}

        def write(self, *a, **kw):
            pass

    py12inifunc = _Py12IniFuncStub()

import analysis.planeeqn as eqn

# ==========================================================================
# Hardware imports — split by motor vs. device debug flags
# ==========================================================================
# Motor-side imports (real when DEBUG_MOTORS is False, i.e. levels 1)
if not DEBUG_MOTORS:
    from ptychosaxs.ptychosaxs import instruments

# Device-side imports (real when DEBUG_DEVICES is False, i.e. level 2)
if not DEBUG_DEVICES:
    import epics
    from tools.softglue import sgz_pty, SOFTGLUE_Setup_Error
    import tools.dg645 as dg645
    from tools.dg645 import DG645_Error
    from tools.struck import struck
    from tools.shutter import shutter
    from tools.detectors import (
        pilatus,
        dante,
        SGstream,
        XSP,
        DET_MIN_READOUT_Error,
        DET_OVER_READOUT_SPEED_Error,
    )

# Infrastructure — only in fully non-debug mode (level 0/1/2 all skip this)
if not DEBUG_MODE:
    from network.server import UDPserver, create_server
    try:
        from tools.files import scp
    except ImportError:
        scp = None

# ==========================================================================
# Hardware object instantiation — split by motor vs. device
# ==========================================================================
# Motors
if DEBUG_MOTORS:
    pts = InstrumentsStub()
else:
    pts = instruments()

# Devices
if DEBUG_DEVICES:
    SCAN_NUMBER_IOC = None
    s12softglue = SGZStub()
    dg645_12ID = DG645Stub()
else:
    SCAN_NUMBER_IOC = epics.PV("12idc:data:fileIndex")
    s12softglue = sgz_pty()
    try:
        dg645_12ID = dg645.dg645_12ID.open_from_uri(dg645.ADDRESS_12IDC)
    except:
        print("failed to connect DG645. Will not be able to collect detector images")

# ==========================================================================
# Constants
# ==========================================================================
status_url = "https://12ide.xray.aps.anl.gov/PVapp/ptycho_status"

HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
FRACTION_EXPOSURE_PERIOD = 0.2
DETECTOR_READOUTTIME = 0.02
DETECTOR_NOT_STARTED_ERROR = -1
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = QDS_UNIT_UM  # default QDS output is um
DEFAULTS = {
    "xmotor": 0,
    "ymotor": 2,
    "phimotor": 6,
}  # vertical stage is Z in the scan_gui, change 'ymotor' from 1 to 2, JD
inifilename = "pty-co-saxs.ini"
STRUCK_CHANNELS = [2, 3, 4, 5]


# ==========================================================================
# Utility functions
# ==========================================================================
def rstrip_from_char(string, char):
    """Removes characters from the right of the string starting from the first occurrence of 'char'."""
    #    print(f'{string=}')
    #    print(f'{char=}')
    if char in string:
        index = string.rfind(char)
        return string[:index]
    return string


async def showerror(msg):
    dlg = QMessageBox()
    dlg.setIcon(QMessageBox.Warning)
    dlg.setText(msg)
    dlg.setWindowTitle("Error")
    dlg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    result = dlg.exec_()
    return result


# ==========================================================================
# GUI helper classes
# ==========================================================================
class InputDialog(QDialog):
    def __init__(self, labels: List[str], parent=None):
        super().__init__(parent)

        buttonBox = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        layout = QFormLayout(self)

        self.inputs = []
        for lab in labels:
            self.inputs.append(QLineEdit(self))
            layout.addRow(lab, self.inputs[-1])

        layout.addWidget(buttonBox)

        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)

    def getInputs(self):
        return tuple(input.text() for input in self.inputs)


# ==========================================================================
# Thread worker classes
# ==========================================================================
class workerSignals(QObject):
    finished = pyqtSignal(bool)
    progress = pyqtSignal(int)
    statusmessage = pyqtSignal(str)


# Step 1: Create a worker class
class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super(Worker, self).__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signal = workerSignals()

    @pyqtSlot()
    def run(self):
        """Long-running task."""
        # print("Worker:", QThread.currentThread())
        self.fn(*self.args, **self.kwargs)
        self.signal.finished.emit(True)


# Step 1: Create a move class
class move(QRunnable):
    def __init__(self, pts, axis, pos):
        super(move, self).__init__()
        self.pts = pts
        self.axis = axis
        self.pos = pos
        self.signal = workerSignals()

    @pyqtSlot()
    def run(self):
        """Long-running task."""
        self.pts.mv(self.axis, self.pos)
        self.signal.finished.emit(True)


# Step 1: Create a move class
class mover(QRunnable):
    def __init__(self, pts, axis, pos):
        super(mover, self).__init__()
        self.pts = pts
        self.axis = axis
        self.pos = pos
        self.signal = workerSignals()

    @pyqtSlot()
    def run(self):
        """Long-running task."""
        self.pts.mvr(self.axis, self.pos)
        self.signal.finished.emit(True)


# # Step 1: Create a move class
# class runstruck(QRunnable):

#     def __init__(self, pulseN, tm):
#         super(mover, self).__init__()
#         self.pulseN = pulseN
#         self.tm = tm
#         self.signal = workerSignals()

#     @pyqtSlot()
#     def run(self):
#         struck.mcs_init()
#         struck.mcs_ready(self.pulseN, self.tm)
#         struck.arm_mcs()
#         self.signal.finished.emit(True)


# ==========================================================================
# Main application controller
# ==========================================================================
class ptyco_main_control(QObject):
    # Controller class — NOT a window itself.
    # The visible window is self.ui, loaded from ptycoSAXS.ui.
    # Inheriting QObject (not QMainWindow) prevents a blank second window
    # from appearing alongside self.ui when the event loop starts.

    def __init__(self):
        super(ptyco_main_control, self).__init__()
        guiName = "ptycoSAXS.ui"
        self.pts = pts
        print("Connecting to PTS...")
        if not DEBUG_MOTORS:
            if not self.pts.hexapod.is_servo_on("X"):
                print("Hexapod servo is off. Trying to turn it on...")
                self.handle_hexapod_error()
                print("Hexapod servo is now on.")
        # self.beamstatus = beamstatus()
        self.ui = uic.loadUi(guiName)

        # Expose module-level hardware objects on self so handlers can access
        # them via self.w.xxx without importing rungui (which would re-execute
        # the module and create a second instance of everything).
        self.SCAN_NUMBER_IOC = SCAN_NUMBER_IOC
        self.s12softglue = s12softglue
        self.dg645_12ID = dg645_12ID
        self.DEBUG_MODE = DEBUG_MODE
        self.DEBUG_MOTORS = DEBUG_MOTORS
        self.DEBUG_DEVICES = DEBUG_DEVICES
        self.status_url = status_url

        # Handlers must be created immediately after the UI is loaded so that
        # any __init__ call to a delegated method (e.g. update_scannumber) works.
        from handlers.motor_handler import MotorHandler
        from handlers.scan_handler import ScanHandler
        from handlers.status_handler import StatusHandler

        self.motor_handler = MotorHandler(self)
        self.scan_handler = ScanHandler(self)
        self.status_handler = StatusHandler(self)

        self.messages = {}
        self.messages["recent error message"] = ""
        self.isOK2run = True
        self.is_softglue_savingdone = True
        self.monitor_beamline_status = True
        # list all possible motors
        # this should came from the pts.
        motornames = ["X", "Y", "Z", "U", "V", "W", "phi"]
        motorunits = ["mm", "mm", "mm", "deg", "deg", "deg", "deg"]
        self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
        #        self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD

        self.is_selfsaved = False
        self.is_ptychomode = True
        self.parameters = py12inifunc.ini(inifilename)
        # When you need new field to inifile, edit the ini file first.
        try:
            self.parameters.readini()
        except:
            self.parameters._qds_unit = QDS_UNIT_DEFAULT
            self.parameters._qds_x_sensor = 0
            self.parameters._qds_y_sensor = 1
            self.parameters.countsperexposure = 0
            self.parameters.working_folder = ""
            self.parameters._ref_X = 0
            self.parameters._ref_Z = 0
            self.parameters._ref_Z2 = 0
            self.parameters._qds_time_interval = 0.1
            self.parameters._waittime_between_scans = 1
            self.parameters._qds_R_vert = 10.0  # 10mm
            self.parameters._qds_th0_vert = -30.0  # degree
            self.parameters._qds_R_cyl = 50.0  # mm
            self.parameters.softglue_channels = ["B", "C", "D"]
            self.parameters.logfilename = ""
            self.parameters.scan_number = 0
            self.parameters.scan_name = ""
            self.parameters._ratio_exp_period = FRACTION_EXPOSURE_PERIOD
            self.parameters._fly_idletime = 0.033
            self.parameters.scan_time = -1
            self.parameters._pulses_per_step = 1
            self.parameters.saxsmode = 1  # 0 for ptychography, 1 for SAXS
            self.parameters.base_linux_datafolder = "/net/s12data/export/12id-c/"

        self.isscan = False
        self.isfly = False
        self.hdf_plugin_savemode_step = 0  # step capture off by default
        self.hdf_plugin_savemode_fly = 1  # fly capture on by default (no SG yet)

        if pts is not None:
            if not hasattr(self.pts.gonio, "channel_names"):
                self.pts.gonio.channel_names = [""]
                self.pts.gonio.units = [""]
            for i, name in enumerate(self.pts.gonio.channel_names):
                if len(name) > 0:
                    motornames.append(name)
            for unit in self.pts.gonio.units:
                if len(unit) > 0:
                    motorunits.append(unit)

        # Disable all motor widgets first; re-enable below only for connected motors
        for n in range(1, len(motornames) + 1):
            self._set_motor_widgets_enabled(n, False)

        # checking only the connected motors..
        # if not done, later it will try to update the position of disconnected motors
        self.motornames = []
        self.motorunits = []
        #        print(motornames, " line 241")
        for i, name in enumerate(motornames):
            try:
                if self.pts.isconnected(name):
                    self.motornames.append(name)
                    self.motorunits.append(motorunits[i])
                else:
                    raise RuntimeError(f"Motor '{name}' failed to connect.")
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"Motor '{name}' failed to connect: {exc}") from exc
        #        print(motornames, " line 252")
        # motors for 2d and 3d scans.....
        xm = DEFAULTS["xmotor"]  # JD
        ym = DEFAULTS["ymotor"]  # JD

        phim = (
            self.motornames.index("phi")
            if "phi" in self.motornames
            else DEFAULTS["phimotor"]
        )

        # Populate motor labels and wire up buttons for each connected motor
        for i, name in enumerate(self.motornames):
            n = i + 1
            self.ui.findChild(QLabel, "lb%i" % n).setText(name)
            if n > 6:
                widget_label_pos = self.ui.findChild(QLabel, "lb_%i" % n)
                widget_label_pos.setContextMenuPolicy(Qt.CustomContextMenu)
                widget_label_pos.customContextMenuRequested.connect(
                    lambda pos, w=n: self._on_motor_context_menu(w, pos)
                )
            self.ui.findChild(QPushButton, "pb_tweak%iL" % n).clicked.connect(
                lambda: self.mvr(-1, -1)
            )
            self.ui.findChild(QPushButton, "pb_tweak%iR" % n).clicked.connect(
                lambda: self.mvr(-1, 1)
            )
            self.ui.findChild(QLineEdit, "ed_%i" % n).returnPressed.connect(
                lambda: self.mv(-1, None)
            )
            if n in (1, 2, 3, 7, 8, 9):
                self.ui.findChild(QPushButton, "pb_lup_%i" % n).clicked.connect(
                    lambda: self.stepscan(-1)
                )
                self.ui.findChild(QPushButton, "pb_SAXSscan_%i" % n).clicked.connect(
                    lambda: self.fly(-1)
                )
            enable = pts is not None and self.pts.isconnected(name)
            self._set_motor_widgets_enabled(n, enable)

        self.read_motor_scan_range()

        # Wire up menu actions
        self.ui.actionSet_Log_Filename.triggered.connect(self.set_logfilename)
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionEnable_fly_with_controller.setCheckable(True)
        self.ui.actionEnable_fly_with_controller.setChecked(True)
        self.ui.actionEnable_fly_with_controller.triggered.connect(
            self.select_flymode
        )  # hexapod flyscan type.
        self.ui.actionRecord_traj_during_scan.triggered.connect(
            self.select_hexrecord
        )  # hexapod record during scan.
        self.ui.actionSet_the_default_vel_acc.triggered.connect(
            self.sethexapodvel_default
        )  # hexapod set vel acc into default
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.ui.actionSave.triggered.connect(self.savescan)
        self.ui.actionSave_flyscan_result.triggered.connect(self.fly_result)
        self.ui.actionFit_QDS_phi.setEnabled(False)
        self.ui.actionFit_QDS_phi.triggered.connect(self.fit_wobble_eccentricity)
        self.ui.actionSet_Interferometer_Param.triggered.connect(
            self.set_interferometer_params
        )
        self.ui.actionLoad_eccentricity_data.triggered.connect(
            self.load_plot_eccentricity
        )
        self.ui.actionLoad_wobble_data.triggered.connect(self.load_plot_wobble)
        self.ui.actionSave_scan.triggered.connect(self.savescan)
        self.ui.actionLoad_scan.triggered.connect(self.loadscan)
        self.ui.actionSelect_units.triggered.connect(self.select_qds_units)
        self.ui.actionSelect_QDS_for_X.triggered.connect(self.select_qds_x)
        self.ui.actionSelect_QDS_for_Y.triggered.connect(self.select_qds_y)
        self.ui.actionCalibrate.triggered.connect(self.smaract_calibrate)
        self.ui.actionFindReference.triggered.connect(self.smaract_findreference)
        self.ui.actionSet_gonio_default_vel_acc.triggered.connect(
            self.smaract_set_defaultspeed
        )
        self.ui.actionScanStop.triggered.connect(self.stopscan)
        self.ui.pushButton_stopScan.clicked.connect(self.stopscan)
        self.ui.pushButton_stopScan.setEnabled(False)
        self.ui.pushButton_stopScan.setStyleSheet(
            "background-color: rgb(230, 230, 230); color: rgb(150, 150, 150);"
        )
        self.isStopScanIssued = False
        self.is_hexrecord_required = False
        self.shutter_close_after_scan = False
        self.ui.actionflyX_and_stepY.triggered.connect(lambda: self.fly2d(xm, ym))
        self.ui.actionsnake.triggered.connect(lambda: self.fly2d(xm, ym, snake=True))
        self.ui.actionstepscan.triggered.connect(lambda: self.stepscan2d(xm, ym))
        self.ui.actionnormal_2D.triggered.connect(lambda: self.fly3d(xm, ym, phim))
        self.ui.actionsnake_2D.triggered.connect(
            lambda: self.fly3d(xm, ym, phim, snake=True)
        )
        self.ui.actionstep_2D.triggered.connect(lambda: self.stepscan3d(xm, ym, phim))
        self.ui.pb_lup_step2d.clicked.connect(lambda: self.stepscan2d(xm, ym))
        self.ui.pb_SAXSscan_fly2d.clicked.connect(
            lambda: self.fly2d(xm, ym, snake=True)
        )
        self.ui.pushButton_plotScanPositions.clicked.connect(
            lambda: self.scan_handler.plot_scan_positions_2d(xm, ym)
        )
        self.ui.pushButton_saveCurrent.clicked.connect(
            self.scan_handler.save_current_positions
        )
        self.ui.pushButton_checkSaved.clicked.connect(
            self.scan_handler.check_saved_positions
        )
        self.ui.pushButton_goToSaved.clicked.connect(
            self.scan_handler.go_to_saved_positions
        )
        self.ui.pb_lup_step3d.clicked.connect(lambda: self.stepscan3d(xm, ym, phim))
        self.ui.pb_SAXSscan_fly3d.clicked.connect(
            lambda: self.fly3d(xm, ym, phim, snake=True)
        )
        self.ui.actionSelect_time_intervals.triggered.connect(self.select_timeintervals)
        self.ui.actionTrigout.triggered.connect(lambda: self.set_softglue_in(1))
        self.ui.actionDetout.triggered.connect(lambda: self.set_softglue_in(2))
        self.ui.actionevery_10_millie_seconds.triggered.connect(
            lambda: self.set_softglue_in(3)
        )
        self.ui.actionPrint_flyscan_settings.triggered.connect(
            lambda: self.print_fly_settings(0)
        )
        self.ui.actionSAXS.triggered.connect(lambda: self.select_detectors(1))
        self.ui.actionWAXS.triggered.connect(lambda: self.select_detectors(2))
        self.ui.actionStruck.triggered.connect(lambda: self.select_detectors(3))
        self.ui.actionSG.triggered.connect(lambda: self.select_detectors(4))
        self.ui.actionDante.triggered.connect(lambda: self.select_detectors(5))
        self.ui.actionXSP3.triggered.connect(lambda: self.select_detectors(6))
        self.ui.actionReset_to_Fly_mode.triggered.connect(self.reset_det_flymode)
        self.ui.actionChannels_to_record.triggered.connect(
            self.choose_softglue_channels
        )
        self.ui.actionSave_current_results.triggered.connect(self.save_softglue)
        if pts is not None:
            self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
            self.pts.signals.AxisNameSignal.connect(self.update_motorname)
        self.ui.actionTestFly.triggered.connect(self.scantest)
        self.ui.edit_workingfolder.setText(self.parameters.working_folder)
        self.ui.edit_workingfolder.returnPressed.connect(self.update_workingfolder)
        self.ui.pushButton_workingfolder_browse.clicked.connect(
            self._browse_workingfolder
        )
        self.ui.edit_scanname.returnPressed.connect(lambda: self.update_scanname(True))
        self.ui.edit_scannumber.returnPressed.connect(
            lambda: self.update_scanname(True)
        )
        self.ui.pushButton_scanNp1.clicked.connect(self._scan_number_plus_one)
        self.ui.pushButton_scanNm1.clicked.connect(self._scan_number_minus_one)
        self.ui.actionSet_waittime_between_scans.triggered.connect(
            self.set_waittime_between_scans
        )
        self.ui.actionMonitor_Beamline_Status.triggered.connect(
            self.set_monitor_beamline_status
        )
        self.ui.actionShutter_Close_Afterscan.triggered.connect(
            self.set_shutter_close_after_scan
        )
        self.ui.actionUse_hdf_plugin.triggered.connect(self.set_hdf_plugin_use)
        self.ui.actionPtychography_mode.triggered.connect(self.select_detector_mode)
        self.ui.actionCapture_multi_frames_step.triggered.connect(
            self.select_hdf_multiframecapture_step
        )
        self.ui.actionCapture_multi_frames_fly.triggered.connect(
            self.select_hdf_multiframecapture_fly
        )
        self.ui.actionSet_basepaths.triggered.connect(self.set_basepaths)
        self.ui.actionPut_DET_alignmode.triggered.connect(self.set_det_alignmode)
        self.ui.actionSet_shot_number_per_a_step.triggered.connect(
            self.set_shotnumber_per_step
        )
        self.parameters.scan_number += 1
        # self.ui.edit_scannumber.setText(str(int(self.parameters.scan_number)+1))
        self.update_scannumber()
        # self.ui.actionRatio_of_exptime_period_for_Flyscan.triggered.connect(self.set_exp_period_ratio)
        self.ui.actionRatio_of_exptime_period_for_Flyscan.triggered.connect(
            self.set_fly_idletime
        )

        if os.name != "nt":
            self.ui.menuQDS.setDisabled(True)
        self.threadpool = QThreadPool.globalInstance()
        self.Worker = Worker  # expose to handlers that don't import rungui

        # QDS buttons
        self.ui.btn_reset_qds_x.clicked.connect(self.reset_qdsX)
        self.ui.btn_reset_qds_z.clicked.connect(self.reset_qdsZ)
        self.ui.btn_reset_qds_z2.clicked.connect(self.reset_qdsZ2)

        self.ui.btn_record_x1.clicked.connect(lambda: self.record_qdsX(1))
        # self.ui.btn_record_x2.clicked.connect(lambda: self.record_qdsX(2))

        self.ui.btn_record_z1.clicked.connect(lambda: self.record_qdsZ(1))
        # self.ui.btn_record_z2.clicked.connect(lambda: self.record_qdsZ(2))

        self.ui.btn_record_z1_2.clicked.connect(lambda: self.record_qdsZ(4))
        # self.ui.btn_record_z2_2.clicked.connect(lambda: self.record_qdsZ(5))
        self.ui.pbar_scan.setValue(0)

        # ── Hexapod navigation panel ───────────────────────────────────────
        self.ui.pb_hp_left.clicked.connect(lambda: self._hp_tweak(0, -1))
        self.ui.pb_hp_right.clicked.connect(lambda: self._hp_tweak(0, 1))
        self.ui.pb_hp_down.clicked.connect(lambda: self._hp_tweak(2, -1))
        self.ui.pb_hp_up.clicked.connect(lambda: self._hp_tweak(2, 1))

        # ── Translation navigation panel ───────────────────────────────────
        self.ui.pb_trans_left.clicked.connect(lambda: self._trans_tweak(False, -1))
        self.ui.pb_trans_right.clicked.connect(lambda: self._trans_tweak(False, 1))
        self.ui.pb_trans_down.clicked.connect(lambda: self._trans_tweak(True, -1))
        self.ui.pb_trans_up.clicked.connect(lambda: self._trans_tweak(True, 1))

        def _on_trans_flip(checked):
            self.ui.label_trans.setText("trans2" if checked else "trans1")
            self.ui.label_trans_2.setText("trans1" if checked else "trans2")

        self.ui.checkBox_transFlip.toggled.connect(_on_trans_flip)
        _on_trans_flip(self.ui.checkBox_transFlip.isChecked())

        # ── Copy current positions ─────────────────────────────────────────
        self.ui.pushButton_copyCurrent.clicked.connect(self._copy_current_positions)

        # ── Setup window ───────────────────────────────────────────────────
        self.ui.pushButton_setup.clicked.connect(self._open_setup_window)
        self.ui.pushButton_openOpticsGui.clicked.connect(self._open_optics_gui)

        # Defaults
        self.isStruckCountNeeded = False
        self.set_hdf_plugin_use(True)
        self.select_hdf_multiframecapture_fly(True)

        # set default softglue collection freq. 1000 micro seconds.
        if s12softglue.isConnected:
            s12softglue.set_count_freq(1000)
        else:
            print("Softglue does not work.")

        self.rpos = []
        self.mpos = []
        ## shutter control
        # self.shutter_status = epics.PV('PA:12ID:STA_A_BEAMREADY_PL.VAL', callback=self.checkshutter)
        # self.shutter = epics.PV('12ida2:rShtrA:Open')
        if DEBUG_DEVICES:
            self.shutter = ShutterStub()
        else:
            self.shutter = shutter()

        # Three side-by-side plots embedded in the UI layout
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.ax = self.plot_widget.addPlot(row=0, col=0)
        self.ax2 = self.plot_widget.addPlot(row=1, col=0)
        self.ax3 = self.plot_widget.addPlot(row=2, col=0)
        self.ui.verticalLayout_2.addWidget(self.plot_widget)

        self.updatepos()

        # Detector state
        self.det_readout_time = DETECTOR_READOUTTIME  # detector minimum readout time.
        self.detector = [None] * 5
        self.detector_mode = ["", "", "", "", "XRF"]
        self.hdf_plugin_name = ["", "", "", "", ""]

        self.ui.edit_scanname.setText(self.parameters.scan_name)

        # Periodic timers (Windows only — EPICS callbacks handle updates on Linux)
        if os.name == "nt":
            self.timer = QTimer()
            self.timer.timeout.connect(self.update_qds)
            self.timer.start(100)

        if os.name == "nt":
            self.timer_update = QTimer()
            self.timer_update.timeout.connect(self.update_status)
            self.timer_update.start(10_000)
        if DEBUG_MODE:
            parts = []
            if DEBUG_MOTORS:
                parts.append("motors=STUB")
            if DEBUG_DEVICES:
                parts.append("devices=STUB")
            self.ui.setWindowTitle(
                self.ui.windowTitle()
                + f" [DEBUG L{DEBUG_LEVEL} — {', '.join(parts) or 'all real'}]"
            )

        self.set_scan_status("No Scan")

        self.ui.pushButton_exit.clicked.connect(self.exit_gui)

        self.ui.show()

    # ── Motor widget enable/disable ────────────────────────────────────────

    def _set_motor_widgets_enabled(self, n: int, enable: bool) -> None:
        """Enable or disable all UI widgets for motor slot n (1-indexed).

        Scan-range widgets (lup buttons, L/R/N fields) are only present for
        motors 1, 2, 3, 7, 8, 9 in the new UI layout. The scan-time field
        (ed_lup_1_t) is only present for motor 1.
        """
        self.ui.findChild(QLabel, "lb%i" % n).setEnabled(enable)
        self.ui.findChild(QPushButton, "pb_tweak%iL" % n).setEnabled(enable)
        self.ui.findChild(QPushButton, "pb_tweak%iR" % n).setEnabled(enable)
        self.ui.findChild(QLineEdit, "ed_%i" % n).setEnabled(enable)
        self.ui.findChild(QLineEdit, "ed_%i_tweak" % n).setEnabled(enable)
        if n in (1, 2, 3, 7, 8, 9):
            self.ui.findChild(QPushButton, "pb_lup_%i" % n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_SAXSscan_%i" % n).setEnabled(enable)
            self.ui.findChild(QLineEdit, "ed_lup_%i_L" % n).setEnabled(enable)
            self.ui.findChild(QLineEdit, "ed_lup_%i_R" % n).setEnabled(enable)
            self.ui.findChild(QLineEdit, "ed_lup_%i_N" % n).setEnabled(enable)
        if n == 1:
            self.ui.findChild(QLineEdit, "ed_lup_1_t").setEnabled(enable)

    # ── Motor context menu ─────────────────────────────────────────────────

    def _on_motor_context_menu(self, n, pos: QPoint):
        # print(f"Context menu on: {n}")
        menu = QMenu(self.ui)
        set_zero_action = menu.addAction("Set to 0")
        set_zero_action.triggered.connect(
            lambda chosen, wn=n: self._on_set_to_zero(chosen, wn)
        )

        # Map the position from the label to global screen coordinates
        global_pos = self.ui.findChild(QLabel, "lb_%i" % n).mapToGlobal(pos)
        menu.exec_(global_pos)

    def _on_set_to_zero(self, checked=False, n=0):
        self.pts.set_pos(self.motornames[n - 1], 0)
        # print(f"onset zero is called for {self.motornames[n-1]}.")
        # Optionally update the label text:
        # self.ui.label_1.setText("0")

    # ── Motor position & movement ──────────────────────────────────────────

    def get_motorpos(self, axis):
        # get motor position from the label
        # i.e. axis = 'X'
        i = self.motornames.index(axis)
        return float(self.ui.findChild(QLabel, "lb_%i" % (i + 1)).text())

    def get_pos_all(self):
        motors = {}
        for name in self.motornames:
            motors[name] = self.pts.get_pos(name)
        return motors

    def updatepos(self, axis="", val=None):
        if len(axis) == 0:
            for i, name in enumerate(self.motornames):
                if val is None:
                    val = self.pts.get_pos(name)
                # self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
                self.ui.findChild(QLabel, "lb_%i" % (i + 1)).setText("%0.6f" % val)
                val = None
        else:
            if val is None:
                val = self.pts.get_pos(axis)
            i = self.motornames.index(axis)
            # self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
            self.ui.findChild(QLabel, "lb_%i" % (i + 1)).setText("%0.6f" % val)

    def update_motorpos(self, value):
        self.updatepos(self.signalmotor, value)

    def update_motorname(self, axis):
        self.signalmotor = axis

    def mv(self, motornumber=-1, val=None):
        if motornumber < 0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r"\d+", objname)[0])
            # n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n - 1

        # print("motor number is ", motornumber)
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        if type(val) == type(None):
            try:
                val = float(val_text)
            except:
                showerror("Text box is empty.")
                return
        # print(f"Move {axis} to {val}")
        w = move(self.pts, axis, val)
        # w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
        self.updatepos(axis)

    def mvr(self, motornumber=-1, sign=1, val=0):
        if motornumber == -1:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r"\d+", objname)[0])
            # n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n - 1
        # print("motornumber is ", motornumber)
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        if val == 0:
            val = float(self.ui.findChild(QLineEdit, "ed_%i_tweak" % n).text())

        w = mover(self.pts, axis, sign * val)
        self.threadpool.start(w)
        self.updatepos(axis)

    # ── Gonio & hexapod speed control ─────────────────────────────────────

    def handle_hexapod_error(self):
        self.pts.hexapod.handle_error()

    def smaract_set_defaultspeed(self):
        for i, connected in enumerate(self.pts.gonio.connected):
            if connected:
                self.pts.gonio.set_speed(i)

    def smaract_calibrate(self):
        for i, connected in enumerate(self.pts.gonio.connected):
            if connected:
                self.pts.gonio.calibrate(i)
        print("MCS2 calibration done..")

    def smaract_findreference(self):
        for i, connected in enumerate(self.pts.gonio.connected):
            if connected:
                self.pts.gonio.findReference(i)
        print("MCS2 finding references done..")

    def setphivel_default(self):
        #        print(self.pts.phi.vel, " This was vel value")
        # self.pts.phi.vel = 36
        # time.sleep(0.1)
        # self.pts.phi.acc = self.pts.phi.vel*10
        self.pts.set_speed("phi", 36, 360)

    def sethexapodvel_default(self):
        #        print(self.pts.phi.vel, " This was vel value")
        self.pts.set_speed(self.pts.hexapod.axes[0], 5, None)

    # ── Motor scan-range persistence ───────────────────────────────────────

    def write_motor_scan_range(self):
        numbers = np.random.rand(len(self.motornames), 6)
        for i, name in enumerate(self.motornames):
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
                except (
                    ValueError,
                    AttributeError,
                ):  # AttributeError when widget absent for this motor slot
                    value = -999999
                arr.append(value)

            numbers[i] = arr

            # Save the array to a file
        np.save("_numbers.npy", numbers)

    def read_motor_scan_range(self):
        # Load the array from the file
        numbers = np.load("_numbers.npy")

        for i, name in enumerate(self.motornames):
            n = i + 1
            if numbers.shape[1] == 5:
                line_edit_suffixes = ["tweak", "L", "R", "N", "t"]
            if numbers.shape[1] == 6:
                line_edit_suffixes = ["pos", "tweak", "L", "R", "N", "t"]
            try:
                for j, suffix in enumerate(line_edit_suffixes):
                    value = "" if numbers[i, j] == -999999 else str(numbers[i, j])
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

        self.scan_handler.update_scan_estimate()

    # ── Exit / shutdown ────────────────────────────────────────────────────

    def exit_gui(self):
        """Safely shut down all hardware, timers, and workers, then quit."""
        # 1. Stop any running scan so workers don't fire callbacks after teardown.
        try:
            self.stopscan()
        except Exception:
            pass

        # 2. Stop periodic timers so no more callbacks fire during teardown.
        for attr in ("timer", "timer_update"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass

        # 3. Wait for thread-pool workers to finish (5 s timeout).
        try:
            self.threadpool.waitForDone(5000)
        except Exception:
            pass

        # 4. Persist scan-range fields and INI settings.
        try:
            self.write_motor_scan_range()
        except Exception:
            pass
        try:
            self.parameters.writeini()
        except Exception:
            pass

        # 5. Quit the Qt event loop (also terminates the asyncio loop in main()).
        QApplication.quit()

    def closeEvent(self, event):
        """Handle the window's X button the same way as pushButton_exit."""
        self.exit_gui()
        event.accept()

    # ── Scan handler delegations ───────────────────────────────────────────
    # All scan logic lives in handlers/scan_handler.py; these are thin pass-throughs.

    def set_hdf_plugin_use(self, value=None):
        return self.scan_handler.set_hdf_plugin_use(value)

    def select_detector_mode(self, value=None):
        return self.scan_handler.select_detector_mode(value)

    def select_hdf_multiframecapture_step(self, value=None):
        return self.scan_handler.select_hdf_multiframecapture_step(value)

    def select_hdf_multiframecapture_fly(self, value=None):
        return self.scan_handler.select_hdf_multiframecapture_fly(value)

    def set_waittime_between_scans(self):
        return self.scan_handler.set_waittime_between_scans()

    def set_shotnumber_per_step(self):
        return self.scan_handler.set_shotnumber_per_step()

    def get_detectors_ready(self):
        return self.scan_handler.get_detectors_ready()

    def update_scanname(self, update_detector=None):
        return self.scan_handler.update_scanname(update_detector)

    def choose_softglue_channels(self):
        return self.scan_handler.choose_softglue_channels()

    def reset_det_flymode(self):
        return self.scan_handler.reset_det_flymode()

    def set_softglue_in(self, val):
        return self.scan_handler.set_softglue_in(val)

    def stopscan(self):
        return self.scan_handler.stopscan()

    def select_detectors(self, N, value=None):
        return self.scan_handler.select_detectors(N, value)

    def switch_SGstream(self, status=True):
        return self.scan_handler.switch_SGstream(status)

    def switch_MCS(self, status=True):
        return self.scan_handler.switch_MCS(status)

    def select_flymode(self):
        return self.scan_handler.select_flymode()

    def select_hexrecord(self):
        return self.scan_handler.select_hexrecord()

    def get_softglue_filename(self):
        return self.scan_handler.get_softglue_filename()

    def softglue_savingdone(self):
        return self.scan_handler.softglue_savingdone()

    def save_softglue(self):
        return self.scan_handler.save_softglue()

    def make_positions_folder(self, foldername):
        return self.scan_handler.make_positions_folder(foldername)

    def prepare_scan_files(self):
        return self.scan_handler._prepare_scan_files()

    def push_filepath_to_detectors(self):
        return self.scan_handler.push_filepath_to_detectors()

    def save2disk_softglue(self):
        return self.scan_handler.save2disk_softglue()

    def save2disk_softglue_original(self):
        return self.scan_handler.save2disk_softglue_original()

    def save_hexapod_record(self, filename, option="a"):
        return self.scan_handler.save_hexapod_record(filename, option)

    def flydone(self, return_motor=True, reset_scannumber=True, donedone=True):
        return self.scan_handler.flydone(return_motor, reset_scannumber, donedone)

    def flydone2d(self, value=0):
        return self.scan_handler.flydone2d(value)

    def flydone3d(self, value=0):
        return self.scan_handler.flydone3d(value)

    # Fly scans
    def fly(self, motornumber=-1):
        return self.scan_handler.fly(motornumber)

    def fly0(self, motornumber=-1, update_progress=None, update_status=None):
        return self.scan_handler.fly0(motornumber, update_progress, update_status)

    def fly2d(self, xmotor=0, ymotor=1, scanname="", snake=False):
        return self.scan_handler.fly2d(xmotor, ymotor, scanname, snake)

    def fly2d0(
        self, xmotor=0, ymotor=1, scanname="", update_progress=None, update_status=None
    ):
        return self.scan_handler.fly2d0(
            xmotor, ymotor, scanname, update_progress, update_status
        )

    def fly2d0_SNAKE(
        self, xmotor=0, ymotor=1, scanname="", update_progress=None, update_status=None
    ):
        return self.scan_handler.fly2d0_SNAKE(
            xmotor, ymotor, scanname, update_progress, update_status
        )

    def fly3d(self, xmotor=0, ymotor=1, phimotor=6, scanname="", snake=False):
        return self.scan_handler.fly3d(xmotor, ymotor, phimotor, scanname, snake)

    def fly3d0(
        self,
        xmotor=0,
        ymotor=1,
        phimotor=6,
        scanname="",
        snake=False,
        update_progress=None,
        update_status=None,
    ):
        return self.scan_handler.fly3d0(
            xmotor, ymotor, phimotor, scanname, snake, update_progress, update_status
        )

    def fly_traj(self, xmotor=0, ymotor=-1):
        return self.scan_handler.fly_traj(xmotor, ymotor)

    def is_traj_running(self):
        return self.scan_handler.is_traj_running()

    # Step scans
    def stepscan(self, motornumber=-1):
        return self.scan_handler.stepscan(motornumber)

    def stepscan0(self, motornumber=-1, update_progress=None, update_status=None):
        return self.scan_handler.stepscan0(motornumber, update_progress, update_status)

    def stepscan2d(self, xmotor=0, ymotor=1):
        return self.scan_handler.stepscan2d(xmotor, ymotor)

    def stepscan2d0(self, xmotor=0, ymotor=1, update_progress=None, update_status=None):
        return self.scan_handler.stepscan2d0(
            xmotor, ymotor, update_progress, update_status
        )

    def stepscan3d(self, xmotor=0, ymotor=1, phimotor=6):
        return self.scan_handler.stepscan3d(xmotor, ymotor, phimotor)

    def stepscan3d0(
        self, xmotor=0, ymotor=-1, phimotor=-1, update_progress=None, update_status=None
    ):
        return self.scan_handler.stepscan3d0(
            xmotor, ymotor, phimotor, update_progress, update_status
        )

    # Scan lifecycle helpers
    def scandone(self, update_scannumber=True, donedone=True):
        return self.scan_handler.scandone(update_scannumber, donedone)

    def check_start_position(self, n):
        return self.scan_handler.check_start_position(n)

    def detectortime_error_question(self, expt, period):
        return self.scan_handler.detectortime_error_question(expt, period)

    def get_detectors_armed(self):
        return self.scan_handler.get_detectors_armed()

    def is_arming_detecotors_timedout(self):
        return self.scan_handler.is_arming_detecotors_timedout()

    def is_waiting_detectors_timedout(self, expt, i):
        return self.scan_handler.is_waiting_detectors_timedout(expt, i)

    def wait_for_beam(self, update_status, value):
        return self.scan_handler.wait_for_beam(update_status, value)

    def refresh_detectors(self):
        return self.scan_handler.refresh_detectors()

    def run_stop_issued(self):
        return self.scan_handler.run_stop_issued()

    def updateprogressbar(self, value):
        return self.scan_handler.updateprogressbar(value)

    def update_status_bar(self, message):
        return self.scan_handler.update_status_bar(message)

    def update_scannumber(self):
        return self.scan_handler.update_scannumber()

    def write_scaninfo_to_logfile(self, strlist):
        return self.scan_handler.write_scaninfo_to_logfile(strlist)

    def log_data(self, data_list):
        return self.scan_handler.log_data(data_list)

    def print_fly_settings(self, motornumber):
        return self.scan_handler.print_fly_settings(motornumber)

    def set_exp_period_ratio(self):
        return self.scan_handler.set_exp_period_ratio()

    def set_fly_idletime(self):
        return self.scan_handler.set_fly_idletime()

    def set_det_alignmode(self, value=None):
        return self.scan_handler.set_det_alignmode(value)

    def set_basepaths(self, text=""):
        return self.scan_handler.set_basepaths(text)

    def save_qds(self, filename="", saveoption="w"):
        return self.scan_handler.save_qds(filename, saveoption)

    def save_list(self, filename, mpos, rpos, col, option="w"):
        return self.scan_handler.save_list(filename, mpos, rpos, col, option)

    def save_nparray(self, filename, mpos, rpos, col, option="w"):
        return self.scan_handler.save_nparray(filename, mpos, rpos, col, option)

    def savescan(self, filename=""):
        return self.scan_handler.savescan(filename)

    def fly_result(self):
        return self.scan_handler.fly_result()

    # ── Status handler delegations ─────────────────────────────────────────
    # All monitoring / plotting / QDS logic lives in handlers/status_handler.py.

    def set_monitor_beamline_status(self, value=None):
        return self.status_handler.set_monitor_beamline_status(value)

    def set_shutter_close_after_scan(self, value=None):
        return self.status_handler.set_shutter_close_after_scan(value)

    def checkshutter(self, value, **kws):
        return self.status_handler.checkshutter(value, **kws)

    def set_interferometer_params(self):
        return self.status_handler.set_interferometer_params()

    def set_logfilename(self):
        return self.status_handler.set_logfilename()

    def update_workingfolder(self, folder=""):
        return self.status_handler.update_workingfolder(folder)

    def select_qds_units(self):
        return self.status_handler.select_qds_units()

    def select_timeintervals(self):
        return self.status_handler.select_timeintervals()

    def select_qds_x(self):
        return self.status_handler.select_qds_x()

    def select_qds_y(self):
        return self.status_handler.select_qds_y()

    def scantest(self):
        return self.status_handler.scantest()

    def fit_wobble_eccentricity(self):
        return self.status_handler.fit_wobble_eccentricity()

    def loadscan(self):
        return self.status_handler.loadscan()

    def fitdata(self, filename="", datacolumn=2, xd=[], yd=[], dtype="wobble"):
        return self.status_handler.fitdata(filename, datacolumn, xd, yd, dtype)

    def load_plot_eccentricity(self):
        return self.status_handler.load_plot_eccentricity()

    def load_plot_wobble(self):
        return self.status_handler.load_plot_wobble()

    def plotfits(self, xd, yd, curve, lbl, ax=2):
        return self.status_handler.plotfits(xd, yd, curve, lbl, ax)

    def update_qds(self):
        return self.status_handler.update_qds()

    def reset_qdsX(self):
        return self.status_handler.reset_qdsX()

    def reset_qdsZ(self):
        return self.status_handler.reset_qdsZ()

    def reset_qdsZ2(self):
        return self.status_handler.reset_qdsZ2()

    def record_qdsX(self, value):
        return self.status_handler.record_qdsX(value)

    def record_qdsZ(self, value):
        return self.status_handler.record_qdsZ(value)

    def plot(self):
        return self.status_handler.plot()

    def clearplot(self):
        return self.status_handler.clearplot()

    def update_graph(self):
        return self.status_handler.update_graph()

    def timescan0(self):
        return self.status_handler.timescan0()

    def timescan(self):
        return self.status_handler.timescan()

    def timescanstop(self):
        return self.status_handler.timescanstop()

    def update_status_scan_time(self, time=-1):
        return self.status_handler.update_status_scan_time(time)

    def update_status(self):
        return self.status_handler.update_status()

    def get_qds_pos(self, isrefavailable=True):
        return self.status_handler.get_qds_pos(isrefavailable)

    def getfilename(self):
        return self.status_handler.getfilename()

    # ── Navigation panel helpers ───────────────────────────────────────────

    def _hp_tweak(self, motornumber, sign):
        """Move hexapod motor by the step entered in ed_hp_tweak (microns → mm)."""
        try:
            step_mm = float(self.ui.ed_hp_tweak.text()) / 1000.0
        except ValueError:
            return
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        w = mover(self.pts, axis, sign * step_mm)
        self.threadpool.start(w)
        self.updatepos(axis)

    def _trans_tweak(self, is_ud, sign):
        """Move a translation motor by the step entered in ed_trans_tweak (microns → mm).

        is_ud=False → left/right axis (motor 8, index 7).
        is_ud=True  → up/down axis   (motor 9, index 8).
        When checkBox_transFlip is checked the two axes are swapped.
        """
        try:
            step_mm = float(self.ui.ed_trans_tweak.text()) / 1000.0
        except ValueError:
            return
        if self.ui.checkBox_transFlip.isChecked():
            is_ud = not is_ud
        motornumber = 8 if is_ud else 7
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        w = mover(self.pts, axis, sign * step_mm)
        self.threadpool.start(w)
        self.updatepos(axis)

    # ── Utility button handlers ────────────────────────────────────────────

    def _scan_number_plus_one(self):
        self.parameters.scan_number = int(self.parameters.scan_number) + 1
        self.update_scannumber()
        self.update_scanname(True)

    def _scan_number_minus_one(self):
        self.parameters.scan_number = max(0, int(self.parameters.scan_number) - 1)
        self.update_scannumber()
        self.update_scanname(True)

    def _browse_workingfolder(self):
        """Open a folder browser, populate edit_workingfolder, and run update logic."""
        current = self.ui.edit_workingfolder.text()
        folder = QFileDialog.getExistingDirectory(
            self.ui, "Select Working Folder", current
        )
        if folder:
            self.ui.edit_workingfolder.setText(folder)
            self.update_workingfolder()

    def _copy_current_positions(self):
        """Copy lb_1..lb_11 label values into ed_1..ed_11 without triggering returnPressed."""
        for i in range(1, 12):
            label = self.ui.findChild(QLabel, "lb_%i" % i)
            line_edit = self.ui.findChild(QLineEdit, "ed_%i" % i)
            if label is not None and line_edit is not None:
                try:
                    val = float(label.text())
                    line_edit.setText("%0.6f" % val)
                except ValueError:
                    pass

    def _open_optics_gui(self):
        if DEBUG_DEVICES:
            QMessageBox.information(
                self.ui, "Optics GUI", "Optics GUI is not available in debug mode."
            )
            return
        import subprocess

        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "optics_motors.py"
        )
        try:
            subprocess.Popen([sys.executable, script])
        except Exception as e:
            QMessageBox.warning(
                self.ui, "Optics GUI", "Could not launch optics GUI:\n%s" % e
            )

    def _open_setup_window(self):
        """Open the setup configuration dialog."""
        import time as _time
        from PyQt5.QtWidgets import QButtonGroup, QFileDialog, QDialog

        dlg = uic.loadUi("setup_configuration.ui")

        # ── Populate with current state ────────────────────────────────────
        dlg.lineEdit_logFname.setText(self.parameters.logfilename)
        dlg.lineEdit_dataBasepaths.setText(
            getattr(self.parameters, "base_linux_datafolder", "")
        )
        dlg.lineEdit_nBursts.setValidator(QIntValidator(1, 9999, dlg))
        dlg.lineEdit_nBursts.setText(str(int(self.parameters._pulses_per_step)))
        dlg.lineEdit_scanWait.setText(str(self.parameters._waittime_between_scans))

        dlg.checkBox_SAXS.setChecked(self.ui.actionSAXS.isChecked())
        dlg.checkBox_WAXS.setChecked(self.ui.actionWAXS.isChecked())
        dlg.checkBox_closeShutterAfterScan.setChecked(
            self.ui.actionShutter_Close_Afterscan.isChecked()
        )
        dlg.checkBox_hdf5Plugin.setChecked(self.ui.actionUse_hdf_plugin.isChecked())
        dlg.checkBox_multiFramesStep.setChecked(
            self.ui.actionCapture_multi_frames_step.isChecked()
        )
        dlg.checkBox_multiFramesFly.setChecked(
            self.ui.actionCapture_multi_frames_fly.isChecked()
        )
        dlg.checkBox_interferometer.setChecked(self.ui.actionSG.isChecked())
        dlg.checkBox_monitorBeamline.setChecked(
            self.ui.actionMonitor_Beamline_Status.isChecked()
        )
        dlg.checkBox_ptychoMode.setChecked(self.ui.actionPtychography_mode.isChecked())
        dlg.checkBox_scalars.setChecked(self.ui.actionStruck.isChecked())
        dlg.checkBox_xrfXSP3.setChecked(self.ui.actionXSP3.isChecked())

        # Radio buttons — grouped by id matching set_softglue_in(val)
        btn_group = QButtonGroup(dlg)
        btn_group.addButton(dlg.radioButton_0p1ms, 1)
        btn_group.addButton(dlg.radioButton_1ms, 2)
        btn_group.addButton(dlg.radioButton_10ms, 3)
        if self.ui.actionTrigout.isChecked():
            dlg.radioButton_0p1ms.setChecked(True)
        elif self.ui.actionDetout.isChecked():
            dlg.radioButton_1ms.setChecked(True)
        else:
            dlg.radioButton_10ms.setChecked(True)

        # Browse button — pick a log file path without closing the dialog
        def _browse_logfile():
            path, _ = QFileDialog.getSaveFileName(
                dlg,
                "Select Log File",
                dlg.lineEdit_logFname.text(),
                "Text files (*.txt);;All files (*)",
            )
            if path:
                dlg.lineEdit_logFname.setText(path)

        dlg.pushButton_logFname.clicked.connect(_browse_logfile)

        # ── Apply settings only on OK ──────────────────────────────────────
        if dlg.exec_() != QDialog.Accepted:
            return

        # Log filename
        logfname = dlg.lineEdit_logFname.text()
        if logfname != self.parameters.logfilename:
            self.parameters.logfilename = logfname
            self.parameters.scan_number = 0
            self.write_scaninfo_to_logfile(["#I logging started on", _time.ctime()])

        # Data base paths
        self.parameters.base_linux_datafolder = dlg.lineEdit_dataBasepaths.text()

        # Numeric fields
        try:
            self.parameters._pulses_per_step = int(dlg.lineEdit_nBursts.text())
        except ValueError:
            pass
        try:
            self.parameters._waittime_between_scans = float(
                dlg.lineEdit_scanWait.text()
            )
        except ValueError:
            pass

        # Detectors
        self.select_detectors(1, dlg.checkBox_SAXS.isChecked())
        self.select_detectors(2, dlg.checkBox_WAXS.isChecked())
        self.select_detectors(3, dlg.checkBox_scalars.isChecked())
        self.select_detectors(4, dlg.checkBox_interferometer.isChecked())
        self.select_detectors(6, dlg.checkBox_xrfXSP3.isChecked())

        # Modes and toggles
        self.set_hdf_plugin_use(dlg.checkBox_hdf5Plugin.isChecked())
        self.select_hdf_multiframecapture_step(dlg.checkBox_multiFramesStep.isChecked())
        self.select_hdf_multiframecapture_fly(dlg.checkBox_multiFramesFly.isChecked())
        self.select_detector_mode(dlg.checkBox_ptychoMode.isChecked())
        self.set_monitor_beamline_status(dlg.checkBox_monitorBeamline.isChecked())
        self.set_shutter_close_after_scan(
            dlg.checkBox_closeShutterAfterScan.isChecked()
        )

        # Softglue collection speed
        speed_id = btn_group.checkedId()
        if speed_id in (1, 2, 3):
            self.set_softglue_in(speed_id)

        self.parameters.writeini()

    # ── Scan status label ──────────────────────────────────────────────────

    def set_scan_status(self, status: str) -> None:
        """Update label_scan_status text and background.

        status: "Scanning"  → green  #C6EFCE
                "Scan Error"→ pink   rgb(255, 199, 206)
                "No Scan"   → pink   rgb(255, 199, 206)  (default/idle)
        """
        scanning = status == "Scanning"
        self.ui.pushButton_stopScan.setEnabled(scanning)
        if scanning:
            self.ui.pushButton_stopScan.setStyleSheet(
                "background-color: rgb(255, 0, 0); color: rgb(255, 255, 255);"
            )
        else:
            self.ui.pushButton_stopScan.setStyleSheet(
                "background-color: rgb(230, 230, 230); color: rgb(150, 150, 150);"
            )
        lbl = self.ui.findChild(QLabel, "label_scan_status")
        if lbl is None:
            return
        if status == "Scanning":
            lbl.setText("Scanning")
            lbl.setStyleSheet("background-color: #C6EFCE; color: #000000")
        elif status == "Scan Error":
            lbl.setText("Scan Error")
            lbl.setStyleSheet("background-color: rgb(255, 0, 0); color: #FFFFFF")
        else:
            lbl.setText("Not Scanning")
            lbl.setStyleSheet("background-color: #fffde7; color: #000000")

    # ── Qt signal slots ────────────────────────────────────────────────────
    # Received from the UDP server (server_json.py) when running with --server.

    @QtCore.pyqtSlot(str, float, float, float, float)
    def set_data(self, axis, L, R, step, rt):
        #        print(axis, L, R, rt, step)
        motornumber = self.motornames.index(axis)
        n = motornumber + 1
        self.ui.findChild(QLineEdit, "ed_lup_%i_L" % n).setText(str(L))
        self.ui.findChild(QLineEdit, "ed_lup_%i_R" % n).setText(str(R))
        self.ui.findChild(QLineEdit, "ed_lup_%i_t" % n).setText(str(rt))
        self.ui.findChild(QLineEdit, "ed_lup_%i_N" % n).setText(str(step))

    @QtCore.pyqtSlot(dict)
    def run_json(self, json_message):
        return self.scan_handler.run_json(json_message)

    @QtCore.pyqtSlot(int)
    def run_cmd(self, n):
        return self.scan_handler.run_cmd(n)

    @QtCore.pyqtSlot(str, float)
    def set_mv(self, axis, pos):
        return self.scan_handler.set_mv(axis, pos)


# ==========================================================================
# Application entry point
# ==========================================================================
app = QApplication(sys.argv)
main_panel = ptyco_main_control()

# import pygetwindow as gw

# def capture_screenshot():
#     """Capture screenshot of main_panel every 10 seconds"""
#     screenshot = app.primaryScreen().grabWindow(0)
#     #timestamp = time.strftime("%Y%m%d_%H%M%S")
#     filename = f"pty-co-SAXS.png"
#     screenshot.save(filename)
#     scp(filename)
#     #pass
#     #print(f"Screenshot saved: {filename}")


def main():
    # Run gui with server option
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # # Create a timer for periodic screenshots
    # screenshot_timer = QTimer()
    # screenshot_timer.timeout.connect(capture_screenshot)
    # screenshot_timer.start(30000)  # 30 seconds in milliseconds

    with loop:
        if not DEBUG_MODE:
            _, protocol = loop.run_until_complete(create_server(loop))
            protocol.rangeChanged.connect(main_panel.set_data)
            protocol.runRequested.connect(main_panel.run_cmd)
            protocol.mvRequested.connect(main_panel.set_mv)
            protocol.jsonReceived.connect(main_panel.run_json)
        loop.run_forever()


def main_no_server():
    # Non-server option
    app = QApplication(sys.argv)
    a = ptyco_main_control()
    sys.exit(app.exec_())


if __name__ == "__main__":
    # Server option included
    main()
