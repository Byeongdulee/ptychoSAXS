beamlinePV = "12idc:"
from epics.devices.struck import Struck
import time

class struck(Struck):
	_nonpvs  = ('_prefix', '_pvs', '_delim', '_nchan',
               'clockrate', 'scaler', 'mcas', 'basepath', 'FileNumber')
	def __init__(self, prefix='12idc:'):
		Struck.__init__(self, prefix+'3820:', scaler='%sscaler1' % prefix, nchan=12)
		self.basepath = "/net/micdata/data2/"
		# self.add_pv('%sAcquireMode' % prefix, attr = 'AcquireMode')
		# self.add_pv('%sInputMode' % prefix, attr = 'InputMode')
		# self.add_pv('%sOutputMode' % prefix, attr = 'OutputMode')
		# self.add_pv('%sOutputPolarity' % prefix, attr = 'OutputPolarity')
		# self.add_pv('%sReadAll.SCAN' % prefix, attr = 'SCAN')
		# self.add_pv('%sSoftwareChannelAdvance' % prefix, attr = 'SoftwareChannelAdvance')
	@property
	def Armed(self):
		return self.Acquiring
	
	def getCapture(self):
		return self.Armed
	
	def nextFileNumber(self):
		current_fn = self.FileNumber
		self.FileNumber = current_fn + 1

	def step_ready(self, expt, imagN, **kwargs):
		if 'pulsespershot' in kwargs:
			pulsespershot = kwargs['pulsespershot']
			imagN = imagN * pulsespershot
		TotalMeasurementTime = expt*imagN + 10000
		self.mcs_ready(imagN, TotalMeasurementTime)
		self.Arm()

	def fly_ready(self, expt, Nstep, y_points=1, period=0, isTest=False, capture=(True, 1), wait=False, fn=""):
		TotalMeasurementTime = expt*Nstep*y_points + 10000
		self.mcs_ready(Nstep*y_points, TotalMeasurementTime)
		self.Arm()
	
	def read_mcs(self, ch):
		self.DoReadAll = 1
		if type(ch) == int:
			return self.mcas[ch].VAL
		if type(ch) == list:
			retarr = []
			for n in ch:
				retarr.append(self.mcas[n].VAL)
			return retarr

	def read_scaler_all(self, lastch=8):
		valarr = []
		valarr.append(self.scaler.T)
		valarr.append(self.scaler.S1)
		valarr.append(self.scaler.S2)
		valarr.append(self.scaler.S3)
		valarr.append(self.scaler.S4)
		valarr.append(self.scaler.S5)
		valarr.append(self.scaler.S6)
		valarr.append(self.scaler.S7)
		valarr.append(self.scaler.S8)
		return valarr
		
	def mcs_ready(self, imagN, TotalMeasurementTime):
		self.mcs_init()
		self.NuseAll = imagN
		self.CountOnStart = 1
		self.PresetReal = TotalMeasurementTime

	def Arm(self):
		self.start()
		print("struck started.")

	def Forcestop(self, timeout=5):
		self.stop()
		
	def Stop(self):
		self.stop()

	def mcscounter_getready(self):
		self.scaler.TP = 0.001
		self.scaler.CNT = 1
		time.sleep(0.01)
		self.scaler.CNT = 0
		
	def mcs_init(self):
		self.stop()
		self.ChannelAdvance = 1
		self.scaler.CONT = 0
		#self.SCAN = 2
		self.CountOnStart = 1
		self.Channel1Source = 0
		self.UserLED = 0
		self.Prescale = 1
		self.InputMode = 2
		self.OutputMode = 0
		self.OutputPolarity = 0
		self.EraseAll = 1
		self.StopAll = 1
		if self.PV('AcquireMode').get() == 1: # if scaler mode, then make counter ready
			self.mcscounter_getready()
		return 1

	def arm_mcs(self):
		self.start()
		self.mcs_waitstarted()
		
	def channelAdvance_mcs(self):
		self.SoftwareChannelAdvance = 1
		
	def mcs_wait(self,timeout=0):
		if timeout > 0:
			t_start = time.time()
			while self.Acquiring:
				time.sleep(0.01)
				if (time.time() - t_start) > timeout:
					print("Timeout occurred in mcs_wait")
					break
			
	def mcs_waitstarted(self):
		TIMEOUT = 2.0
		t_start = time.time()
		while not self.Acquiring:
			self.start()
			time.sleep(0.01)
			if (time.time() - t_start) > TIMEOUT:
				print("Timeout occurred in mcs_waitstarted")
				break
			
	def mcs_counter_count(self,expt):
		self.scaler.CountTime(expt)
		self.scaler.Count()
		self.mcs_counter_waitstarted()
		TIMEOUT = expt + 1.0
		t_start = time.time()
		while self.scaler.CNT:
			time.sleep(0.01)
			if (time.time() - t_start) > TIMEOUT:
				print("Timeout occurred in mcs_counter_count")
				return 0
		return 1
		
	def mcs_counter_init(self):
		self.OneShotMode()
		#self.ChannelAdvance = 1
		#self.scaler.CONT = 0
		#self.SCAN = 0
		#self.Prescale = 1
		return 2
		
	def mcs_counter_ready(self,expt):
		self.mcs_counter_init()
		self.EraseAll = 1
		self.scaler.TP = expt + 20

	def arm_mcs_counter(self):
		self.scaler.Count()
		self.mcs_counter_waitstarted()
		time.sleep(0.1)     
		
	def mcs_counter_wait(self,timeExp):
		wait_time = timeExp + 1
		t = time.time()
		crnt_value = self.scaler.S1
		if (crnt_value == 0):
			while (crnt_value == 0) :
				crnt_value = self.scaler.S1
				if (abs(t-time.time())>wait_time):
					raise ValueError("MCS has not been triggered for %f second" % wait_time)
					break
				time.sleep(0.1)
		while (abs(t-time.time())<=wait_time):
			time.sleep(0.01)
		t = time.time()
		while (self.scaler.S1 is not crnt_value):
			crnt_value = self.scaler.S1
			if (abs(t-time.time())>wait_time):
				raise ValueError('NETWORK timeout in mcs_counter_wait')
			time.sleep(0.1)
		self.scaler.CNT = 0

	def mcs_counter_waittime(self,timeExp):
		crnt_value = 0
		wait_timeout = timeExp + 1
		t = time.time()
		while (crnt_value < timeExp*50000000):
			crnt_value = self.scaler.S1
			time.sleep(0.02)
			if (abs(t-time.time())>wait_timeout):
				raise ValueError('Network timeout in mcs_counter_waittime')
		self.scaler.CNT = 0

	def mcs_counter_waitcountingstarted(self):
		crnt_value = self.scaler.S1
		time.sleep(0.01)
		while (self.scaler.S1 is crnt_value):
			crnt_value = self.scaler.S1
			time.sleep(0.01)
			
	def mcs_counter_waitstarted(self):
		TIMEOUT = 2.0
		t_start = time.time()
		while not self.scaler.CNT:
			self.scaler.Count()
			time.sleep(0.01)
			if (time.time() - t_start) > TIMEOUT:
				print("Timeout occurred in mcs_counter_waitstarted")
				break


