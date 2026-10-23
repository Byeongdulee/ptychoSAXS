"""
debug/debug_stubs.py
==========================
Stub implementations of all hardware classes for debug / no-hardware mode.

Activated by:
    python -m gui.rungui --debug
OR:
    set PTYCHOSAXS_DEBUG=1
    python -m gui.rungui

Each stub:
 - Accepts the same constructor arguments as the real class.
 - Implements the same public interface.
 - Returns plausible fake values (positions start at 0.0, status = idle).
 - Prints what it *would* have done to the console.
 - Never opens a network connection or EPICS channel.

Stubs are NEVER imported in normal operation. Only rungui.py (and
instruments.py when used directly) conditionally imports them via the
DEBUG_MODE check.
"""

import time as _time
import random as _random

from ptychosaxs.hexapod_traj import SimulatedTrajectory


# ---------------------------------------------------------------------------
# Motor stubs
# ---------------------------------------------------------------------------

class PhiStub:
    """Simulates the ACS phi rotation stage."""

    motornames = ["phi"]
    motorunits = ["deg"]

    def __init__(self, *args, **kwargs):
        self._pos = 0.0
        self.axisno = 0
        print("[DEBUG] PhiStub initialised.")

    def connect(self) -> None:
        print("[DEBUG] PhiStub: connect()")

    def disconnect(self) -> None:
        print("[DEBUG] PhiStub: disconnect()")

    def isconnected(self, axis=None) -> bool:
        return True

    def mv(self, target: float, relative: bool = False) -> None:
        if relative:
            self._pos += target
        else:
            self._pos = target
        print(f"[DEBUG] PhiStub: mv({target}, relative={relative}) → {self._pos:.3f} deg")

    def mvr(self, val: float, **kwargs) -> None:
        self.mv(val, relative=True)

    def get_pos(self, axis=0) -> float:
        return round(self._pos, 3)

    def set_pos(self, axis, pos: float = 0) -> None:
        self._pos = pos

    def ismoving(self, axis=None) -> bool:
        return False

    @property
    def in_position(self) -> bool:
        return True

    @property
    def fpos(self) -> float:
        return self._pos

    @property
    def enabled(self) -> bool:
        return True

    def enable(self) -> None:
        pass

    def commutate(self) -> None:
        print("[DEBUG] PhiStub: commutate()")

    def get_speed(self, axis=None):
        return 1.0, 1.0

    def set_speed(self, axis=None, vel: float = 1.0, acc: float = 1.0) -> None:
        print(f"[DEBUG] PhiStub: set_speed(vel={vel}, acc={acc})")

    def ptp(self, target, coordinates="absolute") -> None:
        if coordinates == "relative":
            self._pos += target
        else:
            self._pos = target


