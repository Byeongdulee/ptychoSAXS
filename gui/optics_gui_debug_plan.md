# Debug Mode Plan for `optics_motors.py`

## Overview

Add a `--debug_mode` CLI flag that launches the optics motor GUI without connecting to
any real hardware (EPICS motors or PVs). The GUI renders identically to a normal run;
all buttons and position displays are functional using in-memory fake values. No normal
code path is altered.

Triggered by:

```
python optics_motors.py --debug_mode
```

---

## How the GUI currently uses hardware

`motor_control.__init__` has four distinct hardware touchpoints:

| # | Location | What it touches |
|---|----------|-----------------|
| 1 | Lines 10, 119–125 | `epics.PV` — reads `usxRIO:Galil2Bo0_STATUS.VAL` to set initial xrayeye menu state |
| 2 | Lines 23–27, 44–49 | Imports and instantiates `opticsbox`, `OSA`, `camera`, `beamstop`, `slit` from `optics` |
| 3 | Lines 54–82 | Iterates `controller.motors` to read `.DESC`/`.EGU` (epics motors) or `.name`/`.units` (slits) |
| 4 | Lines 100–104 | Calls `controller.get_pos(axisname)` for the initial position display |

Additional hardware calls happen at runtime:

- `updatepos` — calls `controller.get_pos(axis)` on a 100 ms QTimer
- `mv` / `mvr` — calls `controller.mv()` / `controller.mvr()` with `wait=False`
- `stop` — calls `controller.stop(axis)`
- `reset` — calls `controller.set_pos(axis, val)`
- `put_xrayeye` — creates `PV("usxRIO:Galil2Bo0_CMD")` and calls `.put()`

---

## What stubs are needed

### 1. `FakePV`

Replaces `epics.PV`. `get()` returns `0` (xrayeye-out state); `put()` just prints.

```python
class FakePV:
    def __init__(self, pvname, *args, **kwargs):
        self._pvname = pvname
    def get(self):
        print(f"[DEBUG] FakePV.get({self._pvname!r}) -> 0")
        return 0
    def put(self, val):
        print(f"[DEBUG] FakePV.put({self._pvname!r}, {val})")
```

### 2. `FakeMotorRecord`

Mimics a single `epics.Motor` object (the items stored in `epicsmotor.motors`). The
`opticsbox`/`OSA`/`camera`/`beamstop` loop reads `.DESC` and `.EGU` from each motor.

```python
class FakeMotorRecord:
    def __init__(self, desc, egu):
        self.DESC = desc
        self.EGU  = egu
```

### 3. `DebugEpicsMotorController`

Base class for `opticsbox`, `OSA`, `camera`, `beamstop`. Holds an in-memory position
dict and a `motors` list of `FakeMotorRecord` objects.

Required public interface (matches `epicsmotor`):

| Attribute / Method | Used by |
|--------------------|---------|
| `motors` (list of `FakeMotorRecord`) | `__init__` iteration |
| `motornames` (list of str) | `controller.motornames[idx]` in mv/mvr/stop/updatepos |
| `motorunits` (list of str) | `controller.motorunits[idx]` in mv/mvr |
| `get_pos(axis: str) -> float` | `__init__`, `updatepos` |
| `mv(axis, target, wait=False)` | `mv` method |
| `mvr(axis, delta, wait=False)` | `mvr` method |
| `stop(axis)` | `stop` method |
| `set_pos(axis, pos)` | `reset` method |

### 4. Concrete debug controller subclasses

Each mirrors the real class's motor count and sensible default names/units:

| Class | Motors | Names | Units |
|-------|--------|-------|-------|
| `DebugOpticsbox` | 5 | Optics 1–5 | mm ×5 |
| `DebugOSA` | 3 | OSA Z, OSA X, OSA Y | mm ×3 |
| `DebugCamera` | 1 | Camera | mm |
| `DebugBeamstop` | 2 | BS X, BS Y | mm ×2 |

### 5. `FakeSlitMotor`

Mimics the `slit` object from `epics.py`. The slit loop reads `.name` and `.units`
(not `.DESC`/`.EGU`).

```python
class FakeSlitMotor:
    def __init__(self, name, units):
        self.name  = name
        self.units = units
```

### 6. `DebugSlit`

Mimics `epicsslit`. Holds two `FakeSlitMotor` objects ("H slit" / "V slit") and an
in-memory position dict. Same public interface as `DebugEpicsMotorController`.

