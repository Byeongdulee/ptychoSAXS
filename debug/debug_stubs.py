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


# ---------------------------------------------------------------------------
# Motor stubs
# ---------------------------------------------------------------------------

class HexapodStub:
    """Simulates the PI hexapod 6-axis stage. All positions start at 0.0."""

    axes = ["X", "Y", "Z", "U", "V", "W"]

    def __init__(self, *args, **kwargs):
        self._pos = {ax: 0.0 for ax in self.axes}
        self.motornames = self.axes
        self.motorunits = ["mm", "mm", "mm", "deg", "deg", "deg"]
        self.connected = [True] * 6
        print("[DEBUG] HexapodStub initialised.")

    def connect(self) -> None:
        print("[DEBUG] HexapodStub: connect()")

    def disconnect(self) -> None:
        print("[DEBUG] HexapodStub: disconnect()")

    def isconnected(self, axis=None) -> bool:
        return True

    def is_servo_on(self, axis) -> bool:
        return True

    def mv(self, axis: str, position: float) -> bool:
        print(f"[DEBUG] HexapodStub: mv({axis!r}, {position})")
        self._pos[axis] = position
        return True

    def mvr(self, axis: str, delta: float) -> None:
        self._pos[axis] += delta
        print(f"[DEBUG] HexapodStub: mvr({axis!r}, {delta}) → {self._pos[axis]:.6f}")

    def get_pos(self) -> dict:
        return dict(self._pos)

    def set_pos(self, axis: str, pos: float = 0) -> None:
        self._pos[axis] = pos

    def ismoving(self, axis: str) -> bool:
        return False

    def isattarget(self, axis: str) -> bool:
        return True

    def get_speed(self):
        return 1.0

    def set_speed(self, vel: float) -> None:
        print(f"[DEBUG] HexapodStub: set_speed({vel})")

    def handle_error(self) -> bool:
        return True

    def run_traj(self) -> None:
        print("[DEBUG] HexapodStub: run_traj()")

    def get_records(self) -> dict:
        return {ax: (0.0, 0.0) for ax in self.axes}


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


class GonioStub:
    """Simulates the SmarAct MCS2 goniometer (module-level functions style)."""

    motornames = ["trans1", "trans2", "tilt1", "tilt2"]
    motorunits = ["mm", "mm", "deg", "deg"]
    channel_names = motornames

    def __init__(self, *args, **kwargs):
        self._pos = {n: 0.0 for n in self.motornames}
        print("[DEBUG] GonioStub initialised.")

    def mv(self, axis, target: float, wait: bool = True) -> None:
        name = axis if isinstance(axis, str) else self.motornames[axis]
        self._pos[name] = target
        print(f"[DEBUG] GonioStub: mv({name!r}, {target})")

    def mvr(self, axis, target: float, wait: bool = True) -> None:
        name = axis if isinstance(axis, str) else self.motornames[axis]
        self._pos[name] += target

    def get_pos(self, axis) -> float:
        name = axis if isinstance(axis, str) else self.motornames[axis]
        return self._pos[name]

    def set_pos(self, axis, pos: float = 0) -> None:
        name = axis if isinstance(axis, str) else self.motornames[axis]
        self._pos[name] = pos

    def ismoving(self, axis=None) -> bool:
        return False

    def isconnected(self, ax=-1):
        if ax >= 0:
            return True
        return [True] * len(self.motornames)

    def get_speed(self, axis):
        return (1.0, 10.0)

    def set_speed(self, axis, vel: float = 5, acc: float = 10) -> None:
        print(f"[DEBUG] GonioStub: set_speed({axis!r}, vel={vel}, acc={acc})")


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

    def fileGet(self, key: str):
        return 1

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


class _HexapodInfo:
    """Minimal hexapod attribute bag for InstrumentsStub."""
    axes = ["X", "Y", "Z", "U", "V", "W"]
    motornames = axes
    motorunits = ["mm", "mm", "mm", "deg", "deg", "deg"]

    def __init__(self, pos_dict: dict) -> None:
        self._pos = pos_dict

    def is_servo_on(self, axis) -> bool:
        return True

    def isattarget(self, axis) -> bool:
        return True

    def ismoving(self, axis) -> bool:
        return False

    def get_pos(self) -> dict:
        return {ax: self._pos.get(ax, 0.0) for ax in self.axes}

    def handle_error(self) -> bool:
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

    def set_speed(self, ch, vel=5, acc=10) -> None:
        pass

    def calibrate(self, ch) -> None:
        print(f"[DEBUG L1] GonioInfo: calibrate({ch})")

    def findReference(self, ch) -> None:
        print(f"[DEBUG L1] GonioInfo: findReference({ch})")

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
    """Stub for epics.PV. get() returns 0; put() prints the call."""

    def __init__(self, pvname, *args, **kwargs):
        self._pvname = pvname

    def get(self):
        print(f"[DEBUG] FakePV.get({self._pvname!r}) -> 0")
        return 0

    def put(self, val):
        print(f"[DEBUG] FakePV.put({self._pvname!r}, {val})")


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


class DebugOpticsbox(DebugEpicsMotorController):
    """Debug stub for opticsbox — 5 motors matching the real PV list."""

    def __init__(self):
        super().__init__(
            names=["Optics 1", "Optics 2", "Optics 3", "Optics 4", "Optics 5"],
            units=["mm", "mm", "mm", "mm", "mm"],
        )


class DebugOSA(DebugEpicsMotorController):
    """Debug stub for OSA — 3 motors (Z, X, Y)."""

    def __init__(self):
        super().__init__(
            names=["OSA Z", "OSA X", "OSA Y"],
            units=["mm", "mm", "mm"],
        )


class DebugCamera(DebugEpicsMotorController):
    """Debug stub for camera — 1 motor."""

    def __init__(self):
        super().__init__(
            names=["Camera"],
            units=["mm"],
        )


class DebugBeamstop(DebugEpicsMotorController):
    """Debug stub for beamstop — 2 motors (X, Y)."""

    def __init__(self):
        super().__init__(
            names=["BS X", "BS Y"],
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
