try:
    from .epicsmotor import epicsmotor, epicsslit
except:
    from epicsmotor import epicsmotor, epicsslit

ALL_MOTORS = ["12idcUC8:m2", "12idc:m14", "12idc:m10", "12idc:m11", "12idc:m12", "12idc:m13", "12ideSFT:m4", "12ideSFT:m5", "12idc:m5", "12idc:m6"]
class ptyoptics(epicsmotor): # all motors
    def __init__(self, pvlist=ALL_MOTORS):
        super().__init__(pvlist)

class opticsbox(epicsmotor): # motors for opticsbox
    def __init__(self, pvlist=["12idc:m10", "12idc:m11", "12idc:m12", "12idc:m13", "12idc:m14"]):
        super().__init__(pvlist)

class OSA(epicsmotor): # motors for OSA (Z, X, Y)
    def __init__(self, pvlist=["12idc:m9", "12idc:m15", "12idc:m16"]):
        super().__init__(pvlist)

class camera(epicsmotor): # motors for optical camera
    def __init__(self, pvlist=["12idc:m2"]):
        super().__init__(pvlist)

class beamstop(epicsmotor): # motors for beamstop
    def __init__(self, pvlist=["12ideSFT:m4", "12ideSFT:m5"]):
        super().__init__(pvlist)

class slit(epicsslit): # motors for gentry
    def __init__(self, pvlist=["12idc:CL_SlitH", "12idc:CL_SlitV"]):
        names = ["H slit", "V slit"]
        units = ["mm", "mm"]
        super().__init__(pvlist, names, units)

class slit_CRL(epicsslit): # slits before CRL
    def __init__(self, pvlist=["usxLAX:m58:c1:m7", "usxLAX:m58:c1:m8"]):
        names = ["CRL slit H", "CRL slit V"]
        units = ["mm", "mm"]
        super().__init__(pvlist, names, units)

class gentry(epicsmotor): # motors for gentry
    def __init__(self, pvlist=["12idc:m5", "12idc:m6"]):
        super().__init__(pvlist)