from threading import Thread
from epics import caget, caput, PV

def keepshutteropen():
	val = caget("PB:12ID:STA_C_SCS_CLSD_PL.VAL")
	if val == 0:
		caput("12ida2:rShtrC:Open", 1)

class keepshopenThread(Thread):
	def run(self):
		while self.needOn:
			keepshutteropen()
	def stop(self):
		self.needOn = False