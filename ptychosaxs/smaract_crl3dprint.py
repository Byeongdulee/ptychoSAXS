"""
SmarAct MCS2 device config for the CRL_3dprint rig: 4 axes (X, Y linear in
mm; TILT, PITCH rotary in deg) on one network controller.

This is the single place to edit once the physical hardware is wired up:
  - IP_ADDRESS: the MCS2 controller's network address.
  - MOTOR_SLOTS: reorder the tuples (or just change each channel number) to
    match which physical positioner is plugged into which MCS2 channel.

Built on the generic, reusable driver in smaract_mcs2.py - this file only
adds the name<->channel mapping for this specific rig.
"""

from smaract_mcs2 import SmaractMCS2Controller

# ---------------------------------------------------------------------
# EDIT THESE TWO CONSTANTS FOR YOUR SETUP
# ---------------------------------------------------------------------
IP_ADDRESS = "10.54.122.137"
LOCATOR = f"network:sn:MCS2-00020743"

# Ordered list of (logical_name, channel_index, unit). Position in the list
# is cosmetic; CHANNEL is what actually matters and is trivially
# reorderable/editable in place once the wiring is known.
MOTOR_SLOTS = [
    ("X", 0, "mm"),
    ("Y", 1, "mm"),
    ("TILT", 2, "deg"),
    ("PITCH", 3, "deg"),
]


class CRLAxisController:
    """Thin name-based wrapper: resolves 'X'/'Y'/'TILT'/'PITCH' (or a raw
    channel int) to a channel index and delegates to a generic
    SmaractMCS2Controller. Matches DebugSmaractCRLController's interface
    (see debug/debug_stubs.py) so gui/CRL_3dprint.py can use either
    interchangeably regardless of --debug_mode."""

    def __init__(self, locator: str = LOCATOR, motor_slots=MOTOR_SLOTS):
        self.motor_slots = motor_slots
        self._driver = SmaractMCS2Controller(locator)
        self._name_to_channel = {name: ch for name, ch, _unit in motor_slots}

    def _ch(self, axis):
        return self._name_to_channel[axis] if isinstance(axis, str) else axis

    def connect(self) -> None:
        # Deliberately does not push a velocity at connect time - whatever
        # speed the controller already has (power-on default, or a value
        # set in a previous session's Change Velocities dialog) is left alone.
        self._driver.connect()

    def disconnect(self) -> None:
        self._driver.disconnect()

    def is_connected(self) -> bool:
        return self._driver.is_connected()

    def get_pos(self, axis) -> float:
        return self._driver.get_pos(self._ch(axis))

    def mv(self, axis, target: float, wait: bool = True) -> None:
        self._driver.mv(self._ch(axis), target, wait)

    def mvr(self, axis, delta: float, wait: bool = True) -> None:
        self._driver.mvr(self._ch(axis), delta, wait)

    def stop(self, axis) -> None:
        self._driver.stop(self._ch(axis))

    def set_pos(self, axis, position: float = 0.0) -> float:
        return self._driver.set_pos(self._ch(axis), position)

    def is_moving(self, axis) -> bool:
        return self._driver.is_moving(self._ch(axis))

    def set_speed(self, axis, vel: float = 1, acc: float = 10) -> None:
        self._driver.set_speed(self._ch(axis), vel, acc)

    def get_speed(self, axis) -> tuple:
        return self._driver.get_speed(self._ch(axis))

    def calibrate(self, axis) -> None:
        self._driver.calibrate(self._ch(axis))

    def find_reference(self, axis) -> None:
        self._driver.find_reference(self._ch(axis))
