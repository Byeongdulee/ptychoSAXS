from epics import PV
import time
import sys
harmonics = PV("S12ID:USID:HarmonicValueC")
engset = PV("S12ID:USID:EnergySetC.VAL")
start = PV("S12ID:USID:StartC.VAL")
mono = PV("12ida2:E2P_driveValue.A")
mirrorY = PV("12ida2:table1.Y")

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