---

## Implementation steps

### Step 1 — Add `--debug_mode` argument in `main()`

```python
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug_mode", action="store_true",
                        help="Run without connecting to motors or EPICS PVs")
    args, _ = parser.parse_known_args()   # parse_known_args so Qt args are ignored

    app = QApplication(sys.argv)
    motor_panel = motor_control(debug_mode=args.debug_mode)
    sys.exit(app.exec_())
```

`parse_known_args` is used so that any Qt-specific arguments in `sys.argv` do not
cause argparse to fail.

### Step 2 — Thread `debug_mode` into `motor_control.__init__`

Change the signature:

```python
def __init__(self, debug_mode=False):
    super().__init__()
    self.debug_mode = debug_mode
    ...
```

### Step 3 — Add stub classes to `debug_stubs.py`

Add `FakePV`, `FakeMotorRecord`, `DebugEpicsMotorController`, `DebugOpticsbox`,
`DebugOSA`, `DebugCamera`, `DebugBeamstop`, `FakeSlitMotor`, and `DebugSlit` to the
existing `ptychosaxs/debug_stubs.py` file. This keeps all stub code in one place,
consistent with how the main GUI (`rungui.py`) already uses the file.

### Step 4 — Conditional hardware construction in `__init__`

Replace the current unconditional instantiation block:

```python
# BEFORE (lines 44–49)
self.control["opticsbox"] = opticsbox()
self.control["OSA"]       = OSA()
self.control["camera"]    = camera()
self.control["beamstop"]  = beamstop()
self.control["slit"]      = slit()
```

With:

```python
if self.debug_mode:
    from ptychosaxs.debug_stubs import (
        DebugOpticsbox, DebugOSA, DebugCamera, DebugBeamstop, DebugSlit
    )
    self.control["opticsbox"] = DebugOpticsbox()
    self.control["OSA"]       = DebugOSA()
    self.control["camera"]    = DebugCamera()
    self.control["beamstop"]  = DebugBeamstop()
    self.control["slit"]      = DebugSlit()
else:
    self.control["opticsbox"] = opticsbox()
    self.control["OSA"]       = OSA()
    self.control["camera"]    = camera()
    self.control["beamstop"]  = beamstop()
    self.control["slit"]      = slit()
```

### Step 5 — Conditional PV access for xrayeye status

Replace the bare `PV(...)` call (lines 119–125) and the `put_xrayeye` method:

```python
# __init__ — xrayeye menu initial state
if self.debug_mode:
    from ptychosaxs.debug_stubs import FakePV as PV
status = PV("usxRIO:Galil2Bo0_STATUS.VAL")
if status.get() == 0:
    ...
```

And in `put_xrayeye`:

```python
def put_xrayeye(self, ins=True):
    if self.debug_mode:
        from ptychosaxs.debug_stubs import FakePV
        pvs = FakePV("usxRIO:Galil2Bo0_CMD")
    else:
        pvs = PV("usxRIO:Galil2Bo0_CMD")
    pvs.put(1 if ins else 0)
```

### Step 6 — Guard the top-level `epics` import

The `from epics import PV` at line 10 will fail if `epics` is not installed. Wrap it:

```python
try:
    from epics import PV
except ImportError:
    PV = None   # will be replaced by FakePV in debug mode
```

This way, running with `--debug_mode` on a machine without `pyepics` still works.

---

## What is NOT changed

- The `motor_control` class logic for `mv`, `mvr`, `stop`, `reset`, `updatepos`,
  `enable_motors`, `set_ui_enability`, and all menu handlers is untouched.
- The UI file (`motorGUI.ui`) is untouched.
- Normal (non-debug) startup is identical to today.
- The existing stubs in `debug_stubs.py` for hexapod, phi, gonio, Pilatus, etc. are
  untouched.

---

## Files to change

| File | Change |
|------|--------|
| `gui/optics_motors.py` | Add `argparse`, `debug_mode` param, conditional construction, guarded `epics` import |
| `ptychosaxs/debug_stubs.py` | Add `FakePV`, `FakeMotorRecord`, `DebugEpicsMotorController`, `DebugOpticsbox`, `DebugOSA`, `DebugCamera`, `DebugBeamstop`, `FakeSlitMotor`, `DebugSlit` |

No new files are required.