class PilatusStub:
    """Simulates the Pilatus 2D X-ray detector. Frames complete instantly."""

    Acquire_RBV = False
    _prefix = 'PILATUS:'
    basepath = '/debug/'

    def __init__(self, *args, **kwargs):
        self._num_frames = 1
        self._exposure = 0.1
        self.ArrayCounter = 0
        print("[DEBUG] PilatusStub initialised.")

    def arm(self) -> None:
        print("[DEBUG] PilatusStub: arm()")

    def Arm(self) -> None:
        self.arm()

    def stop(self) -> None:
        print("[DEBUG] PilatusStub: stop()")

    def ForceStop(self, val=0) -> None:
        self.stop()

    def wait_ready(self) -> None:
        pass

    def wait_done(self) -> None:
        pass

    def CCD_waitCaptureDone(self) -> None:
        pass

    def CCD_waitFileWriting(self) -> None:
        pass

    def wait_capturedone(self) -> None:
        pass

    def wait_trigDone(self) -> None:
        pass

    def set_exposure(self, t: float) -> None:
        self._exposure = t
        print(f"[DEBUG] PilatusStub: set_exposure({t})")

    def SetExposureTime(self, t: float) -> None:
        self.set_exposure(t)

    def SetExposurePeriod(self, t: float) -> None:
        print(f"[DEBUG] PilatusStub: SetExposurePeriod({t})")

    def set_num_frames(self, n: int) -> None:
        self._num_frames = n

    def SetNumImages(self, n: int) -> None:
        self.set_num_frames(n)
        print(f"[DEBUG] PilatusStub: SetNumImages({n})")

    def SetMultiFrames(self, n: int, x: int = 1) -> None:
        print(f"[DEBUG] PilatusStub: SetMultiFrames({n}, {x})")

    def set_fly_configuration(self) -> None:
        print("[DEBUG] PilatusStub: set_fly_configuration()")

    def fly_ready(self, *args, **kwargs) -> None:
        print("[DEBUG] PilatusStub: fly_ready()")

    def step_ready(self, *args, **kwargs) -> None:
        print("[DEBUG] PilatusStub: step_ready()")

    def StartCapture(self) -> None:
        print("[DEBUG] PilatusStub: StartCapture()")

    def StartSingleFrame(self, fn="") -> None:
        print(f"[DEBUG] PilatusStub: StartSingleFrame({fn!r})")

    def setFilePath(self, path: str) -> None:
        print(f"[DEBUG] PilatusStub: setFilePath({path!r})")

    def setFileName(self, name: str) -> None:
        print(f"[DEBUG] PilatusStub: setFileName({name!r})")

    def setArrayCounter(self, n: int) -> None:
        pass

    def getArrayCounter(self) -> int:
        return 1

    def getCapture(self) -> int:
        return 1

    def getNumCaptured(self) -> int:
        return self._num_frames

    def filePut(self, key: str, val) -> None:
        pass

    def fileGet(self, key: str, as_string=False):
        if key == "FileNumber_RBV":
            return 1 if not as_string else "1"
        elif key == "FullFileName_RBV":
            return "/debug/image_0001.h5" if as_string else b"/debug/image_0001.h5"
        elif key == "WriteFile_RBV":
            return 0
        return 1 if not as_string else "1"

    def setNDArrayPort(self) -> None:
        pass

    def setFileTemplate(self, tmpl: str) -> None:
        pass

    def set_scanNumberAsfilename(self) -> None:
        print("[DEBUG] PilatusStub: set_scanNumberAsfilename()")

    def change2alignment_mode(self) -> None:
        print("[DEBUG] PilatusStub: change2alignment_mode()")

    def change2multitrigger_mode(self) -> None:
        print("[DEBUG] PilatusStub: change2multitrigger_mode()")

    def refresh(self) -> int:
        print("[DEBUG] PilatusStub: refresh()")
        return 1

    NumImages = 1
    TriggerMode = 0
    ImageMode = 0
    AutoIncrement = 1
    FileNumber = 1
    FilePath = "/debug/"


class ShutterStub:
    """Simulates the beam shutter."""

    def __init__(self, *args, **kwargs):
        self._open = False
        print("[DEBUG] ShutterStub initialised.")

    def open(self) -> None:
        self._open = True
        print("[DEBUG] ShutterStub: open()")

    def close(self) -> None:
        self._open = False
        print("[DEBUG] ShutterStub: close()")

    def get_status(self) -> str:
        return "open" if self._open else "closed"

    def open_A(self) -> None:
        print("[DEBUG] ShutterStub: open_A()")

    def open_shutterC(self) -> None:
        self.open()

    def close_shutterC(self) -> None:
        self.close()


class DG645Stub:
    """Simulates the DG645 delay generator."""

    def __init__(self, *args, **kwargs):
        print("[DEBUG] DG645Stub initialised.")

    def set_pilatus_fly(self, *args, **kwargs) -> None:
        print("[DEBUG] DG645Stub: set_pilatus_fly()")

    def set_pilatus(self, *args, **kwargs) -> None:
        print("[DEBUG] DG645Stub: set_pilatus()")

    def set_pilatus2(self, *args, **kwargs) -> None:
        print("[DEBUG] DG645Stub: set_pilatus2()")

    def set_pilatus3(self, *args, **kwargs) -> None:
        print("[DEBUG] DG645Stub: set_pilatus3()")

    def check_error(self) -> bool:
        return False

    def burst_init(self) -> None:
        pass

    def burst_set(self, *args, **kwargs) -> None:
        pass

    def disp(self) -> None:
        print("[DEBUG] DG645Stub: disp()")

    def get_status(self) -> str:
        return "OK"


