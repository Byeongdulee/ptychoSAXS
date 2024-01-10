#!/usr/bin/python
import sys
import time
from epics import Device

# Pilatus uses "external series mode, 2" 
# while Eiger uses "external enable mode, 3". (NumImage always 1 and NumTriggers are the number of shots)
EIGERMODE = 3
PILATUSMODE = 2
class AD_Pilatus(Device):
    camattrs = ('NumImages', 'NumTriggers', 
                'ImageMode', 'TriggerMode',
                'Acquire', 'Acquire_RBV', 'AcquireTime', 'AcquirePeriod',
                'Armed', 'ArrayCounter', 'ArrayCounter_RBV')

    pathattrs = ('FileNumber', 'FileNumber_RBV', 
                 'FilePath', 'FileWriteMode',
                 'FileName', 'FileName_RBV', 'FullFileName_RBV', 
                 'Capture',  'Capture_RBV', 'NumCapture', 'NumCaptured_RBV', 
                 'WriteFile', 'WriteFile_RBV',
                 'AutoSave', 'AutoIncrement', 'EnableCallbacks', 
                 'FileTemplate', 'FileTemplate_RBV', 'NDArrayPort')

    _nonpvs  = ('_prefix', '_pvs', '_delim', 'filesaver',
                'camattrs', 'pathattrs', '_nonpvs')

    def __init__(self, prefix, filesaver='HDF1:'):
        camprefix = prefix + 'cam1:'
        Device.__init__(self, camprefix, delim='',
                        mutable=False,
                        attrs=self.camattrs)
        self.filesaver = "%s%s" % (prefix, filesaver)
        for p in self.pathattrs:
            pvname = '%s%s%s' % (prefix, filesaver, p)
            self.add_pv(pvname, attr='File_'+p)

    def Arm(self, nimg = 1):
        self.ImageMode = 1
        self.Acquire = 0 # stop acquire
        self.SetNumImages(nimg)
        self.Acquire = 1
        self.CCD_waitstarted()

    def CCD_waitstarted(self):
        t = time.time()
        TIMEOUT = 0.5
        while self.Armed == 0:
            self.Acquire = 1
            time.sleep(0.01)
            if abs(time.time()-t)>TIMEOUT:
                raise TimeoutError
            
    def SetNumImages(self, n, detmode = PILATUSMODE):
        if detmode == PILATUSMODE:
            self.NumImages = n
            self.NumTriggers = 1
        if detmode == EIGERMODE:
            self.NumImages = 1
            self.NumTriggers = n

    def CCD_waitFileWriting(self):
        while not self.FileWriteComplete():
            time.sleep(0.1)
            
    def SetExposureTime(self, t):
        "set exposure time, re-acquire offset correction"
        self.AcquireTime = t

    def SetMultiFrames(self, n_trig, n_cap):
        """set number of images(triggers) for camera
        AND the number of images to capture with file plugin
        When you want to arm camera for 100 images and save every 20 images into a file,
        SetMultiFrames(100, 20)
        """
        self.ImageMode = 1  #  multiple images
        time.sleep(0.1)
        self.TriggerMode = 3

        # number of images for collection and capture
        self.SetNumImages(n_trig)

        # set filesaver
        self.filePut('NumCapture',    n_cap)
        self.filePut('EnableCallbacks', 1)
        self.filePut('FileNumber',    1)
        self.filePut('AutoIncrement', 1)
        time.sleep(0.25)

    def StartCapture(self):
        self.ShutterMode = 0
        self.filePut('AutoSave', 0)
        self.filePut('FileWriteMode', 2)  # capture
        time.sleep(0.05)
        self.filePut('Capture', 1)  # start capture
        self.Acquire = 1
        time.sleep(0.25)

    def CCD_waitCaptureDone(self):
        #t0 = time.time()
        Ncapr = self.getNumCaptured()
        #Ncoll = self.getArrayCounter()
        NcapAll = self.fileGet('NumCapture')
        while (Ncapr<NcapAll):
            time.sleep(0.01)
            if self.Acquire_RBV.get() == 1:
                Ncapr = self.getNumCaptured()
        #        Ncoll = self.getArrayCounter()
            else:
                break
        
    def StartStreaming(self):
        """start streamed acquisition to save with
        file saving plugin, and start acquisition
        """
        self.ShutterMode = 0
        self.filePut('AutoSave', 1)
        self.filePut('FileWriteMode', 2)  # stream
        time.sleep(0.05)
        self.filePut('Capture', 1)  # stream
        self.Acquire = 1
        time.sleep(0.25)


    def FinishStreaming(self, timeout=5.0):
        """start streamed acquisition to save with
        file saving plugin, and start acquisition
        """
        t0 = time.time()
        capture_on = self.fileGet('Capture_RBV')
        while capture_on==1 and time.time() - t0 < timeout:
            time.sleep(0.05)
            capture_on = self.fileGet('Capture_RBV')
        if capture_on != 0:
            print( 'Forcing XRD Streaming to stop')
            self.filePut('Capture', 0)
            t0 = time.time()
            while capture_on==1 and time.time() - t0 < timeout:
                time.sleep(0.05)
                capture_on = self.fileGet('Capture_RBV')
        time.sleep(0.50)


    def filePut(self, attr, value, **kw):
        return self.put("File_%s" % attr, value, **kw)

    def fileGet(self, attr, **kw):
        return self.get("File_%s" % attr, **kw)

    def setFilePath(self, pathname):
        return self.filePut('FilePath', pathname)

    def setFileTemplate(self, fmt):
        return self.filePut('FileTemplate', fmt)

    def setFileWriteMode(self, mode):
        return self.filePut('FileWriteMode', mode)

    def setFileName(self, fname):
        return self.filePut('FileName', fname)

    def nextFileNumber(self):
        self.setFileNumber(1+self.fileGet('FileNumber'))

    def setFileNumber(self, fnum=None):
        if fnum is None:
            self.filePut('AutoIncrement', 1)
        else:
            self.filePut('AutoIncrement', 0)
            return self.filePut('FileNumber',fnum)

    def getLastFileName(self):
        return self.fileGet('FullFileName_RBV',as_string=True)

    def FileCaptureOn(self):
        return self.filePut('Capture', 1)

    def FileCaptureOff(self):
        return self.filePut('Capture', 0)

    def setFileNumCapture(self,n):
        return self.filePut('NumCapture', n)

    def FileWrite(self):
        self.filePut('WriteFile', 1)

    def FileWriteComplete(self):
        return (0==self.fileGet('WriteFile_RBV') )

    def getFileTemplate(self):
        return self.fileGet('FileTemplate_RBV',as_string=True)

    def getFileName(self):
        return self.fileGet('FileName_RBV',as_string=True)

    def getFileNumber(self):
        return self.fileGet('FileNumber_RBV')

    def getFilePath(self):
        return self.fileGet('FilePath_RBV',as_string=True)

    def getFileNameByIndex(self,index):
        return self.getFileTemplate() % (self.getFilePath(), self.getFileName(), index)

    def getNumCaptured(self):
        return self.fileGet('NumCaptured_RBV')
    
    def getArrayCounter(self):
        return self.ArrayCounter_RBV.get()
    
    def setNDArrayPort(self, port='PIL'):
        self.filePut('NDArrayPort', port)