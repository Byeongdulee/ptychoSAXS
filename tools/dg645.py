from instruments.srs import SRSDG645
import quantities as pq
from enum import IntEnum
from instruments.util_fns import ProxyList
import time

BEAMLINE_12IDB = 0
BEAMLINE_12IDC = 1

ADDRESS_12IDB = "tcpip://164.54.122.66:5025" # 12idb
ADDRESS_12IDC = "tcpip://dg645.xray.aps.anl.gov:5025" # 12idc 
#DG = ik.srs.SRSDG645.open_from_uri(address)
UBZ_SHUTTER_DEADTIME = 0.005   # 5 ms.
SOFTWARE_INIT_DEADTIME = 0.001
DG645_BURST_MAX_TIME = 41
# Example:
# import dg645 as DG
# a = DG.dg645_12ID.open_from_uri(DG.addressB)
# a.pilatus(0.1, 5, 2)
# a.instrument["shutter"].delay
# a.instrument["shutter"].pulsewidth
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

INHIBIT_OFF = 0
INHIBIT_TRIGGERS = 1
INHIBIT_AB = 2
INHIBIT_AB_AND_CD = 3
INHIBIT_AB_CD_AND_EF = 4
INHIBIT_AB_CD_EF_AND_GH = 5

PRESCALE_TRIGGER_INPUT = 0
PRESCALE_OUTPUT_AB = 1
PRESCALE_OUTPUT_CD = 2
PRESCALE_OUTPUT_EF = 3
PRESCALE_OUTPUT_GH = 4

INSTRUMENT_STATUS = {0: 'A trigger has been detected', 
                    1: 'A trigger was detected while a delay or burst cycle was in progress.', 
                    2: 'A delay cycle has completed.', 
                    3: 'A burst of delay cycles has completed.',
                    4: 'A delay cycle was inhibited.', 
                    5: 'A delay cycle was aborted prematurely in order to change instrument delay settings.', 
                    6: 'The 100 MHz PLL came unlocked.', 
                    7: 'The Rb timebase came unlocked. '}
STANDARD_EVENT_STATUS = {0: 'Operation complete. All previous commands have completed.', 
                    1: 'Reserved', 
                    2: 'Query error occurred.', 
                    3: 'Device dependent error.',
                    4: 'Execution error. A command failed to execute correctly because a parameter was out of range. ', 
                    5: 'Command error. The parser detected a syntax error ', 
                    6: 'Reserved', 
                    7: 'Power on. The CG635 has been power cycled. '}
SERIAL_POLL_STATUS = {0: 'An unmasked bit in the instrument status register (INSR) has been set. ', 
                    1: 'Set if a delay cycle is in progress. Otherwise cleared.', 
                    2: 'Set if a burst cycle is in progress. Otherwise cleared.', 
                    3: '',
                    4: 'The interface output buffer is non-empty.', 
                    5: 'An unmasked bit in the standard event status register (*ESR) has been set.', 
                    6: 'Master summary bit. Indicates that the CG635 is requesting service because an unmasked bit in this register has been set. ', 
                    7: ''}
ERRORS = {0: 'No Error',
          10: 'Illigal Value',
          11: 'Illigal Mode',
          12: 'Illigal Delay',
          13: 'Illigal Link',
          14: 'Recall Failed',
          15: 'Not Allowed',
          16: 'Failed Self Test',
          17: 'Failed Auto Calibration',
          30: 'Lost Data',
          32: 'No Listener',
          110: 'Illegal Command',
          111: 'Undefined Command',
          112: 'Illegal Query',
          113: 'Illegal Set',
          114: 'Null Parameter',
          115: 'Extra Parameters',
          116: 'Missing Parameters',
          117: 'Parameter Overflow',
          118: 'Invalid Floating Point Number',
          120: 'Invalid Integer',
          121: 'Integer Overflow',
          122: 'Invalid Hexadecimal',
          126: 'Syntax Error',
          170: 'Communication Error',
          171: 'Over run',
          254: 'Too Many Errors'}

class DG645_Error(Exception):
    pass

