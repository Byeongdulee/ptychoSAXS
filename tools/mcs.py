import time
from epics.devices.struck import Struck

beamlinePV = '12idc:'

class mcs(Struck):
    def __init__(self, PV = beamlinePV):
        strkPV = "%s3820:"%PV
        super().__init__(strkPV, scaler='%sscaler1'%strkPV, nchan=12)
        self.add_pv('%sAcquireMode'%strkPV, attr = 'AcquireMode')
        self.add_pv('%sInputMode'%strkPV, attr = 'InputMode')
        self.add_pv('%sOutputMode'%strkPV, attr = 'OutputMode')
        self.add_pv('%sOutputPolarity'%strkPV, attr = 'OutputPolarity')
        self.add_pv('%sReadAll.SCAN'%strkPV, attr = 'SCAN')
        self.add_pv('%sSoftwareChannelAdvance'%strkPV, attr = 'SoftwareChannelAdvance')
    
    def mcs_config(self, imagN, TotalMeasurementTime):
        self.mcs_init()
        self.NuseAll = imagN
        self.CountOnStart = 1
        self.PresetReal = TotalMeasurementTime
        
    def mcs_getready(self):
        self.scaler.TP = 0.001
        self.scaler.CNT = 1
        time.sleep(0.01)
        self.scaler.CNT = 0
        
    def mcs_init(self):
        self.stop()
        self.ChannelAdvance = 1
        self.scaler.CONT = 0
        self.SCAN = 2
        self.CountOnStart = 1
        self.Channel1Source = 0
        self.UserLED = 0
        self.Prescale = 1
        self.InputMode = 2
        self.OutputMode = 0
        self.OutputPolarity = 0
        self.mcs_wait()
        self.EraseAll = 1
        if self.AcquireMode == "Scaler":
            self.mcs_getready()
        return 1

    def arm_mcs(self):
        self.EraseStart = 1
        self.mcs_waitstarted()
        
    def channelAdvance_mcs(self):
        self.SoftwareChannelAdvance = 1
        
    def mcs_wait(self):
        while self.Acquiring:
            self.Acquring = 0
            time.sleep(0.01)
            
    def mcs_waitstarted(self):
        while not self.Acquiring:
            time.sleep(0.01)

class counter(Struck):
    def __init__(self, PV = beamlinePV):
        strkPV = "%s3820:"%PV
        super().__init__(strkPV, scaler='%sscaler1'%strkPV, nchan=12)
        self.add_pv('%sAcquireMode'%strkPV, attr = 'AcquireMode')
        self.add_pv('%sInputMode'%strkPV, attr = 'InputMode')
        self.add_pv('%sOutputMode'%strkPV, attr = 'OutputMode')
        self.add_pv('%sOutputPolarity'%strkPV, attr = 'OutputPolarity')
        self.add_pv('%sReadAll.SCAN'%strkPV, attr = 'SCAN')
        self.add_pv('%sSoftwareChannelAdvance'%strkPV, attr = 'SoftwareChannelAdvance')
            
    def counter_init(self):
        self.ChannelAdvance = 1
        self.scaler.CONT = 0
        self.SCAN = 0
        self.Prescale = 1
        return 2
        
    def counter_ready(self, expt):
        self.counter_init()
        self.EraseAll = 1
        self.scaler.TP = expt + 20

    def arm_counter(self):
        self.scaler.CNT = 1
        self.counter_waitstarted()
        time.sleep(0.1)     
        
    def counter_waittime(self, timeExp):
        crnt_value = 0
        wait_timeout = timeExp + 1
        t = time.time()
        while (crnt_value < timeExp*50000000):
            crnt_value = self.scaler.S1
            time.sleep(0.02)
            if (abs(t-time.time())>wait_timeout):
                raise ValueError('Network timeout in mcs_counter_waittime')
                break
        self.scaler.CNT = 0

    def counter_waitcountingstarted(self):
        crnt_value = self.scaler.S1
        time.sleep(0.01)
        while (self.scaler.S1 is crnt_value):
            crnt_value = self.scaler.S1
            time.sleep(0.01)
            
    def counter_waitstarted(self):
        while not self.scaler.CNT:
            time.sleep(0.01)

    def counter_wait(self, timeExp):
        wait_time = timeExp + 1
        t = time.time()
        crnt_value = self.scaler.S1
        if (crnt_value == 0):
            while (crnt_value == 0) :
                crnt_value = self.scaler.S1
                if (abs(t-time.time())>wait_time):
                    raise ValueError("MCS has not been triggered for %f second" % wait_time)
                time.sleep(0.1)
        while(abs(t - time.time()) <= wait_time):
            time.sleep(0.01)
        t = time.time()
        while (self.scaler.S1 is not crnt_value):
            crnt_value = self.scaler.S1
            if (abs(t-time.time())>wait_time):
                raise ValueError('NETWORK timeout in mcs_counter_wait')
            time.sleep(0.1)
        self.scaler.CNT = 0
