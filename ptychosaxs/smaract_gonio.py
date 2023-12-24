import smaract.ctl as ctl

smaract = None

# SmarAct MCS2 programming example: Movement
#
# This programming example shows you how to
# find available MCS2 devices to connect to
# and how to perform different movement commands.
# For a full command reference see the MCS2 Programmers Guide.

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

# MOVE
# The move command instructs a positioner to perform a movement.
# The given "move_value" parameter is interpreted according to the previously configured move mode.
# It can be a position value (in case of closed loop movement mode), a scan value (in case of scan move mode)
# or a number of steps (in case of step move mode).
def move(channel, target=0.001, absolute=True, wait=True):
    # input target is in mm
    # this stage requires pm...
    target = target*1E9
    # Set move mode depending properties for the next movement.
    if absolute:
        #move_mode = ctl.MoveMode.CL_ABSOLUTE
        # Set move velocity [in pm/s].
        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY, 1000000000)
        # Set move acceleration [in pm/s2].
        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION, 1000000000)
        # Specify absolute position [in pm].
        # (For Piezo Scanner channels adjust to valid value within move range, e.g. +-10000000.)

        print("MCS2 move channel {} to absolute position: {} pm.".format(channel, target))
    else:
        #move_mode = ctl.MoveMode.CL_RELATIVE
        # Set move velocity [in pm/s].
        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_VELOCITY, 500000000)
        # Set move acceleration [in pm/s2].
        ctl.SetProperty_i64(smaract, channel, ctl.Property.MOVE_ACCELERATION, 10000000000)
        # Specify relative position distance [in pm] and direction.
        # (For Piezo Scanner channels adjust to valid value within move range, e.g. 10000000.)
        print("MCS2 move channel {} relative: {} pm.".format(channel, target))

    # Start actual movement.
    ctl.Move(smaract, channel, target, 0)
    # Note that the function call returns immediately, without waiting for the movement to complete.
    if wait:
        ctl.ChannelState()
    # The "ChannelState.ACTIVELY_MOVING" (and "ChannelState.CLOSED_LOOP_ACTIVE") flag in the channel state
    # can be monitored to determine the end of the movement.

# STOP
# This command stops any ongoing movement. It also stops the hold position feature of a closed loop command.
# Note for closed loop movements with acceleration control enabled:
# The first "stop" command sent while moving triggers the positioner to come to a halt by decelerating to zero.
# A second "stop" command triggers a hard stop ("emergency stop").
def stop(channel):
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
    if not (smaractstage in buffer):
        print("MCS2 no devices found.")
except:
    pass


try:
    # Open the first MCS2 device from the list
    smaract = ctl.Open(smaractstage)
    print("MCS2 opened {}.".format(smaractstage))
except:
    pass

channels = [0, 1, 2, 3]
for ch in channels:
    ctl.SetProperty_i32(smaract, ch, ctl.Property.MAX_CL_FREQUENCY, 6000)
    ctl.SetProperty_i32(smaract, ch, ctl.Property.HOLD_TIME, 1000)

    # The move mode states the type of movement performed when sending the "Move" command.
move_mode = ctl.MoveMode.CL_ABSOLUTE