# # Struck setting
# strkPV = "%s3820:"%beamlinePV
# strk = Struck(strkPV, scaler='%sscaler1'%strkPV, nchan=12)
# strk.add_pv('%sAcquireMode'%strkPV, attr = 'AcquireMode')
# strk.add_pv('%sInputMode'%strkPV, attr = 'InputMode')
# strk.add_pv('%sOutputMode'%strkPV, attr = 'OutputMode')
# strk.add_pv('%sOutputPolarity'%strkPV, attr = 'OutputPolarity')
# strk.add_pv('%sReadAll.SCAN'%strkPV, attr = 'SCAN')
# strk.add_pv('%sSoftwareChannelAdvance'%strkPV, attr = 'SoftwareChannelAdvance')

# def read_mcs(ch):
# 	strk.DoReadAll = 1
# 	if type(ch) == int:
# 		return strk.mcas[ch].VAL
# 	if type(ch) == list:
# 		retarr = []
# 		for n in ch:
# 			retarr.append(strk.mcas[n].VAL)
# 		return retarr

# def read_scaler_all(lastch=8):
# 	valarr = []
# 	valarr.append(strk.scaler.T)
# 	valarr.append(strk.scaler.S1)
# 	valarr.append(strk.scaler.S2)
# 	valarr.append(strk.scaler.S3)
# 	valarr.append(strk.scaler.S4)
# 	valarr.append(strk.scaler.S5)
# 	valarr.append(strk.scaler.S6)
# 	valarr.append(strk.scaler.S7)
# 	valarr.append(strk.scaler.S8)
# 	return valarr
	
# def mcs_ready(imagN, TotalMeasurementTime):
# 	mcs_init()
# 	strk.NuseAll = imagN
# 	strk.CountOnStart = 1
# 	strk.PresetReal = TotalMeasurementTime

# def Arm():
# 	strk.start()

# def Stop():
# 	strk.stop()

