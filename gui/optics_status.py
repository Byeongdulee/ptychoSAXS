"""Upstream optics in/out state, shared by the two GUI processes.

optics_motors.py (the optics panel) and rungui.py (the main scan GUI) run as
separate processes -- rungui._open_optics_gui launches the panel with
subprocess.Popen -- so they share no Python objects. The only things both can
see are gui/ini/optics_motors.ini, which holds the in/out *definitions*, and
the EPICS PVs, which hold the *live* positions. This module owns both halves
and the comparison between them, so the panel's status pills and rungui's
pre-scan check can never disagree about what "In" means.

Deliberately Qt-free and hardware-free: it imports neither PyQt5 nor
ptychosaxs, so rungui and handlers/ can use it without pulling in the panel's
hardware imports, and so tests/test_optics_status.py runs headlessly. Same split as alignment_flow.py and pv_consent.py.
"""

import configparser
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

INI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ini")
OPTICS_INI_PATH = os.path.join(INI_DIR, "optics_motors.ini")

# Position comparison tolerance, mm. A saved position matches the live
# readback only if every axis is within this.
THRESH = 0.005

# Seconds to wait on a single caget. Kept short because read_live_positions is
# called from a pre-scan guard: 11 PVs on pyepics' ~1 s default would freeze
# the GUI for ten seconds when the IOC is down.
CAGET_TIMEOUT = 0.5


# ---------------------------------------------------------------------------
# Hardware map -- the single place these motors are named.
#
# Controllers come from ptychosaxs.optics; the index is the axis' position in
# that controller's pvlist:
#   opticsbox  12idc:m10, m11, m12, m13, m14
#   OSA        12idc:m9, m15, m16
#   saxs_z     12idcACS1:m1
#   beamstop   12ideSFT:m4, m5
# ---------------------------------------------------------------------------
MOTORS = OrderedDict([
    ("BS_ver",      ("opticsbox", 0)),
    ("BS_hor",      ("opticsbox", 1)),
    ("ZP_ver",      ("opticsbox", 2)),
    ("ZP_hor",      ("opticsbox", 3)),
    ("BSZP_Ztrans", ("opticsbox", 4)),
    ("OSA_X",       ("OSA", 0)),
    ("OSA_Z",       ("OSA", 1)),
    ("OSA_Y",       ("OSA", 2)),
    ("SAXS_Z",      ("saxs_z", 0)),
    ("SAXSBS_hor",  ("beamstop", 0)),
    ("SAXSBS_ver",  ("beamstop", 1)),
])

# PV record base for each motor. Readback is "<base>.RBV", move-to is
# "<base>.VAL" (see ptychosaxs/epicsmotor.py get_pos/mv).
#
# These are load-bearing in two different ways and must stay in step with the
# pvlist order in MOTORS above. The optics panel drives motion through the
# controllers, not through this table, but read_live_positions() below cagets
# these names directly, and scan_handler/nexus_metadata_config derive their
# PV strings from here -- so a wrong entry silently logs the wrong motor
# rather than failing loudly.
PV_BASES = {
    "BS_ver":      "12idc:m10",
    "BS_hor":      "12idc:m11",
    "ZP_ver":      "12idc:m12",
    "ZP_hor":      "12idc:m13",
    "BSZP_Ztrans": "12idc:m14",
    "OSA_X":       "12idc:m9",
    "OSA_Z":       "12idc:m15",
    "OSA_Y":       "12idc:m16",
    "SAXS_Z":      "12idcACS1:m1",
    "SAXSBS_hor":  "12ideSFT:m4",
    "SAXSBS_ver":  "12ideSFT:m5",
}

# The upstream optics logged per scan, in the order they appear in the scan
# summary CSV and in scan_handler._US_OPTICS_PVS. Both derive from this list,
# so adding a motor here adds the column and the value together.
US_OPTICS_MOTORS = [
    "BS_ver", "BS_hor", "ZP_ver", "ZP_hor", "BSZP_Ztrans",
    "OSA_X", "OSA_Z", "OSA_Y",
]


