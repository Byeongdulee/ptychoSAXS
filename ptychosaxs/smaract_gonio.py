import smaract.ctl as ctl
import sys
import time
# installation: 
# 1. download SDK
# 2. Look for python setup.py (C:\SmarAct\MCS2\SDK\Python\packages\smaract.ctl-1.3.36\smaract.ctl-1.3.36)
# 3. python setup.py install

smaract = None

# These codes are based on SmarAct MCS2 programming examples
# Some codes are used without modification
motornames = ["trans1","trans2","tilt1","tilt2"]
motorunits = ["mm","mm","deg","deg"]
def assert_lib_compatibility():
    """
    Checks that the major version numbers of the Python API and the
    loaded shared library are the same to avoid errors due to 
    incompatibilities.
    Raises a RuntimeError if the major version numbers are different.
    """
    vapi = ctl.api_version
    vlib = [int(i) for i in ctl.GetFullVersionString().split('.')]
    if vapi[0] != vlib[0]:
        raise RuntimeError("Incompatible SmarActCTL python api and library version.")

def printMenu():
    print("*******************************************************")
    print("WARNING: make sure the positioner can move freely\n \
            without damaging other equipment!")
    print("*******************************************************")
    print("Enter command and return:")
    print("[?] print this menu")
    print("[c] calibrate")
    print("[f] find reference")
    print("[+] perform movement in positive direction")
    print("[-] perform movement in negative direction")
    print("[s] stop")
    print("[p] get current position")
    print("[0] set move mode: closed loop absolute move")
    print("[1] set move mode: closed loop relative move")
    print("[2] set move mode: open loop scan absolute*")
    print("[3] set move mode: open loop scan relative*")
    print("[4] set move mode: open loop step*")
    print("[5] set control mode: standard mode*")
    print("[6] set control mode: quiet mode*")
    print("  * not available for E-Magnetic Driver channels")
    print("[q] quit")

# CALIBRATION
# The calibration sequence is used to increase the precision of the position calculation. This function
# should be called once for each channel if the mechanical setup changes.
# (e.g. a different positioner was connected to the channel, the positioner type was set to a different type)
# The calibration data will be saved to non-volatile memory, thus it is not necessary to perform the calibration sequence
# on each initialization.
# Note: the "ChannelState.IS_CALIBRATED" in the channel state can be used to determine
# if valid calibration data is stored for the specific channel.

# During the calibration sequence the positioner performs a movement of up to several mm, make sure to not start
# the calibration near a mechanical endstop in order to ensure proper operation.
# See the MCS2 Programmers Guide for more information on the calibration.
def calibrate(channel):
    print("MCS2 start calibration on channel: {}.".format(channel))
    # Set calibration options (start direction: forward)
    ctl.SetProperty_i32(smaract, channel, ctl.Property.CALIBRATION_OPTIONS, 0)
    # Start calibration sequence
    ctl.Calibrate(smaract, channel)
    # Note that the function call returns immediately, without waiting for the movement to complete.
    # The "ChannelState.CALIBRATING" flag in the channel state can be monitored to determine
    # the end of the calibration sequence.
    while True:
        state = ctl.GetProperty_i32(smaract, channel, ctl.Property.CHANNEL_STATE)
        if state & ctl.ChannelState.CALIBRATING:
            time.sleep(0.1)
        else:
            break
# FIND REFERENCE
# Since the position sensors work on an incremental base, the referencing sequence is used to
# establish an absolute positioner reference for the positioner after system startup.
# Note: the "ChannelState.IS_REFERENCED" in the channel state can be used to to decide
# whether it is necessary to perform the referencing sequence.
def findReference(channel):
    print("MCS2 find reference on channel: {}.".format(channel))
    # Set find reference options.
    # The reference options specify the behavior of the find reference sequence.
    # The reference flags can be ORed to build the reference options.
    # By default (options = 0) the positioner returns to the position of the reference mark.
    # Note: In contrast to previous controller systems this is not mandatory.
    # The MCS2 controller is able to find the reference position "on-the-fly".
    # See the MCS2 Programmer Guide for a description of the different modes.
    ctl.SetProperty_i32(smaract, channel, ctl.Property.REFERENCING_OPTIONS, 0)
    # Set velocity to 1mm/s
    ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY, 1000000000)
    # Set acceleration to 10mm/s2.
    ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION, 10000000000)
    # Start referencing sequence
    ctl.Reference(smaract, channel)
    # Note that the function call returns immediately, without waiting for the movement to complete.
    # The "ChannelState.REFERENCING" flag in the channel state can be monitored to determine
    # the end of the referencing sequence.
    while True:
        state = ctl.GetProperty_i32(smaract, channel, ctl.Property.CHANNEL_STATE)
        if state & ctl.ChannelState.REFERENCING:
            time.sleep(0.1)
        else:
            break