# def mcs_getready():
# 	strk.scaler.TP = 0.001
# 	strk.scaler.CNT = 1
# 	time.sleep(0.01)
# 	strk.scaler.CNT = 0
	
# def mcs_init():
# 	strk.stop()
# 	strk.ChannelAdvance = 1
# 	strk.scaler.CONT = 0
# 	strk.SCAN = 2
# 	strk.CountOnStart = 1
# 	strk.Channel1Source = 0
# 	strk.UserLED = 0
# 	strk.Prescale = 1
# 	strk.InputMode = 2
# 	strk.OutputMode = 0
# 	strk.OutputPolarity = 0
# 	strk.EraseAll = 1
# 	strk.StopAll = 1
# 	if strk.AcquireMode == "Scaler":
# 		mcs_getready()
# 	return 1

# def arm_mcs():
# 	strk.start()
# 	mcs_waitstarted()
	
# def channelAdvance_mcs():
# 	strk.SoftwareChannelAdvance = 1
	
# def mcs_wait(timeout=0):
# 	if timeout > 0:
# 		t_start = time.time()
# 		while strk.Acquiring:
# 			time.sleep(0.01)
# 			if (time.time() - t_start) > timeout:
# 				print("Timeout occurred in mcs_wait")
# 				break
		
# def mcs_waitstarted():
# 	TIMEOUT = 2.0
# 	t_start = time.time()
# 	while not strk.Acquiring:
# 		strk.start()
# 		time.sleep(0.01)
# 		if (time.time() - t_start) > TIMEOUT:
# 			print("Timeout occurred in mcs_waitstarted")
# 			break
		
# def mcs_counter_count(expt):
# 	strk.scaler.CountTime(expt)
# 	strk.scaler.Count()
# 	mcs_counter_waitstarted()
# 	TIMEOUT = expt + 1.0
# 	t_start = time.time()
# 	while strk.scaler.CNT:
# 		time.sleep(0.01)
# 		if (time.time() - t_start) > TIMEOUT:
# 			print("Timeout occurred in mcs_counter_count")
# 			return 0
# 	return 1
	
# def mcs_counter_init():
# 	strk.OneShotMode()
# 	#strk.ChannelAdvance = 1
# 	#strk.scaler.CONT = 0
# 	#strk.SCAN = 0
# 	#strk.Prescale = 1
# 	return 2
	
# def mcs_counter_ready(expt):
# 	mcs_counter_init()
# 	strk.EraseAll = 1
# 	strk.scaler.TP = expt + 20

# def arm_mcs_counter():
# 	strk.scaler.Count()
# 	mcs_counter_waitstarted()
# 	time.sleep(0.1)     
	
# def mcs_counter_wait(timeExp):
# 	wait_time = timeExp + 1
# 	t = time.time()
# 	crnt_value = strk.scaler.S1
# 	if (crnt_value == 0):
# 		while (crnt_value == 0) :
# 			crnt_value = strk.scaler.S1
# 			if (abs(t-time.time())>wait_time):
# 				raise ValueError("MCS has not been triggered for %f second" % wait_time)
# 				break
# 			time.sleep(0.1)
# 	while (abs(t-time.time())<=wait_time):
# 		time.sleep(0.01)
# 	t = time.time()
# 	while (strk.scaler.S1 is not crnt_value):
# 		crnt_value = strk.scaler.S1
# 		if (abs(t-time.time())>wait_time):
# 			raise ValueError('NETWORK timeout in mcs_counter_wait')
# 		time.sleep(0.1)
# 	strk.scaler.CNT = 0

# def mcs_counter_waittime(timeExp):
# 	crnt_value = 0
# 	wait_timeout = timeExp + 1
# 	t = time.time()
# 	while (crnt_value < timeExp*50000000):
# 		crnt_value = strk.scaler.S1
# 		time.sleep(0.02)
# 		if (abs(t-time.time())>wait_timeout):
# 			raise ValueError('Network timeout in mcs_counter_waittime')
# 	strk.scaler.CNT = 0

# def mcs_counter_waitcountingstarted():
# 	crnt_value = strk.scaler.S1
# 	time.sleep(0.01)
# 	while (strk.scaler.S1 is crnt_value):
# 		crnt_value = strk.scaler.S1
# 		time.sleep(0.01)
		
# def mcs_counter_waitstarted():
# 	TIMEOUT = 2.0
# 	t_start = time.time()
# 	while not strk.scaler.CNT:
# 		strk.scaler.Count()
# 		time.sleep(0.01)
# 		if (time.time() - t_start) > TIMEOUT:
# 			print("Timeout occurred in mcs_counter_waitstarted")
# 			break