'''
Trigger holdoff : 
Trigger holdoff sets the minimum allowed time between successive triggers.
For example, if the trigger holdoff is set to 10 µs, then successive triggers 
will be ignored until at least 10 µs have passed since the last trigger. 
The red RATE LED will flash with each ignored trigger. Specifying holdoff is 
useful if a trigger event in your application generates a significant noise 
transient that must have time to decay away before the next trigger is generated.  
Trigger holdoff can also be used to trigger the DG645 at a sub-multiple of 
a known input trigger rate. For example, by selecting LINE as the trigger source 
and setting the holdoff to 0.99 s, the DG645 can be triggered synchronously with 
the power line, but at 1 Hz. This technique works as long as the timebase of 
the trigger source doesn't vary significantly relative to the DG645's timebase. 
Otherwise, trigger prescaling should be used. 
Note that trigger holdoff is available only after advanced triggering is enabled. 
Once advanced triggering is enabled, the user can view and modify the trigger 
holdoff by successively pressing the 'TRIG' key in the DISPLAY section of 
the front panel until the display prefix is 'HOLD'. 
If the trigger holdoff is 10 µs, then the main display will show 'HOLD 0.000010000000', 
and the STATUS LED just below the main display will be highlighted. 
Once displayed, the user can modify the trigger holdoff using any of the methods 
discussed in the section Front-Panel Interface earlier in this chapter. 


Trigger Prescaling: (Not available)
The DG645 supports a number of complex triggering requirements through a set of 
prescaling registers. Trigger prescaling enables the DG645 to be triggered synchronously 
with a much faster source, but at a sub-multiple of the original trigger frequency. 
For example, the DG645 can be triggered at 1 kHz, but synchronously with a mode locked laser 
running at 80 MHz, by prescaling the trigger input by 80,000.  
Furthermore, the DG645 also contains a separate prescaler for each front panel output 
that enables it to operate at a sub-multiple of the prescaled trigger input frequency. 
Continuing with the example above, if the AB prescaler is set to 100, the AB output will 
only be enabled for 1 out of every 100 delay cycles which is equivalent to a rate of 
1 kHz/100 = 10 Hz. 
Lastly, the DG645 contains a separate phase register for each output prescaler 
that determines the phase of the prescaler output relative to the other prescaled outputs. 
For example, if both the AB and CD prescalers are set to 100 and their phase registers to 
0 and 50, respectively, then AB and CD will both run at 10 Hz, but CD's output will be 
enabled 50 delay cycles after AB's output. 
'''
def connect(beamline=BEAMLINE_12IDB):
    if beamline==BEAMLINE_12IDB:
        addr = ADDRESS_12IDB
    if beamline==BEAMLINE_12IDC:
        addr = ADDRESS_12IDC
    return dg645_12ID.open_from_uri(addr)

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
    def name(self):
        """
        Gets the channel identifier number as used for communication
        :return: The communication identification number for the specified
            channel
        :rtype: `int`
        """
        return self._ddg.instrument._valid_set(self.idx).__dict__['_name_']

    @property
    def delay(self):
        endtime = self._ddg.channel[2*self.idx].delay
#        if not (endtime[0] == self._ddg.Channels.T0):
#            self._ddg.channel[2*self.idx+1].delay = (self._ddg.channel[0], endtime[1])
        return endtime[1]

    @delay.setter
    def delay(self, newval):
        #self._ddg.channel[2*self.idx+1].delay = (self._ddg.channel[0], pq.Quantity(newval, "s"))
        self._ddg.channel[2*self.idx].delay = (self._ddg.channel[0], newval)

    @property
    def pulsewidth(self):
        endtime = self._ddg.channel[2*self.idx+1].delay
#        if not (endtime[0] == self._ddg.Channels((2*self.idx+1))):
#            self._ddg.channel[2*self.idx+1].delay = (self._ddg.channel[2*self.idx], endtime[1])
        return endtime[1]
        
    @pulsewidth.setter
    def pulsewidth(self, newval):
