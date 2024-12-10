import ptychosaxs.tw_galil as gl
from ptychosaxs.epicsmotor import epicsmotor
from epics import PV, caput

# galil motor names = ['Beamstopv','Beamstoph','ZFv','ZFh','Camerav','Camerah','OSAv','OSAh']
#                      [A, B, C, D, E, F, G, H]
galil = gl
slitwidth = PV("12idc:CL_SlitHsize.VAL")
gentry = epicsmotor(["12idc:m5"]) 
def set2align():
    # gentry V, 
    gentry.mv(1, 0, wait=True)
    slitwidth.put(0.8)
    gl.mv('H', 64_000, wait=True)
    gl.mv('A', 500)

def set2exp():
    slitwidth.put(0.4)
    gl.mv('H', 0, wait=True)
    gl.mv('A', 0, wait=True)    
    gentry.mv(1, 25, wait=True)

def det2align():
    caput("S12-PILATUS1:cam1:FilePath", "/disk2")
    caput("S12-PILATUS1:cam1:TriggerMode", 4)
    caput("S12-PILATUS1:cam1:Acquire", 1)

def det2exp():
    caput("S12-PILATUS1:cam1:Acquire", 0)
    caput("S12-PILATUS1:cam1:FilePath", "/ramdisk/Dec_2024/tifs/")
    caput("S12-PILATUS1:cam1:TriggerMode", 0)

# Using the special variable 
import sys
# __name__
if __name__=="__main__":
    if sys.argv[1]=="set2align":
        set2align()
    if sys.argv[1]=="set2exp":
        set2exp()
    if sys.argv[1]=="det2align":
        det2align()
    if sys.argv[1]=="det2exp":
        det2exp()

