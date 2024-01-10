from instruments.srs import SRSDG645
import quantities as pq
from enum import IntEnum
from instruments.util_fns import ProxyList
address = "tcpip://164.54.122.66:5025"
#DG = ik.srs.SRSDG645.open_from_uri(address)
UBZ_shutter_deadtime = 0.005   # 5 ms.
software_init_deadtime = 0.001
DG645_BurstmaxtimeLimit = 41
# Example:
# import dg645 as DG
# a = DG.dg645_12ID.open_from_uri(DG.address)
# a.pilatus(0.1, 5, 2)
# a.instrument["shutter"].initime
# a.instrument["shutter"].duration

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
        shutter = 0
        detector = 1
        struck = 2
        inhibitor = 3
    
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
        
    def set_pilatus(self, DGexpt, *kwd):
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
        self.instrument["shutter"].duration = DGexpt+UBZ_shutter_deadtime*2
        self.instrument["detector"].initime = UBZ_shutter_deadtime
        self.instrument["detector"].duration = 0.001
        self.instrument["struck"].initime = UBZ_shutter_deadtime
        self.instrument["struck"].duration = 0.001
        self.instrument["inhibitor"].initime = 0
        self.instrument["inhibitor"].duration = 0
        Cycdelay = software_init_deadtime
        if ((Cycperiod < DG645_BurstmaxtimeLimit) and (DGNimage > 1)):
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
        self.instrument["shutter"].duration = DGexpt+UBZ_shutter_deadtime*2
        self.instrument["detector"].initime = UBZ_shutter_deadtime
        self.instrument["detector"].duration = DGexpt
        self.instrument["struck"].initime = 0
        self.instrument["struck"].duration = 0.001
        self.instrument["inhibitor"].initime = UBZ_shutter_deadtime
        self.instrument["inhibitor"].duration = DGexpt
        Cycdelay = software_init_deadtime
        if ((Cycperiod < DG645_BurstmaxtimeLimit) and (DGNimage > 1)):
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