class SGZStub:
    """Simulates the SoftGlue Zynq position/timing system."""

    isConnected = True   # rungui.py checks this attribute on s12softglue

    def __init__(self, *args, **kwargs):
        self._count = 0
        print("[DEBUG] SGZStub initialised.")

    def enable(self) -> None:
        pass

    def disable(self) -> None:
        pass

    def default_clock(self) -> None:
        pass

    def set_clock_in(self, *args) -> None:
        pass

    def set_count_freq(self, *args) -> None:
        pass

    def number_acquisition(self, *args) -> None:
        pass

    def _reset(self) -> None:
        pass

    def ckTime_reset(self) -> None:
        pass

    def memory_clear(self) -> None:
        pass

    def buffer_clear(self) -> None:
        pass

    def get_eventN(self) -> int:
        return 0

    def get_buffN(self) -> int:
        return 0

    def get_time(self) -> list:
        return []

    def get_position(self) -> list:
        self._count += 1
        return [self._count * 1.0]  # simulated encoder count

    def flush(self) -> None:
        pass

    def StartStreaming(self) -> None:
        print("[DEBUG] SGZStub: StartStreaming()")

    def FileCaptureOff(self) -> None:
        pass

    Acquire = 0
    Acquire_RBV = False

    def getArrayCounter(self) -> int:
        return 1

    def getNumCaptured(self) -> int:
        return 1

    def setNDArrayPort(self) -> None:
        pass


class QDSStub:
    """Simulates the QDS interferometer. Returns slowly incrementing positions."""

    def __init__(self, *args, **kwargs):
        self._count = 0
        print("[DEBUG] QDSStub initialised.")

    def get_position(self):
        self._count += 1
        r = [[self._count * 0.001, self._count * 0.002, 0.0]]
        a = None
        return r, a

    def connect(self) -> None:
        print("[DEBUG] QDSStub: connect()")

    def disconnect(self) -> None:
        print("[DEBUG] QDSStub: disconnect()")


class StruckStub:
    """Simulates the Struck 3820 multi-channel scaler."""

    _prefix = '12idc:3820'

    def __init__(self, *args, **kwargs):
        self.ArrayCounter = 0
        print("[DEBUG] StruckStub initialised.")

    def arm(self) -> None:
        print("[DEBUG] StruckStub: arm()")

    def Arm(self) -> None:
        self.arm()

    def stop(self) -> None:
        pass

    def ForceStop(self) -> None:
        pass

    def step_ready(self, *args, **kwargs) -> None:
        pass

    def fly_ready(self, *args, **kwargs) -> None:
        pass

    def read_scaler_all(self) -> list:
        return [0] * 16

    def mcs_init(self) -> None:
        pass

    def arm_mcs(self) -> None:
        pass

    def mcs_wait(self) -> None:
        pass

    def read_mcs(self, channels=None) -> list:
        return [0] * 16

    def read_scaler(self, channels=None) -> list:
        return [0] * 16


class SGStreamStub:
    """Simulates the SGstream detector interface (SoftGlue stream, detector slot 3)."""

    _prefix = '12idSGSocket:'
    basepath = '/debug/'
    Acquire = 0
    Acquire_RBV = False

    def __init__(self, *args, **kwargs):
        self.ArrayCounter = 0
        print("[DEBUG] SGStreamStub initialised.")

    def arm(self) -> None:
        print("[DEBUG] SGStreamStub: arm()")

    def stop(self) -> None:
        pass

    def ForceStop(self, val=0) -> None:
        pass

    def wait_ready(self) -> None:
        pass

    def StartStreaming(self) -> None:
        print("[DEBUG] SGStreamStub: StartStreaming()")

    def FileCaptureOff(self) -> None:
        pass

    def set_fly_configuration(self) -> None:
        pass

    def fly_ready(self, *args, **kwargs) -> None:
        pass

    def step_ready(self, *args, **kwargs) -> None:
        pass

    def filePut(self, key: str, val) -> None:
        pass

    def fileGet(self, key: str, **kwargs):
        return 1

    def setNDArrayPort(self) -> None:
        pass

    def setFilePath(self, path: str) -> None:
        pass

    def setFileName(self, name: str) -> None:
        pass

    def getArrayCounter(self) -> int:
        return 0

    def getNumCaptured(self) -> int:
        return 0

    def flush(self) -> None:
        pass