def mv(ax, target, wait=True):
    if type(ax) == str:
        ax = motornames.index(ax)
    #ax = channels[chname]
    move(ax, target=target, absolute=True, wait=wait)
    
def mvr(ax, target, wait=True):
    if type(ax) == str:
        ax = motornames.index(ax)
    #ax = channels[chname]
    move(ax, target=target, absolute=False, wait=wait)

def set_speed(channel, vel=5, acc=10):
    if type(channel) == str:
        channel = motornames.index(channel)

    # input vel and acc should be in mm/s and mm/s^2
    vel = int(vel*1E9)
    acc = int(acc*1E9)
    # velocity 1000000000 equals 1mm/s
    # acceler  10000000000 equals 10mm/s2.
    ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY, vel)
    ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION, acc)

def get_speed(channel):
    if type(channel) == str:
        channel = motornames.index(channel)
    vel = ctl.GetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY)
    acc = ctl.GetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION)
    return (vel/1E9, acc/1E9)

# MOVE
# The move command instructs a positioner to perform a movement.
# The given "move_value" parameter is interpreted according to the previously configured move mode.
# It can be a position value (in case of closed loop movement mode), a scan value (in case of scan move mode)
# or a number of steps (in case of step move mode).
def move(channel, target=0.001, absolute=True, wait=True):
    # input target is in mm or deg.
    target = int(target*1E9)
    # Set move mode depending properties for the next movement.
    if absolute:
        move_mode = ctl.MoveMode.CL_ABSOLUTE
        # Set move velocity [in pm/s].
#        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY, 1000000000)
        # Set move acceleration [in pm/s2].
#        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION, 1000000000)
        # Specify absolute position [in pm].
        # (For Piezo Scanner channels adjust to valid value within move range, e.g. +-10000000.)

#        print("MCS2 move channel {} to absolute position: {} pm.".format(channel, target))
    else:
        move_mode = ctl.MoveMode.CL_RELATIVE
        # Set move velocity [in pm/s].
#        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY, 500000000)
        # Set move acceleration [in pm/s2].
#        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION, 10000000000)
        # Specify relative position distance [in pm] and direction.
        # (For Piezo Scanner channels adjust to valid value within move range, e.g. 10000000.)
#        print("MCS2 move channel {} relative: {} pm.".format(channel, target))

    # Start actual movement.
    ctl.SetProperty_i32(smaract, channel, ctl.Property.MOVE_MODE, move_mode)
    ctl.Move(smaract, channel, target, 0)
    # Note that the function call returns immediately, without waiting for the movement to complete.
    if wait:
        while ismoving(channel):
            time.sleep(0.01)
    # The "ChannelState.ACTIVELY_MOVING" (and "ChannelState.CLOSED_LOOP_ACTIVE") flag in the channel state
    # can be monitored to determine the end of the movement.

# STOP
# This command stops any ongoing movement. It also stops the hold position feature of a closed loop command.
# Note for closed loop movements with acceleration control enabled:
# The first "stop" command sent while moving triggers the positioner to come to a halt by decelerating to zero.
# A second "stop" command triggers a hard stop ("emergency stop").
def stop(channel):
    if type(channel) == str:
        channel = motornames.index(channel)
    print("MCS2 stop channel: {}.".format(channel))
    ctl.Stop(smaract, channel)

# Read the version of the library
# Note: this is the only function that does not require the library to be initialized.
version = ctl.GetFullVersionString()
print("SmarActCTL library version: '{}'.".format(version))

# Find available MCS2 devices
smaractstage = 'network:sn:MCS2-00012316'
try:
    buffer = ctl.FindDevices()
    print("")
    if not (smaractstage in buffer):
        print("MCS2 no devices found.")
    print("MCS2 is found.")
    print(buffer)
    print("")
except:
    pass

channels = [0, 1, 2, 3]
base_units = []
units = []

def _get_unit(channel):
    base_unit = ctl.GetProperty_i32(smaract, channel, ctl.Property.POS_BASE_UNIT)
    return base_unit

def get_unit(channel):
    base_unit = _get_unit(channel)
    if base_unit == ctl.BaseUnit.METER:
        return "mm", base_unit
    else: 
        return "deg", base_unit


    # The move mode states the type of movement performed when sending the "Move" command.
#move_mode = ctl.MoveMode.CL_ABSOLUTE

