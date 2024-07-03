import time
from epics import Device, PV

class sgz_pty(Device):
    basePV = "12IFMZ:"
    dmaPV = '%s1acquireDma'%basePV
    SGpv = "%sSG:"%basePV
    sg10 = PV('%s10MHZ_CLOCK_Signal'%SGpv)
    sg10.put("ck10")
    sg20 = PV('%s20MHZ_CLOCK_Signal'%SGpv)
    sg20.put("ck20")
    attrs = ('VALA', 'VALB', 'VALC', 'VALD', 'VALE', 'VALF', 'VALI', 'VALJ', 'PROC', 'D', 'F')

    def __init__(self, prefix = dmaPV):
        myattrs =list(self.attrs)
        Device.__init__(self, prefix, delim='.', attrs=myattrs)
        self.add_pv('%sEnable'%prefix, attr = 'Enable')
        self.add_pv('%sBUFFER-1_IN_Signal'%self.SGpv, attr="buf_in1")
        self.add_pv('%sBUFFER-2_IN_Signal'%self.SGpv, attr="buf_in2")
        self.add_pv('%sBUFFER-4_IN_Signal'%self.SGpv, attr="buf_in4")
        self.add_pv('%sAND-3_IN1_Signal'%self.SGpv, attr="in1")
        self.add_pv('%sAND-4_IN1_Signal'%self.SGpv, attr="in2")
        self.add_pv('%sFI1_Signal'%self.SGpv, attr="ch_input1")
        self.add_pv('%sFO1_Signal'%self.SGpv, attr="ch_output1")
        self.add_pv('%sFO1_Signal'%self.SGpv, attr="ch_output2")
        self.add_pv('%sDivByN-2_N'%self.SGpv, attr="div2")
        self.add_pv('%sDivByN-1_N'%self.SGpv, attr="div1")
        self.add_pv('%sDivByN-2_N'%self.SGpv, attr="div2")
        self.add_pv('%sDivByN-3_N'%self.SGpv, attr="div3")
        self.add_pv('%sDivByN-1_CLOCK_Signal'%self.SGpv, attr="div1clock")
        self.add_pv('%sDivByN-2_CLOCK_Signal'%self.SGpv, attr="div2clock")
        self.add_pv('%sDivByN-3_CLOCK_Signal'%self.SGpv, attr="div3clock")
        self.add_pv('%sUpDnCntr-1_CLEAR_Signal'%self.SGpv, attr="_clockreset")
        self.add_pv('%sUpDnCntr-1_COUNTS'%self.SGpv, attr="ckTime")
        self.Enable = 1

    def enable(self):
        self.Enable = 1

    def disable(self):
        self.Enable = 0
    
    def default_clock(self, freq=10000):
        # default clock 10 kHz
        self.div1clock = "ck10"
        self.div2clock = "ck10"
        self.div3clock = "ck10"
        self.clock_in = 10000000 # 10MHz
        self.div1 = self.clock_in/freq
        self.div2 = self.clock_in/freq
        self.div3 = self.clock_in/freq

    def set_clock_in(self, clock=10000000):
        assert clock not in [10000000, 20000000], "Clock can be 10E6 or 20E6"
        self.clock_in = clock
        if clock == 10000000:
            self.div1clock = "ck10"
            self.div2clock = "ck10"
            self.div3clock = "ck10"
        if clock == 20000000:
            self.div1clock = "ck20"
            self.div2clock = "ck20"
            self.div3clock = "ck20"

    def set_count_freq(self, freq=10000):
        # default clock 10 kHz
        self.div1 = self.clock_in/freq
        self.div2 = self.clock_in/freq
        self.div3 = self.clock_in/freq
    
    def _reset(self):
        self.buf_in4 = '0'
        self.buf_in1 = "1!"        
        self.buf_in2 = "1!"
        self.buf_in4 = '1'
    
    def ckTime_reset(self):
        ckT = self.ckTime
        self._clockreset = "1!"
        timeout = 1
        tm = time.time()
        while self.ckTime>ckT:
            time.sleep(0.001)
            if time.time()-tm>timeout:
                raise TimeoutError
    
    def memory_clear(self):
        self.D = 1
        self.PROC = 1
        timeout = 1
        tm = time.time()
        while self.get_eventN()>0:
            time.sleep(0.001)
            if time.time()-tm>timeout:
                raise TimeoutError

    def buffer_clear(self):
        self.F = 1
        timeout = 1
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

    def set_detout_in(self):
        self.in1 = 'det_out'
        self.in2 = 'det_out'

    def set_trigout_in(self):
        self.in1 = 'trig_out'
        self.in2 = 'trig_out'
        self.ch_input1 = 'trig_out' # input from the hexapod
        self.ch_output1 = 'trig_out' # to trigger SAXS
        self.ch_output2 = 'trig_out' # to trigger WAXS

    def get_data(self):
        return [self.VALA, self.VALB, self.VALC, self.VALD]
    
    def get_position(self):
        return [self.VALB[self.VALI], self.VALC[self.VALI], self.VALD[self.VALI]]
    
    def get_pos_array(self):
        return [self.VALB[0:self.VALI], self.VALC[0:self.VALI], self.VALD[0:self.VALI]]