from epics import caget, caput, PV
import time
beamlinePV = '12idc:'

try:
	from .ad_pilatus import AD_Pilatus, AD_Dante, AD_XSP, AD_SG, EIGERMODE, PILATUSMODE
except:
	from ad_pilatus import AD_Pilatus, AD_Dante, AD_XSP, AD_SG, EIGERMODE, PILATUSMODE
#pil = AD_Pilatus('12idcPIL:')

class DET_MIN_READOUT_Error(Exception):
    pass

class DET_OVER_READOUT_SPEED_Error(Exception):
    pass

class pilatus(AD_Pilatus):
	mode = ""
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

	def fly_ready(self, expt, x_points, y_points=1, wait=False, period=0, isTest=False, capture=(True, 1), fn=""):
		Npoints = x_points*y_points
		self.SetExposureTime(expt)
		if period>0:
			self.SetExposurePeriod(period)
		self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')
#		print("")
#		print(Npoints, x_points)
#		print(capture)
#		print("")
		#if capture[0]: # as long as 
		
		isHDFMode = capture[0]; #use hdf plugin?

		self.SetMultiFrames(Npoints, x_points)
		#self.setFileNumber(1)
		if not isTest:
			if isHDFMode:
				if capture[1]==2: # for SG is in streammode, save all images into a file.
					self.SetMultiFrames(Npoints, Npoints)
				try:
					if capture[1]==0:
						self.StartSingleFrame(fn)
					else:
						self.StartCapture()
						if wait:
							self.wait_capturedone()
				except TimeoutError:
					raise TimeoutError
			else:
				self.Arm()
	
	def step_ready(self, expt, N_image, fn=""):
		self.SetExposureTime(expt)
		self.setArrayCounter(0)
		self.ImageMode = 1  #  multiple image
		self.TriggerMode = 3 # external triger mode
		self.FileNumber = 1

        # number of images for collection and capture
		self.NumImages = N_image

        # set filesaver
		self.filePut('NumCapture',   1)
		self.filePut('FileNumber',    1)
		self.StartSingleFrame(fn=fn) # Arm the detector


	def set_scanNumberAsfilename(self):
		fw_dir = caget(f"{beamlinePV}data:userDir")
		self.setFilePath(fw_dir)
		self.setFileName('scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))

	def change2alignment_mode(self):
		self.filePut('AutoSave', 0)
		self.setArrayCounter(0)
		self.TriggerMode = 4 # external triger mode
	
	def change2multitrigger_mode(self):
		self.filePut('AutoSave', 1)
		self.setArrayCounter(0)
		self.TriggerMode = 3 # external triger mode

	def refresh(self):
		self.Acquire = 0
		self.NumImages = 0
		time.sleep(1)
		self.change2alignment_mode()
		time.sleep(1)
		self.TriggerMode = 0 # internal triger mode
		time.sleep(1)
		self.TriggerMode = 4 # alignment
		time.sleep(10)
		self.Acquire = 1
		time.sleep(1)
		t0 = time.time()
		timeout_trial = 3
		while time.time()-t0 < 20:
			if self.getArrayCounter() == 0:
				time.sleep(0.1)
				self.Acquire = 0
				time.sleep(0.1)
				self.TriggerMode = 0 # internal triger mode
				time.sleep(0.1)
				self.TriggerMode = 4 # alignment
				time.sleep(3)
				self.Acquire = 1
				time.sleep(1)
			if self.getArrayCounter() > 10:
				break
			if timeout_trial > 3:
				return 0 # failed to refresh
			timeout_trial += 1
		self.Acquire = 0
		self.change2multitrigger_mode()
		return 1 # scuccessfully refreshed
		

class dante(AD_Dante):
	mode = ""
	def __init__(self, basename="12idcDAN:"):
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
		while self.MCAAcquiring:
			if self.getCapture()==0:
				if (self.fileGet("AutoSave")==0):
					self.FileWrite()

	def wait_capturedone(self):
		self.CCD_waitCaptureDone()
		if (self.fileGet("AutoSave")==0):
			self.FileWrite()
		self.CCD_waitFileWriting()

	def set_fly_configuration(self):
#		self.AutoIncrement = 1
#		self.FileNumber = 1
#		self.FilePath = '/net/s12data/export/12id-c/2025_Data/2025_3/'
		self.filePut('FilePath', '//net/s12data/export/12id-c/2025_Data/2025_3/')
		self.filePut('AutoIncrement', 1)
		self.filePut('AutoSave', 1)
		self.filePut('FileWriteMode', 1)

	def fly_ready(self, expt, x_points, y_points=1, wait=False, period=0, isTest=False, capture=(True, 1), fn=""):
		Npoints = x_points*y_points
		self.SetExposureTime(expt)
		if period>0:
			self.SetExposurePeriod(period)
		self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')
		
		isHDFMode = capture[0]; #use hdf plugin?

		self.SetMultiFrames(Npoints)
		#if fn != "":
		#	fn = bytes(self.FileName_RBV).decode().strip('\x00')
		if len(fn)>0:
			self.setFileName("%s"%fn)
		#self.setFileNumber(1)
		if not isTest:
			if isHDFMode:
				if capture[1]==2: # for SG is in streammode, save all images into a file.
					self.SetMultiFrames(Npoints)
				try:
					if capture[1]==0:
						self.StartSingleFrame(fn)
					else:
						self.StartCapture()
						if wait:
							self.wait_capturedone()
				except TimeoutError:
					raise TimeoutError
			else:
				self.Arm()
	
	def step_ready(self, expt, N_image, fn=""):
		self.SetExposureTime(expt)
		self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')

		self.SetMultiFrames(N_image)
		if len(fn)>0:
			self.setFileName("%s"%fn)
        # set filesaver
		self.filePut('NumCapture',   1)
		self.filePut('FileNumber',    1)
		self.StartCapture() # Arm the detector
		self.Arm()

	def set_scanNumberAsfilename(self):
		fw_dir = caget(f"{beamlinePV}data:userDir")
		self.setFilePath(fw_dir)
		self.setFileName('scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))

	def change2alignment_mode(self):
		self.filePut('AutoSave', 0)
		self.setArrayCounter(0)
		self.TriggerMode = 4 # external triger mode
	
	def change2multitrigger_mode(self):
		self.filePut('AutoSave', 1)
		self.setArrayCounter(0)
		self.TriggerMode = 3 # external triger mode

	def refresh(self):
		self.Acquire = 0
		self.NumImages = 0
		time.sleep(1)
		self.change2alignment_mode()
		time.sleep(1)
		self.TriggerMode = 0 # internal triger mode
		time.sleep(1)
		self.TriggerMode = 3 # external triger mode
		time.sleep(10)
		self.Acquire = 1
		time.sleep(1)
		t0 = time.time()
		timeout_trial = 3
		while time.time()-t0 < 20:
			if self.getArrayCounter() == 0:
				time.sleep(0.1)
				self.Acquire = 0
				time.sleep(0.1)
				self.TriggerMode = 0 # internal triger mode
				time.sleep(0.1)
				self.TriggerMode = 3 # external triger mode
				time.sleep(3)
				self.Acquire = 1
				time.sleep(1)
			if self.getArrayCounter() > 10:
				break
			if timeout_trial > 3:
				return 0 # failed to refresh
			timeout_trial += 1
		self.Acquire = 0
		self.change2multitrigger_mode()
		return 1 # scuccessfully refreshed
		
class XSP(AD_XSP):
	mode = ""
	def __init__(self, basename="XSP3_4Chan:"):
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
#		self.AutoIncrement = 1
#		self.FileNumber = 1
		self.filePut('FilePath', '//net/s12data/export/12id-c/2025_Data/2025_3/')
		self.filePut('AutoIncrement', 1)
		self.filePut('AutoSave', 1)
		self.filePut('FileWriteMode', 1)

	def fly_ready(self, expt, x_points, y_points=1, wait=False, period=0, isTest=False, capture=(True, 1), fn=""):
		Npoints = x_points*y_points
		self.SetExposureTime(expt)
		if period>0:
			self.SetExposurePeriod(period)
		self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')
		
		isHDFMode = capture[0]; #use hdf plugin?

		self.SetMultiFrames(Npoints)
		#if fn != "":
		#	fn = bytes(self.FileName_RBV).decode().strip('\x00')
		if len(fn)>0:
			self.setFileName("%s"%fn)
		#self.setFileNumber(1)
		if not isTest:
			if isHDFMode:
				if capture[1]==2: # for SG is in streammode, save all images into a file.
					self.SetMultiFrames(Npoints)
				try:
					if capture[1]==0:
						self.StartSingleFrame(fn)
					else:
						self.StartCapture()
						if wait:
							self.wait_capturedone()
				except TimeoutError:
					raise TimeoutError
			else:
				self.Arm()
	
	def step_ready(self, expt, N_image, fn=""):
		self.SetExposureTime(expt)
		self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')

		self.SetMultiFrames(N_image)
		if len(fn)>0:
			self.setFileName("%s"%fn)
        # set filesaver
		self.filePut('NumCapture',   1)
		self.filePut('FileNumber',    1)
		self.StartCapture() # Arm the detector
		self.Arm()

	def set_scanNumberAsfilename(self):
		fw_dir = caget(f"{beamlinePV}data:userDir")
		self.setFilePath(fw_dir)
		self.setFileName('scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))

	def change2alignment_mode(self):
		self.filePut('AutoSave', 0)
		self.setArrayCounter(0)
		self.TriggerMode = 0 # software
	
	def change2multitrigger_mode(self):
		self.filePut('AutoSave', 1)
		self.setArrayCounter(0)
		self.TriggerMode = 3 # external triger mode

	def refresh(self):
		self.Acquire = 0
		self.NumImages = 0
		time.sleep(1)
		self.change2alignment_mode()
		time.sleep(1)
		self.TriggerMode = 0 # internal triger mode
		time.sleep(1)
		self.TriggerMode = 3 # external triger mode
		time.sleep(10)
		self.Acquire = 1
		time.sleep(1)
		t0 = time.time()
		timeout_trial = 3
		while time.time()-t0 < 20:
			if self.getArrayCounter() == 0:
				time.sleep(0.1)
				self.Acquire = 0
				time.sleep(0.1)
				self.TriggerMode = 0 # internal triger mode
				time.sleep(0.1)
				self.TriggerMode = 3 # external triger mode
				time.sleep(3)
				self.Acquire = 1
				time.sleep(1)
			if self.getArrayCounter() > 10:
				break
			if timeout_trial > 3:
				return 0 # failed to refresh
			timeout_trial += 1
		self.Acquire = 0
		self.change2multitrigger_mode()
		return 1 # scuccessfully refreshed
		

