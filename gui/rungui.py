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

# ==========================================================================
# PER-CONTROLLER DEBUG FLAGS — EDIT THESE TO RUN WITHOUT HARDWARE
# ==========================================================================
# True  -> that controller's library is never imported and never connected
#          to. A simulated stand-in takes its place, so its motors still read
#          back and still tweak, but nothing reaches the hardware.
# False -> the real hardware is imported and connected to as usual.
#
# Which axes / devices belong to which flag:
#
#   DEBUG_HEXAPOD  PI hexapod, pihexapod.gcs over TCP (10.54.122.145)
#                    axes: X, Y, Z, U, V, W
#   DEBUG_TOMO_PHI      The phi stage, whichever driver PHI_USE_ACS selects -
#                    the 12idePtycho motor records, or acspy over ethernet
#                    (10.54.122.157)
#                    axes: phi
#   DEBUG_GONIO    Goniometer over EPICS, IOC 12ideMCS1 (ptychosaxs.epics_gonio)
#                    axes: trans1, trans2, tilt1, tilt2
#   DEBUG_QDS      QDS interferometer over USB, softglueZynq fallback
#                    no axes — position sensor behind the QDS readouts
#   DEBUG_EPICS    Every other EPICS device: scan-number PV, softglue, DG645,
#                    struck scaler, shutter, detectors
#                    no axes — devices only
#   DEBUG_DG645    Just the DG645 delay generator, separately from the rest
#                    of DEBUG_EPICS. tools/dg645.py is the only module here
#                    that imports InstrumentKit (`instruments`), so this is
#                    the switch that lets the GUI run with real detectors
#                    and softglue in an environment without that package.
#                    no axes — one device
#
# DEBUG_HEXAPOD, DEBUG_GONIO, DEBUG_EPICS or DEBUG_DG645 being True disables
# every scan button: a scan would record fabricated positions, or frames
# taken on fabricated triggers, into a real data file. DEBUG_TOMO_PHI does not -
# phi is optional hardware, so a simulated phi just reports as not
# connected and disables its own row.
DEBUG_HEXAPOD = False
DEBUG_TOMO_PHI = False
DEBUG_GONIO = False
DEBUG_QDS = True
DEBUG_EPICS = False
DEBUG_DG645 = False

# ==========================================================================
# Which driver each stage uses
# ==========================================================================
# The hexapod is the only stage reachable two ways - the same GCS protocol
# either goes over TCP to the C-887, or the stage is driven through the
# 12idbHXP motor records instead. Everything else has one driver.
#
#   "direct"  pihexapod/pipython straight at the C-887 at 10.54.122.145,
#             one connection serving both motion and trajectory. Needs the
#             12idbHXP IOC stopped and PI's vendor software closed: the
#             controller accepts exactly one TCP connection and whoever
#             holds it locks the others out.
#   "epics"   the 12idbHXP motor records. Step motion only - there is no
#             EPICS trajectory interface, and tunnelling GCS through
#             12idbHXP:asyn_1 does not work: that record shares its port
#             with the motor driver's poller, a multi-line GCS reply is one
#             transaction per line, and the two sides consume each other's
#             lines. It crashed the controller three times. See
#             hexapod_traj.AsynGcsTrajectory.
#
# Set to "direct" 2026-10-20 so the hexapod - step moves, tweaks and
# readback included - speaks GCS over its own socket and touches no EPICS
# record. If that socket cannot be opened the stage falls back to a
# simulated stand-in with the reason printed, never silently back to EPICS.
HEXAPOD_TRANSPORT = "direct"

# The goniometer: the SmarAct MCS2 SDK through ptychosaxs/smaract_gonio.py
# instead of the 12ideMCS1 motor records. No EPICS either way - this is a
# choice of driver, not of transport. Needs the SmarAct SDK installed and a
# route to the crate, so it is for the machine wired to the MCS2.
GONIO_USE_SMARACT = False

# The tomography rotation stage: acspy straight at the ACS controller at
# 10.54.122.157 instead of the 12idePtycho motor records (12idePtycho:m1,
# "Tomo Rot"). No EPICS either way - this is a choice of driver, not of
# transport. Needs acspy installed and a route to the ACS crate.
PHI_USE_ACS = False

_DEBUG_FLAG_NAMES = (
    "DEBUG_HEXAPOD", "DEBUG_TOMO_PHI", "DEBUG_GONIO", "DEBUG_QDS",
    "DEBUG_EPICS", "DEBUG_DG645",
)

# `--debug` still works: its levels force whole groups of the flags above.
if DEBUG_MOTORS:
    DEBUG_HEXAPOD = DEBUG_TOMO_PHI = DEBUG_GONIO = DEBUG_QDS = True
if DEBUG_DEVICES:
    DEBUG_EPICS = True

# DEBUG_DEVICES stays the one switch the device imports and the handlers read,
# so DEBUG_EPICS folds into it instead of duplicating every check downstream.
DEBUG_DEVICES = DEBUG_DEVICES or DEBUG_EPICS

# Stubbing every device stubs the DG645 too; the flag exists so the reverse
# is also possible - one simulated device, everything else real.
DEBUG_DG645 = DEBUG_DG645 or DEBUG_DEVICES

# A scan writes motor positions into a real data file, so a simulated stage
# whose axes a scan would record disables every scan.
#
# phi is deliberately not in this list. It is already optional hardware -
# the motor panel has always tolerated it being absent - so a simulated phi
# takes that same path: its own row is disabled and nothing else is. The
# DG645 counts because it gates the detector triggers, so a simulated one
# means the frames a scan records are not the frames it thinks they are.
DEBUG_SCAN_BLOCKING = DEBUG_HEXAPOD or DEBUG_GONIO or DEBUG_EPICS or DEBUG_DG645

# Kept for the caution banner: "is anything simulated at all", which is a
# wider question than "may a scan run".
DEBUG_ANY_MOTOR = DEBUG_HEXAPOD or DEBUG_TOMO_PHI or DEBUG_GONIO

# The motor and interferometer libraries are imported by
# ptychosaxs.motions_ver2 / ptychosaxs.interferometers, which read the flags
# back out of the environment. This must happen before those imports below.
os.environ["PTYCHOSAXS_DEBUG_HEXAPOD"] = "1" if DEBUG_HEXAPOD else "0"
os.environ["PTYCHOSAXS_DEBUG_TOMO_PHI"] = "1" if DEBUG_TOMO_PHI else "0"
os.environ["PTYCHOSAXS_DEBUG_GONIO"] = "1" if DEBUG_GONIO else "0"
os.environ["PTYCHOSAXS_DEBUG_QDS"] = "1" if DEBUG_QDS else "0"
# scan_handler imports DG645_Error at module scope and reads this to decide
# whether it can.
os.environ["PTYCHOSAXS_DEBUG_DG645"] = "1" if DEBUG_DG645 else "0"

# motions_ver2 reads these back out of the environment, for the same reason
# it reads the debug flags that way: it is imported long before anything
# holds a reference to this module's globals.
os.environ["PTYCHOSAXS_HEXAPOD_TRANSPORT"] = HEXAPOD_TRANSPORT
os.environ["PTYCHOSAXS_GONIO_SMARACT"] = "1" if GONIO_USE_SMARACT else "0"
os.environ["PTYCHOSAXS_PHI_ACS"] = "1" if PHI_USE_ACS else "0"


def _debug_flag_lines():
    """Line range of the editable flag block above, for the caution banner.

    Read back out of the source so the printed numbers cannot drift as this
    file is edited. Only the first contiguous run of unindented
    `NAME = ...` assignments counts - that is the block a reader edits. The
    `--debug` overrides reassign the same names indented, and a couple of
    flags are later OR-ed together at the left margin; neither belongs in
    the range.
    """
    try:
        with open(__file__, encoding="utf-8") as fh:
            nums = [
                i for i, line in enumerate(fh, 1)
                if line.split("=")[0].rstrip() in _DEBUG_FLAG_NAMES
            ]
    except OSError:
        nums = []
    if not nums:
        return "the DEBUG_* block near the top"
    last = nums[0]
    for n in nums[1:]:
        if n != last + 1:
            break
        last = n
    return f"{nums[0]}-{last}"


_debug_on = [
    name for name, is_on in (
        ("hexapod", DEBUG_HEXAPOD),
        ("ACS/phi", DEBUG_TOMO_PHI),
        ("gonio", DEBUG_GONIO),
        ("QDS", DEBUG_QDS),
        ("EPICS devices", DEBUG_EPICS),
        ("DG645", DEBUG_DG645 and not DEBUG_EPICS),
    ) if is_on
]
if _debug_on:
    print(
        "Caution: Debug Mode set for at least one controller or device "
        f"({', '.join(_debug_on)}). "
        f"To disable, see rungui.py:{_debug_flag_lines()}"
    )
    if DEBUG_SCAN_BLOCKING:
        print("         Scanning is disabled while those are simulated.")
    if DEBUG_TOMO_PHI and not DEBUG_SCAN_BLOCKING:
        print("         phi is simulated, so it reports as not connected; "
              "every other stage still scans.")

_gui_dir = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(_gui_dir, "ui")

from gui import pv_consent
from ptychosaxs import pv_audit

if DEBUG_MODE:
    print(f"[DEBUG MODE] Level {DEBUG_LEVEL}")
    print(f"  Motors:  {'STUB' if DEBUG_MOTORS else 'REAL'}")
    print(f"  Devices: {'STUB' if DEBUG_DEVICES else 'REAL'}")

