# ptychoSAXS Analysis: Non-Debug Mode Readiness

This document examines the current state of the codebase and identifies what may need
fixing before the GUI can operate reliably in non-debug (real hardware) mode.

---

## 1. Debug Mode Architecture (Current State)

Debug mode is activated by `--debug [LEVEL]` or `PTYCHOSAXS_DEBUG=1`.

| Level | Motors | Devices (detectors, scaler, DG645, shutter, SoftGlue) | Data Saved |
|-------|--------|-------------------------------------------------------|------------|
| 0     | STUB   | STUB                                                  | None       |
| 1     | REAL   | STUB                                                  | Motor data only |
| 2     | STUB   | REAL                                                  | Device data only |
| (off) | REAL   | REAL                                                  | All        |

The flags `DEBUG_MOTORS` and `DEBUG_DEVICES` in `rungui.py` control which
subsystems are stubbed.  Each scanning function, detector selector, and
data-saving path checks the appropriate flag.

---

## 2. Unresolved Name References in `scan_handler.py`

**Severity: Will crash in non-debug mode.**

When the handler classes were extracted from `rungui.py` into
`handlers/scan_handler.py`, several names that were previously available as
module-level globals in `rungui.py` were **not imported** into the new module.
These names are now resolved via lazy imports inside `select_detectors()` and
`switch_SGstream()` (fixed as part of the debug-level work), but the following
names are still used bare in the **full fly-scan path** and are NOT imported in
`scan_handler.py`:

| Name | Used at (approx. line) | Source module |
|------|------------------------|---------------|
| `DET_MIN_READOUT_Error` | ~2623, ~2749 | `ptychosaxs.detectors.pilatus` |
| `DET_OVER_READOUT_SPEED_Error` | ~2628 | `ptychosaxs.detectors.pilatus` |
| `DG645_Error` | ~2635, ~2755 | `ptychosaxs.detectors.dg645` |
| `SOFTGLUE_Setup_Error` | ~2649 | `ptychosaxs.detectors.softglue` |

**Impact:** If any of these error conditions triggers during a real fly scan,
Python will raise `NameError` instead of the intended exception.

**Fix:** Add lazy imports before each `raise`, or add conditional imports at the
top of `scan_handler.py`:

```python
# At top of scan_handler.py, after other imports:
try:
    from ptychosaxs.detectors.pilatus import DET_MIN_READOUT_Error, DET_OVER_READOUT_SPEED_Error
    from ptychosaxs.detectors.dg645 import DG645_Error
    from ptychosaxs.detectors.softglue import SOFTGLUE_Setup_Error
except ImportError:
    # Not available in debug mode — these exceptions are only raised
    # in the real-hardware fly-scan path.
    pass
```

---

## 3. Bare Name `s12softglue` in `scan_handler.py`

**Severity: Fixed for `switch_SGstream`; check other usages.**

The module-level object `s12softglue` (created in `rungui.py`) was referenced
as a bare name in `switch_SGstream()`.  This has been fixed to use
`self.w.s12softglue`.  However, **verify there are no other bare references**
to `s12softglue` in the handler files.  The correct pattern everywhere is
`self.w.s12softglue`.

---

## 4. `_rg.Worker` Reference in `status_handler.py`

**Severity: Fixed (was a crash in timescan).**

Line ~263 of `status_handler.py` referenced `_rg.Worker(self.timescan0)` but
`_rg` (alias for `import rungui`) was never imported.  This has been changed to
`self.w.Worker(...)` which uses the `Worker` class exposed on the window object
at `rungui.py` line ~529: `self.Worker = Worker`.

---

## 5. `PilatusStub` Missing Attributes Used in `scandone()`

**Severity: Medium — stubs may need updates for certain code paths.**

The `scandone()` function checks detector attributes like:
- `det.Armed` (property)
- `det.Acquire` (property)
- `det.ArrayCounter_RBV`
- `det.FullFileName_RBV`
- `det.FileNumber_RBV`

