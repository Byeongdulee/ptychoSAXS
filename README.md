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

`optics_motors` and `CRL_3dprint` are clients of the beamline server: each
starts one if none is running, opens no EPICS channels of its own, and
cannot be opened twice — a second launch raises the window that is already
open and exits. They share one server, and each opens only the devices it
needs, so the CRL panel never brings the hexapod up. Whether hardware is
real or simulated is the *server's* choice, so simulate by starting the
server with `--simulate` rather than passing a flag to a panel.

`rungui` has not migrated yet and still takes `--debug` to use the stubs in
`ptychosaxs/debug/`. Its child windows — sample alignment and the optics
status report — migrate with it.

## The beamline server

Hardware is owned by one shared server process, not by each GUI. The first
panel to start spawns it, every panel after that joins it, and it exits when
the last one disconnects. That is what makes two panels safe to run at once:
the PI C-887 accepts exactly one TCP connection, so the old arrangement --
every GUI opening its own -- genuinely conflicted.

The GUIs manage it for you. To drive it directly:

```
python -m ptychosaxs.server --simulate --foreground   # run one by hand
python -m ptychosaxs.server --status                  # what is running
python -m ptychosaxs.server --stop                    # ask it to exit
python -m ptychosaxs.client.cli status                # query it
python -m ptychosaxs.client.cli watch                 # stream its events
```

The socket, lockfile and log live in `$XDG_RUNTIME_DIR/ptychosaxs/`
(`PTYCHOSAXS_RUNTIME_DIR` overrides it). The log is where the device layer's
output goes, including every driver fallback -- read it first when a stage
comes up simulated.

Two rules worth knowing:

* **One of each GUI.** A second copy of a panel that is already open is
  refused, and the existing window is raised instead. Different panels
  running together is fine.
* **A fly scan is never interrupted.** Its trajectory runs in the C-887's
  wave generator and has no host-side stop, so closing the last GUI during
  one leaves the server up until it finishes. A step scan is asked to stop
  and finishes the step it is on.

## MCP

An MCP server over stdio is wired up but exposes no tools yet; tools get
added in `ptychosaxs/mcp/tools.py`. It connects to the beamline server only
when a tool is actually called, so adding it to a host does not power up the
instrument.

```json
{"mcpServers": {"ptychosaxs": {
  "command": "/home/beams/S12IDE/.conda/envs/ptychoSAXS/bin/python",
  "args": ["-m", "ptychosaxs.mcp"]}}}
```

The absolute interpreter path matters: MCP hosts do not run a login shell,
so `conda activate` has not happened.

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

666 tests, no hardware and no display required -- the device layer imports
without Qt and without opening a single connection, which
`tests/test_device_layer_headless.py` enforces with an audit hook. The
server tests start real daemons on a private socket in a tmpdir. Two
failures in `tests/test_sample_alignment_flow.py` are known and
pre-existing.

## Layout

| | |
|---|---|
| `ptychosaxs/api/` | the control surface: session, devices, jobs, events |
| `ptychosaxs/server/` | the shared daemon: registry, dispatch, lifecycle |
| `ptychosaxs/client/` | client library; `client/qt.py` is the only Qt importer |
| `ptychosaxs/link/` | the wire shared by both ends (not named "transport" -- see below) |
| `ptychosaxs/mcp/` | MCP server over stdio, no tools yet |
| `ptychosaxs/` | device layer: motors, hexapod, goniometer, phi, optics, driver-selection policy. No Qt, and no connections at import |
| `ptychosaxs/tools/` | detectors, DG645 timing, scalers, softglue, shutter, file transfer |
| `ptychosaxs/debug/` | simulated stand-ins for every device |
| `ptychosaxs/analysis/` | offline analysis helpers |
| `gui/` | PyQt5 panels; layout in `gui/ui/`, persisted settings in `gui/ini/` |
| `qds_bridge/` | standalone ZMQ bridge for the QDS interferometer. Deliberately walled off: it imports nothing from the rest of the repo and is copied to the Windows box on its own |
| `tests/` | headless unit tests |
