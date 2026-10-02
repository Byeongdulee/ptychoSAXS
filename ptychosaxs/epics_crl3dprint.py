"""
EPICS device config for the CRL_3dprint rig: 4 axes (X, Y linear in mm;
TILT, PITCH rotary in deg) on one IOC.

This is the single place to edit when the hardware wiring changes:
  - IOC_PREFIX: the IOC's EPICS prefix.
  - MOTOR_SLOTS: change each motor record suffix to match which physical
    positioner is which. The unit in each tuple is only a fallback - the
    IOC's own .EGU wins once it can be read.

Built on the generic, reusable driver in epics_motor.py - this file only
adds the name<->record mapping for this specific rig.
"""

try:
    from .epics_motor import EpicsMotorController
except ImportError:
    from epics_motor import EpicsMotorController

# ---------------------------------------------------------------------
# EDIT THESE FOR YOUR SETUP
# ---------------------------------------------------------------------
IOC_PREFIX = "12ideMCS2"

# Ordered list of (logical_name, pv_prefix, fallback_unit). Position in the
# list is cosmetic; the PV prefix is what actually matters.
MOTOR_SLOTS = [
    ("X", f"{IOC_PREFIX}:m1", "mm"),
    ("Y", f"{IOC_PREFIX}:m2", "mm"),
    ("TILT", f"{IOC_PREFIX}:m3", "deg"),
    ("PITCH", f"{IOC_PREFIX}:m4", "deg"),
]


class CRLAxisController(EpicsMotorController):
    """The CRL rig's four stages, named 'X'/'Y'/'TILT'/'PITCH'."""

    def __init__(self, motor_slots=MOTOR_SLOTS, pv_class=None):
        super().__init__(motor_slots, pv_class=pv_class)
