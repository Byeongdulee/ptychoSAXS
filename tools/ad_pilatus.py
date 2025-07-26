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
                'AutoIncrement', 'FileNumber', 'FileNumber_RBV', 'FilePath',
                'FileTemplate', 'FileTemplate_RBV', 
                'FileName', 'FileName_RBV', 'FullFileName_RBV', 
                'Acquire', 'Acquire_RBV', 'AcquireTime', 'AcquirePeriod',
                'Armed', 'ArrayCounter', 'ArrayCounter_RBV')

    pathattrs = ('FileNumber', 'FileNumber_RBV', 
                 'FilePath', 'FileWriteMode',
                 'FileName', 'FileName_RBV', 'FullFileName_RBV', 
                 'Capture',  'Capture_RBV', 'NumCapture', 'NumCaptured_RBV', 
                 'WriteFile', 'WriteFile_RBV',
                 'AutoSave', 'AutoIncrement', 'EnableCallbacks', 
                 'FileTemplate', 'FileTemplate_RBV', 'NDArrayPort')

    _nonpvs  = ('_prefix', '_pvs', '_delim', 'filesaver','basepath',
                'camattrs', 'pathattrs', '_nonpvs')
    def __init__(self, prefix, filesaver='HDF1:', basepath = "/ramdisk"):
        camprefix = prefix + 'cam1:'
        Device.__init__(self, camprefix, delim='',
                        mutable=False,
                        attrs=self.camattrs)
        self.filesaver = "%s%s" % (prefix, filesaver)
        self.basepath = basepath
        for p in self.pathattrs:
            pvname = '%s%s%s' % (prefix, filesaver, p)
            self.add_pv(pvname, attr='File_'+p)
            
    def Arm(self, nimg = 0):
        if self.Acquire == 1:
            t = time.time()
            self.Acquire = 0 # stop acquire
            while self.Acquire == 1:
                self.Acquire = 0
                time.sleep(0.1)
                if abs(time.time()-t)>10:
                    print("CCD still running timeout.")
                    raise TimeoutError        
        self.ImageMode = 1
        if nimg>0:
            self.NumImages = nimg
        self.Acquire = 1
        time.sleep(0.05)
        try:
            self.CCD_waitstarted()
        except TimeoutError:
            raise TimeoutError

    def CCD_waitstarted(self):
        t = time.time()
        TIMEOUT = 10
        while self.Armed == 0:
            self.Acquire = 1
            time.sleep(0.1)
            if abs(time.time()-t)>TIMEOUT:
                print("CCD Arming timeout.")
                raise TimeoutError
            
    def CCD_waitFileWriting(self):
        while not self.FileWriteComplete():
            time.sleep(0.025)
            
    def SetExposureTime(self, t):
        "set exposure time, re-acquire offset correction"
        self.AcquireTime = t

    def SetExposurePeriod(self, period):
        "set exposure time, re-acquire offset correction"
        self.AcquirePeriod = period

    def SetMultiFrames(self, n_trig, n_cap):
        """set number of images(triggers) for camera
        AND the number of images to capture with file plugin
        When you want to arm camera for 100 images and save every 20 images into a file,
        SetMultiFrames(100, 20)
        """
        self.ImageMode = 1  #  multiple images
        #time.sleep(0.1)
        self.TriggerMode = 3

        # number of images for collection and capture
        self.NumImages = n_trig

        # set filesaver
        self.filePut('NumCapture',    n_cap)
        self.filePut('EnableCallbacks', 1)
        #self.filePut('FileNumber',    1)
        time.sleep(0.025)

    def StartCapture(self):
        self.ShutterMode = 0
        self.filePut('AutoSave', 1)
        self.filePut('FileWriteMode', 1)  # Stream..... BL 12/2/2024. This was 1 for capture.
        time.sleep(0.05)
        self.filePut('Capture', 1)  # start capture
        self.Arm()
        time.sleep(0.025)

    def StartSingleFrame(self):
        self.ShutterMode = 0
        fn = bytes(self.FileName_RBV).decode().strip('\x00')
        self.setFileName("%s_%5.5d"%(fn, self.FileNumber_RBV))
        self.AutoIncrement = 1
        self.filePut('AutoIncrement', 1)
        self.filePut('FileNumber',1)
        self.filePut('AutoSave', 1)
        self.filePut('NumCapture',    1)
        self.filePut('FileWriteMode', 0)  # single frame
        time.sleep(0.025)
        #self.filePut('Capture', 1)  # start capture
        self.Arm()
        #time.sleep(0.25)

    def ForceStop(self, timeouttime = 2):
        t0 = time.time()
        Ncapr = self.getNumCaptured()
        NcapAll = self.fileGet('NumCapture')
        timeout = False
        while (Ncapr<NcapAll):
            time.sleep(0.01)
            if self.Acquire_RBV == 1:
                Ncapr = self.getNumCaptured()
            else:
                break
            if (time.time()-t0) > timeouttime:
                #print(time.time()-t0)
                timeout = True
        if timeout:
            if self.fileGet('Capture_RBV') > 0:
                self.filePut('Capture', 0)  # start capture
            self.FileWrite()
            self.CCD_waitFileWriting()
            if self.Acquire_RBV > 0:
                self.Acquire = 0

    def CCD_waitCaptureDone(self):
        #t0 = time.time()
        Ncapr = self.getNumCaptured()
        NcapAll = self.fileGet('NumCapture')
        while (Ncapr<NcapAll):
            time.sleep(0.01)
            if self.Acquire_RBV == 1:
                Ncapr = self.getNumCaptured()
            else:
                break
        
    def StartStreaming(self):
        """start streamed acquisition to save with
        file saving plugin, and start acquisition
        """
        self.ShutterMode = 0
        self.filePut('AutoSave', 1)
        self.filePut('FileWriteMode', 2)  # stream
        time.sleep(0.025)
        self.filePut('Capture', 1)  # stream
        self.Acquire = 1
        time.sleep(0.025)


    def FinishStreaming(self, timeout=5.0):
        """start streamed acquisition to save with
        file saving plugin, and start acquisition
        """
        t0 = time.time()
        capture_on = self.fileGet('Capture_RBV')
        while capture_on==1 and time.time() - t0 < timeout:
            time.sleep(0.025)
            capture_on = self.fileGet('Capture_RBV')
        if capture_on != 0:
            print( 'Forcing XRD Streaming to stop')
            self.filePut('Capture', 0)
            t0 = time.time()
            while capture_on==1 and time.time() - t0 < timeout:
                time.sleep(0.025)
                capture_on = self.fileGet('Capture_RBV')
        time.sleep(0.025)


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
    
    def setArrayCounter(self, N=0):
        self.ArrayCounter = N

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

    def getCapture(self):
        return self.fileGet('Capture_RBV')

    def getFilePath(self):
        return self.fileGet('FilePath',as_string=True)

    def getFileNameByIndex(self,index):
        return self.getFileTemplate() % (self.getFilePath(), self.getFileName(), index)

    def getNumCaptured(self):
        return self.fileGet('NumCaptured_RBV')
    
    def getArrayCounter(self):
        return self.ArrayCounter_RBV
    
    def setNDArrayPort(self, port='PIL'):
        self.filePut('NDArrayPort', port)