class BeamStatusStub:
    """Simulates the beam status monitor."""

    def __init__(self, *args, **kwargs):
        print("[DEBUG] BeamStatusStub initialised.")

    def get_status(self) -> str:
        return "beam_on"

    shutter_status = "open"
    beam_current = 100.0


# ===========================================================================
# Level-1 debug stubs — motor positions tracked; QDS returns random values
# ===========================================================================

class _MotorSignals:
    """
    PyQt5 signal container for InstrumentsStub.
    Imported lazily so that PyQt5 is only required when level-1 is active.
    """
    def __new__(cls):
        from PyQt5.QtCore import QObject, pyqtSignal

        class _Signals(QObject):
            AxisNameSignal = pyqtSignal(str)
            AxisPosSignal = pyqtSignal(float)

        return _Signals()


class _HexapodInfo(SimulatedTrajectory):
    """Minimal hexapod attribute bag for InstrumentsStub.

    The trajectory half - set_traj, set_traj_SNAKE2, the pulse arithmetic and
    WaveGenID - is inherited from SimulatedTrajectory rather than copied, so
    the stub and the debug-mode hexapod cannot drift apart. Only the motion
    half is local, because it reads positions from the dict InstrumentsStub
    shares across all its sub-objects.
    """

    axes = ["X", "Y", "Z", "U", "V", "W"]
    motornames = axes
    motorunits = ["mm", "mm", "mm", "deg", "deg", "deg"]

    def __init__(self, pos_dict: dict) -> None:
        super().__init__()
        self._pos = pos_dict

    # The facade's fly-scan predicates. _HexapodInfo stands in for the whole
    # hexapod here, not just its trajectory half, so it has to answer these
    # too - scan_handler asks can_fly before offering a fly scan.
    can_fly = True
    traj_state = SimulatedTrajectory.state
    traj_reason = SimulatedTrajectory.reason

    def is_servo_on(self, axis="") -> bool:
        return True

    def isattarget(self, axis="") -> bool:
        return True

    def ismoving(self, axis) -> bool:
        return False

    def get_pos(self, axis=None):
        if axis is None:
            return {ax: self._pos.get(ax, 0.0) for ax in self.axes}
        return self._pos.get(axis, 0.0)

    def handle_error(self) -> bool:
        return True

    def mv(self, *args, wait=True):
        """Stub for moving axes."""
        print(f"[DEBUG] _HexapodInfo.mv({args}, wait={wait})")
        for axis, target in zip(args[0::2], args[1::2]):
            self._pos[axis] = target
        return True


class _PhiInfo:
    """Minimal phi attribute bag for InstrumentsStub."""
    motornames = ["phi"]
    motorunits = ["deg"]

    def __init__(self, pos_dict: dict) -> None:
        self._pos = pos_dict

    def isconnected(self, axis=None) -> bool:
        return True

    def get_pos(self, axis=0) -> float:
        return self._pos.get("phi", 0.0)

    def ismoving(self, axis=None) -> bool:
        return False


class _GonioInfo:
    """Minimal goniometer attribute bag for InstrumentsStub."""
    motornames = ["trans1", "trans2", "tilt1", "tilt2"]
    motorunits = ["mm", "mm", "deg", "deg"]
    channel_names = motornames
    units = motorunits
    connected = [True, True, True, True]

    def __init__(self, pos_dict: dict) -> None:
        self._pos = pos_dict

    def isconnected(self, ax=-1):
        if ax >= 0:
            return True
        return [True] * len(self.motornames)

    def get_pos(self, axis) -> float:
        name = axis if isinstance(axis, str) else self.motornames[axis]
        return self._pos.get(name, 0.0)

    def set_speed(self, ch, vel=5, acc=None) -> None:
        pass

    def mv_many(self, targets, wait=True, timeout=None) -> bool:
        for axis, target in targets:
            name = axis if isinstance(axis, str) else self.motornames[axis]
            self._pos[name] = float(target)
        print(f"[DEBUG L1] GonioInfo: mv_many({list(targets)})")
        return True

    def ismoving(self, axis) -> bool:
        return False


