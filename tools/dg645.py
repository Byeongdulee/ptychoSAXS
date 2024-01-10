from instruments.srs import SRSDG645
import quantities as pq
from enum import IntEnum
from instruments.util_fns import ProxyList
addressB = "tcpip://164.54.122.66:5025" # 12idb
addressC = "tcpip://164.54.122.33:5025" # 12idc 
#DG = ik.srs.SRSDG645.open_from_uri(address)
UBZ_SHUTTER_DEADTIME = 0.005   # 5 ms.
SOFTWARE_INIT_DEADTIME = 0.001
DG645_BURST_MAX_TIME = 41
# Example:
# import dg645 as DG
# a = DG.dg645_12ID.open_from_uri(DG.addressB)
# a.pilatus(0.1, 5, 2)
# a.instrument["shutter"].initime
# a.instrument["shutter"].duration
# a.instrument["shutter"].polarity
# a.instrument["shutter"].level_amplitude
# a.instrument["shutter"].level_offset

TRSC_INTERNAL = 0
TSRC_EXTERNAL_RISING_EGDES = 1
TSRC_EXTERNAL_FALLING_EGDES = 2
TSRC_SINGLE_SHOT_EXTERNAL_RISING_EGDES = 3
TSRC_SINGLE_SHOT_EXTERNAL_FALLING_EGDES = 4
TSRC_SINGLE_SHOT = 5
TSRC_LINE = 6

class _dg645Instrument(object):
    def __init__(self, ddg, chan):
        if not isinstance(ddg, dg645_12ID):
            raise TypeError("Don't do that.")
        if isinstance(chan, dg645_12ID.Instruments):
            self._chan = chan.value
        else:
            self._chan = chan
        self._ddg = ddg
        
    @property
    def idx(self):
        """
        Gets the channel identifier number as used for communication
        :return: The communication identification number for the specified
            channel
        :rtype: `int`
        """
        return self._chan

    @property
    def initime(self):
        endtime = self._ddg.channel[2*(self.idx+1)].delay
        if not (endtime[0] == self._ddg.Channels.T0):
            self._ddg.channel[2*(self.idx+1)].delay = (self._ddg.channel[0], endtime[1])
        return endtime[1]

    @initime.setter
    def initime(self, newval):
        self._ddg.channel[2*(self.idx+1)].delay = (self._ddg.channel[0], pq.Quantity(newval, "s"))

    @property
    def duration(self):
        endtime = self._ddg.channel[2*(self.idx+1)+1].delay
        if not (endtime[0] == self._ddg.Channels(2*(self.idx+1))):
            self._ddg.channel[2*(self.idx+1)+1].delay = (self._ddg.channel[2*(self.idx+1)], endtime[1])
        return endtime[1]
        
    @duration.setter
    def duration(self, newval):
        self._ddg.channel[2*(self.idx+1)+1].delay = (self._ddg.channel[2*(self.idx+1)], pq.Quantity(newval, "s"))

    @property
    def polarity(self):
        endtime = self._ddg.output[self.idx].polarity
        return endtime
        
    @polarity.setter
    def polarity(self, newval):
        self._ddg.output[self.idx].polarity = SRSDG645.LevelPolarity(newval)

    @property
    def level_amplitude(self):
        endtime = self._ddg.output[self.idx].level_amplitude
        return endtime
        
    @level_amplitude.setter
    def level_amplitude(self, newval):
        self._ddg.output[self.idx].level_amplitude = newval

    @property
    def level_offset(self):
        endtime = self._ddg.output[self.idx].level_offset
        return endtime
        
    @level_offset.setter
    def level_offset(self, newval):
        self._ddg.output[self.idx].level_offset = newval

    