@dataclass(frozen=True)
class OpticsBlockSpec:
    """One in/out group: its ini section and the motors it moves.

    `section` is the optics_motors.ini section name and is NOT always the key
    -- the SAXS beamstop is keyed "saxsbs" but stored under "SAXSbs".

    `motors` order is [vertical, horizontal], which is what fixes the meaning
    of the ini's in_0/in_1 and out_0/out_1 suffixes. For the OSA that means
    index 0 is OSA_Y and index 1 is OSA_X.

    `has_list` blocks store a dict of named positions under the "positions"
    key instead of a single in_N pair; they have no in_N entries at all.
    """

    key: str
    title: str
    section: str
    motors: list
    has_list: bool = False


# BSZP_Ztrans is deliberately absent. It stays a driveable motor (it is in
# MOTORS, PV_BASES and US_OPTICS_MOTORS above, so it is still nudged from the
# optics panel and still logged per scan) but it no longer owns a named
# position registry, so there is nothing about it to classify as In or Out
# and no [ztrans] section in the ini to classify it against.
BLOCKS = OrderedDict((b.key, b) for b in [
    OpticsBlockSpec("bs",     "BS",      "bs",      ["BS_ver", "BS_hor"]),
    OpticsBlockSpec("zp",     "ZP",      "zp",      ["ZP_ver", "ZP_hor"], has_list=True),
    OpticsBlockSpec("osa",    "OSA",     "osa",     ["OSA_Y", "OSA_X"]),
    OpticsBlockSpec("saxsbs", "SAXS BS", "SAXSbs",  ["SAXSBS_ver", "SAXSBS_hor"]),
])

# Blocks whose status is reported as the matched position *name* rather than
# "In", because they keep a named-position registry instead of one saved In.
NAME_BLOCKS = ("zp",)


# ---------------------------------------------------------------------------
# States and the ready-for-ptychography rule
# ---------------------------------------------------------------------------
IN = "in"               # live position matches a saved In / named position
OUT = "out"             # live position matches the saved Out
BETWEEN = "between"     # definitions exist but match neither -- the panel's "----"
UNDEFINED = "undefined" # nothing saved to compare against, or no readback

# Edit this to change what counts as ready for ptychography. Keys are BLOCKS
# keys; values are the state each must be in.
GOOD_FOR_PTYCHO = OrderedDict([
    ("bs", IN),
    ("zp", IN),
    ("osa", IN),
    ("saxsbs", OUT),
])


# ---------------------------------------------------------------------------
# Optic naming
# ---------------------------------------------------------------------------
# First match wins, so order is load-bearing: sRZP/cRZP/tRZP must be tested
# before the generic "rzp" rule that would otherwise swallow them, and "rzp"
# before the bare "zp" rule that is a substring of all of them.
OPTIC_TYPE_RULES = [
    (r"srzp|square",     "square_RZP"),
    (r"crzp|circle",     "circle_RZP"),
    (r"trzp|triangle",   "triangle_RZP"),
    (r"rzp|random",      "RZP"),
    (r"zp|zone|regular", "regular_ZP"),
]
UNKNOWN_OPTIC = "unknown_optic"
UNKNOWN_OZW = 1

# An outer zone width is an integer directly followed by "nm" (optional space
# allowed, so "50 nm" works too). The >4 floor rejects index-like names, so
# "optic 1" and even "optic 1nm" do not yield an outer zone width. The
# lookbehind stops "1.530nm" from yielding 530.
OZW_RE = re.compile(r"(?<![\d.])(\d+)\s*nm", re.IGNORECASE)
OZW_MIN = 4


