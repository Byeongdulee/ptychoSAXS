# Migration Plan: ptychoSAXS_v2 → ptychoSAXS

## Scope constraint
**Only `gui/` and `debug/` are ever modified after step 1.** Everything else is either left alone or replaced wholesale by the user (step 3).

---

## Step 1 — Rename ptychoSAXS_v2 → ptychoSAXS

This is purely a string-substitution pass inside the files we own.

**Files to update:**

### `gui/rungui.py`
- Line 19: update comment `ptychosaxs_v2/debug_stubs.py` → `debug/debug_stubs.py`
- Line 52: `sys.path.append("../ptychosaxs_v2")` → `sys.path.append("../ptychosaxs")`
- Line 58: `from ptychosaxs_v2.debug_stubs import` → `from debug_stubs import`  
  *(after debug/ is on the path — see step 2)*
- Line 172: `from ptychosaxs_v2.ptychosaxs_v2 import instruments` → `from ptychosaxs.ptychosaxs import instruments`  
  *(exact mapping updated in step 4)*
- Lines 177–196: all `ptychosaxs_v2.xxx` → updated in step 4

### `gui/handlers/scan_handler.py`
- All 12 inline imports of `ptychosaxs_v2.debug_stubs` and `ptychosaxs_v2.detectors.*` → updated in step 4

### `gui/optics_motors.py`
- Line 47: `sys.path.append(..., "ptychosaxs_v2")` → removed (replaced by package import in step 4)
- Lines 51–60: `from motors.optics import ...` → `from ptychosaxs.optics import ...` (step 4)

---

## Step 2 — Create `debug/` folder and move debug_stubs.py

**Location:** `debug/` sits at the same level as `gui/`, i.e.:
```
ptychoSAXS_v2/
  gui/
  debug/          ← new folder
    __init__.py   ← empty, makes it a package
    debug_stubs.py
```

**Actions:**
1. Create `debug/__init__.py` (empty).
2. Copy `ptychosaxs_v2/debug_stubs.py` → `debug/debug_stubs.py`.
   - Update the module docstring to reflect new location.
   - No other content changes needed.
3. Update all import sites in `gui/` to use `from debug_stubs import ...` (with `debug/` on sys.path) **or** `from debug.debug_stubs import ...`.
   - `gui/rungui.py`: add `sys.path.append(os.path.join(os.path.dirname(__file__), "..", "debug"))` near the top, then `from debug_stubs import ...`
   - `gui/handlers/scan_handler.py`: same sys.path already inherited from rungui; inline imports become `from debug_stubs import PilatusStub` etc.
   - `gui/optics_motors.py`: add the debug path to sys.path; `from debug_stubs import DebugOpticsbox, ...` and `from debug_stubs import FakePV as _PV`

---

## Step 3 — User manually replaces the backend

The user replaces everything **except** `gui/` and `debug/` with the contents of `Z:\ptychoSAXS\`. After this, the directory layout becomes:

```
ptychoSAXS/           ← repo root (renamed from ptychoSAXS_v2/)
  gui/                ← our files, untouched by user
  debug/              ← our files, untouched by user
  ptychosaxs/         ← original backend (was ptychosaxs_v2/)
    __init__.py
    ptychosaxs.py     ← contains instruments class
    motions_ver2.py
    optics.py
    epicsmotor.py
    ...
  tools/              ← original tools (was ptychosaxs_v2/detectors/ + hardware/ + utils/)
    ad_pilatus.py
    detectors.py      ← pilatus, dante, SGstream, XSP classes
    dg645.py
    softglue.py       ← sgz_pty, SOFTGLUE_Setup_Error
    struck.py
    shutter.py        ← keepshutteropen, keepshopenThread (no shutter class)
    files.py          ← no scp function
    ...
  setup.py
  ...
