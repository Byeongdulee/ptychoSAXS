# ptychoSAXS

Control software for the 12-ID-C ptycho-SAXS instrument: device drivers, scan
logic and the Qt panels that drive them.

## Environment

Use the `ptychoSAXS` conda environment. It is the only one here with
InstrumentKit, which `tools/dg645.py` needs:

```
/home/beams/S12IDE/.conda/envs/ptychoSAXS/bin/python      # Python 3.12
```

Running against a different interpreter is the usual cause of a confusing
`ModuleNotFoundError: No module named 'instruments'` during test collection.

## Install

Editable install, so the working tree is what runs:

```
python -m pip install -e .
```

This puts `ptychosaxs` (with `tools`, `debug` and `analysis` as subpackages)
and `gui` on the path as real packages. Nothing manipulates `sys.path` at import time any more, and no
command has to be run from a particular directory.

## Running the GUIs

From anywhere:

```
python -m gui.rungui                 # main ptycho-SAXS panel
python -m gui.optics_motors          # optics panel
python -m gui.CRL_3dprint            # 3D-printed CRL rig
```

Add `--debug` (`--debug_mode` for the optics and CRL panels) to run with
simulated hardware from `debug/debug_stubs.py` — no EPICS, no network, no
instrument connections.

## Library use

`instruments` aggregates the hexapod, phi and goniometer, and adds the QDS
interferometer:

```python
from ptychosaxs.ptychosaxs import instruments

pts = instruments()
pts.get_pos("X")
pts.mv("X", 0.1)
pts.hexapod.get_pos()
```

Which driver each stage is built on is decided by `ptychosaxs/transport.py`
from the `PTYCHOSAXS_*` environment variables. A stage whose driver cannot be
built falls back to a simulated stand-in and says why, rather than raising, so
a machine that can reach only some of the hardware still comes up.

## Tests

```
python -m pytest
```

510 tests, no hardware and no display required. Two failures in
`tests/test_sample_alignment_flow.py` are known and pre-existing.

## Layout

| | |
|---|---|
| `ptychosaxs/` | device layer: motors, hexapod, goniometer, phi, transport policy |
| `ptychosaxs/tools/` | detectors, DG645 timing, scalers, softglue, shutter, file transfer |
| `ptychosaxs/debug/` | simulated stand-ins for every device |
| `ptychosaxs/analysis/` | offline analysis helpers |
| `gui/` | PyQt5 panels; layout in `gui/ui/`, persisted settings in `gui/ini/` |
| `qds_bridge/` | standalone ZMQ bridge for the QDS interferometer. Deliberately walled off: it imports nothing from the rest of the repo and is copied to the Windows box on its own |
| `tests/` | headless unit tests |