The `PilatusStub` in `debug_stubs.py` defines `FileNumber` and `ArrayCounter`
but does NOT define:
- `Armed` (used in `if det.Armed == 1`; would raise `AttributeError`)
- `ArrayCounter_RBV` (used in `is_waiting_detectors_timedout`)
- `FullFileName_RBV` (used in scandone non-hdf path)
- `FileNumber_RBV` (used in scandone non-hdf path)

Currently these paths are skipped when `DEBUG_DEVICES=True` (the `scandone`
device-cleanup block is gated by `not self.w.DEBUG_DEVICES`).  But if stubs
are ever used with real device cleanup paths (e.g., future debug levels), these
attributes should be added to `PilatusStub`:

```python
Armed = 0
ArrayCounter_RBV = 0
FullFileName_RBV = b'/debug/test.tif\x00'
FileNumber_RBV = 1
```

---

## 6. `SCAN_NUMBER_IOC` Is `None` When Devices Are Debug

**Severity: Low — guarded correctly.**

`SCAN_NUMBER_IOC` is set to `None` when `DEBUG_DEVICES=True`.  The
`update_scannumber()` method in `scan_handler.py` checks:
```python
if SCAN_NUMBER_IOC is not None:
    SCAN_NUMBER_IOC.put(int(...))
```
This guard works correctly.  No fix needed.

---

## 7. Network Server Only Available in Full Non-Debug

**Severity: Low — by design.**

The UDP server (`network.server.create_server`) is only imported and started
when `DEBUG_MODE` is entirely `False`.  This means remote JSON commands
(fly2d, stepscan, mv, etc. from external clients) are not available in any
debug level.  If remote-control testing is needed in debug mode, the server
import could be moved to `if not DEBUG_DEVICES:` instead.

---

## 8. QDS Interferometer in Level 2 (Debug Motors, Real Devices)

**Severity: Low — known limitation.**

At debug level 2, motors are stubbed via `InstrumentsStub`.  This stub includes
a `_QDSLevel1` object that returns random values.  Since the real QDS hardware
is bundled inside the `instruments` object, there is no clean way to have real
QDS with stub motors without restructuring the `instruments` class.

At level 2, QDS readings will be random stub values even though `DEBUG_DEVICES`
is `False`.  The `get_qds_pos()` in `status_handler.py` checks
`self.w.DEBUG_DEVICES` to decide the code path, so at level 2 it will attempt
to use `self.w.pts.qds.get_position()` — which is the stub returning random
values.  This is functional but not accurate.

**Potential fix (future):** Extract QDS from the `instruments` class into its own
independently-instantiated object, or allow `InstrumentsStub` to accept a real
QDS instance when `DEBUG_DEVICES=False`.

---

## 9. Hexapod Wavelet / Trajectory Attributes

**Severity: Will crash if fly scan is attempted with real hexapod but stub
setup.**

The fly scan code references several hexapod attributes that are set by
`set_traj()` / `fly_traj()`:
- `self.w.pts.hexapod.pulse_step`
- `self.w.pts.hexapod.pulse_number`
- `self.w.pts.hexapod.pulse_positions_index`
- `self.w.pts.hexapod.WaveGenID`

These are populated by `fly_traj()` which calls `hexapod.set_traj()`.  The
`_HexapodInfo` stub inside `InstrumentsStub` does NOT define these attributes.
When `DEBUG_MOTORS=True` (levels 0, 2), this is fine because the debug scan loop
runs instead of the wavelet path.  But if the code path ever reaches the wavelet
section with a stub hexapod, it will crash.

**Recommendation:** Add stub values to `_HexapodInfo` in `debug_stubs.py`:
```python
pulse_step = 0.01
pulse_number = 10
pulse_positions_index = []
WaveGenID = {'X': 0, 'Y': 1, 'Z': 2}
```

---

## 10. `flydone()` Calls `s12softglue.flush()` Unconditionally

**Severity: Low — stub handles it.**

