"""Set the undulator and monochromator energy.

The five PVs used to be created at module scope, so merely importing this
-- which anything walking the package does -- opened Channel Access
connections to the storage ring. They are created on first use instead.
"""

import time
import sys

PV_NAMES = {
    "harmonics": "S12ID:USID:HarmonicValueC",
    "engset": "S12ID:USID:EnergySetC.VAL",
    "start": "S12ID:USID:StartC.VAL",
    "mono": "12ida2:E2P_driveValue.A",
    "mirrorY": "12ida2:table1.Y",
}

_pvs = {}


def _pv(key):
    """The named PV, created on first use and cached."""
    if key not in _pvs:
        from epics import PV
        _pvs[key] = PV(PV_NAMES[key])
    return _pvs[key]


class _LazyPV:
    """Keeps `harmonics.put(1)` reading the way it always did."""

    def __init__(self, key):
        self._key = key

    def put(self, *a, **kw):
        return _pv(self._key).put(*a, **kw)

    def get(self, *a, **kw):
        return _pv(self._key).get(*a, **kw)


harmonics = _LazyPV("harmonics")
engset = _LazyPV("engset")
start = _LazyPV("start")
mono = _LazyPV("mono")
mirrorY = _LazyPV("mirrorY")

def set_energy(energy):
    if energy <= 14.0:
        harmonics.put(1)
        mirrorY.put(0.0)
    else:
        harmonics.put(3)
        mirrorY.put(10.0)
    engset.put(energy)
    mono.put(energy)
    time.sleep(0.5)
    start.put(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m ptychosaxs.energy <energy>")
        sys.exit(1)

    try:
        energy = float(sys.argv[1])
    except ValueError:
        print("Invalid energy value:", sys.argv[1])
        sys.exit(1)

    set_energy(energy)