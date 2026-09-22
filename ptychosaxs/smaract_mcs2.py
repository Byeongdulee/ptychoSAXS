"""
Generic, reusable wrapper around the SmarAct MCS2 (smaract.ctl) SDK.

Ports the same SDK call idioms already proven in smaract_gonio.py (property
request/read for position, SetProperty for move mode/speed, Move/Stop,
picometer/nanodegree <-> mm/deg conversion via *1E9 / /1E9) into a class
that:
  - knows nothing about how many channels exist or what they're named
    (that's the caller's concern - see smaract_crl3dprint.py for a
    device-specific config built on top of this),
  - never touches the network at import time - only connect() does,
  - can be instantiated more than once / reused across rigs.

Installation (see smaract_gonio.py for the original notes):
  1. Download the SmarAct MCS2 SDK.
  2. Find the Python package folder (C:\\SmarAct\\MCS2\\SDK\\Python\\packages\\).
  3. python -m pip install smaract.<productname>-<version>.zip
"""

import time

import smaract.ctl as ctl


class SmaractMCS2Controller:
    """Generic per-channel controller for one SmarAct MCS2 network device.

    All positions are floats in the channel's native unit (mm for linear
    channels, deg for rotary channels) - converted to/from the SDK's
    integer picometer/nanodegree representation internally.
    """

    def __init__(self, locator: str):
        self.locator = locator
        self._handle = None  # set only by connect() - importing this module never connects

    # -- connection lifecycle ------------------------------------------------

    def connect(self) -> None:
        """Open the MCS2 device at self.locator. Raises on failure."""
        self._handle = ctl.Open(self.locator)

    def disconnect(self) -> None:
        if self._handle is not None:
            ctl.Close(self._handle)
            self._handle = None

    def is_connected(self) -> bool:
        return self._handle is not None

    # -- motion ----------------------------------------------------------------

    def get_pos(self, channel: int) -> float:
        """Return the current position in mm or deg."""
        r_id = ctl.RequestReadProperty(self._handle, channel, ctl.Property.POSITION, 0)
        raw = ctl.ReadProperty_i64(self._handle, r_id)
        return raw / 1e9

    def mv(self, channel: int, target: float, wait: bool = True) -> None:
        """Move to an absolute position (mm or deg)."""
        self._move(channel, target, absolute=True, wait=wait)

    def mvr(self, channel: int, delta: float, wait: bool = True) -> None:
        """Move by a relative amount (mm or deg)."""
        self._move(channel, delta, absolute=False, wait=wait)

    def _move(self, channel: int, value: float, absolute: bool, wait: bool) -> None:
        raw = int(value * 1e9)
        move_mode = ctl.MoveMode.CL_ABSOLUTE if absolute else ctl.MoveMode.CL_RELATIVE
        ctl.SetProperty_i32(self._handle, channel, ctl.Property.MOVE_MODE, move_mode)
        ctl.Move(self._handle, channel, raw, 0)
        if wait:
            while self.is_moving(channel):
                time.sleep(0.01)

    def stop(self, channel: int) -> None:
        ctl.Stop(self._handle, channel)

    def set_pos(self, channel: int, position: float = 0.0) -> float:
        """Redefine the current physical position as `position` (mm or deg)."""
        raw = int(position * 1e9)
        r_id = ctl.RequestWriteProperty_i64(self._handle, channel, ctl.Property.POSITION, raw)
        ctl.WaitForWrite(self._handle, r_id)
        return self.get_pos(channel)

    def is_moving(self, channel: int) -> bool:
        r_id = ctl.RequestReadProperty(self._handle, channel, ctl.Property.CHANNEL_STATE, 0)
        state = ctl.ReadProperty_i32(self._handle, r_id)
        return bool(state & ctl.ChannelState.ACTIVELY_MOVING)

    # -- speed / acceleration ---------------------------------------------------

    def set_speed(self, channel: int, vel: float = 1, acc: float = 10) -> None:
        """vel/acc are in mm/s (or deg/s) and mm/s^2 (or deg/s^2)."""
        ctl.SetProperty_i64(self._handle, channel, ctl.Property.MOVE_VELOCITY, int(vel * 1e9))
        ctl.SetProperty_i64(self._handle, channel, ctl.Property.MOVE_ACCELERATION, int(acc * 1e9))

    def get_speed(self, channel: int) -> tuple:
        vel = ctl.GetProperty_i64(self._handle, channel, ctl.Property.MOVE_VELOCITY)
        acc = ctl.GetProperty_i64(self._handle, channel, ctl.Property.MOVE_ACCELERATION)
        return (vel / 1e9, acc / 1e9)

    # -- calibration / referencing ------------------------------------------------

    def calibrate(self, channel: int) -> None:
        """Blocks until calibration completes. See MCS2 Programmer's Guide."""
        ctl.SetProperty_i32(self._handle, channel, ctl.Property.CALIBRATION_OPTIONS, 0)
        ctl.Calibrate(self._handle, channel)
        while True:
            state = ctl.GetProperty_i32(self._handle, channel, ctl.Property.CHANNEL_STATE)
            if state & ctl.ChannelState.CALIBRATING:
                time.sleep(0.1)
            else:
                break

    def find_reference(self, channel: int) -> None:
        """Blocks until the referencing sequence completes."""
        ctl.SetProperty_i32(self._handle, channel, ctl.Property.REFERENCING_OPTIONS, 0)
        ctl.SetProperty_i64(self._handle, channel, ctl.Property.MOVE_VELOCITY, int(1 * 1e9))
        ctl.SetProperty_i64(self._handle, channel, ctl.Property.MOVE_ACCELERATION, int(10 * 1e9))
        ctl.Reference(self._handle, channel)
        while True:
            state = ctl.GetProperty_i32(self._handle, channel, ctl.Property.CHANNEL_STATE)
            if state & ctl.ChannelState.REFERENCING:
                time.sleep(0.1)
            else:
                break

    # -- status -----------------------------------------------------------------

    def get_unit(self, channel: int) -> tuple:
        """Return ('mm'|'deg', raw_base_unit) for the channel."""
        base_unit = ctl.GetProperty_i32(self._handle, channel, ctl.Property.POS_BASE_UNIT)
        if base_unit == ctl.BaseUnit.METER:
            return "mm", base_unit
        return "deg", base_unit

    def is_axis_connected(self, channel: int) -> bool:
        state = ctl.GetProperty_i32(self._handle, channel, ctl.Property.CHANNEL_STATE)
        return bool(state & ctl.ChannelState.SENSOR_PRESENT)