# Keyed on what is actually simulated, not on --debug. The DEBUG_* flags
# above are hand-editable, so a stub can be selected with DEBUG_MODE false -
# which used to leave the name undefined and fail at instantiation.
# Only the stubs this module instantiates are imported; the rest were dead
# names that an import error would have hidden.
if DEBUG_MOTORS or DEBUG_DEVICES or DEBUG_DG645:
    from ptychosaxs.debug.debug_stubs import (
        ShutterStub,
        DG645Stub,
        SGZStub,
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
from PyQt5.QtWidgets import QApplication, QPushButton, QFileDialog, QFormLayout
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
    QMetaObject,
    Q_ARG,
    QSettings,
)
from asyncqt import QEventLoop

from gui.font_utils import apply_font_size_to_tree, apply_saved_font_size, DEFAULT_FONT_SIZE
from gui.ini_utils import INI_DIR
from gui.resize_utils import ProportionalResizer

import pyqtgraph as pg

try:
    import requests
except ImportError:
    requests = None  # only used for optional beamline status URL fetch

# The .ini format, its percent-encoding layer and its first-run handling all
# live in beamline_ini. That module replaces the external py12inifunc package,
# which was imported behind a try/except that fell back to a no-op stub: on any
# machine without it installed the GUI silently stopped remembering the working
# folder, log filename, data basepaths, scan name and scan number.
from gui.beamline_ini import (
    SafeIni as _IniClass,
    ensure_default_ini as _ensure_default_ini,
    quote_list as _ini_quote_list,
    quote_text as _ini_quote_text,
)

# ==========================================================================
# Hardware imports — split by motor vs. device debug flags
# ==========================================================================
# Motor-side imports (real when DEBUG_MOTORS is False, i.e. levels 1)
if not DEBUG_MOTORS:
    from ptychosaxs.ptychosaxs import instruments

# Device-side imports (real when DEBUG_DEVICES is False, i.e. level 2)
# tools/dg645.py imports InstrumentKit, which the other device modules do
# not, so it gets its own guard: an environment without that package can
# still run everything else for real.
if not DEBUG_DG645:
    import ptychosaxs.tools.dg645 as dg645
    from ptychosaxs.tools.dg645 import DG645_Error
else:
    class DG645_Error(Exception):
        """Stand-in for tools.dg645's, so the handlers' except clauses still
        name something when the real module is not imported."""

if not DEBUG_DEVICES:
    import epics
    from ptychosaxs.tools.softglue import sgz_pty, SOFTGLUE_Setup_Error
    from ptychosaxs.tools.struck import struck
    from ptychosaxs.tools.shutter import shutter
    from ptychosaxs.tools.detectors import (
        pilatus,
        dante,
        SGstream,
        XSP,
        DET_MIN_READOUT_Error,
        DET_OVER_READOUT_SPEED_Error,
    )

# Infrastructure — only in fully non-debug mode (level 0/1/2 all skip this)
if not DEBUG_MODE:
    from gui.network.server import create_server
    try:
        from ptychosaxs.tools.files import scp
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
else:
    SCAN_NUMBER_IOC = epics.PV("12idc:data:fileIndex")
    s12softglue = sgz_pty()

if DEBUG_DG645:
    dg645_12ID = DG645Stub()
else:
    try:
        dg645_12ID = dg645.dg645_12ID.open_from_uri(dg645.ADDRESS_12IDC)
    except Exception as exc:
        print(
            f"failed to connect DG645 ({exc}). Will not be able to collect "
            "detector images. Set DEBUG_DG645 = True to run with a simulated "
            "one instead."
        )
        dg645_12ID = None

# ==========================================================================
# Constants
# ==========================================================================
status_url = "https://12ide.xray.aps.anl.gov/PVapp/ptycho_status"

# "The GUI itself is running on Linux", set by the setup window's
# "Running on Linux?" checkbox.
#
# It governs how the Working folder on the main window is read. On Linux the
# Working folder is already a Linux path, so the GUI does its own I/O there
# and hands the same path to the detector IOCs. On Windows it is a Windows
# path (Z:\12IDC\...) whose drive letter has to be swapped for
# base_linux_datafolder before the IOCs, which always run on Linux, see it.
#
# Kept in QSettings rather than the .ini because it describes the machine the
# GUI is installed on, not the experiment - an .ini copied between the Windows
# console and a Linux workstation should not carry the wrong answer with it.
# The default is this machine's own OS, so a fresh install is already right.
RUNNING_ON_LINUX_KEY = "paths/runningOnLinux"

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
inifilename = os.path.join(INI_DIR, "pty-co-saxs.ini")
STRUCK_CHANNELS = [2, 3, 4, 5]

# Default contents of pty-co-saxs.ini as (description, attribute, value)
# triples, in beamline_ini's "<description>, <attribute> : <value>" line
# format. The file holds per-installation state (reference positions,
# working folder, scan number, ...) and is not tracked in git, so this table
# is the only definition of a fresh one. Values match the fallbacks
# ptyco_main_control.__init__ applies when readini() fails. A description
# should not contain a comma - that is the field separator.
INI_DEFAULT_ENTRIES = [
    ("Reference Position X", "_ref_X", "0.0000"),
    ("Reference Position Y", "_ref_Z", "0.0000"),
    ("Reference Position Z", "_ref_Z2", "0.0000"),
    ("Working Folder Name", "working_folder", _ini_quote_text("")),
    ("Unit of QDS (nm. um. mm for 0. 1. 2)", "_qds_unit", "%.4f" % QDS_UNIT_DEFAULT),
    ("Axis of QDS X", "_qds_x_sensor", "0.0000"),
    ("Axis of QDS Y", "_qds_y_sensor", "1.0000"),
    ("QDS Time Interval", "_qds_time_interval", "0.1000"),
    ("Number of Pulses per Exposure", "countsperexposure", "0.0000"),
    ("Wait Time Between Fly Scan (s)", "_waittime_between_scans", "0.0000"),
    ("Radial Distance of the QDS V laser (mm)", "_qds_R_vert", "10.0000"),
    ("Azimuthal Angular Position of the QDS V laser (deg)", "_qds_th0_vert", "-30.0000"),
    ("Radial Distance of the QDS H laser (mm)", "_qds_R_cyl", "50.0000"),
    ("SoftGlue Channels", "softglue_channels", _ini_quote_list(["B", "C", "D"])),
    ("Log File Name", "logfilename", _ini_quote_text("")),
    ("Scan Number", "scan_number", "0.0000"),
    ("Flyscan step time-exptime", "_fly_idletime", "0.0000"),
    (
        "Ratio between Exposure and Period for Flyscan (exposure / (flyidletime + expsosure))",
        "_ratio_exp_period",
        "%.4f" % FRACTION_EXPOSURE_PERIOD,
    ),
    ("Scan Time", "scan_time", "-1.0000"),
    ("Base Name of Data Files in Linux", "base_linux_datafolder", "/net/s12data/export/12id-c/"),
    ("Scan Name", "scan_name", _ini_quote_text("")),
    ("Number of Frames per Exposure", "_pulses_per_step", "1.0000"),
    ("ABC", "_step_acq_time", "1.0000"),
    ("ABC", "_fly_acq_time", "0.0330"),
    ("Index of the 2D scan fast axis", "_xmotor", "%.4f" % DEFAULTS["xmotor"]),
    ("Index of the 2D scan slow axis", "_ymotor", "%.4f" % DEFAULTS["ymotor"]),
    ("Save Upstream Optics Positions", "_save_us_optics", "1.0000"),
    ("Check Upstream Optics Before Scans", "_check_optics_prescan", "1.0000"),
    ("SAXS Mode (0 for ptychography. 1 for SAXS)", "saxsmode", "1.0000"),
]


def ensure_default_ini(path=inifilename):
    """Create pty-co-saxs.ini from INI_DEFAULT_ENTRIES if it does not exist,
    and back-fill any entry missing from an existing file.

    Thin wrapper binding this GUI's default table to the generic
    implementation in beamline_ini.
    """
    _ensure_default_ini(path, INI_DEFAULT_ENTRIES)

# Detector attribute/layout PVs (PILATUS1). The attributes XML drives
# NDAttributes; the layout XML drives the HDF1 file writer.
PV_PILATUS_ATTRIBUTES = "S12-PILATUS1:cam1:NDAttributesFile"
PV_PILATUS_LAYOUT = "S12-PILATUS1:HDF1:XMLFileName"


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
    error = pyqtSignal(str)
    # Emitted by 3-D fly/step executors from the worker thread after advancing to
    # a new per-slice scan folder; Qt auto-queues delivery onto the GUI thread
    # since the connected slot lives on a QObject (RunGUI) with GUI-thread affinity.
    scanFolderAdvanced = pyqtSignal(int)


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
        try:
            self.fn(*self.args, **self.kwargs)
        except Exception as e:
            import traceback
            self.signal.error.emit(traceback.format_exc())
            return
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
        try:
            self.pts.mv(self.axis, self.pos)
        except Exception as e:
            import traceback
            self.signal.error.emit(traceback.format_exc())
            return
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
        try:
            self.pts.mvr(self.axis, self.pos)
        except Exception as e:
            import traceback
            self.signal.error.emit(traceback.format_exc())
            return
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


