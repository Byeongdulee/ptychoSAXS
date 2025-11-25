import time
from epics import Device, PV, caget_many
import numpy as np
from epics import caget, caput, PV

beamlinePV = '12idc:'

try:
	from .ad_pilatus import AD_SG
except:
	from ad_pilatus import AD_SG


class SOFTGLUE_Setup_Error(Exception):
    pass


class SG(AD_SG):
	def __init__(self, basename="12idSGSocket:"):
		super().__init__(basename)
		self.setNDArrayPort()
#		self.detmode = PILATUSMODE
#		self.dettype = "SG"

	def SetNumImages(self, n):
		pass
		#self.NumImages = n
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
		self.filePut('FilePath', '/net/micdata/data2')
		self.filePut('AutoIncrement', 0)
		self.filePut('AutoSave', 1)
		self.filePut('FileWriteMode', 2)

	def fly_ready(self, expt, x_points, y_points=1, wait=False, period=0, isTest=False, capture=(True, 1)):
		#Npoints = x_points*y_points
		#if period>0:
		#	self.SetExposurePeriod(period)
		#self.setArrayCounter(0)
		self.setFileTemplate('%s%s_%5.5d.h5')
		#self.SetMultiFrames(Npoints, x_points)
		#self.setFileNumber(1)
		if not isTest:
			self.StartCapture()
			#if wait:
			#	self.wait_capturedone()
	def set_scanNumberAsfilename(self):
		fw_dir = caget(f"{beamlinePV}data:userDir")
		self.setFilePath(fw_dir)
		self.setFileName('scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))

class sgz_pty(Device):
    basePV = "12IFMZ:"
    dmaPV = '%s1acquireDma'%basePV
    SGpv = "%sSG:"%basePV
    sg10 = PV('%s10MHZ_CLOCK_Signal'%SGpv)
    if sg10.wait_for_connection(timeout=3):
        sg10.put("ck10")
        isConnected = True
    else:
        isConnected = False
    sg20 = PV('%s20MHZ_CLOCK_Signal'%SGpv)
    if isConnected:
        sg20.put("ck20")
#    attrs = ('VALB', 'VALC', 'VALD', 'VALE', 'VALF', 'VALG', 'VALH', 'PROC', 'D', 'F')
    attrs = ('VALA', 'VALB', 'VALC', 'VALD', 'VALE', 'VALF', 'VALG', 'VALH', 'PROC', 'D', 'F')
    data = []
    index = 0
    def __init__(self, prefix = dmaPV):
        if self.isConnected:
            myattrs =list(self.attrs)
            Device.__init__(self, prefix, delim='.', attrs=myattrs, timeout=10)
    #        self.add_pv('%s.VALA'%prefix, attr = 'VALA')
            self.add_pv('%s.VALI'%prefix, attr = 'VALI', timeout=10)
            self.add_pv('%s.VALJ'%prefix, attr = 'VALJ', timeout=10)
            self.add_pv('%sEnable'%prefix, attr = 'Enable', timeout=10)
            self.add_pv('%sBUFFER-1_IN_Signal'%self.SGpv, attr="buf_in1", timeout=10)
            self.add_pv('%sBUFFER-2_IN_Signal'%self.SGpv, attr="buf_in2", timeout=10)
            self.add_pv('%sBUFFER-4_IN_Signal'%self.SGpv, attr="buf_in4", timeout=10)
            self.add_pv('%sAND-3_IN1_Signal'%self.SGpv, attr="in1", timeout=10)
            self.add_pv('%sAND-4_IN1_Signal'%self.SGpv, attr="in2", timeout=10)
            self.add_pv('%sFI1_Signal'%self.SGpv, attr="ch_input1", timeout=10)
            #self.add_pv('%sFO1_Signal'%self.SGpv, attr="ch_output1")
            #self.add_pv('%sFO1_Signal'%self.SGpv, attr="ch_output2")
            self.add_pv('%sDivByN-1_N'%self.SGpv, attr="div1", timeout=10)
            self.add_pv('%sDivByN-2_N'%self.SGpv, attr="div2", timeout=10)
            self.add_pv('%sDivByN-3_N'%self.SGpv, attr="div3", timeout=10)
            self.add_pv('%sDivByN-1_CLOCK_Signal'%self.SGpv, attr="div1clock", timeout=10)
            self.add_pv('%sDivByN-2_CLOCK_Signal'%self.SGpv, attr="div2clock", timeout=10)
            self.add_pv('%sDivByN-3_CLOCK_Signal'%self.SGpv, attr="div3clock", timeout=10)
            self.add_pv('%sUpDnCntr-1_CLEAR_Signal'%self.SGpv, attr="_clockreset", timeout=10)
            self.add_pv('%sUpDnCntr-1_COUNTS'%self.SGpv, attr="ckTime", timeout=10)
            self.add_pv('%sscalToStream-1_FLUSH_Signal'%self.SGpv, attr="_flush", timeout=10)
            self.Enable = 1

    def onChange(self, value, **kws):
        self.index = value

    def enable(self):
        self.Enable = 1

    def disable(self):
        self.Enable = 0
    
    def default_clock(self, freq=10000):
        # default clock 10 kHz and collect 10,000 pnts/second -- > 0.1 millisconds/pnt
        self.div1clock = "ck10"
        self.div2clock = "ck10"
        self.div3clock = "ck10"
        if self.div1clock == 'ck10':
            clock_in = 10E6 # 10MHz
        elif self.div1clock == 'ck20':
            clock_in = 20E6 # 20MHz
        self.div1 = clock_in/freq
        self.div2 = clock_in/1E6
        self.div3 = clock_in/freq

    def set_clock_in(self, clock=10E6):
        assert clock not in [10E6, 20E6], "Clock can be 10E6 or 20E6"
        if clock == 10E6:
            self.div1clock = "ck10"
            self.div2clock = "ck10"
            self.div3clock = "ck10"
        if clock == 20E6:
            self.div1clock = "ck20"
            self.div2clock = "ck20"
            self.div3clock = "ck20"

    def set_count_freq(self, freq=10):
        # unit of the freq is [micro second]
        if self.div1clock == 'ck10':
            clock_in = 10E6 # 10MHz
        elif self.div1clock == 'ck20':
            clock_in = 20E6 # 20MHz

        self.div1 = clock_in/1E6*freq*10
        self.div2 = clock_in/1E6
        self.div3 = clock_in/1E6*freq*10
    
    def number_acquisition(self, exptime, N=1):
        return np.round(exptime/((self.div1/self.div2)*1E-6)*N) 
    
    def _reset(self):
        self.buf_in4 = '0'
        self.buf_in1 = "1!"        
        self.buf_in2 = "1!"
        self.buf_in4 = '1'
    
    def ckTime_reset(self):
        ckT = self.ckTime
        timeout = 10
        self.put('_clockreset', "1!", timeout=timeout)
        tm = time.time()
        while self.get('ckTime', timeout=timeout)>ckT:
            time.sleep(0.1)
            if time.time()-tm>timeout:
                raise TimeoutError
    
    def memory_clear(self):
        self.D = 1
        self.PROC = 1
        timeout = 5
        tm = time.time()
        while self.get_eventN()>0:
            time.sleep(0.001)
            if time.time()-tm>timeout:
                raise TimeoutError

    def buffer_clear(self):
        self.F = 1
        timeout = 5
        tm = time.time()
        while self.get_buffN()>0:
            time.sleep(0.001)
            if time.time()-tm>timeout:
                raise TimeoutError

    def get_eventN(self):
        return self.VALI
    
    def get_buffN(self):
        return self.VALJ
        
    def get_time(self):
        return self.VALA

    def set_trigout_in(self):
        self.in1 = 'trig_out'
        self.in2 = 'trig_out'
        self.ch_input1 = 'trig_out'

    def get_position(self, pos = ['B', 'C', 'D']):
        self.memory_clear()
        while (self.VALI!=0):
            self.PROC = 1
            time.sleep(0.01)
        #time.sleep(0.02)
        self.in1 = '1'
        self.in2 = '1'
        while (self.VALI<10):
            self.PROC = 1
            time.sleep(0.01)
        #time.sleep(timeout)
        data = self.get_array('A')
        lastind = 0
        for dd in range(self.VALI):
            if data[self.VALI-dd]!=0:
                lastind = self.VALI-dd
                break
        self.set_trigout_in()
        arr = []
        for p in pos:
            data = self.get_array(p)
            arr.append(data[lastind])
        return arr
    
    def get_arrays_fulltime(self, pos = ['B', 'C', 'D']):
        arr = []
        for p in pos:
            data = self.get_array(p)
            arr.append(data)
        return arr
    
    def get_latest_positions(self, pos = ['B', 'C', 'D']):
        arr = []
        for p in pos:
            data = self.get_array(p)
            arr.append(data[self.VALI])
        return arr
    
    def get_arrays(self, pos = ['B', 'C', 'D']):  # returns time and position arrays
        pvlist = [f"{self.dmaPV}.VAL{p}" for p in pos]
        arrs = caget_many(pvlist, as_numpy=True)
        return arrs
    
    def slice_arrays(self, indices, arrs): # when indices and arrs are given, slice arrs.
       # Fetch arrays for each position and slice them
        arrays = []
        for arr in arrs:  
            sliced_data = [
                arr[(indices[i] + 1):indices[i + 1]]
                for i in range(len(indices) - 1)
            ]
            arrays.append(sliced_data)
        return arrays
        
    def get_sliced_arrays(self, pos=['B', 'C', 'D']):
        # Build PV list for caget_many
        arrs = self.get_arrays(pos)
        data, indices = self.slice_timearray(arrs[0])
        arrays = self.slice_arrays(indices, arrs[1:]) # Skip the first array (timearray)
 
        return data, arrays

    def get_array(self, pos='B'):
        fieldname = f'VAL{pos}'
        timeout = 10
        i = 0
        val = self.get(fieldname, timeout=10)
        return val

    def get_latest_scantime(self):
        clock_in = 20_000_000 if self.div1clock == 'ck20' else 10_000_000
        #div1 = self.div1 or 1000
        div2 = self.div2 or 10
        ckTime_unit = clock_in / div2
        ta = self.get_timearray()
        res = ta[ta != 0]
        if len(res) ==0:
            return 0
        else:
            return (res[-1]-np.min(res))/ckTime_unit, ta

    def slice_timearray(self, timearray):
    # Determine clock unit time
        clock_in = 20_000_000 if self.div1clock == 'ck20' else 10_000_000
        div1 = self.div1 or 1000
        div2 = self.div2 or 10
        ckTime_unit = clock_in / div2

        d = np.diff(timearray)
        p0_candidates = np.where(d < -1 * (div1 / div2))[0]
        p_candidates = np.where(d > div1 / div2)[0]

        if len(p0_candidates) == 0 or len(p_candidates) == 0:
            return [], []  # No valid indices found

        p0 = p0_candidates[0]
        if p0 > p_candidates[0]:
            p0 = p_candidates[0]
            p_candidates = p_candidates[1:]

        indices = np.concatenate([[p0], p_candidates])
        data = [
            timearray[(indices[i] + 1):indices[i + 1]] / ckTime_unit
            for i in range(len(indices) - 1)
        ]
        return data, indices
            
    def get_timearray(self):
        # returns (time in second, time bin indices)
        timearray = self.get_array('A')
        return timearray
        #timearray = timearray[0:self.VALI]
        #return timearray
    
    def flush(self):
#        time.sleep(0.1)
        self._flush = '0'
        time.sleep(0.01)
        self._flush = '1'
        time.sleep(0.01)
        self._flush = '0'
        time.sleep(0.01)
        self._flush = '1'
        time.sleep(0.05)
    
    def cleanup(self, arrays):
        # remove data that does not vary with time.
        timearray = self.get_timearray()