class AD_SG(Device):
    camattrs = ('Acquire', 'Acquire_RBV', 'Armed', 'ArrayCounter', 'ArrayCounter_RBV')

    pathattrs = ('FileNumber', 'FileNumber_RBV', 
                 'FilePath', 'FileWriteMode',
                 'FileName', 'FileName_RBV', 'FullFileName_RBV', 
                 'Capture',  'Capture_RBV', 'NumCapture', 'NumCaptured_RBV', 
                 'WriteFile', 'WriteFile_RBV',
                 'AutoSave', 'AutoIncrement', 'EnableCallbacks', 
                 'FileTemplate', 'FileTemplate_RBV', 'NDArrayPort')

    _nonpvs  = ('_prefix', '_pvs', '_delim', 'filesaver','basepath',
                'camattrs', 'pathattrs', '_nonpvs')
    def __init__(self, prefix, FileNumber=0, filesaver='HDF1:', basepath = "/net/micdata/data2"):
        camprefix = prefix + 'SG1:'
        self.FileNumber = FileNumber
        Device.__init__(self, camprefix, delim='',
                        mutable=False,
                        attrs=self.camattrs)
        self.filesaver = "%s%s" % (prefix, filesaver)
        self.basepath = basepath
        for p in self.pathattrs:
            pvname = '%s%s%s' % (prefix, filesaver, p)
            self.add_pv(pvname, attr='File_'+p)
            
    def Arm(self, nimg = 0):
        if self.Acquire == 1:
            t = time.time()
            self.Acquire = 0 # stop acquire
            while self.Acquire == 1:
                self.Acquire = 0
                time.sleep(0.1)
                if abs(time.time()-t)>10:
                    print("CCD still running timeout.")
                    raise TimeoutError        
        self.ImageMode = 1
        if nimg>0:
            self.NumImages = nimg
        self.Acquire = 1
        time.sleep(0.05)
        try:
            self.CCD_waitstarted()
        except TimeoutError:
            raise TimeoutError

    def CCD_waitstarted(self):
        t = time.time()
        TIMEOUT = 10
        while self.Armed == 0:
            self.Acquire = 1
            time.sleep(0.1)
            if abs(time.time()-t)>TIMEOUT:
                print("CCD Arming timeout.")
                raise TimeoutError
            
    def CCD_waitFileWriting(self):
        while not self.FileWriteComplete():
            time.sleep(0.025)
            
    def SetExposureTime(self, t):
        "set exposure time, re-acquire offset correction"
        self.AcquireTime = t

    def SetExposurePeriod(self, period):
        "set exposure time, re-acquire offset correction"
        self.AcquirePeriod = period

    def SetMultiFrames(self, n_trig, n_cap):
        """set number of images(triggers) for camera
        AND the number of images to capture with file plugin
        When you want to arm camera for 100 images and save every 20 images into a file,
        SetMultiFrames(100, 20)
        """
        self.ImageMode = 1  #  multiple images
        #time.sleep(0.1)
        self.TriggerMode = 3

        # number of images for collection and capture
        self.NumImages = n_trig

        # set filesaver
        self.filePut('NumCapture',    n_cap)
        self.filePut('EnableCallbacks', 1)
        #self.filePut('FileNumber',    1)
        time.sleep(0.025)

    def StartCapture(self):
        self.ShutterMode = 0
        self.filePut('AutoSave', 1)
        self.filePut('FileWriteMode', 2)  # Stream..... BL 12/2/2024. This was 1 for capture.
        time.sleep(0.05)
        self.filePut('Capture', 1)  # start capture
        self.Arm()
        time.sleep(0.025)

    def StartSingleFrame(self):
        self.ShutterMode = 0
        fn = bytes(self.FileName_RBV).decode().strip('\x00')
        self.setFileName("%s_%5.5d"%(fn, self.FileNumber_RBV))
        self.AutoIncrement = 1
        self.filePut('AutoIncrement', 1)
        self.filePut('FileNumber',1)
        self.filePut('AutoSave', 1)
        self.filePut('NumCapture',    1)
        self.filePut('FileWriteMode', 0)  # single frame
        time.sleep(0.025)
        #self.filePut('Capture', 1)  # start capture
        self.Arm()
        #time.sleep(0.25)

    def ForceStop(self, timeouttime = 2):
        t0 = time.time()
        Ncapr = self.getNumCaptured()
        NcapAll = self.fileGet('NumCapture')
        timeout = False
        while (Ncapr<NcapAll):
            time.sleep(0.01)
            if self.Acquire_RBV == 1:
                Ncapr = self.getNumCaptured()
            else:
                break
            if (time.time()-t0) > timeouttime:
                print(time.time()-t0)
                timeout = True
        if timeout:
            if self.fileGet('Capture_RBV') > 0:
                self.filePut('Capture', 0)  # start capture
            self.FileWrite()
            self.CCD_waitFileWriting()
            if self.Acquire_RBV > 0:
                self.Acquire = 0

    def CCD_waitCaptureDone(self):
        #t0 = time.time()
        Ncapr = self.getNumCaptured()
        NcapAll = self.fileGet('NumCapture')
        while (Ncapr<NcapAll):
            time.sleep(0.01)
            if self.Acquire_RBV == 1:
                Ncapr = self.getNumCaptured()
            else:
                break
        
    def StartStreaming(self):
        """start streamed acquisition to save with
        file saving plugin, and start acquisition
        """
        self.ShutterMode = 0
        self.filePut('AutoSave', 1)
        self.filePut('FileWriteMode', 2)  # stream
        time.sleep(0.025)
        self.filePut('Capture', 1)  # stream
        self.Acquire = 1
        time.sleep(0.025)


    def FinishStreaming(self, timeout=5.0):
        """start streamed acquisition to save with
        file saving plugin, and start acquisition
        """
        t0 = time.time()
        capture_on = self.fileGet('Capture_RBV')
        while capture_on==1 and time.time() - t0 < timeout:
            time.sleep(0.025)
            capture_on = self.fileGet('Capture_RBV')
        if capture_on != 0:
            print( 'Forcing XRD Streaming to stop')
            self.filePut('Capture', 0)
            t0 = time.time()
            while capture_on==1 and time.time() - t0 < timeout:
                time.sleep(0.025)
                capture_on = self.fileGet('Capture_RBV')
        time.sleep(0.025)


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
    
    def setArrayCounter(self, N=0):
        self.ArrayCounter = N

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

    def getCapture(self):
        return self.fileGet('Capture_RBV')

    def getFilePath(self):
        return self.fileGet('FilePath',as_string=True)

    def getFileNameByIndex(self,index):
        return self.getFileTemplate() % (self.getFilePath(), self.getFileName(), index)

    def getNumCaptured(self):
        return self.fileGet('NumCaptured_RBV')
    
    def getArrayCounter(self):
        return self.ArrayCounter_RBV
    
    def setNDArrayPort(self, port='SG'):
        self.filePut('NDArrayPort', port)