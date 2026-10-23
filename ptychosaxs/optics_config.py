"""Which EPICS record each upstream optics motor is.

One table, imported by both the device layer (ptychosaxs/optics.py, which
builds controllers from it) and the status logic (gui/optics_status.py,
which cagets the records directly). It used to be two: optics.py carried
ordered PV lists and optics_status.py carried a parallel name->PV dict, with
a comment warning that they "must stay in step ... a wrong entry silently
logs the wrong motor rather than failing loudly". Now they cannot disagree.

Pure data. No imports at all, so gui/optics_status.py keeps the property its
own docstring claims -- Qt-free and hardware-free, importable in a test with
no pyepics present.
"""

from collections import OrderedDict

#: Logical motor name -> EPICS record base. Readback is "<base>.RBV",
#: move-to is "<base>.VAL".
PV_BASES = OrderedDict([
    ("BS_ver",      "12idc:m10"),
    ("BS_hor",      "12idc:m11"),
    ("ZP_ver",      "12idc:m12"),
    ("ZP_hor",      "12idc:m13"),
    ("BSZP_Ztrans", "12idc:m14"),
    ("OSA_X",       "12idc:m9"),
    ("OSA_Z",       "12idc:m15"),
    ("OSA_Y",       "12idc:m16"),
    ("SAXS_Z",      "12idcACS1:m1"),
    ("SAXSBS_hor",  "12ideSFT:m4"),
    ("SAXSBS_ver",  "12ideSFT:m5"),
])

#: Which controller each motor belongs to, in the order that controller
#: exposes them. The optics panel addresses motors as (group, index), so
#: this order is load-bearing -- reordering a group silently re-points the
#: panel's buttons at different stages.
GROUPS = OrderedDict([
    ("opticsbox", ["BS_ver", "BS_hor", "ZP_ver", "ZP_hor", "BSZP_Ztrans"]),
    ("OSA",       ["OSA_X", "OSA_Z", "OSA_Y"]),
    ("saxs_z",    ["SAXS_Z"]),
    ("beamstop",  ["SAXSBS_hor", "SAXSBS_ver"]),
])

#: Motor name -> (group, index within that group). Derived, so it cannot
#: drift from GROUPS.
MOTORS = OrderedDict(
    (name, (group, index))
    for group, names in GROUPS.items()
    for index, name in enumerate(names)
)

#: Fallback engineering unit, used only until the IOC's own .EGU is read.
DEFAULT_UNITS = {name: ("mm" if name == "SAXS_Z" else "um") for name in PV_BASES}


def slots_for(group):
    """motor_slots for EpicsMotorController: [(name, pv_base, unit), ...]."""
    return [(name, PV_BASES[name], DEFAULT_UNITS[name])
            for name in GROUPS[group]]
