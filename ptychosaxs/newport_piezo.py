from .epicsmotor import epicsmotor
class newport(epicsmotor):
    def __init__(self, pvlist=["12idcUC8:m1", "12idcUC8:m2", "12idcUC8:m3"]):
        super().__init__(pvlist)