class _QDSLevel1:
    """
    QDS stub for level 1: returns random values between 0 and 1000.
    After get_qds_pos() divides by 1000, displayed values are 0–1.
    Format matches the real QDS: get_position() -> (r, a)
    where r = [[x, y, z]] (list of one list of three floats).
    """

    def get_position(self):
        vals = [
            _random.uniform(0, 1000),
            _random.uniform(0, 1000),
            _random.uniform(0, 1000),
        ]
        return [vals], None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


class InstrumentsStub:
    """
    Level-1 debug stub for the ``instruments`` / ``pts`` object.

    Motors start at 0.0 and respond correctly to mv() and mvr() — the GUI
    position displays and tweak buttons work as they would with real hardware.
    QDS returns random values in 0–1 range (after the /1000 in get_qds_pos).

    Motor names match the real system so the motor panel populates correctly.
    """

    motornames = ["X", "Y", "Z", "U", "V", "W", "phi",
                  "trans1", "trans2", "tilt1", "tilt2"]
    motorunits = ["mm", "mm", "mm", "deg", "deg", "deg", "deg",
                  "mm", "mm", "deg", "deg"]

    def __init__(self) -> None:
        self._pos = {name: 0.0 for name in self.motornames}
        self.signals = _MotorSignals()
        self.qds = _QDSLevel1()
        self.hexapod = _HexapodInfo(self._pos)
        self.phi = _PhiInfo(self._pos)
        self.gonio = _GonioInfo(self._pos)
        print("[DEBUG L1] InstrumentsStub initialised - all motors at 0.0")

    def mv(self, axis, pos, wait=True) -> None:
        if axis in self._pos:
            self._pos[axis] = float(pos)
            self.signals.AxisNameSignal.emit(str(axis))
            self.signals.AxisPosSignal.emit(float(pos))
            print(f"[DEBUG L1] mv({axis!r}, {float(pos):.6f})")

    def mvr(self, axis, delta, wait=True) -> None:
        if axis in self._pos:
            self._pos[axis] += float(delta)
            new_pos = self._pos[axis]
            self.signals.AxisNameSignal.emit(str(axis))
            self.signals.AxisPosSignal.emit(new_pos)
            print(f"[DEBUG L1] mvr({axis!r}, {float(delta):.6f}) -> {new_pos:.6f}")

    def can_coordinate(self, xaxis, yaxis) -> bool:
        """Every axis here is simulated by the same object, so a pair can
        always be "started together"."""
        return True

    def mv_xy(self, xaxis, xp, yaxis, yp, wait=True, timeout=None) -> bool:
        """Move a scan's two grid axes to one point.

        The 2-D step scan calls this for every point, so without it the
        scan's own `except Exception` would swallow an AttributeError and
        the stage would silently never move - every point recording the
        same position.
        """
        self.mv(xaxis, xp, wait=wait)
        self.mv(yaxis, yp, wait=wait)
        return True

    def controller_for(self, axis):
        return self

    def get_pos(self, axis) -> float:
        return self._pos.get(axis, 0.0)

    def set_pos(self, axis, pos=0) -> None:
        if axis in self._pos:
            self._pos[axis] = float(pos)
            print(f"[DEBUG L1] set_pos({axis!r}, {pos})")

    def ismoving(self, axis=None) -> bool:
        return False

    def isconnected(self, axis=None) -> bool:
        return True

    def get_speed(self, axis):
        return (1.0, 10.0)

    def set_speed(self, axis, vel=1, acc=1) -> None:
        pass

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


# ===========================================================================
# Optics GUI stubs — used by gui/optics_motors.py --debug_mode
# ===========================================================================

class FakePV:
    """Stub for epics.PV. get() returns 0; put() prints the call.
    Always reports connected - debug mode has no real CA connections to lose."""

    connected = True

    def __init__(self, pvname, *args, **kwargs):
        self._pvname = pvname

    def get(self):
        return 0

    def put(self, val):
        print(f"[DEBUG] FakePV.put({self._pvname!r}, {val})")

    def wait_for_connection(self, timeout=None):
        return True


class FakeMotorRecord:
    """
    Mimics a single epics.Motor object as stored in epicsmotor.motors.
    The optics __init__ loop reads .DESC and .EGU from each entry.
    """

    def __init__(self, desc: str, egu: str):
        self.DESC = desc
        self.EGU = egu


