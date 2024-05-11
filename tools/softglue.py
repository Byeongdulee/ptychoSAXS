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
        self.add_pv('%sDivByN-1_N'%self.SGpv, attr="div1")
        self.add_pv('%sDivByN-2_N'%self.SGpv, attr="div2")
        self.add_pv('%sDivByN-3_N'%self.SGpv, attr="div3")
        self.add_pv('%sDivByN-1_CLOCK_Signal'%self.SGpv, attr="div1clock")
        self.add_pv('%sDivByN-2_CLOCK_Signal'%self.SGpv, attr="div2clock")
        self.add_pv('%sDivByN-3_CLOCK_Signal'%self.SGpv, attr="div3clock")
        self.add_pv('%sUpDnCntr-1_CLEAR_Signal'%self.SGpv, attr="_clockreset")
        self.add_pv('%sUpDnCntr-1_COUNTS'%self.SGpv, attr="ckTime")

    def enable(self):
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
        self._clockreset = "1!"
    
    def memory_clear(self):
        self.D = 1
        self.PROC = 1

    def buffer_clear(self):
        self.F = 1

    def get_eventN(self):
        return self.VALI
    
    def get_buffN(self):
        return self.VALJ
        
    def get_time(self):
        return self.VALA

    def get_data(self):
        return [self.VALA, self.VALB, self.VALC, self.VALD]
    
    def get_position(self):
        return [self.VALB, self.VALC, self.VALD]