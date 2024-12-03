from epics import caget, caput, PV

beamlinePV = '12idc:'

try:
	from .ad_pilatus import AD_Pilatus, EIGERMODE, PILATUSMODE
except:
	from ad_pilatus import AD_Pilatus, EIGERMODE, PILATUSMODE
#pil = AD_Pilatus('12idcPIL:')

class pilatus(AD_Pilatus):
	def __init__(self, basename="S12-PILATUS1:"):
		super().__init__(basename)
		self.setNDArrayPort()
#		self.detmode = PILATUSMODE
#		self.dettype = "pilatus"

	def SetNumImages(self, n):
		self.NumImages = n
            #self.NumTriggers = 1
        # if self.detmode == EIGERMODE:
        #     self.NumImages = 1
        #     self.NumTriggers = n

	def wait_trigDone(self):
		while self.Acquire_RBV:
			if self.getCapture()==0:
				if (self.fileGet("AutoSave")==0):
					self.FileWrite()

	def wait_capturedone(self):
		self.CCD_waitCaptureDone()
		if (self.fileGet("AutoSave")==0):
			self.FileWrite()
		self.CCD_waitFileWriting()

	def set_fly_configuration(self):
		self.AutoIncrement = 1
		self.FileNumber = 1
		self.FilePath = '/ramdisk/'
		self.filePut('FilePath', '/ramdisk/')
		self.filePut('AutoIncrement', 1)
		self.filePut('AutoSave', 1)
		self.filePut('FileWriteMode', 1)

	def fly_ready(self, expt, x_points, y_points=1, wait=False, period=0, isTest=False, capture=True):
		Npoints = x_points*y_points
		self.SetExposureTime(expt)
		if period>0:
			self.SetExposurePeriod(period)
		self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')
		self.SetMultiFrames(Npoints, x_points)
		#self.setFileNumber(1)
		if not isTest:
			if capture:
				try:
					self.StartCapture()
					if wait:
						self.wait_capturedone()
				except TimeoutError:
					raise TimeoutError
			else:
				self.Arm()
			
	def set_scanNumberAsfilename(self):
		fw_dir = caget(f"{beamlinePV}data:userDir")
		self.setFilePath(fw_dir)
		self.setFileName('scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))