def classify_optic(name):
    """Map a saved ZP position name to (optics_type, optics_name, optics_ozw).

    An unrecognised or empty name gives ("unknown_optic", name or "", 1) --
    never None, so the caller can always write all three to the master file.
    """
    text = (name or "").strip()
    optics_type = UNKNOWN_OPTIC
    lowered = text.lower()
    for pattern, type_name in OPTIC_TYPE_RULES:
        if re.search(pattern, lowered):
            optics_type = type_name
            break

    ozw = UNKNOWN_OZW
    for match in OZW_RE.finditer(text):
        value = int(match.group(1))
        if value > OZW_MIN:
            ozw = value
            break

    return optics_type, text, ozw


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BlockStatus:
    """Where one in/out group actually is, and what to print for it."""

    key: str
    title: str
    state: str                      # IN / OUT / BETWEEN / UNDEFINED
    name: str = None                # matched named position, has_list blocks only
    live: dict = field(default_factory=dict)   # motor -> mm, None if unreadable
    ambiguous: bool = False         # more than one named position matched

    @property
    def label(self):
        """The text to show next to this block in the status popup."""
        if self.state == IN:
            # The named-registry blocks say which position they are at; a
            # bare "In" would throw away the only interesting part.
            if self.key in NAME_BLOCKS and self.name:
                return self.name
            return "In"
        if self.state == OUT:
            return "Out"
        if self.state == UNDEFINED:
            return "undefined"
        return "----"


@dataclass(frozen=True)
class OpticsStatus:
    """A single point-in-time read of every block."""

    blocks: "OrderedDict"
    ini_path: str = OPTICS_INI_PATH
    ini_present: bool = True
    ini_error: str = None
    epics_ok: bool = True
    read_at: datetime = field(default_factory=datetime.now)

    def __getitem__(self, key):
        return self.blocks[key]

    @property
    def zp_name(self):
        """The ZP's matched position name, or None."""
        block = self.blocks.get("zp")
        return block.name if block is not None else None


# ---------------------------------------------------------------------------
# Reading the definitions
# ---------------------------------------------------------------------------
# optics_motors.PresetBlock._save_ini writes the file atomically, but an older
# panel still running the truncate-then-write version can be caught mid-save.
# Keep the last good parse so a torn read degrades to slightly stale data
# rather than to "everything undefined".
_last_good_definitions = None


def _parse_definitions(path):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    defs = {}
    for spec in BLOCKS.values():
        entry = {"in": [None] * len(spec.motors),
                 "out": [None] * len(spec.motors),
                 "positions": {}}
        if cfg.has_section(spec.section):
            section = cfg[spec.section]
            for i in range(len(spec.motors)):
                if not spec.has_list:
                    entry["in"][i] = _as_float(section.get("in_%d" % i, ""))
                entry["out"][i] = _as_float(section.get("out_%d" % i, ""))
            if spec.has_list:
                # A corrupt or truncated value must not take the whole read
                # down -- the other blocks are still usable.
                raw = section.get("positions", "") or "{}"
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    entry["positions"] = {
                        k: [_as_float(v) for v in vals]
                        for k, vals in parsed.items()
                        if isinstance(vals, (list, tuple))
                    }
        defs[spec.key] = entry
    return defs


def _as_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_definitions(path=OPTICS_INI_PATH):
    """Saved in/out positions per block, as {key: {in, out, positions}}.

    Returns (definitions, present, error). Never raises: a missing file gives
    an all-None set of definitions, which classify_block reports as
    UNDEFINED rather than as a spurious match.
    """
    global _last_good_definitions

    present = os.path.exists(path)
    if not present:
        return _parse_definitions(path), False, None

    error = None
    for attempt in (0, 1):
        try:
            defs = _parse_definitions(path)
        except (configparser.Error, OSError, ValueError) as exc:
            error = str(exc)
            if attempt == 0:
                time.sleep(0.05)   # most likely a concurrent save; retry once
                continue
        else:
            _last_good_definitions = defs
            return defs, True, None

    if _last_good_definitions is not None:
        return _last_good_definitions, True, error
    return _empty_definitions(), True, error


def _empty_definitions():
    """Every block undefined -- what an unreadable ini degrades to."""
    return {spec.key: {"in": [None] * len(spec.motors),
                       "out": [None] * len(spec.motors),
                       "positions": {}}
            for spec in BLOCKS.values()}