class DebugEpicsMotorController:
    """
    Base debug controller for opticsbox / OSA / camera / beamstop.
    Holds an in-memory position dict and a list of FakeMotorRecord objects
    so the motor_control.__init__ iteration works without hardware.
    """

    def __init__(self, names: list, units: list):
        self.motors = [FakeMotorRecord(n, u) for n, u in zip(names, units)]
        self.motornames = list(names)
        self.motorunits = list(units)
        self._pos = {n: 0.0 for n in names}
        print(f"[DEBUG] {self.__class__.__name__} initialised: {names}")

    def _resolve(self, axis) -> str:
        if isinstance(axis, int):
            return self.motornames[axis]
        return axis

    def get_pos(self, axis) -> float:
        return self._pos.get(self._resolve(axis), 0.0)

    def mv(self, axis, target, wait=False) -> None:
        name = self._resolve(axis)
        self._pos[name] = float(target)
        print(f"[DEBUG] {self.__class__.__name__}.mv({name!r}, {target})")

    def mvr(self, axis, delta, wait=False) -> None:
        name = self._resolve(axis)
        self._pos[name] += float(delta)
        print(f"[DEBUG] {self.__class__.__name__}.mvr({name!r}, {delta}) -> {self._pos[name]:.4f}")

    def stop(self, axis) -> None:
        print(f"[DEBUG] {self.__class__.__name__}.stop({self._resolve(axis)!r})")

    def set_pos(self, axis, pos) -> float:
        name = self._resolve(axis)
        self._pos[name] = float(pos)
        print(f"[DEBUG] {self.__class__.__name__}.set_pos({name!r}, {pos})")
        return self._pos[name]


# The names below are per-axis in the same order as each controller's real PV
# list, so they line up index-for-index with gui/optics_motors.py's MOTORS
# table -- debug output then names the motor the panel thinks it is driving.
class DebugOpticsbox(DebugEpicsMotorController):
    """Debug stub for opticsbox — 5 motors matching the real PV list."""

    def __init__(self):
        super().__init__(
            names=["BS_ver", "BS_hor", "ZP_ver", "ZP_hor", "BSZP_Ztrans"],
            units=["mm", "mm", "mm", "mm", "mm"],
        )


class DebugOSA(DebugEpicsMotorController):
    """Debug stub for OSA — 3 motors (X, Z, Y)."""

    def __init__(self):
        super().__init__(
            names=["OSA_X", "OSA_Z", "OSA_Y"],
            units=["mm", "mm", "mm"],
        )


class DebugCamera(DebugEpicsMotorController):
    """Debug stub for the SAXS detector translation — 1 motor."""

    def __init__(self):
        super().__init__(
            names=["SAXS_Z"],
            units=["mm"],
        )


class DebugSaxsZ(DebugEpicsMotorController):
    """Debug stub for the SAXS detector Z stage (12idcACS1:m1) — 1 motor.

    Separate from DebugCamera, which stubs the optical camera (12idc:m2);
    the optics panel drives this one.
    """

    def __init__(self):
        super().__init__(
            names=["SAXS_Z"],
            units=["mm"],
        )


class DebugBeamstop(DebugEpicsMotorController):
    """Debug stub for the SAXS beamstop — 2 motors (hor, ver)."""

    def __init__(self):
        super().__init__(
            names=["SAXSBS_hor", "SAXSBS_ver"],
            units=["mm", "mm"],
        )


class FakeSlitMotor:
    """
    Mimics the slit object from epics.py.
    The slit iteration in motor_control.__init__ reads .name and .units
    (not .DESC / .EGU like epics motors).
    """

    def __init__(self, name: str, units: str):
        self.name = name
        self.units = units