#        self._ddg.channel[2*self.idx+1].delay = (self._ddg.channel[2*self.idx], pq.Quantity(newval, "s"))
        self._ddg.channel[2*self.idx+1].delay = (self._ddg.channel[2*self.idx], newval)

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
    '''
        For DG645 at 12ID-B
        Front 2 BNCs will be used: 
            AB for shutter
            CD for detectors 
            EF for struck
            GH for inhibitor
            
        Need to use "Burtst mode", which determine the number of delay cycle or shots.
        "Single shot triggering" will trigger N bursts.
    '''
    
    def __init__(self, filelike):
        super(SRSDG645, self).__init__(filelike)
        self._exposuretime = 0
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
        >>> inst.instrument["shutter"].delay
        >>> inst.instrument["shutter"].pulsewidth
        >>> inst.instrument["shutter"].delay = 0.1
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
        self.instrument["shutter"].delay = delaytime
        self.instrument["shutter"].pulsewidth = DGexpt + UBZ_SHUTTER_DEADTIME*2
        self.instrument["detector"].delay = detector_delay
        self.instrument["detector"].pulsewidth = DGexpt
        self.instrument["struck"].delay = detector_delay + DGexpt + 0.00001
        self.instrument["struck"].pulsewidth = 0.0001
        self.instrument["inhibitor"].delay = detector_delay
        self.instrument["inhibitor"].pulsewidth = DGexpt

        if Cycperiod == 0:
            self.enable_burst_mode = 0
            return
        
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
    
        self.instrument["shutter"].delay = dA
        self.instrument["shutter"].pulsewidth = lAB
        self.instrument["detector"].delay = dC
        self.instrument["detector"].pulsewidth = lCD
        self.instrument["struck"].delay = dE
        self.instrument["struck"].pulsewidth = lEF
        self.instrument["inhibitor"].delay = dG
        self.instrument["inhibitor"].pulsewidth = lGH
        if ((Cycperiod < DG645_BURST_MAX_TIME) and (DGNimage > 1)):
            self.burst_set(DGNimage, Cycperiod, Cycdelay)
            self.check_error()
        else:
            self.burst_enable = 0

    def set_pilatus_fly(self, DGexpt=0.001, trigger_source=TSRC_EXTERNAL_RISING_EGDES):
        # this mode can be used for a fly scan.
        # leave the shutter open
        # trigger DG with external risign edge
        # default exposure time is 0.001 # 1ms long pulse.

        self.instrument["struck"].polarity = 0
        self.instrument["detector"].polarity = 1

        self.trigger_source = trigger_source

        delaytime = 0
        self._exposuretime = DGexpt
    
        dA = delaytime # shutter
        lAB = DGexpt

        dC = delaytime  # Pilatus
        lCD = DGexpt
	
        dE = dC+DGexpt+0.00001		# Struck channel advance.
        lEF = 0.0001
	
        dG = dC
        lGH = DGexpt

        self.instrument["shutter"].delay = dA
        self.instrument["shutter"].pulsewidth = lAB
        self.instrument["detector"].delay = dC
        self.instrument["detector"].pulsewidth = lCD
        self.instrument["struck"].delay = dE
        self.instrument["struck"].pulsewidth = lEF
        self.instrument["inhibitor"].delay = dG
        self.instrument["inhibitor"].pulsewidth = lGH
        self.enable_burst_mode = 0

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
        self.instrument["shutter"].delay = 0    
        self.instrument["shutter"].pulsewidth = DGexpt+UBZ_SHUTTER_DEADTIME*2
        self.instrument["detector"].delay = UBZ_SHUTTER_DEADTIME
        self.instrument["detector"].pulsewidth = DGexpt
        self.instrument["struck"].delay = 0
        self.instrument["struck"].pulsewidth = 0.001
        self.instrument["inhibitor"].delay = UBZ_SHUTTER_DEADTIME
        self.instrument["inhibitor"].pulsewidth = DGexpt
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
            try:
                errorstr = ERRORS[rtn]
            except:
                if (rtn>=40) and (rtn<100):
                    errorstr = "Device Dependent Error Code %i"%rtn
                else:
                    errorstr = "Unknown Code"
            raise DG645_Error(errorstr)
    
    def clear(self):
        self.sendcmd('*CLS')

    def burst_init(self):
        self.enable_burst_mode = 1
        self.burst_delay = 0
        self.enable_burst_t0_first = 0
    
    def burst_set(self, Ncycle, Period, delay):
        self.burst_cycle = Ncycle
#        self.check_error()
        self.burst_period = Period
#        self.check_error()
        self.burst_delay = delay
#        self.check_error()
        self.burst_enable = 1
#        self.check_error()
    
    def disp(self):
        print('')
        print('Delays:')
        for each in range(self.instrument.__len__()):
            resp = self.instrument[each].delay
            print("\t%i:  %s\t: %s" % (each, self.instrument[each].name, resp))
        print('')
        print('Amplitudes:')
        for each in range(self.instrument.__len__()):
            amp = self.instrument[each].level_amplitude
            print("\t%i:  %s\t: %s" % (each, self.instrument[each].name, amp))
        print('')
        print('Polarities:')
        for each in range(self.instrument.__len__()):
            pol = self.instrument[each].polarity
            print("\t%i:  %s\t: %s" % (each, self.instrument[each].name, pol))

    def get_status(self):
        resp = self.query("*STB?\n")
        code = bin(int(resp))
        code = code[2:]
        print("Serial Poll STATUS: %s"%code)
        for idx in range(len(code)):
            if code[-idx-1] == '1':
                print("\t%s"%SERIAL_POLL_STATUS[idx])

        resp = self.query("*ESR?\n")
        code = bin(int(resp))
        code = code[2:]
        print("Standard Event STATUS: %s"%code)
        for idx in range(len(code)):
            if code[-idx-1] == '1':
                print("\t%s"%STANDARD_EVENT_STATUS[idx])

        resp = self.query("INSR?\n")
        code = bin(int(resp))
        code = code[2:]
        print("Instrument STATUS CODE: %s"%code)
        for idx in range(len(code)):
            if code[-idx-1] == '1':
                print("\t%s"%INSTRUMENT_STATUS[idx])

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
        self.sendcmd("BURP {}".format(float(newval)))
        
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
        self.sendcmd("BURD {}".format(float(newval)))

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

    @property
    def inhibit(self):
        """
        Gets/sets whether inhibit.
        use after enable_adv_triggering
        This requires inhibitor signal to the BNC in the backpanel.
        :type: `int`
        """
        return int(self.query("INHB?"))

    @inhibit.setter
    def inhibit(self, newval=INHIBIT_TRIGGERS):
        self.sendcmd("INHB {}".format(int(newval)))

    '''Prescale is not available for my dg645.'''
    # @property
    # def prescale_factor(self):
    #     """
    #     Gets/sets prescale factor.
    #     use after enable_adv_triggering
    #     :type: `int`
    #     """
    #     resp = self.query("PRES?").split(",")
    #     return (int(resp[0]), int(resp[1]))

    # @prescale_factor.setter
    # def prescale_factor(self, newval=PRESCALE_TRIGGER_INPUT, factor=10):
    #     # default: taking only every 10 trigger inputs. 
    #     self.sendcmd("PRES {},{}".format(int(newval), int(factor)))