# ---------------------------------------------------------------------------
# Reading the live positions
# ---------------------------------------------------------------------------
def read_live_positions(motors=None, caget=None, timeout=CAGET_TIMEOUT):
    """{motor: position in mm or None} for `motors` (default: all of MOTORS).

    `caget` is injectable so tests can feed canned values and so a debug-mode
    GUI can pass a stub instead of touching EPICS. Returns (values, epics_ok);
    epics_ok is False when pyepics is missing or nothing could be read.
    """
    names = list(MOTORS) if motors is None else list(motors)

    if caget is None:
        try:
            import epics
        except ImportError:
            return {name: None for name in names}, False
        caget = lambda pv: epics.caget(pv, timeout=timeout)

    values = {}
    for name in names:
        try:
            values[name] = _as_float(caget(PV_BASES[name] + ".RBV"))
        except Exception:
            values[name] = None
    return values, any(v is not None for v in values.values())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _matches(live_values, saved, thresh=THRESH):
    """True when every axis has both a reading and a saved value, within tol.

    The all-or-nothing rule is what makes an unset position harmless: the
    stock {"test position 1": [null, null]} can never match, so a fresh
    install reports UNDEFINED rather than claiming the optic is inserted.
    """
    if not saved or len(saved) != len(live_values):
        return False
    return all(s is not None and c is not None and abs(c - s) <= thresh
               for c, s in zip(live_values, saved))


def _has_any_definition(entry, has_list):
    if has_list:
        if any(any(v is not None for v in vals)
               for vals in entry["positions"].values()):
            return True
    elif any(v is not None for v in entry["in"]):
        return True
    return any(v is not None for v in entry["out"])


def classify_block(spec, live, defs):
    """One BlockStatus for `spec`, given live readbacks and saved definitions.

    Mirrors optics_motors.PresetBlock.update_status exactly -- the panel
    delegates here so the two can never drift.
    """
    entry = defs.get(spec.key) or {"in": [], "out": [], "positions": {}}
    live_values = [live.get(m) for m in spec.motors]

    if all(v is None for v in live_values):
        return BlockStatus(spec.key, spec.title, UNDEFINED, live=dict(
            zip(spec.motors, live_values)))

    matched = [name for name, saved in entry["positions"].items()
               if _matches(live_values, saved)]
    in_match = bool(matched) if spec.has_list else _matches(live_values, entry["in"])

    if in_match:
        state, name = IN, (matched[0] if matched else None)
    elif _matches(live_values, entry["out"]):
        state, name = OUT, None
    elif _has_any_definition(entry, spec.has_list):
        state, name = BETWEEN, None
    else:
        state, name = UNDEFINED, None

    return BlockStatus(spec.key, spec.title, state, name=name,
                       live=dict(zip(spec.motors, live_values)),
                       ambiguous=len(matched) > 1)


def read_status(ini_path=OPTICS_INI_PATH, caget=None, live=None):
    """Read the ini and the PVs once and classify every block.

    The single call both GUIs make. `caget` and `live` are injectable so a
    debug-mode session or a test can supply positions without EPICS.
    """
    defs, present, error = read_definitions(ini_path)
    if live is None:
        live, epics_ok = read_live_positions(caget=caget)
    else:
        epics_ok = any(v is not None for v in live.values())

    blocks = OrderedDict(
        (key, classify_block(spec, live, defs)) for key, spec in BLOCKS.items())
    return OpticsStatus(blocks=blocks, ini_path=ini_path, ini_present=present,
                        ini_error=error, epics_ok=epics_ok)


def check_good_for_ptycho(status, rule=GOOD_FOR_PTYCHO):
    """(ok, problems) against GOOD_FOR_PTYCHO. problems is one line per fault."""
    problems = []
    for key, wanted in rule.items():
        block = status.blocks.get(key)
        if block is None:
            continue
        if block.state == wanted:
            continue
        if block.state == UNDEFINED:
            problems.append(
                "%s has no saved %s position - cannot tell where it is"
                % (block.title, wanted.capitalize()))
        else:
            problems.append("%s is %s (expected %s)"
                            % (block.title, block.label, wanted.capitalize()))
    return not problems, problems