class DebugSlit:
    """
    Debug stub for epicsslit / slit controller.
    Exposes .motors (list of FakeSlitMotor), .motornames, .motorunits,
    and the same get_pos / mv / mvr / stop / set_pos interface.
    """

    def __init__(self):
        _names = ["H slit", "V slit"]
        _units = ["mm", "mm"]
        self.motors = [FakeSlitMotor(n, u) for n, u in zip(_names, _units)]
        self.motornames = list(_names)
        self.motorunits = list(_units)
        self._pos = {n: 0.0 for n in _names}
        print("[DEBUG] DebugSlit initialised.")

    def _resolve(self, axis) -> str:
        if isinstance(axis, int):
            return self.motornames[axis]
        return axis

    def get_pos(self, axis) -> float:
        return self._pos.get(self._resolve(axis), 0.0)

    def mv(self, axis, target, wait=False) -> None:
        name = self._resolve(axis)
        self._pos[name] = float(target)
        print(f"[DEBUG] DebugSlit.mv({name!r}, {target})")

    def mvr(self, axis, delta, wait=False) -> None:
        name = self._resolve(axis)
        self._pos[name] += float(delta)
        print(f"[DEBUG] DebugSlit.mvr({name!r}, {delta}) -> {self._pos[name]:.4f}")

    def stop(self, axis) -> None:
        print(f"[DEBUG] DebugSlit.stop({self._resolve(axis)!r})")

    def set_pos(self, axis, pos) -> float:
        name = self._resolve(axis)
        self._pos[name] = float(pos)
        print(f"[DEBUG] DebugSlit.set_pos({name!r}, {pos})")
        return self._pos[name]


# ===========================================================================
# CRL_3dprint stubs - used by gui/CRL_3dprint.py --debug_mode
# ===========================================================================

# Field suffixes FakeMotorPV gives motion meaning to. Anything else is stored
# and returned verbatim.
_MOTOR_DRIVE = ".VAL"
_MOTOR_READBACK = ".RBV"
_MOTOR_RELATIVE = ".RLV"
_MOTOR_TWEAK_STEP = ".TWV"
_MOTOR_TWEAK_FORWARD = ".TWF"
_MOTOR_TWEAK_REVERSE = ".TWR"
_MOTOR_VELOCITY = ".VELO"
_MOTOR_SET_VELOCITY = "_vCh.A"
_MOTOR_SET_MODE = ".SET"
_MOTOR_SERVO = ".CNEN"
_MOTOR_SERVO_READBACK = ":SERVO"

# Starting value for any field not written yet. A record whose suffix is
# absent here reads back 0.
_MOTOR_DEFAULTS = {
    _MOTOR_READBACK: 0.0,
    _MOTOR_DRIVE: 0.0,
    ".DMOV": 1,  # never busy - simulated moves complete instantly
    _MOTOR_VELOCITY: 1.0,
    ".ACCL": 1.0,  # seconds from rest to .VELO, as the motor record defines it
    _MOTOR_TWEAK_STEP: 0.001,
    ".SPMG": 3,
    _MOTOR_SET_VELOCITY: 1.0,
    _MOTOR_SET_MODE: 0,  # Use mode: a .VAL write moves rather than redefines
    # Servo on by default. Starting it off would make the hexapod's startup
    # check in rungui.py try to recover from a fault on every debug launch.
    _MOTOR_SERVO: 1,
    _MOTOR_SERVO_READBACK: 1,
    ".MSTA": 0,
    # asynRecord .CNCT: 1 is Connect. Offline, the simulated controller is
    # reachable - an unreachable one is a hardware state, not a debug one.
    ".CNCT": 1,
}

# Engineering unit per record prefix, so .EGU reads back something sensible
# without an IOC. Prefixes not listed here report "mm".
_MOTOR_UNITS = {
    "12ideMCS2:m3": "deg",
    "12ideMCS2:m4": "deg",
    "12ideMCS1:m3": "deg",
    "12ideMCS1:m4": "deg",
    # phi, the tomography rotation.
    "12idePtycho:m1": "deg",
    # Hexapod: X/Y/Z are linear, U/V/W are the rotations.
    "12idbHXP:m4": "deg",
    "12idbHXP:m5": "deg",
    "12idbHXP:m6": "deg",
}

# Every field suffix FakeMotorPV recognises, longest first so a suffix that
# ends with a shorter one still matches as itself. Ties break on the name so
# the order does not depend on set iteration.
_MOTOR_FIELDS = tuple(
    sorted(
        set(_MOTOR_DEFAULTS)
        | {".EGU", _MOTOR_TWEAK_FORWARD, _MOTOR_TWEAK_REVERSE, _MOTOR_RELATIVE},
        key=lambda suffix: (-len(suffix), suffix),
    )
)