class _ErrorDict(dict):
    """dict subclass that fires a callback whenever 'recent error message' is set."""

    def __init__(self, callback, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._callback = callback

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "recent error message":
            self._callback(value)


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
        guiName = os.path.join(UI_DIR, "ptycoSAXS.ui")
        self.pts = pts
        print("Connecting to PTS...")
        if not DEBUG_MOTORS:
            if not self.pts.hexapod.is_servo_on("X"):
                print("Hexapod servo is off. Trying to turn it on...")
                if self.handle_hexapod_error():
                    print("Hexapod servo is now on.")
                else:
                    # Not fatal: the panel and step moves still work once the
                    # servo comes back, and saying so beats a silent failure.
                    print(
                        "Hexapod servo could not be enabled. Check that the "
                        "IOC can reach the controller (File > IOC info)."
                    )
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
        from gui.handlers.motor_handler import MotorHandler
        from gui.handlers.scan_handler import ScanHandler
        from gui.handlers.status_handler import StatusHandler

        self.motor_handler = MotorHandler(self)
        self.scan_handler = ScanHandler(self)
        self.status_handler = StatusHandler(self)

        # A per-instance copy of the module defaults. The scan-axis selectors
        # write here, and every scan entry point resolves its unset motor
        # indices from it, so the two cannot disagree.
        self.DEFAULTS = dict(DEFAULTS)

        # Asked once, here, and nowhere else. The answer governs every
        # scan-number write for the session, including the ones scans make
        # on worker threads where no dialog could be shown.
        self.pv_consent = pv_consent.PvWriteConsent(parent=self.ui)
        if SCAN_NUMBER_IOC is not None:
            if self.pv_consent.request() == pv_consent.CLOSED:
                # Dismissed without answering. Guessing either way is worse
                # than not starting.
                print("No answer given about beamline PV writes; exiting.")
                sys.exit(1)

        self.messages = _ErrorDict(self._on_error_message)
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
        ensure_default_ini(inifilename)
        self.parameters = _IniClass(inifilename)
        # New fields are added to the ini file by ensure_default_ini above.
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
            self.parameters._step_acq_time = 1
            self.parameters._qds_R_vert = 10.0  # 10mm
            self.parameters._qds_th0_vert = -30.0  # degree
            self.parameters._qds_R_cyl = 50.0  # mm
            self.parameters.softglue_channels = ["B", "C", "D"]
            self.parameters.logfilename = ""
            self.parameters.scan_number = 0
            self.parameters.scan_name = ""
            self.parameters._ratio_exp_period = FRACTION_EXPOSURE_PERIOD
            self.parameters._fly_acq_time = 0.033
            self.parameters.scan_time = -1
            self.parameters._pulses_per_step = 1
            self.parameters.saxsmode = 1  # 0 for ptychography, 1 for SAXS
            self.parameters.base_linux_datafolder = "/net/s12data/export/12id-c/"

        if not hasattr(self.parameters, "_save_us_optics"):
            self.parameters._save_us_optics = True
        if not hasattr(self.parameters, "_check_optics_prescan"):
            self.parameters._check_optics_prescan = True

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
        # self.motorconnected[i] tracks whether self.motornames[i] is live.
        # phi is optional: when it isn't connected we still keep its entry (so
        # the motors after it don't shift slots) but mark it disconnected so we
        # never query its position. Every other motor must connect or we raise.
        self.motornames = []
        self.motorunits = []
        self.motorconnected = []
        phi_not_connected = False
        #        print(motornames, " line 241")
        for i, name in enumerate(motornames):
            try:
                connected = self.pts.isconnected(name)
            except Exception as exc:
                if name == "phi":
                    connected = False
                else:
                    raise RuntimeError(
                        f"Motor '{name}' failed to connect: {exc}"
                    ) from exc
            if connected:
                self.motornames.append(name)
                self.motorunits.append(motorunits[i])
                self.motorconnected.append(True)
            elif name == "phi":
                # Keep phi's slot as a disabled placeholder.
                self.motornames.append(name)
                self.motorunits.append(motorunits[i])
                self.motorconnected.append(False)
                phi_not_connected = True
            else:
                raise RuntimeError(f"Motor '{name}' failed to connect.")

        if phi_not_connected:
            # A modal dialog is right for phi having gone missing, and wrong
            # for phi having been switched off on purpose - that would stop
            # every debug launch dead until someone clicked it.
            if DEBUG_TOMO_PHI:
                print(
                    "phi is simulated (DEBUG_TOMO_PHI/DEBUG_PHI), so it reports as "
                    "not connected and its controls are disabled."
                )
            else:
                QMessageBox.warning(
                    self.ui,
                    "Warning",
                    "Warning: tomography phi stage not connected",
                    QMessageBox.Ok,
                )
        #        print(motornames, " line 252")
        # motors for 2d and 3d scans.....
        self._init_scan_axis_selectors()
        xm = self.DEFAULTS["xmotor"]
        ym = self.DEFAULTS["ymotor"]

        phim = (
            self.motornames.index("phi")
            if "phi" in self.motornames
            else DEFAULTS["phimotor"]
        )
        self.DEFAULTS["phimotor"] = phim

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
            enable = pts is not None and self.motorconnected[i]
            self._set_motor_widgets_enabled(n, enable)

        self.ui.pb_helix_scan.setEnabled(
            pts is not None
            and self.motorconnected[phim]
            and (self.motorconnected[ym] or self.motorconnected[xm])
        )

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
        self.ui.actionSet_gonio_default_vel_acc.triggered.connect(
            self.smaract_set_defaultspeed
        )
        self.ui.actionScanStop.triggered.connect(self.stopscan)
        self.ui.pushButton_stopScan.clicked.connect(self.stopscan)
        self.ui.pushButton_stopScan.setEnabled(False)
        self.ui.pushButton_resetError.clicked.connect(self.reset_error_status)
        self.ui.pushButton_stopScan.setStyleSheet(
            "background-color: rgb(230, 230, 230); color: rgb(150, 150, 150);"
        )
        for _lcd in (self.ui.lcd_X, self.ui.lcd_Z, self.ui.lcd_Z_2):
            _lcd.setStyleSheet("background-color: rgb(240, 240, 240);")
        self.isStopScanIssued = False
        self.is_hexrecord_required = False
        self.shutter_close_after_scan = False
        self.ui.actionflyX_and_stepY.triggered.connect(lambda: self.fly2d())
        self.ui.actionsnake.triggered.connect(lambda: self.fly2d(snake=True))
        self.ui.actionstepscan.triggered.connect(lambda: self.stepscan2d())
        self.ui.actionnormal_2D.triggered.connect(lambda: self.fly3d())
        self.ui.actionsnake_2D.triggered.connect(
            lambda: self.fly3d(snake=True)
        )
        self.ui.actionstep_2D.triggered.connect(
            lambda: self.stepscan3d(*self._scan_axis_indices())
        )
        self.ui.pb_lup_step2d.clicked.connect(lambda: self.stepscan2d())
        self.ui.pb_SAXSscan_fly2d.clicked.connect(
            lambda: self.fly2d(snake=True)
        )
        self.ui.pb_extra_scans.clicked.connect(self._open_extra_scans_window)
        # pb_sample_alignment is added to ptycoSAXS.ui by hand. Warn rather
        # than raise if it is missing, so the rest of the panel still starts.
        _pb_align = self.ui.findChild(QPushButton, "pb_sample_alignment")
        if _pb_align is None:
            print("[WARNING] pb_sample_alignment not found in ptycoSAXS.ui - "
                  "the sample alignment window cannot be opened. Add a "
                  "QPushButton with that objectName to the .ui file.")
        else:
            _pb_align.clicked.connect(self._open_sample_alignment_window)
        self.ui.pushButton_checkFlyBlur.clicked.connect(
            self.scan_handler.check_fly_blur
        )
        self.ui.pb_timeSeries.clicked.connect(self.scan_handler.time_series)
        self.ui.pb_takeshot.clicked.connect(self.scan_handler.takeshot)
        self.ui.pb_writeMacro.clicked.connect(self._open_macro_window)
        self.ui.pb_sendScanParamToMacro.setEnabled(False)
        self.ui.pb_sendScanParamToMacro.clicked.connect(self._send_scan_param_to_macro)
        self.ui.pushButton_plotScanPositions.clicked.connect(
            lambda: self.scan_handler.plot_scan_positions_2d(
                self.DEFAULTS["xmotor"], self.DEFAULTS["ymotor"]
            )
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
        self.ui.pb_lup_step3d.clicked.connect(
            lambda: self.stepscan3d(*self._scan_axis_indices())
        )
        self.ui.pb_SAXSscan_fly3d.clicked.connect(
            lambda: self.fly3d(snake=True)
        )
        self.ui.pb_helix_scan.clicked.connect(
            lambda: self.scan_handler.helix_fly_choose_axis(self.DEFAULTS["phimotor"])
        )

        if DEBUG_SCAN_BLOCKING:
            self._disable_all_scan_controls()
        elif not self.pts.hexapod.can_fly:
            self._disable_fly_scan_controls()

        self.ui.actionIOC_info.triggered.connect(self.show_ioc_info)
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
        self._wire_xray_eye_menu()
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
        self.ui.actionSet_step_acq_time.triggered.connect(
            self.set_step_acq_time
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
        self.ui.pushButton_setAlign.clicked.connect(lambda: self.set_det_alignmode(True))
        self.ui.pushButton_setScan.clicked.connect(lambda: self.set_det_alignmode(False))
        self.ui.pushButton_checkSAXS.clicked.connect(self.update_saxs_det_status)
        # Deliberately not connected. test_hexapod_fly() drives the direct
        # GCS socket, which requires the 12idbHXP IOC to be stopped, and that
        # is not a state a running GUI should assume. Disabled rather than
        # left silently inert; wire it up when the fly path is driven from
        # the IOC instead.
        self.ui.pushButton_test_gcs.setEnabled(False)
        self.ui.pushButton_test_gcs.setToolTip(
            "Disabled: the hexapod fly test needs the 12idbHXP IOC stopped. "
            "See rungui.test_hexapod_fly().")
        self.ui.actionSet_shot_number_per_a_step.triggered.connect(
            self.set_shotnumber_per_step
        )
        # Start from the IOC's fileIndex where it can be read, so this GUI
        # picks up whatever the beamline last numbered rather than carrying
        # on from its own ini. Reading is unconditional - consent governs
        # writes, and this writes nothing.
        self.parameters.scan_number = pv_audit.startup_scan_number(
            self.read_scan_number_from_ioc(), self.parameters.scan_number
        )
        self.update_scannumber()
        self.ui.actionRatio_of_exptime_period_for_Flyscan.triggered.connect(
            self.set_fly_acquisition_time
        )

        if os.name != "nt":
            self.ui.menuQDS.setDisabled(True)
        self.threadpool = QThreadPool.globalInstance()
        self.Worker = Worker  # expose to handlers that don't import rungui
        # Same reason as Worker above: importing rungui from a child window
        # re-executes this module and builds a second GUI (see the note in
        # handlers/status_handler.py). Child windows take the move runnables
        # from here instead.
        self.MoveRunnable = move  # absolute move
        self.MoveRelRunnable = mover  # relative move

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
        # self.ui.pb_hp_left.clicked.connect(lambda: self._hp_tweak(0, -1))
        # self.ui.pb_hp_right.clicked.connect(lambda: self._hp_tweak(0, 1))
        self.ui.pb_hp_down.clicked.connect(lambda: self._hp_tweak(2, -1))
        self.ui.pb_hp_up.clicked.connect(lambda: self._hp_tweak(2, 1))

        # ── Translation navigation panel ───────────────────────────────────
        self.ui.pb_trans_left.clicked.connect(lambda: self._trans_tweak(False, -1))
        self.ui.pb_trans_right.clicked.connect(lambda: self._trans_tweak(False, 1))

        def _on_trans_flip(checked):
            self.ui.label_trans.setText("trans2" if checked else "trans1")
            # self.ui.label_trans_2.setText("trans1" if checked else "trans2")

        self.ui.checkBox_transFlip.setChecked(True)
        self.ui.checkBox_transFlip.toggled.connect(_on_trans_flip)
        _on_trans_flip(self.ui.checkBox_transFlip.isChecked())

        # ── Copy current positions ─────────────────────────────────────────
        self.ui.pushButton_copyCurrent.clicked.connect(self._copy_current_positions)

        # ── Setup window ───────────────────────────────────────────────────
        self.ui.pushButton_setup.clicked.connect(self._open_setup_window)
        self.ui.pushButton_openOpticsGui.clicked.connect(self._open_optics_gui)
        self.ui.pushButton_checkUpOptics.clicked.connect(
            self._open_optics_status_window)

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

        # QMainWindow only resolves its central widget's real size once the
        # window is actually shown (layout activation is deferred) — force
        # that to happen now, before snapshotting the as-designed layout, so
        # the reference size/geometry used for proportional rescaling is
        # accurate. Capturing this before restoreGeometry() means later
        # rescales are always relative to the true .ui-authored arrangement.
        QApplication.processEvents()
        self._main_resizer = ProportionalResizer(self.ui.centralWidget())
        self.ui.centralWidget().setMaximumSize(16777215, 16777215)
        self.ui.setMinimumSize(
            int(self._main_resizer.orig_size.width() * 0.4),
            int(self._main_resizer.orig_size.height() * 0.4),
        )

        _s = QSettings("ptychoSAXS", "ptychoSAXS")
        _geom = _s.value("mainWindow/geometry")
        if _geom is not None:
            self.ui.restoreGeometry(_geom)
        # The restored (or otherwise resolved) window size may differ from
        # the design-time size captured above — resync widget geometry to it
        # immediately instead of waiting for the user to resize manually.
        self._main_resizer.rescale()

        apply_saved_font_size(self.ui)

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

    def _disable_all_scan_controls(self) -> None:
        """Grey out every control that starts a scan.

        Called when a simulated stage or device would corrupt a scan's
        record of itself - see DEBUG_SCAN_BLOCKING at the top of this file.
        A scan run against one would write fabricated positions into a real
        data file, so none may start. Acquisition that moves no motor
        (takeshot, time series) stays live, as do the stop and reset
        controls.

        A simulated phi does not land here: it is optional hardware, so it
        reports as not connected and only its own row is disabled.
        """
        scan_buttons = [
            "pb_lup_step2d",
            "pb_SAXSscan_fly2d",
            "pb_lup_step3d",
            "pb_SAXSscan_fly3d",
            "pb_helix_scan",
            "pb_extra_scans",
        ]
        # Per-motor 1D scan launchers exist only for these slots.
        for n in (1, 2, 3, 7, 8, 9):
            scan_buttons += ["pb_lup_%i" % n, "pb_SAXSscan_%i" % n]
        for name in scan_buttons:
            widget = self.ui.findChild(QPushButton, name)
            if widget is not None:
                widget.setEnabled(False)

        for name in (
            "actionflyX_and_stepY",
            "actionsnake",
            "actionstepscan",
            "actionnormal_2D",
            "actionsnake_2D",
            "actionstep_2D",
        ):
            action = getattr(self.ui, name, None)
            if action is not None:
                action.setEnabled(False)

    def _disable_fly_scan_controls(self) -> None:
        """Grey out only the controls that need the hexapod's wave generator.

        Narrower than _disable_all_scan_controls: step scans move the stage
        through the EPICS motor records and are unaffected by a missing
        trajectory transport, so disabling them too would be wrong.
        """
        for name in ("pb_SAXSscan_fly2d", "pb_SAXSscan_fly3d", "pb_helix_scan"):
            widget = self.ui.findChild(QPushButton, name)
            if widget is not None:
                widget.setEnabled(False)
        # Only the hexapod's own slots. The 1-D fly launchers for phi and
        # the gonio stages (slots 7-9) drive scan_kind "fly_phi", which
        # steps the stage and never touches the wave generator.
        for n, name in enumerate(self.motornames, start=1):
            if name not in self.pts.hexapod.axes:
                continue
            widget = self.ui.findChild(QPushButton, "pb_SAXSscan_%i" % n)
            if widget is not None:
                widget.setEnabled(False)
        for name in ("actionflyX_and_stepY", "actionsnake", "actionnormal_2D",
                     "actionsnake_2D"):
            action = getattr(self.ui, name, None)
            if action is not None:
                action.setEnabled(False)
        print(
            "Fly scans disabled: hexapod trajectory transport is %s - %s"
            % (self.pts.hexapod.traj_state, self.pts.hexapod.traj_reason)
        )

    # ── 2-D scan axis selection ────────────────────────────────────────────

    def _scan_axis_indices(self):
        """(x, y, phi) motor indices as the selectors currently have them."""
        return (
            self.DEFAULTS["xmotor"],
            self.DEFAULTS["ymotor"],
            self.DEFAULTS["phimotor"],
        )

    def _init_scan_axis_selectors(self) -> None:
        """Populate the 2-D scan axis combo boxes and seed them from the ini.

        The scan engine was already parameterised by motor index; what was
        missing was any way to choose. These write into self.DEFAULTS, which
        every scan entry point resolves its unset indices from, so changing a
        selection takes effect on the next scan without rewiring anything.
        """
        for key, widget_name in (("xmotor", "cb_xmotor"), ("ymotor", "cb_ymotor")):
            combo = getattr(self.ui, widget_name, None)
            if combo is None:
                continue  # older .ui without the selectors
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self.motornames)
            index = self._saved_scan_axis(key)
            combo.setCurrentIndex(index)
            self.DEFAULTS[key] = index
            combo.blockSignals(False)
            combo.currentIndexChanged.connect(
                lambda i, key=key: self._on_scan_axis_changed(key, i)
            )

    def _saved_scan_axis(self, key):
        """The stored index for 'xmotor'/'ymotor', clamped to what exists.

        A stale ini - written when a different set of motors was connected -
        must not be able to index past the end of motornames.
        """
        try:
            index = int(float(getattr(self.parameters, "_" + key)))
        except (AttributeError, TypeError, ValueError):
            index = DEFAULTS[key]
        if not 0 <= index < len(self.motornames):
            index = min(DEFAULTS[key], len(self.motornames) - 1)
        return max(index, 0)

    def _on_scan_axis_changed(self, key, index) -> None:
        if index < 0:
            return
        self.DEFAULTS[key] = index
        setattr(self.parameters, "_" + key, float(index))
        try:
            self.parameters.writeini()
        except Exception as exc:
            print(f"Could not save the {key} selection: {exc}")

    # ── IOC info ───────────────────────────────────────────────────────────

    def show_ioc_info(self) -> None:
        """Report which machine serves each IOC, and when it last started.

        An IOC that restarted mid-session is otherwise invisible: the records
        still answer and autosave has put plausible numbers back, with
        nothing to say the positions predate the restart.
        """
        from ptychosaxs import ioc_info

        try:
            rows = ioc_info.collect()
            link_ok = ioc_info.hexapod_link_status()
            report = ioc_info.format_report(rows, link_ok)
        except Exception as exc:
            report = f"Could not read the IOC information: {exc}"
        dialog = QMessageBox(self.ui)
        dialog.setWindowTitle("IOC info")
        dialog.setText("IOCs this GUI talks to:")
        dialog.setDetailedText(report)
        # Monospace, or the columns do not line up.
        dialog.setStyleSheet("QTextEdit { font-family: monospace; min-width: 620px; }")
        dialog.exec_()

    # ── Hexapod fly-scan test ──────────────────────────────────────────────
    #
    # A short fly scan on one hexapod axis, in three stages you step through
    # with a dialog each time. Split this way because the stages carry very
    # different risk: stage 1 writes nothing, stage 2 writes to the
    # controller but moves nothing, and only stage 3 moves the stage. Stage 2
    # is where the GCS-over-EPICS transport is actually exercised - it sends
    # the WAV and TWS commands that used to be truncated by .AOUT - so a
    # failure there is a transport problem, and a failure in stage 3 is not.

    # Deliberately tiny. Every trigger point is one TWS command, and every
    # command is a separate transaction on the port the motor driver polls.
    # Writes are off and the button is unwired (see where the other buttons
    # are connected). This drives the DIRECT socket transport, which needs
    # the EPICS IOC stopped, because the C-887 accepts one TCP connection and
    # the IOC holds it. That is not a state a running GUI should assume, so
    # this is kept for hand-testing rather than offered as a control.
    #
    # It no longer touches asyn_1. Driving a fly scan through that record
    # crashed the controller three times: the record and the motor driver's
    # poller share one port, a multi-line GCS reply is one transaction per
    # line, and each side consumes the other's lines in the gaps. No buffer
    # or batch size fixes that, so the EPICS path is abandoned here.
    FLY_TEST_ALLOW_WRITES = False

    FLY_TEST_AXIS = "X"
    FLY_TEST_TRAVEL_MM = 0.02      # 20 um
    FLY_TEST_TIME_S = 2.0
    FLY_TEST_PULSE_PERIOD_S = 0.1  # -> 21 triggers
    FLY_TEST_RAMP_POINTS = 5

    def test_hexapod_fly(self) -> None:
        """Build and optionally run a short hexapod fly scan over GCS.

        Builds its own direct-socket backend rather than using self.pts.hexapod,
        whose transport is whatever the GUI is configured for. Position comes
        from the controller (POS?) rather than the motor records, so the whole
        test is one connection to the C-887 and nothing else.
        """
        if DEBUG_HEXAPOD:
            QMessageBox.information(
                self.ui, "Hexapod fly test",
                "DEBUG_HEXAPOD is on - the hexapod is a stub.")
            return

        from ptychosaxs import hexapod as hexapod_mod
        from ptychosaxs import hexapod_traj

        # -- stage 1: resolve a direct transport. Writes nothing. ---------
        try:
            traj = hexapod_traj.make_trajectory_backend(
                hexapod_mod.HEXAPOD_IP, prefer="direct", log=lambda *a: None)
            report = self._fly_test_readiness(traj)
        except Exception as exc:
            QMessageBox.critical(self.ui, "Hexapod fly test",
                                 f"Could not resolve the hexapod transport.\n\n"
                                 f"{type(exc).__name__}: {exc}")
            return

        if not traj.available:
            dialog = QMessageBox(self.ui)
            dialog.setWindowTitle("Hexapod fly test")
            dialog.setIcon(QMessageBox.Warning)
            dialog.setText("No fly transport is available.")
            dialog.setDetailedText(report)
            dialog.setStyleSheet(
                "QTextEdit { font-family: monospace; min-width: 620px; }")
            dialog.exec_()
            return

        # Python imports a module once per process, so editing pihexapod
        # while this GUI is running leaves the old code in memory and the
        # object here will not match the file on disk. Refuse rather than
        # let a stale module reach the hardware.
        stale = self._fly_test_stale_transport(traj)
        if stale:
            QMessageBox.critical(
                self.ui, "Hexapod fly test",
                "The loaded pihexapod is out of date - restart the GUI "
                "before testing.\n\n" + stale)
            return

        axis = self.FLY_TEST_AXIS
        travel = self.FLY_TEST_TRAVEL_MM
        try:
            # POS? on the controller, not the motor record: this test holds
            # the only connection, and the IOC is expected to be stopped.
            start = float(traj._dev.get_pos()[axis])
        except Exception as exc:
            QMessageBox.critical(self.ui, "Hexapod fly test",
                                 f"Could not read {axis}: {exc}")
            return

        pulses = int(self.FLY_TEST_TIME_S / self.FLY_TEST_PULSE_PERIOD_S) + 1
        plan = (
            f"axis            {axis}\n"
            f"start           {start:.5f} mm   (current position)\n"
            f"travel          {travel:+.5f} mm\n"
            f"end             {start + travel:.5f} mm\n"
            f"duration        {self.FLY_TEST_TIME_S:g} s\n"
            f"triggers        {pulses}  (every {self.FLY_TEST_PULSE_PERIOD_S:g} s)\n"
        )

        if not self.FLY_TEST_ALLOW_WRITES:
            dialog = QMessageBox(self.ui)
            dialog.setWindowTitle("Hexapod fly test")
            dialog.setIcon(QMessageBox.Warning)
            dialog.setText("Readiness only - writing is disabled.")
            dialog.setInformativeText(
                "This drives the direct socket transport, which needs the "
                "12idbHXP IOC and PI's vendor software both stopped - the "
                "C-887 accepts only one TCP connection.\n\n"
                "Set FLY_TEST_ALLOW_WRITES = True in rungui.py to enable the "
                "build and run stages.")
            dialog.setDetailedText(report + "\n\nPlanned trajectory\n" + plan)
            dialog.setStyleSheet(
                "QTextEdit { font-family: monospace; min-width: 620px; }")
            dialog.exec_()
            return

        if not self._fly_test_confirm(
                "Build the trajectory?",
                "This writes the waveform and trigger tables to the controller.\n"
                "It does NOT move the hexapod.",
                report + "\n\nPlanned trajectory\n" + plan):
            return

        # -- stage 2: WAV / TWS / CTO. Writes to the controller, no motion. -
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            t0 = time.time()
            traj.set_traj(axis, self.FLY_TEST_TIME_S, travel, start, 1,
                             self.FLY_TEST_PULSE_PERIOD_S,
                             self.FLY_TEST_RAMP_POINTS)
            build_s = time.time() - t0
        except Exception as exc:
            dialog = QMessageBox(self.ui)
            dialog.setWindowTitle("Hexapod fly test")
            dialog.setIcon(QMessageBox.Critical)
            dialog.setText("Building the trajectory failed.")
            dialog.setDetailedText(
                f"{type(exc).__name__}: {exc}\n\n{report}\n\n{plan}")
            dialog.setStyleSheet(
                "QTextEdit { font-family: monospace; min-width: 620px; }")
            dialog.exec_()
            return
        finally:
            QApplication.restoreOverrideCursor()

        built = (f"Trajectory built in {build_s:.2f} s.\n\n" + plan +
                 f"\n{pulses} trigger points were written over "
                 f"{getattr(traj, 'transport', traj.state)}.")

        if not self._fly_test_confirm(
                "Run it? This MOVES the hexapod.",
                f"The hexapod will move {axis} to {start:.5f} mm and then fly "
                f"{travel:+.5f} mm over {self.FLY_TEST_TIME_S:g} s, firing "
                f"{pulses} triggers.\n\nMake sure the sample and detector are "
                f"clear.",
                built):
            QMessageBox.information(
                self.ui, "Hexapod fly test",
                "Stopped after building the trajectory. Nothing moved.\n\n"
                "The transport worked: the waveform and trigger tables are "
                "loaded on the controller.")
            return

        # -- stage 3: motion. --------------------------------------------
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            t0 = time.time()
            traj.goto_start_pos(axis)
            traj.run_traj(axis, wait=True)
            run_s = time.time() - t0
            landed = float(traj._dev.get_pos()[axis])
        except Exception as exc:
            QMessageBox.critical(
                self.ui, "Hexapod fly test",
                f"The trajectory was built but running it failed.\n\n"
                f"{type(exc).__name__}: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        dialog = QMessageBox(self.ui)
        dialog.setWindowTitle("Hexapod fly test")
        dialog.setIcon(QMessageBox.Information)
        dialog.setText("Fly scan completed.")
        dialog.setDetailedText(
            built + f"\n\nran in        {run_s:.2f} s\n"
                    f"landed at     {landed:.5f} mm\n"
                    f"expected      {start + travel:.5f} mm\n"
                    f"difference    {(landed - start - travel) * 1000:+.2f} um")
        dialog.setStyleSheet(
            "QTextEdit { font-family: monospace; min-width: 620px; }")
        dialog.exec_()

    def _fly_test_readiness(self, traj) -> str:
        """What the direct transport resolved to. Touches no EPICS records."""
        lines = [
            f"available       {traj.available}",
            f"state           {traj.state}",
            f"transport       {getattr(traj, 'transport', '-')}",
            f"address         {getattr(traj, 'address', '-')}",
        ]
        if traj.reason:
            lines.append(f"reason          {traj.reason}")
        # Reaching through to the library's transport: only this test needs
        # it, so it is not worth a facade method.
        dev = getattr(getattr(traj, "_dev", None), "pidev", None)
        if dev is not None:
            gateway = getattr(getattr(dev, "pidev", None), "_gateway", None)
            lines.append(f"device          {type(dev).__name__}")
            lines.append(f"gateway         "
                         f"{type(gateway).__name__ if gateway else 'n/a'}")
        lines.append("")
        lines.append("The C-887 accepts one TCP connection. This test needs")
        lines.append("the 12idbHXP IOC and PI's vendor software both stopped.")
        return "\n".join(lines)

    def _fly_test_stale_transport(self, traj) -> str:
        """Empty if the live transport has the fixes; else why it doesn't.

        Checks the object in memory, not the file on disk - a stale import
        is exactly the case where those two disagree.
        """
        import inspect
        import sys

        if getattr(traj, "transport", None) != "direct":
            return ""  # only the direct transport is used here
        dev = getattr(getattr(traj, "_dev", None), "pidev", None)
        if dev is None:
            return "could not reach the transport object to check it"
        module = sys.modules.get(type(dev).__module__)
        if not hasattr(module, "GCS_PORT"):
            # The pre-fix pigcs built GCSDevice(devname), which loads PI's
            # libpi_pi_gcs2.so and fails on Linux. The socket-gateway
            # version defines GCS_PORT; its absence means the old module.
            return ("Loaded from:\n  " + inspect.getfile(type(dev)) +
                    "\n\nMissing:\n  - connect through a PISocket gateway "
                    "(no libpi_pi_gcs2.so)")
        return ""

    def _fly_test_confirm(self, text, informative, detail) -> bool:
        dialog = QMessageBox(self.ui)
        dialog.setWindowTitle("Hexapod fly test")
        dialog.setIcon(QMessageBox.Question)
        dialog.setText(text)
        dialog.setInformativeText(informative)
        dialog.setDetailedText(detail)
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        dialog.setDefaultButton(QMessageBox.Cancel)
        dialog.setStyleSheet(
            "QTextEdit { font-family: monospace; min-width: 620px; }")
        return dialog.exec_() == QMessageBox.Yes

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
        for i, name in enumerate(self.motornames):
            if not self.motorconnected[i]:
                continue  # skip disconnected placeholders (e.g. phi)
            motors[name] = self.pts.get_pos(name)
        return motors

    def updatepos(self, axis="", val=None):
        if len(axis) == 0:
            for i, name in enumerate(self.motornames):
                if not self.motorconnected[i]:
                    continue  # skip disconnected placeholders (e.g. phi)
                if val is None:
                    val = self.pts.get_pos(name)
                # self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
                self.ui.findChild(QLabel, "lb_%i" % (i + 1)).setText("%0.6f" % val)
                val = None
        else:
            i = self.motornames.index(axis)
            if not self.motorconnected[i]:
                return  # disconnected placeholder (e.g. phi) — nothing to update
            if val is None:
                val = self.pts.get_pos(axis)
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
        w.signal.error.connect(self._on_worker_error)
        self.threadpool.start(w)
        self.updatepos(axis)
        return w

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
        w.signal.error.connect(self._on_worker_error)
        self.threadpool.start(w)
        self.updatepos(axis)
        return w

    # ── Gonio & hexapod speed control ─────────────────────────────────────

    def handle_hexapod_error(self):
        return self.pts.hexapod.handle_error()

    def smaract_set_defaultspeed(self):
        gonio = self.pts.gonio
        for name, connected in zip(gonio.motornames, gonio.isconnected()):
            if connected:
                gonio.set_speed(name)

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
        # _numbers.npy is untracked per-installation state: create it filled
        # with the "field left blank" sentinel on first start.
        if not os.path.exists("_numbers.npy"):
            np.save("_numbers.npy", np.full((len(self.motornames), 6), -999999.0))

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

        # 5. Persist window position.
        try:
            _s = QSettings("ptychoSAXS", "ptychoSAXS")
            _s.setValue("mainWindow/geometry", self.ui.saveGeometry())
        except Exception:
            pass

        # 6. Quit the Qt event loop (also terminates the asyncio loop in main()).
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

    def set_step_acq_time(self):
        return self.scan_handler.set_step_acq_time()

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

    def flydone(self, return_motor=True, reset_scannumber=True, donedone=True):
        return self.scan_handler.flydone(return_motor, reset_scannumber, donedone)

    def helixdone(self, return_motor=True, reset_scannumber=True, donedone=True):
        return self.scan_handler.helixdone(return_motor, reset_scannumber, donedone)

    def flydone2d(self, value=0):
        return self.scan_handler.flydone2d(value)

    def flydone3d(self, value=0):
        return self.scan_handler.flydone3d(value)

    # Fly scans
    def fly(self, motornumber=-1):
        return self.scan_handler.fly(motornumber)

    def fly0(self, motornumber=-1, update_progress=None, update_status=None):
        return self.scan_handler.fly0(motornumber, update_progress, update_status)

    def fly2d(self, xmotor=None, ymotor=None, scanname="", snake=False):
        return self.scan_handler.fly2d(xmotor, ymotor, snake)

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

    def fly3d(self, xmotor=None, ymotor=None, phimotor=None, scanname="", snake=False):
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

    def stepscan2d(self, xmotor=None, ymotor=None):
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
    def scandone(self, update_scannumber=True, donedone=True, update_gui=True,
                 direct_gui_updates=True, notify_folder_advanced=None, link_scan_number=None,
                 link_detector_data=True):
        return self.scan_handler.scandone(
            update_scannumber, donedone, update_gui=update_gui,
            direct_gui_updates=direct_gui_updates,
            notify_folder_advanced=notify_folder_advanced,
            link_scan_number=link_scan_number,
            link_detector_data=link_detector_data,
        )

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

    def read_scan_number_from_ioc(self):
        """The IOC's fileIndex as an int, or None if it cannot be read.

        None covers every way that can happen - no PV object in debug
        mode, the IOC not answering, a disconnected channel - because the
        caller treats them the same: fall back to the ini.
        """
        pv = self.SCAN_NUMBER_IOC
        if pv is None or not pv.connected:
            return None
        try:
            value = pv.get(timeout=1.0)
        except Exception as exc:
            print(f"Could not read {pv_audit.SCAN_NUMBER_PV}: {exc}")
            return None
        if value is None:
            return None
        print(f"Scan number taken from {pv_audit.SCAN_NUMBER_PV}: {int(value)}")
        return int(value)

    def write_scan_number_to_ioc(self) -> bool:
        """Put the scan number to the IOC at the start of a scan.

        The only thing in this GUI that writes a beamline PV. Call it when
        a scan begins and at no other time: starting up, finishing a scan
        and nudging the number with the +/- buttons all leave the IOC
        alone, so the PV reflects the scan that is actually running.

        Silently does nothing when the operator declined at startup, which
        is what declining means. Returns whether the PV was written.
        """
        if not self.pv_consent.granted:
            return False
        pv = self.SCAN_NUMBER_IOC
        if pv is None:
            return False
        pv.put(int(self.parameters.scan_number))
        return True

    def on_configuration_changed(self, what="the configuration"):
        """Ask for PV-write consent again after a configuration change."""
        self.pv_consent.revoke()
        if self.pv_consent.request(reason=f"reloading after {what} changed") \
                == pv_consent.CLOSED:
            print("No answer given about beamline PV writes; exiting.")
            sys.exit(1)

    def on_scan_folder_advanced(self, scan_number):
        return self.scan_handler.on_scan_folder_advanced(scan_number)

    def write_scaninfo_to_logfile(self, strlist):
        return self.scan_handler.write_scaninfo_to_logfile(strlist)

    def log_data(self, data_list):
        return self.scan_handler.log_data(data_list)

    def print_fly_settings(self, motornumber):
        return self.scan_handler.print_fly_settings(motornumber)

    def set_exp_period_ratio(self):
        return self.scan_handler.set_exp_period_ratio()

    def set_fly_acquisition_time(self):
        return self.scan_handler.set_fly_acquisition_time()

    def set_det_alignmode(self, value=None):
        return self.scan_handler.set_det_alignmode(value)

    def update_saxs_det_status(self):
        return self.scan_handler.update_saxs_det_status()

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
        w.signal.error.connect(self._on_worker_error)
        self.threadpool.start(w)
        self.updatepos(axis)

    def _trans_tweak(self, is_ud, sign):
        """Move a translation motor by the step entered in ed_hp_tweak (microns → mm).

        is_ud=False → left/right axis (motor 8, index 7).
        is_ud=True  → up/down axis   (motor 9, index 8).
        When checkBox_transFlip is checked the two axes are swapped.
        """
        try:
            step_mm = float(self.ui.ed_hp_tweak.text()) / 1000.0
        except ValueError:
            return
        if self.ui.checkBox_transFlip.isChecked():
            is_ud = not is_ud
        motornumber = 8 if is_ud else 7
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        w = mover(self.pts, axis, sign * step_mm)
        w.signal.error.connect(self._on_worker_error)
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
        import subprocess

        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "optics_motors.py"
        )
        cmd = [sys.executable, script]
        if DEBUG_DEVICES:
            cmd.append("--debug_mode")
        try:
            subprocess.Popen(cmd)
        except Exception as e:
            QMessageBox.warning(
                self.ui, "Optics GUI", "Could not launch optics GUI:\n%s" % e
            )

    def _optics_caget(self):
        """The caget optics_status should use, or None to let it use epics.

        In debug mode there are no PVs to read, so hand it a reader that
        always fails -- every block then reports "undefined" rather than the
        panel inventing positions.
        """
        if DEBUG_DEVICES:
            return lambda pv: None
        return None

    def optics_status_report(self):
        """Current upstream optics state, as an optics_status.OpticsStatus.

        One place both the Check Upstream Optics window and the pre-scan
        guard read from, so they can never disagree.
        """
        from gui import optics_status

        return optics_status.read_status(caget=self._optics_caget())

    def _open_optics_status_window(self):
        """Show the upstream optics report. Non-modal, so it can stay open
        while motors are nudged from the optics GUI's separate process."""
        from gui.optics_status_dialog import OpticsStatusDialog

        window = getattr(self, "optics_status_window", None)
        if window is None:
            window = OpticsStatusDialog(
                self.ui,
                caget=self._optics_caget(),
                eye_state=lambda: self.xray_eye.is_in(),
            )
            self.optics_status_window = window
        window.show()
        window.raise_()
        window.activateWindow()

    def _open_macro_window(self):
        """Open (or raise) the non-modal scan macro window."""
        if getattr(self, "macro_window", None) is None:
            from gui.macro_window import MacroWindow

            self.macro_window = MacroWindow(self)
            apply_saved_font_size(self.macro_window)
        self.macro_window.show()
        self.macro_window.raise_()
        self.macro_window.activateWindow()

    def _send_scan_param_to_macro(self):
        """pb_sendScanParamToMacro: only reachable while the macro window is
        open (see MacroWindow.showEvent/hideEvent, which enable/disable this
        button)."""
        if getattr(self, "macro_window", None) is not None and self.macro_window.isVisible():
            self.macro_window.add_scan_param_snapshot()

    # ── X-ray eye (Detectors menu) ─────────────────────────────────────────
    # State is shared with the optics GUI (a separate process) and the sample
    # alignment window through the command PV's readback -- see gui/xray_eye.py.

    def _wire_xray_eye_menu(self):
        from gui.xray_eye import XrayEye

        if DEBUG_DEVICES:
            from ptychosaxs.debug.debug_stubs import FakePV as _PV
        else:
            _PV = epics.PV
        self.xray_eye = XrayEye(_PV, debug=DEBUG_DEVICES)
        self.ui.actionXrayEyeIn.toggled.connect(self._on_xray_eye_toggled)
        # Read the shared state exactly when the menu is opened, rather than
        # polling for something that changes a few times a shift.
        self.ui.menuDetectors.aboutToShow.connect(self._refresh_xray_eye_menu)
        self._refresh_xray_eye_menu()

    def _refresh_xray_eye_menu(self):
        state = self.xray_eye.is_in()
        self.ui.actionXrayEyeStatus.setText(
            "X-ray eye: %s" % {True: "IN", False: "OUT"}.get(state, "unknown"))
        self.ui.actionXrayEyeIn.blockSignals(True)
        self.ui.actionXrayEyeIn.setChecked(state is True)
        self.ui.actionXrayEyeIn.blockSignals(False)

    def _on_xray_eye_toggled(self, checked):
        try:
            self.xray_eye.set_in(checked)
        except Exception as e:
            QMessageBox.warning(
                self.ui, "X-ray eye", "Could not command the X-ray eye:\n%s" % e
            )
        self._refresh_xray_eye_menu()

    def _open_sample_alignment_window(self):
        """Open (or raise) the non-modal guided sample alignment window."""
        if getattr(self, "sample_alignment_window", None) is None:
            from gui.sample_alignment import SampleAlignmentWindow

            self.sample_alignment_window = SampleAlignmentWindow(self)
        # SampleAlignmentWindow.show() also raises, activates, restarts the
        # workflow on a fresh open, and starts its position poll timer.
        self.sample_alignment_window.show()

    def _open_extra_scans_window(self):
        """Open (or raise) the non-modal Extra Scans window."""
        if getattr(self, "extra_scans_window", None) is None:
            dlg = uic.loadUi(os.path.join(UI_DIR, "extra_scans.ui"))
            dlg._resizer = ProportionalResizer(dlg)
            dlg.setMinimumSize(
                int(dlg._resizer.orig_size.width() * 0.4),
                int(dlg._resizer.orig_size.height() * 0.4),
            )
            dlg.pushButton_piezoStep2d.clicked.connect(self.scan_handler.piezo_step2d)
            dlg.pushButton_exitExtraScans.clicked.connect(dlg.close)

            _extra_settings = QSettings("ptychoSAXS", "ptychoSAXS")
            _extra_geom = _extra_settings.value("extraScansWindow/geometry")
            if _extra_geom is not None:
                dlg.restoreGeometry(_extra_geom)
                dlg._resizer.rescale()

            def _save_extra_scans_geom_and_close(event):
                QSettings("ptychoSAXS", "ptychoSAXS").setValue(
                    "extraScansWindow/geometry", dlg.saveGeometry()
                )
                event.accept()

            dlg.closeEvent = _save_extra_scans_geom_and_close

            apply_saved_font_size(dlg)

            self.extra_scans_window = dlg
        self.extra_scans_window.show()
        self.extra_scans_window.raise_()
        self.extra_scans_window.activateWindow()

    # ── Which OS the GUI is on ─────────────────────────────────────────────

    def running_on_linux(self) -> bool:
        """Whether the Working folder is already a Linux path.

        See RUNNING_ON_LINUX_KEY. Defaults to this machine's own OS, so an
        installation that never opens the setup window is still correct.
        """
        return QSettings("ptychoSAXS", "ptychoSAXS").value(
            RUNNING_ON_LINUX_KEY, os.name != "nt", type=bool
        )

    def set_running_on_linux(self, value: bool) -> None:
        """Persist the "Running on Linux?" answer and re-push the paths.

        The detector FilePaths currently on the IOCs were derived under the
        old answer, so they are recomputed here rather than left stale until
        the next scan starts.
        """
        if bool(value) == self.running_on_linux():
            return
        QSettings("ptychoSAXS", "ptychoSAXS").setValue(
            RUNNING_ON_LINUX_KEY, bool(value)
        )
        self.push_filepath_to_detectors()

    def _open_setup_window(self):
        """Open the setup configuration dialog."""
        import time as _time
        from PyQt5.QtWidgets import QButtonGroup, QFileDialog, QDialog

        dlg = uic.loadUi(os.path.join(UI_DIR, "setup_configuration.ui"))

        # Make the dialog proportionally resizable, same as the main window.
        dlg._resizer = ProportionalResizer(dlg)
        dlg.setMinimumSize(
            int(dlg._resizer.orig_size.width() * 0.4),
            int(dlg._resizer.orig_size.height() * 0.4),
        )

        _setup_settings = QSettings("ptychoSAXS", "ptychoSAXS")
        _setup_geom = _setup_settings.value("setupWindow/geometry")
        if _setup_geom is not None:
            dlg.restoreGeometry(_setup_geom)
            dlg._resizer.rescale()

        apply_saved_font_size(dlg)

        # ── Populate with current state ────────────────────────────────────
        dlg.spinBox_fontSize.setValue(
            QSettings("ptychoSAXS", "ptychoSAXS").value(
                "ui/fontSize", DEFAULT_FONT_SIZE, type=int
            )
        )

        def _on_font_size_changed(size):
            apply_font_size_to_tree(self.ui, size)
            apply_font_size_to_tree(dlg, size)
            if getattr(self, "macro_window", None) is not None:
                apply_font_size_to_tree(self.macro_window, size)
            if getattr(self, "extra_scans_window", None) is not None:
                apply_font_size_to_tree(self.extra_scans_window, size)
            if getattr(self, "sample_alignment_window", None) is not None:
                apply_font_size_to_tree(self.sample_alignment_window.ui, size)
            QSettings("ptychoSAXS", "ptychoSAXS").setValue("ui/fontSize", size)

        dlg.spinBox_fontSize.valueChanged.connect(_on_font_size_changed)

        dlg.lineEdit_logFname.setText(self.parameters.logfilename)
        dlg.lineEdit_dataBasepaths.setText(
            getattr(self.parameters, "base_linux_datafolder", "")
        )

        # "Running on Linux?" — see RUNNING_ON_LINUX_KEY. When it is checked
        # the Working folder is the data basepath, so the basepath field
        # below it is unused; greying it out says so rather than leaving a
        # stale value looking as though it still applies.
        dlg.checkBox_runningOnLinux.setChecked(self.running_on_linux())

        def _on_running_on_linux_toggled(checked):
            dlg.lineEdit_dataBasepaths.setEnabled(not checked)
            dlg.label_dataBasepaths.setEnabled(not checked)

        dlg.checkBox_runningOnLinux.toggled.connect(_on_running_on_linux_toggled)
        _on_running_on_linux_toggled(dlg.checkBox_runningOnLinux.isChecked())

        dlg.lineEdit_nBursts.setValidator(QIntValidator(1, 9999, dlg))
        dlg.lineEdit_nBursts.setText(str(int(self.parameters._pulses_per_step)))
        dlg.lineEdit_scanWait.setText(str(self.parameters._step_acq_time))
        dlg.lineEdit_acqTime.setText(str(self.parameters._fly_acq_time))

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
        dlg.checkBox_usOpticsScanSave.setChecked(
            getattr(self.parameters, "_save_us_optics", True)
        )
        dlg.checkBox_opticsPreScanCheck.setChecked(
            getattr(self.parameters, "_check_optics_prescan", True)
        )
        # Sample alignment workflow mode. Lives in QSettings rather than the
        # scan .ini because it is a per-operator UI preference, not a scan
        # parameter, and it is mirrored by the "Expert User" checkbox on the
        # alignment window's first page -- both are views of the one setting.
        from gui.sample_alignment import EXPERT_KEY, confirm_beamline_staff

        dlg.checkBox_expertMode.setChecked(
            QSettings("ptychoSAXS", "ptychoSAXS").value(
                EXPERT_KEY, False, type=bool
            )
        )

        def _on_expert_toggled(checked):
            # Same gate as the alignment window's own checkbox.
            if checked and not confirm_beamline_staff(dlg):
                dlg.checkBox_expertMode.blockSignals(True)
                dlg.checkBox_expertMode.setChecked(False)
                dlg.checkBox_expertMode.blockSignals(False)

        dlg.checkBox_expertMode.toggled.connect(_on_expert_toggled)

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

        # Browse button — pick a log file path without closing the dialog.
        # Logging only ever appends to this file (see write_scaninfo_to_logfile),
        # so picking an existing filename should not warn about overwriting it.
        def _browse_logfile():
            path, _ = QFileDialog.getSaveFileName(
                dlg,
                "Select Log File",
                dlg.lineEdit_logFname.text(),
                "Text files (*.txt);;All files (*)",
                options=QFileDialog.DontConfirmOverwrite,
            )
            if path:
                dlg.lineEdit_logFname.setText(path)

        dlg.pushButton_logFname.clicked.connect(_browse_logfile)

        # Detector attribute/layout buttons — these act immediately (independent
        # of the dialog's OK/Cancel), so they are wired to their own handlers.
        dlg.pushButton_setLayout.clicked.connect(
            lambda: self._set_attr_filepaths_dialog(dlg)
        )
        dlg.pushButton_setPtychoLayout.clicked.connect(
            lambda: self._apply_detector_layout("ptycho")
        )
        dlg.pushButton_setSAXSLayout.clicked.connect(
            lambda: self._apply_detector_layout("saxs")
        )
        dlg.pushButton_checkLayout.clicked.connect(self._check_detector_layout)

        # ── Apply settings only on OK ──────────────────────────────────────
        _setup_result = dlg.exec_()
        QSettings("ptychoSAXS", "ptychoSAXS").setValue(
            "setupWindow/geometry", dlg.saveGeometry()
        )
        if _setup_result != QDialog.Accepted:
            return

        # Log filename
        logfname = dlg.lineEdit_logFname.text()
        if logfname != self.parameters.logfilename:
            self.parameters.logfilename = logfname
            self.parameters.scan_number = 0
            self.write_scaninfo_to_logfile(["#I logging started on", _time.ctime()])

        # Data base paths
        self.parameters.base_linux_datafolder = dlg.lineEdit_dataBasepaths.text()

        # Which OS the GUI is on. Applied before the detector selections below,
        # so anything that pushes a FilePath on the way through already uses
        # the new interpretation of the Working folder.
        self.set_running_on_linux(dlg.checkBox_runningOnLinux.isChecked())

        # Numeric fields
        try:
            self.parameters._pulses_per_step = int(dlg.lineEdit_nBursts.text())
        except ValueError:
            pass
        try:
            self.parameters._step_acq_time = float(
                dlg.lineEdit_scanWait.text()
            )
        except ValueError:
            pass
        try:
            acq_time = float(dlg.lineEdit_acqTime.text())
            acq_time = max(acq_time, self.scan_handler.OVERHEAD_FLY)
            self.parameters._fly_acq_time = acq_time
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
        # Stored as 0/1, not a bool: the ini round-trips numbers reliably,
        # whereas a False written as the text "False" would read back truthy.
        self.parameters._save_us_optics = int(
            dlg.checkBox_usOpticsScanSave.isChecked()
        )
        self.parameters._check_optics_prescan = int(
            dlg.checkBox_opticsPreScanCheck.isChecked()
        )

        # Sample alignment expert mode. Only takes effect the next time the
        # alignment workflow is started, so toggling it mid-alignment never
        # reshuffles pages underneath the operator.
        QSettings("ptychoSAXS", "ptychoSAXS").setValue(
            EXPERT_KEY, dlg.checkBox_expertMode.isChecked()
        )
        if getattr(self, "sample_alignment_window", None) is not None:
            self.sample_alignment_window.refresh_expert_mode()

        # Softglue collection speed
        speed_id = btn_group.checkedId()
        if speed_id in (1, 2, 3):
            self.set_softglue_in(speed_id)

        self.parameters.writeini()
        self.scan_handler.update_scan_estimate()

    # ── Detector attribute / layout filepaths ──────────────────────────────

    @staticmethod
    def _attr_filepaths():
        """Return the (ptycho, saxs) attribute filepaths persisted between sessions."""
        s = QSettings("ptychoSAXS", "ptychoSAXS")
        return (
            s.value("layout/ptychoAttributesPath", "", type=str),
            s.value("layout/saxsAttributesPath", "", type=str),
        )

    @staticmethod
    def _layout_path_from_attributes(attr_path):
        """Derive the layout filepath from an attributes filepath.

        /path/to/attributes_suffix.xml  ->  /path/to/layout_suffix.xml

        Only the first 'attributes' in the basename is replaced, so a parent
        directory named 'attributes' is left untouched. Forward slashes are
        preserved (these are remote Linux paths on the detector IOC).
        """
        idx = attr_path.rfind("/")
        dirname = attr_path[: idx + 1]
        basename = attr_path[idx + 1 :]
        return dirname + basename.replace("attributes", "layout", 1)

    def _set_attr_filepaths_dialog(self, parent=None):
        """pushButton_setLayout: edit and persist the two attribute filepaths."""
        ptycho_path, saxs_path = self._attr_filepaths()

        dlg = QDialog(parent or self.ui)
        dlg.setWindowTitle("Set Attribute Filepaths")
        layout = QFormLayout(dlg)
        ed_ptycho = QLineEdit(ptycho_path, dlg)
        ed_saxs = QLineEdit(saxs_path, dlg)
        ed_ptycho.setMinimumWidth(400)
        ed_saxs.setMinimumWidth(400)
        layout.addRow("Ptychography attributes filepath", ed_ptycho)
        layout.addRow("SAXS attributes filepath", ed_saxs)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
        layout.addWidget(bb)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)

        if dlg.exec_() != QDialog.Accepted:
            return
        s = QSettings("ptychoSAXS", "ptychoSAXS")
        s.setValue("layout/ptychoAttributesPath", ed_ptycho.text().strip())
        s.setValue("layout/saxsAttributesPath", ed_saxs.text().strip())

    def _apply_detector_layout(self, which):
        """pushButton_setPtychoLayout / pushButton_setSAXSLayout.

        Push the (ptycho|saxs) attributes filepath to the NDAttributes PV and
        the derived layout filepath to the HDF1 XML PV.
        """
        ptycho_path, saxs_path = self._attr_filepaths()
        attr_path = ptycho_path if which == "ptycho" else saxs_path
        if not attr_path:
            QMessageBox.warning(
                self.ui,
                "Set Layout",
                "No %s attributes filepath set.\nUse 'Set Layout' to configure it first."
                % which,
            )
            return
        layout_path = self._layout_path_from_attributes(attr_path)

        if self.DEBUG_DEVICES:
            print("[DEBUG] would set %s = %s" % (PV_PILATUS_ATTRIBUTES, attr_path))
            print("[DEBUG] would set %s = %s" % (PV_PILATUS_LAYOUT, layout_path))
            return

        import epics

        epics.caput(PV_PILATUS_ATTRIBUTES, attr_path)
        epics.caput(PV_PILATUS_LAYOUT, layout_path)
        print("Set %s layout:" % which)
        print("  %s = %s" % (PV_PILATUS_ATTRIBUTES, attr_path))
        print("  %s = %s" % (PV_PILATUS_LAYOUT, layout_path))

    def _check_detector_layout(self):
        """pushButton_checkLayout: print the current attributes and layout PVs."""
        if self.DEBUG_DEVICES:
            print("[DEBUG] PV reads unavailable in debug mode.")
            return

        import epics

        attr = epics.caget(PV_PILATUS_ATTRIBUTES, as_string=True)
        layout = epics.caget(PV_PILATUS_LAYOUT, as_string=True)
        print("Current detector layout PVs:")
        print("  %s = %s" % (PV_PILATUS_ATTRIBUTES, attr))
        print("  %s = %s" % (PV_PILATUS_LAYOUT, layout))

    # ── Scan status label ──────────────────────────────────────────────────

    def set_scan_status(self, status: str) -> None:
        """Update label_scan_status text and background.

        status: "Scanning"  → green  #C6EFCE
                "Stopping"  → orange #FFA500
                "Stopped"   → amber  #FFD700
                "Scan Error"→ red    rgb(255, 0, 0)
                "No Scan"   → yellow #fffde7  (default/idle)
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
        elif status == "Stopping":
            lbl.setText("Stopping\u2026")
            lbl.setStyleSheet("background-color: #FFA500; color: #000000")
        elif status == "Stopped":
            lbl.setText("Stopped")
            lbl.setStyleSheet("background-color: #FFD700; color: #000000")
        elif status == "Scan Error":
            lbl.setText("Scan Error")
            lbl.setStyleSheet("background-color: rgb(255, 0, 0); color: #FFFFFF")
        else:
            lbl.setText("Not Scanning")
            lbl.setStyleSheet("background-color: #fffde7; color: #000000")

    def _on_error_message(self, msg: str) -> None:
        if msg:
            QMetaObject.invokeMethod(
                self,
                "_apply_error_status",
                Qt.QueuedConnection,
                Q_ARG(str, msg),
            )

    @pyqtSlot(str)
    def _on_worker_error(self, traceback_str: str) -> None:
        print(traceback_str)
        self._apply_error_status(traceback_str)

    @pyqtSlot(str)
    def _apply_error_status(self, msg: str) -> None:
        lbl = self.ui.findChild(QLabel, "label_scan_status")
        if lbl is None:
            return
        lbl.setText("Error...")
        lbl.setStyleSheet("background-color: rgb(200, 0, 0); color: #FFFFFF")
        escaped = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        lbl.setToolTip(f"<pre style='white-space:pre; color: black;'>{escaped}</pre>")

    def reset_error_status(self) -> None:
        lbl = self.ui.findChild(QLabel, "label_scan_status")
        if lbl is None:
            return
        lbl.setText("Not Scanning")
        lbl.setStyleSheet("background-color: #fffde7; color: #000000")
        lbl.setToolTip("")

    # ── Qt signal slots ────────────────────────────────────────────────────
    # Received from the UDP server (gui/network/server.py).

    @QtCore.pyqtSlot(dict)
    def run_json(self, json_message):
        return self.scan_handler.run_json(json_message)


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
            protocol.jsonReceived.connect(main_panel.run_json)
        loop.run_forever()


if __name__ == "__main__":
    # Server option included
    main()