```

---

## Step 4 — Update gui/ imports to use original backend

This is the main work. Every `ptychosaxs_v2.xxx` import in `gui/` is remapped to the equivalent original location. Below is the full mapping:

### Mapping table

| v2 import | Original equivalent | Notes |
|---|---|---|
| `ptychosaxs_v2.ptychosaxs_v2.instruments` | `ptychosaxs.ptychosaxs.instruments` | same class name |
| `ptychosaxs_v2.detectors.softglue.sgz_pty` | `tools.softglue.sgz_pty` | same class |
| `ptychosaxs_v2.detectors.softglue.SOFTGLUE_Setup_Error` | `tools.softglue.SOFTGLUE_Setup_Error` | same |
| `ptychosaxs_v2.detectors.dg645` (module) | `tools.dg645` | same |
| `ptychosaxs_v2.detectors.dg645.DG645_Error` | `tools.dg645.DG645_Error` | same |
| `ptychosaxs_v2.detectors.struck.struck` | `tools.struck.struck` | same class |
| `ptychosaxs_v2.hardware.shutter.shutter` | *(no shutter class)* — use `tools.shutter.keepshutteropen` / `keepshopenThread` directly | see note A |
| `ptychosaxs_v2.detectors.pilatus.pilatus` | `tools.detectors.pilatus` | same |
| `ptychosaxs_v2.detectors.pilatus.dante` | `tools.detectors.dante` | same |
| `ptychosaxs_v2.detectors.pilatus.SGstream` | `tools.detectors.SGstream` (via `tools.softglue` or `tools.detectors`) | verify class name |
| `ptychosaxs_v2.detectors.pilatus.XSP` | `tools.detectors.XSP` | same |
| `ptychosaxs_v2.detectors.pilatus.DET_MIN_READOUT_Error` | `tools.detectors.DET_MIN_READOUT_Error` | same |
| `ptychosaxs_v2.detectors.pilatus.DET_OVER_READOUT_SPEED_Error` | `tools.detectors.DET_OVER_READOUT_SPEED_Error` | same |
| `ptychosaxs_v2.utils.files.scp` | *(no scp in tools/files.py)* — guard with try/except, disable feature | see note B |
| `motors.optics.*` (optics_motors.py) | `ptychosaxs.optics.*` | same classes/names |
| `ptychosaxs_v2.debug_stubs.*` | `debug_stubs.*` | from debug/ path |

**Note A — shutter:** The v2 `shutter` class wraps shutter open/close EPICS calls. In `rungui.py`, the `shutter` object is used for `.open()` and `.close()`. The original `tools/shutter.py` has `keepshutteropen()` function and `keepshopenThread`. Examine all `.open()` / `.close()` call sites in `gui/` and determine if `keepshutteropen` / `keepshopenThread` covers them, or if a thin adapter is needed (but the adapter would live in `gui/`, not touching the backend).

**Note B — scp:** The `scp` import is guarded under `if not DEBUG_MODE` and used for file transfer. Wrap it in a `try/except ImportError` so the GUI still launches if `scp` doesn't exist.

### sys.path changes in `rungui.py`

Replace:
```python
sys.path.append("..")
sys.path.append("../ptychosaxs_v2")
```
With:
```python
sys.path.append("..")
sys.path.append("../debug")
```
(`ptychosaxs` and `tools` are importable via `..` since they sit next to `gui/`.)

### `gui/rungui.py` hardware import block (lines 171–196)

```python
# Motor-side imports
if not DEBUG_MOTORS:
    from ptychosaxs.ptychosaxs import instruments

# Device-side imports
if not DEBUG_DEVICES:
    import epics
    from tools.softglue import sgz_pty, SOFTGLUE_Setup_Error
    import tools.dg645 as dg645
    from tools.dg645 import DG645_Error
    from tools.struck import struck
    from tools.shutter import keepshutteropen, keepshopenThread  # Note A
    from tools.detectors import (
        pilatus,
        dante,
        SGstream,
        XSP,
        DET_MIN_READOUT_Error,
        DET_OVER_READOUT_SPEED_Error,
    )

if not DEBUG_MODE:
    from network.server import UDPserver, create_server
    try:
        from tools.files import scp  # Note B: may not exist
    except ImportError:
        scp = None
```

### `gui/handlers/scan_handler.py` inline imports (12 occurrences)

Each `from ptychosaxs_v2.debug_stubs import X` → `from debug_stubs import X`  
Each `from ptychosaxs_v2.detectors.pilatus import X` → `from tools.detectors import X`  
Each `from ptychosaxs_v2.detectors.struck import struck` → `from tools.struck import struck`

### `gui/optics_motors.py`

Remove: `sys.path.append(os.path.join(os.path.dirname(__file__), "..", "ptychosaxs_v2"))`  
Add: `sys.path.append(os.path.join(os.path.dirname(__file__), "..", "debug"))`

Change:
```python
from motors.optics import (ptyoptics, opticsbox, OSA, camera, beamstop, slit, slit_CRL, gentry)
```
To:
```python
from ptychosaxs.optics import (ptyoptics, opticsbox, OSA, camera, beamstop, slit, slit_CRL, gentry)
```

Debug stubs imports (`from debug_stubs import ...`) remain the same since debug/ is now on sys.path.

---

## Step 5 — Verify before Git merge (user-triggered)

Do not do this step until the user explicitly asks.

1. Run `python rungui.py --debug` — GUI should open, all debug stubs load correctly.
2. Run `python optics_motors.py --debug_mode` — optics GUI should open.
3. Review git diff — confirm only `gui/` and `debug/` have changes.
4. Commit with message summarising the migration.

---

## Open questions / things to verify during implementation

1. **SGstream location:** Confirm whether `SGstream` class lives in `tools/detectors.py` or `tools/softglue.py` in the original — check before writing the import.
2. **shutter adapter:** After examining all `.open()` / `.close()` call sites on the `shutter` object in `rungui.py` and `scan_handler.py`, determine if a 2-line adapter class inside `gui/rungui.py` is needed (would not touch the backend).
3. **`dg645.ADDRESS_12IDC` and `dg645.dg645_12ID`:** Verify these names exist in `tools/dg645.py` (they appear to be the same based on the file header).
4. **`network/server.py`:** This is inside `gui/network/` and is not touched — just confirm the import still resolves.