class FakeMotorPV:
    """Offline stand-in for epics.PV over a simulated EPICS motor record.

    Instances share one in-memory store keyed by full PV name, so a write to
    '<prefix>.VAL' is visible to a reader of '<prefix>.RBV'. Nothing here
    imports pyepics, opens a socket or broadcasts a CA search - debug mode is
    completely offline.

    Used by injecting it as EpicsMotorController's pv_class, so the real
    controller's move/tweak/limit logic is what runs in debug mode and under
    test, rather than a parallel stub that can drift from it.
    """

    _store = {}

    # Every put in order, as (pvname, value). A store only keeps the latest
    # value per record, which hides sequences: a deferred move writes DEFER 1,
    # then the targets, then DEFER 0, and ends up reading 0 exactly as if it
    # had never been set. Shared like _store, and cleared by reset().
    _history = []

    connected = True

    # EPICS alarm severity, as epics.PV exposes it. 0 is NO_ALARM; a test
    # sets 3 (INVALID) to stand for a record whose device support could not
    # read its hardware, which still has a value but not a trustworthy one.
    severity = 0

    def __init__(self, pvname, *args, **kwargs):
        self.pvname = pvname
        self._prefix, self._field = self._split(pvname)
        self.put_complete = True
        self._store.setdefault(pvname, self._initial())

    @classmethod
    def reset(cls):
        """Drop all simulated records - lets a test start from a clean slate."""
        cls._store = {}
        cls._history = []

    @classmethod
    def history(cls, pvname=None):
        """Puts in order: every one, or just those to a named record."""
        if pvname is None:
            return list(cls._history)
        return [value for name, value in cls._history if name == pvname]

    @staticmethod
    def _split(pvname):
        """Separate a record prefix from its field suffix by matching the
        suffixes we know, longest first - '_vCh.A' is one suffix rather than
        a '_vCh' record with an 'A' field, and a prefix containing '_' or '.'
        stays intact."""
        for suffix in _MOTOR_FIELDS:
            if pvname.endswith(suffix):
                return pvname[: -len(suffix)], suffix
        return pvname, ""

    def _initial(self):
        if self._field == ".EGU":
            return _MOTOR_UNITS.get(self._prefix, "mm")
        return _MOTOR_DEFAULTS.get(self._field, 0)

    def _peer(self, field):
        name = self._prefix + field
        if name not in self._store:
            FakeMotorPV(name)
        return name

    def get(self, *args, **kwargs):
        return self._store[self.pvname]

    @property
    def value(self):
        return self.get()

    def put(self, val, wait=False, timeout=None, use_complete=False, callback=None,
            callback_data=None):
        """Writes land verbatim; the fields that command motion additionally
        move the simulated stage. Moves complete instantly, so .DMOV never
        leaves its done state and put-completion returns straight away.

        Returns 1 like a successful epics.PV.put, and None when the record is
        disconnected - callers distinguish a dropped put by that None.
        """
        if not self.connected:
            return None
        self._store[self.pvname] = val
        self._history.append((self.pvname, val))
        moved = None

        if self._field == _MOTOR_DRIVE:
            # In set mode the record redefines the current position instead of
            # moving; the simulated outcome is the same readback either way.
            moved = float(val)
        elif self._field == _MOTOR_RELATIVE:
            moved = self._position() + float(val)
        elif self._field in (_MOTOR_TWEAK_FORWARD, _MOTOR_TWEAK_REVERSE):
            step = float(self._store[self._peer(_MOTOR_TWEAK_STEP)])
            sign = 1.0 if self._field == _MOTOR_TWEAK_FORWARD else -1.0
            moved = self._position() + sign * step
        elif self._field == _MOTOR_SET_VELOCITY:
            # The IOC forwards the requested velocity to the record's own
            # .VELO, which is where it is read back from.
            self._store[self._peer(_MOTOR_VELOCITY)] = float(val)

        if moved is not None:
            self._store[self._peer(_MOTOR_READBACK)] = moved
            self._store[self._peer(_MOTOR_DRIVE)] = moved

        print(f"[DEBUG] FakeMotorPV.put({self.pvname!r}, {val})")
        self.put_complete = True
        if callback is not None:
            callback(pvname=self.pvname, data=callback_data)
        return 1

    def _position(self) -> float:
        return float(self._store[self._peer(_MOTOR_READBACK)])

    def wait_for_connection(self, timeout=None):
        return True

    def disconnect(self):
        pass