The `flydone()` method in `scan_handler.py` calls `self.w.s12softglue.flush()`
without checking debug status.  This works because `SGZStub.flush()` is a no-op.
No fix needed, but worth noting for documentation.

---

## 11. `stepscan2d()` / `fly2d()` Call `dg645_12ID.set_pilatus_fly()` Before Debug Check

**Severity: Medium — will fail if DG645 is stub but motors are real.**

In `stepscan2d()` (line ~1856) and `fly()` (line ~1497), the code calls:
```python
self.w.dg645_12ID.set_pilatus_fly(0.001)
```
This happens in the **wrapper** function before the Worker thread is started —
i.e., before the debug motor loop has a chance to take over.  The `DG645Stub`
handles this call as a no-op, so it works.  But if `DEBUG_DEVICES=True`, the
call goes to the stub.  If `DEBUG_DEVICES=False`, it goes to real DG645.  Both
cases are correct.

No fix needed — just documenting the flow.

---

## 12. Data Saving Summary by Debug Level

| What is saved | Level 0 | Level 1 | Level 2 | Non-debug |
|---------------|---------|---------|---------|-----------|
| Motor scan positions (`.npy`) | Yes (stubs) | Yes (real) | Yes (stubs) | Yes (real) |
| INI file | Yes | Yes | Yes | Yes |
| Detector HDF5/TIFF files | No | No | Yes | Yes |
| Struck scaler `.txt` | No | No | Yes | Yes |
| SoftGlue position `.dat` | No | No | Yes | Yes |
| Log file scan entries | No | No | Yes | Yes |
| QDS timescan data | No | No | Stub values | Real values |

The "No" entries for levels 0/1 are because `DEBUG_DEVICES=True` gates the
`scandone()` device cleanup/save block, the Struck save, and the logfile
detector-filename entries.

---

## 13. Checklist for Testing Non-Debug Mode

Before running without `--debug` on the beamline:

1. **Verify all hardware libraries are importable:**
   - `epics`, `pihexapod`, `acspy`, `smaract`, `gclib`
   - Run `python -c "from ptychosaxs import instruments"` to test motor imports
   - Run `python -c "from ptychosaxs.detectors.pilatus import pilatus"` to test detector imports

2. **Fix the unresolved names in `scan_handler.py`** (Section 2 above) —
   add try/except imports for error exception classes.

3. **Test detector selection:** Enable SAXS, WAXS, Struck, SoftGlue via the
   Setup dialog and verify no `NameError` occurs.

4. **Test a 1D step scan end-to-end:** This exercises `stepscan()` ->
   `stepscan0()` -> `scandone()` including detector arm/wait/cleanup.

5. **Test a 1D fly scan:** This exercises the wavelet trajectory path and
   DG645 timing setup.  Verify hexapod `set_traj` populates `pulse_step` etc.

6. **Verify log file writing:** Set a log filename and run a scan. Check that
   `#S`, `#D`, `#I detector_filename` entries are written.

7. **Verify scan number increment:** After each scan, the scan number in the
   INI file and the IOC PV should increment by 1.

---

## 14. Architecture Notes for Future Debugging

- **Handler pattern:** `rungui.py` owns all hardware objects. Handlers access
  them via `self.w.<object>`.  Never `import rungui` from handlers (causes
  re-execution and a second GUI window).

- **Worker threads:** All scan execution happens in `QRunnable` workers on the
  thread pool.  The `Worker` class is exposed as `self.w.Worker` for handlers.

- **Stub contract:** Every stub must implement the same method signatures as
  the real class.  When adding new methods to real hardware classes, add
  corresponding no-ops to the stub.

- **Motor vs. device boundary:** Motors are everything in the `pts`
  (`instruments`) object: hexapod (X/Y/Z/U/V/W), phi, gonio (trans1/trans2/
  tilt1/tilt2).  Devices are everything else: detectors (Pilatus, Dante, XSP3),
  scaler (Struck 3820), timing (DG645, SoftGlue/SGZ), shutter, QDS
  interferometer.  QDS is an edge case — see Section 8.