def isconnected(ax=-1):
    # Now we read the state for all available channels.
    # The passed "idx" parameter (the channel index in this case) is zero-based.
    if ax>-1:
        state = ctl.GetProperty_i32(smaract, ax, ctl.Property.CHANNEL_STATE)
        if state & ctl.ChannelState.SENSOR_PRESENT:
            return True
        else:
            return False
    status = []
    for channel in channels:
        state = ctl.GetProperty_i32(smaract, channel, ctl.Property.CHANNEL_STATE)
        # The returned channel state holds a bit field of several state flags.
        # See the MCS2 Programmers Guide for the meaning of all state flags.
        # We pick the "sensorPresent" flag to check if there is a positioner connected
        # which has an integrated sensor.
        # Note that in contrast to previous controller systems the controller supports
        # hotplugging of the sensor module and the actuators.
        if state & ctl.ChannelState.SENSOR_PRESENT:
            status.append(True)
            print("MCS2 channel {} has a sensor.".format(channel))
        else:
            status.append(False)
            print("MCS2 channel {} has no sensor.".format(channel))
    return status

# try:
#     # First we want to know if the configured positioner type is a linear or a rotatory type.
#     # For this purpose we can read the base unit property.
#     base_unit = ctl.GetProperty_i32(smaract, channel, ctl.Property.POS_BASE_UNIT)

#     # Next we read the current position of channel 0. Position values have the data type int64,
#     # thus we need to use "getProperty_i64".
#     # Note that there is no distinction between linear and rotatory positioners regarding the functions which
#     # need to be used (getPosition / getAngle) and there is no additional "revolutions" parameter for rotatory positioners
#     # as it was in the previous controller systems.
#     # Depending on the preceding read base unit, the position is in pico meter [pm] for linear positioners
#     # or nano degree [ndeg] for rotatory positioners.
#     # Note: it is also possible to read the base resolution of the unit using the property key "POS_BASE_RESOLUTION".
#     # To keep things simple this is not shown in this example.
#     position = ctl.GetProperty_i64(d_handle, channel, ctl.Property.POSITION)
#     print("MCS2 position of channel {}: {}".format(channel, position), end='')
#     print("pm.") if base_unit == ctl.BaseUnit.METER else print("ndeg.")

#     # To show the use of the setProperty function, we set the position to 100 um respectively 100 milli degree.
#     # This is the synchronous (blocking) method. The function call blocks until the property value was sent to
#     # the controller and the reply was received.
#     position = 100000000 # in pm | ndeg
#     print("MCS2 set position of channel {} to {}".format(channel, position), end='')
#     print("pm.") if base_unit == ctl.BaseUnit.METER else print("ndeg.")
#     ctl.SetProperty_i64(smaract, channel, ctl.Property.POSITION, position)

    # Now we want to read the the position again (and the channel state in addition).
    # This time we use the asynchronous (non-blocking) method.
    # This method requires two function calls for getting one property value.
    # One for requesting the property value and one for retrieving the answer.
    # The advantage of this method is that the application may request several property values in fast
    # succession and then perform other tasks before blocking on the reception of the results.

    # Received values can later be accessed via the obtained request ID and the corresponding ReadProperty functions.

    # The tHandle parameter is used for output buffering with the CreateOutputBuffer and FlushOutputBuffer functions.
    # (Not shown in this example.) By passing the transmit handle the request is associated with the output buffer
    # and therefore only sent when the buffer is flushed.
    # The transmit handle must be set to zero if output buffers are not used.
# except ctl.Error as e:
#     # Catching the "ctl.Error" exceptions may be used to handle errors of SmarActCTL function calls.
#     # The "e.func" element holds the name of the function that caused the error and
#     # the "e.code" element holds the error code.
#     # Passing an error code to "GetResultInfo" returns a human readable string specifying the error.
#     print("MCS2 {}: {}, error: {} (0x{:04X}) in line: {}."
#           .format(e.func, ctl.GetResultInfo(e.code), ctl.ErrorCode(e.code).name, e.code, (sys.exc_info()[-1].tb_lineno)))

# except Exception as ex:
#     print("Unexpected error: {}, {} in line: {}".format(ex, type(ex), (sys.exc_info()[-1].tb_lineno)))
#     raise
    
# r_pos_handle = []
# r_state_handle = []

# def _set_poshandles():
#     # Issue requests for the two properties "position" and "channel state".
#     for ch in channels:
#         r_id1 = ctl.RequestReadProperty(smaract, ch, ctl.Property.POSITION, 0)
#     # The function call returns immediately, allowing the application to issue another request or to perform other tasks.
#     # We simply request a second property. (the channel state in this case)
#         r_id2 = ctl.RequestReadProperty(smaract, ch, ctl.Property.CHANNEL_STATE, 0)
#         r_pos_handle.append(r_id1)
#         r_state_handle.append(r_id2)