class dg645_12ID(SRSDG645):
#######################################################
#######################################################
# For DG645 at 12ID-B
# Front 2 BNCs will be used: 
#	AB for shutter
#	CD for Detector 
#	EF for struck
# Need to use "Burtst mode", which determine the number of delay cycle or shots.
# "Single shot triggering" will trigger N bursts.
#######################################################
    
    def __init__(self, filelike):
        super(SRSDG645, self).__init__(filelike)
    # ENUMS #
        
    class Instruments(IntEnum):
        """ 
        definition of instruments
        """
        base = 0
        shutter = 1
        detector = 2
        struck = 3
        inhibitor = 4
    
    @property
    def instrument(self):
        """
        Gets a specific instrument object.
        The desired channel is accessed by passing an EnumValue from
        `~SRSDG645.Channels`. For example, to access channel A:
        >>> import instruments as ik
        >>> inst = ik.srs.SRSDG645.open_gpibusb('/dev/ttyUSB0', 1)
        >>> inst.instrument[inst.Instruments.shutter]
        >>> inst.instrument["shutter"]
        >>> inst.instrument["shutter"].initime
        >>> inst.instrument["shutter"].duration
        >>> inst.instrument["shutter"].initime = 0.1
        See the example in `dg645_12ID` for a more complete example.
        :rtype: `_dg645Instrument`
        """
        return ProxyList(self, _dg645Instrument, dg645_12ID.Instruments)
        
    def pilatus(self, DGexpt, *kwd):
        self.set_pilatus(DGexpt, *kwd)
        self.trigger()
        
    def set_pilatus(self, DGexpt, trigger_source=5,DGNimage=1,Cycperiod=0):
        delaytime = 0
        if (delaytime >= UBZ_SHUTTER_DEADTIME):
            delaytime = delaytime - UBZ_SHUTTER_DEADTIME
        self.instrument["struck"].polarity = 0
        self.instrument["detector"].polarity = 1
        
        self.trigger_source = trigger_source

        if (DGNimage>1):
            if (DGexpt >= Cycperiod):
                raise ValueError("Image period should be longer than the exposure time + 0.004")
        detector_delay = delaytime + UBZ_SHUTTER_DEADTIME
        self.instrument["shutter"].initime = delaytime
        self.instrument["shutter"].duration = DGexpt + UBZ_SHUTTER_DEADTIME*2
        self.instrument["detector"].initime = detector_delay
        self.instrument["detector"].duration = DGexpt
        self.instrument["struck"].initime = detector_delay + DGexpt + 0.00001
        self.instrument["struck"].duration = 0.0001
        self.instrument["inhibitor"].initime = detector_delay
        self.instrument["inhibitor"].duration = DGexpt

        longesttime = delaytime + DGexpt + UBZ_SHUTTER_DEADTIME*2
        if (detector_delay+DGexpt) > longesttime:
            longesttime = detector_delay+DGexpt
        if (Cycperiod < longesttime):
            raise TimeoutError("Cycperiod should be longer than a single pulse")
        Cycdelay = SOFTWARE_INIT_DEADTIME
        
        self.burst_init()

        if ((Cycperiod < DG645_BURST_MAX_TIME) and (DGNimage > 1)):
            self.burst_set(DGNimage, Cycperiod, Cycdelay)
            self.check_error()
        else:
            self.burst_enable = 0
    
    def set_pilatus2(self, DGexpt, DGNimage=1, Cycperiod=0, negativedelay=0, triggerDelay=0, trigger_source=5):
        delaytime = 0
        if (delaytime >= UBZ_SHUTTER_DEADTIME):
            delaytime = delaytime - UBZ_SHUTTER_DEADTIME
        self.instrument["struck"].polarity = 0
        self.instrument["detector"].polarity = 1
        
        self.trigger_source = trigger_source

        dA = negativedelay+0 # shutter
        lAB = DGexpt+UBZ_SHUTTER_DEADTIME*2
        dC = negativedelay+UBZ_SHUTTER_DEADTIME  # Pilatus
        lCD = 0.001
        
        dE = dC		# Struck
        lEF = lCD
        
        dG = triggerDelay
        lGH = DGexpt		# Struck Inhibitor -- this should be come from Detector.

        Cycdelay = SOFTWARE_INIT_DEADTIME
        self.burst_init()
        if (DGNimage > 1):
            if (DGexpt>=Cycperiod):
                raise TimeoutError("Cycperiod should be longer than a single pulse")
    
        self.instrument["shutter"].initime = dA
        self.instrument["shutter"].duration = lAB
        self.instrument["detector"].initime = dC
        self.instrument["detector"].duration = lCD
        self.instrument["struck"].initime = dE
        self.instrument["struck"].duration = lEF
        self.instrument["inhibitor"].initime = dG
        self.instrument["inhibitor"].duration = lGH
        if ((Cycperiod < DG645_BURST_MAX_TIME) and (DGNimage > 1)):
            self.burst_set(DGNimage, Cycperiod, Cycdelay)
            self.check_error()
        else:
            self.burst_enable = 0

    def set_pilatus_fly(self, DGexpt, DGNimage=1, Cycperiod=0,trigger_source=5):

        self.instrument["struck"].polarity = 0
        self.instrument["detector"].polarity = 1

        self.trigger_source = trigger_source

        delaytime = 0
    
        dA = delaytime # shutter
        lAB = DGexpt

        dC = delaytime  # Pilatus
        lCD = DGexpt
	
        dE = dC+DGexpt+0.00001		# Struck channel advance.
        lEF = 0.0001
	
        dG = dC
        lGH = DGexpt
        longesttime = dA+lAB
        if ((dE+lEF) > longesttime) :
            longesttime = dE+lEF
        if (Cycperiod < longesttime) :
            raise TimeoutError("Cycperiod should be longer than a single pulse")

        Cycdelay = SOFTWARE_INIT_DEADTIME

        self.burst_init()
        if (DGNimage > 1):
            if (DGexpt >= Cycperiod):
                raise TimeoutError("Cycperiod should be longer than a single pulse")

        self.instrument["shutter"].initime = dA
        self.instrument["shutter"].duration = lAB
        self.instrument["detector"].initime = dC
        self.instrument["detector"].duration = lCD
        self.instrument["struck"].initime = dE
        self.instrument["struck"].duration = lEF
        self.instrument["inhibitor"].initime = dG
        self.instrument["inhibitor"].duration = lGH

        if ((Cycperiod < DG645_BURST_MAX_TIME) and (DGNimage > 1)):
            self.burst_set(DGNimage, Cycperiod, Cycdelay)
            self.check_error()
        else:
            self.burst_enable = 0

    def PE(self, DGexpt, *kwd):
        self.set_PE(DGexpt, *kwd)
        self.trigger()

    def set_PE(self, DGexpt, *kwd):
        DGNimage = 1
        Cycperiod = DGexpt + 1 # no meaning.        
        if len(kwd)==0:
            self.trigger_source = 5
        elif (len(kwd)==1):
            self.trigger_source = kwd
        elif (len(kwd)>2):
            DGNimage = kwd[0]
            Cycperiod = kwd[1]
        if (len(kwd)==3):
            self.trigger_source = kwd[2]
        if (DGNimage>1):
            if (DGexpt >= Cycperiod):
                raise ValueError("Image period should be longer than the exposure time + 0.004")
        self.instrument["shutter"].initime = 0    
        self.instrument["shutter"].duration = DGexpt+UBZ_SHUTTER_DEADTIME*2
        self.instrument["detector"].initime = UBZ_SHUTTER_DEADTIME
        self.instrument["detector"].duration = DGexpt
        self.instrument["struck"].initime = 0
        self.instrument["struck"].duration = 0.001
        self.instrument["inhibitor"].initime = UBZ_SHUTTER_DEADTIME
        self.instrument["inhibitor"].duration = DGexpt
        Cycdelay = SOFTWARE_INIT_DEADTIME
        if ((Cycperiod < DG645_BURST_MAX_TIME) and (DGNimage > 1)):
            self.burst_set(DGNimage, Cycperiod, Cycdelay)
            self.check_error()
        else:
            self.burst_enable = 0
        
    def struck_terminate(self):
        self.set_pilatus(0.01, 1, 0.1)
        
    def check_error(self):
        rtn = int(self.query("LERR?"))
        if rtn>0:
            raise IOError("Error in setting DG645")
        
    def burst_init(self):
        self.enable_burst_mode = 1
        self.burst_delay = 0
        self.enable_burst_t0_first = 0
    
    def burst_set(self, Ncycle, Period, delay):
        self.burst_cycle = Ncycle
        self.burst_period = Period
        self.burst_delay = delay
        self.burst_enable = 1
            
    @property
    def burst_cycle(self):
        """
        Gets/sets the burst cycle.
        :type: '~quantities.Quantity` or 'float'
        :units: As passed or second
        """
        return pq.Quantity(float(self.query("BURC?")), "s")

    @burst_cycle.setter
    def burst_cycle(self, newval):
        self.sendcmd("BURC {}".format(int(newval)))

    @property
    def burst_period(self):
        """
        Gets/sets the burst period.
        :type: '~quantities.Quantity` or 'float'
        :units: As passed or second
        """
        return pq.Quantity(float(self.query("BURP?")), "s")

    @burst_period.setter
    def burst_period(self, newval):
        self.sendcmd("BURP {}".format(int(newval)))
        
    @property
    def burst_delay(self):
        """
        Gets/sets the burst delay.
        :type: '~quantities.Quantity` or 'float'
        :units: As passed or second
        """
        return pq.Quantity(float(self.query("BURD?")), "s")

    @burst_delay.setter
    def burst_delay(self, newval):
        self.sendcmd("BURD {}".format(int(newval)))

    @property
    def burst_enable(self):
        """
        Gets/sets whether burst is enabled.
        :type: `bool`
        """
        return bool(int(self.query("BURM?")))

    @burst_enable.setter
    def burst_enable(self, newval):
        self.sendcmd("BURM {}".format(1 if newval else 0))
