beamlinePV = "12idc:"
from epics.devices.struck import Struck
import time

# Struck setting
strkPV = "%s3820:"%beamlinePV
strk = Struck(strkPV, scaler='%sscaler1'%strkPV, nchan=12)
strk.add_pv('%sAcquireMode'%strkPV, attr = 'AcquireMode')
strk.add_pv('%sInputMode'%strkPV, attr = 'InputMode')
strk.add_pv('%sOutputMode'%strkPV, attr = 'OutputMode')
strk.add_pv('%sOutputPolarity'%strkPV, attr = 'OutputPolarity')
strk.add_pv('%sReadAll.SCAN'%strkPV, attr = 'SCAN')
strk.add_pv('%sSoftwareChannelAdvance'%strkPV, attr = 'SoftwareChannelAdvance')

def read_mcs(ch):
	strk.DoReadAll = 1
	if type(ch) == int:
		return strk.mcas[ch].VAL
	if type(ch) == list:
		retarr = []
		for n in ch:
			retarr.append(strk.mcas[n].VAL)
		return retarr

def read_scaler_all(lastch=8):
	valarr = []
	valarr.append(strk.scaler.T)
	valarr.append(strk.scaler.S1)
	valarr.append(strk.scaler.S2)
	valarr.append(strk.scaler.S3)
	valarr.append(strk.scaler.S4)
	valarr.append(strk.scaler.S5)
	valarr.append(strk.scaler.S6)
	valarr.append(strk.scaler.S7)
	valarr.append(strk.scaler.S8)
	return valarr
	
def mcs_ready(imagN, TotalMeasurementTime):
	mcs_init()
	strk.NuseAll = imagN
	strk.CountOnStart = 1
	strk.PresetReal = TotalMeasurementTime
	
def mcs_getready():
	strk.scaler.TP = 0.001
	strk.scaler.CNT = 1
	time.sleep(0.01)
	strk.scaler.CNT = 0
	
def mcs_init():
	strk.stop()
	strk.ChannelAdvance = 1
	strk.scaler.CONT = 0
	strk.SCAN = 2
	strk.CountOnStart = 1
	strk.Channel1Source = 0
	strk.UserLED = 0
	strk.Prescale = 1
	strk.InputMode = 2
	strk.OutputMode = 0
	strk.OutputPolarity = 0
	strk.Acquring = 0
	strk.EraseAll = 1
	if strk.AcquireMode == "Scaler":
		mcs_getready()
	return 1

def arm_mcs():
	strk.EraseStart = 1
	mcs_waitstarted()
	
def channelAdvance_mcs():
	strk.SoftwareChannelAdvance = 1
	
def mcs_wait():
	while strk.Acquiring:
		strk.Acquring = 0
		time.sleep(0.01)
		
def mcs_waitstarted():
	TIMEOUT = 2.0
	t_start = time.time()
	while not strk.Acquiring:
		time.sleep(0.01)
		if (time.time() - t_start) > TIMEOUT:
			print("Timeout occurred in mcs_waitstarted")
			break
		
def mcs_counter_count(expt):
	strk.scaler.CountTime(expt)
	strk.scaler.Count()
	mcs_counter_waitstarted()
	TIMEOUT = expt + 1.0
	t_start = time.time()
	while strk.scaler.CNT:
		time.sleep(0.01)
		if (time.time() - t_start) > TIMEOUT:
			print("Timeout occurred in mcs_counter_count")
			break
	
def mcs_counter_init():
	strk.ChannelAdvance = 1
	strk.scaler.CONT = 0
	strk.SCAN = 0
	strk.Prescale = 1
	return 2
	
def mcs_counter_ready(expt):
	mcs_counter_init()
	strk.EraseAll = 1
	strk.scaler.TP = expt + 20

def arm_mcs_counter():
	strk.scaler.CNT = 1
	mcs_counter_waitstarted()
	time.sleep(0.1)     
	
def mcs_counter_wait(timeExp):
	wait_time = timeExp + 1
	t = time.time()
	crnt_value = strk.scaler.S1
	if (crnt_value == 0):
		while (crnt_value == 0) :
			crnt_value = strk.scaler.S1
			if (abs(t-time.time())>wait_time):
				raise ValueError("MCS has not been triggered for %f second" % wait_time)
				break
			time.sleep(0.1)
	while (abs(t-time.time())<=wait_time):
		time.sleep(0.01)
	t = time.time()
	while (strk.scaler.S1 is not crnt_value):
		crnt_value = strk.scaler.S1
		if (abs(t-time.time())>wait_time):
			raise ValueError('NETWORK timeout in mcs_counter_wait')
		time.sleep(0.1)
	strk.scaler.CNT = 0

def mcs_counter_waittime(timeExp):
	crnt_value = 0
	wait_timeout = timeExp + 1
	t = time.time()
	while (crnt_value < timeExp*50000000):
		crnt_value = strk.scaler.S1
		time.sleep(0.02)
		if (abs(t-time.time())>wait_timeout):
			raise ValueError('Network timeout in mcs_counter_waittime')
	strk.scaler.CNT = 0

def mcs_counter_waitcountingstarted():
	crnt_value = strk.scaler.S1
	time.sleep(0.01)
	while (strk.scaler.S1 is crnt_value):
		crnt_value = strk.scaler.S1
		time.sleep(0.01)
		
def mcs_counter_waitstarted():
	TIMEOUT = 2.0
	t_start = time.time()
	while not strk.scaler.CNT:
		time.sleep(0.01)
		if (time.time() - t_start) > TIMEOUT:
			print("Timeout occurred in mcs_counter_waitstarted")
			break