#     # ...process other tasks...

#     # Receive the results
#     # While the request-function is non-blocking the read-functions block until the desired data has arrived.
#     # Note that we must use the correct "ReadProperty_ixx" function depending on the datatype of the requested property.
#     # Otherwise a ctl.ErrorCode.INVALID_DATA_TYPE error is returned.

# _set_poshandles()

def get_pos(ax):
    # return position in deg or mm
    if type(ax) == str:
        ax = motornames.index(ax)
    try:
        r_id1 = ctl.RequestReadProperty(smaract, ax, ctl.Property.POSITION, 0)
        position = ctl.ReadProperty_i64(smaract, r_id1)

        # Print the results
        #print("MCS2 current position of channel {}: {}".format(ax, position), end='')
        #print("pm.") if base_units[ax] == ctl.BaseUnit.METER else print("ndeg.")
        return position/1E9
    except ctl.Error as e:
        # Catching the "ctl.Error" exceptions may be used to handle errors of SmarActCTL function calls.
        # The "e.func" element holds the name of the function that caused the error and
        # the "e.code" element holds the error code.
        # Passing an error code to "GetResultInfo" returns a human readable string specifying the error.
        print("MCS2 {}: {}, error: {} (0x{:04X}) in line: {}."
            .format(e.func, ctl.GetResultInfo(e.code), ctl.ErrorCode(e.code).name, e.code, (sys.exc_info()[-1].tb_lineno)))

    except Exception as ex:
        print("Unexpected error: {}, {} in line: {}".format(ex, type(ex), (sys.exc_info()[-1].tb_lineno)))
        raise

def set_pos(ax, position=0):
    if type(ax) == str:
        ax = motornames.index(ax)

    pos=position*1E9
    # For the sake of completeness, finally we use the asynchronous (non-blocking) write function to
    # set the position to -0.1 mm respectively -100 degree.
#    position = -100000000
    print("MCS2 set position of channel {} to {}".format(ax, pos), end='')
    print("pm.") if base_units[ax] == ctl.BaseUnit.METER else print("ndeg.")
    r_id = ctl.RequestWriteProperty_i64(smaract, ax, ctl.Property.POSITION, pos)
    # The function call returns immediately, without waiting for the reply from the controller.
    # ...process other tasks...

    # Wait for the result to arrive.
    ctl.WaitForWrite(smaract, r_id)
    return get_pos(ax)

    # Alternatively, the "call-and-forget" mechanism for asynchronous (non-blocking) write functions
    # may be used:
    # For property writes the result is only used to report errors. With the call-and-forget mechanism
    # the device does not generate a result for writes and the application can continue processing other
    # tasks immediately. Compared to asynchronous accesses, the application doesn’t need to keep
    # track of open requests and collect the results at some point. This mode should be used with care
    # so that written values are within the valid range.
    # The call-and-forget mechanism is activated by passing "False" to the optional pass_rID parameter of the
    # RequestWriteProperty_x functions.
#    ctl.RequestWriteProperty_i64(d_handle, channel, ctl.Property.POSITION, position, pass_rID = False)
    # No result must be requested with the WaitForWrite function in this case.

def ismoving(ax):
    if type(ax) == str:
        ax = channels.index(ax)
    r_id2 = ctl.RequestReadProperty(smaract, ax, ctl.Property.CHANNEL_STATE, 0)
    state = ctl.ReadProperty_i32(smaract, r_id2)
    if (state & ctl.ChannelState.ACTIVELY_MOVING) == 0:
        return False
#        print("MCS2 channel {} is stopped.".format(ax))
    else:
        return True
    

channel_names = []
try:
    # Open the first MCS2 device from the list
    smaract = ctl.Open(smaractstage)
    print("MCS2 opened {}.".format(smaractstage))
    trnum=0
    tinum=0
    for ch in channels:
        ctl.SetProperty_i32(smaract, ch, ctl.Property.MAX_CL_FREQUENCY, 6000)
        ctl.SetProperty_i32(smaract, ch, ctl.Property.HOLD_TIME, 1000)
        set_speed(ch)  # return the speed and acc to defaults (1mm/s, 10mm/s2)
        un, base_unit = get_unit(ch)
        base_units.append(base_unit)
        units.append(un)
        if un == 'mm':
            name0 = 'trans'
            trnum += 1
            n = trnum
        elif un == "deg":
            name0 = 'tilt'
            tinum += 1
            n = tinum
        else:
            name0 = 'None'
            k = k+1
            n = k
        channel_names.append("%s%i"%(name0, n))
except:
    channel_names = []
    units = []
    pass

