import time
from epics import Device, PV
import numpy as np
from tqdm import tqdm

class SOFTGLUE_Setup_Error(Exception):
    pass

class sgz_pty(Device):
    basePV = "12IFMZ:"
    dmaPV = '%s1acquireDma'%basePV
    SGpv = "%sSG:"%basePV
    sg10 = PV('%s10MHZ_CLOCK_Signal'%SGpv)
    sg10.put("ck10")
    sg20 = PV('%s20MHZ_CLOCK_Signal'%SGpv)
    sg20.put("ck20")
#    attrs = ('VALB', 'VALC', 'VALD', 'VALE', 'VALF', 'VALG', 'VALH', 'PROC', 'D', 'F')
    attrs = ('VALA', 'VALB', 'VALC', 'VALD', 'VALE', 'VALF', 'VALG', 'VALH', 'PROC', 'D', 'F')
    data = []
    index = 0
    def __init__(self, prefix = dmaPV):
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
    
    def get_arrays(self, pos = ['B', 'C', 'D']):
        # returns time and position arrays
        t, ind = self.get_timearray()
        arr = []
        for p in pos:
            data = self.get_array(p)
            dt = []
            for i in range(len(ind)-1):
                dt.append(data[(ind[i]+1):ind[i+1]])
            arr.append(dt)
        return (t, arr)
    
    def get_array(self, pos='B'):
        fieldname = f'VAL{pos}'
        timeout = 10
        i = 0
        val = self.get(fieldname, timeout=10)
        return val

    def get_timearray(self):
        # returns (time in second, time bin indices)
        if self.div1clock == 'ck10':
            clock_in = 100000000 # 10MHz
        elif self.div1clock == 'ck20':
            clock_in = 200000000 # 20MHz
        else:
            clock_in = 100000000 # 10MHz
        if self.div1 is None:
            self.div1 = 1000
        if self.div2 is None:
            self.div2 = 10
        ckTime_unit = clock_in/(self.div1/self.div2)
        timearray = self.get_array('A')
        d = np.diff(timearray)
        p0 = np.where(d<-1*(self.div1/self.div2))
        p0 = p0[0]
        if type(p0) == np.ndarray:
            p0 = p0[0]
        p = np.where(d>self.div1/self.div2)
        p = p[0]
        data = []
        if p0>p[0]:
            p0 = p[0]
            p = p[1:]
        index = [p0]
        for i in range(len(p)):
            if i==0:
                index_start = p0
            else:
                index_start = p[i-1]
            index.append(p[i])
            data.append(timearray[(index_start+1):p[i]]/ckTime_unit)
        return (data, index)
        #timearray = timearray[0:self.VALI]
        #return timearray
    
    def flush(self):
#        time.sleep(0.1)
        self._flush = '0'
        time.sleep(0.01)
        self._flush = '1'
        time.sleep(0.2)
    
    def cleanup(self, arrays):
        # remove data that does not vary with time.
        timearray = self.get_timearray()
