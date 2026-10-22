"""Reader/writer for the beamline's flat "<description>, <key> : <value>" .ini
format, plus the quoting layer that makes arbitrary text safe in it.

This replaces the external ``py12inifunc`` package. That package was imported
with a ``try/except ImportError`` fallback to a stub whose ``readini`` and
``writeini`` were no-ops, so on any machine where it was not installed the GUI
silently stopped remembering the working folder, log filename, data basepaths,
scan name and scan number -- the .ini on disk stayed frozen at the defaults
``ensure_default_ini`` had generated. Vendoring it here removes that failure
mode entirely: there is no longer an import that can fall back to nothing.

The on-disk format is unchanged, so .ini files written by the original package
are read back identically and vice versa.

Three layers, lowest first:

``Ini``
    The format itself. One line per attribute, values parsed as float where
    they look numeric and kept as text otherwise. Unlike the original it uses
    bounded splits, keeps each key's description, and skips blank or
    unparseable lines instead of raising -- in the original a single bad line
    broke every later read *and* write, because ``writeini`` re-reads the file
    to learn its layout before rewriting it.

``SafeIni``
    Percent-encodes the fields that routinely contain the format's own
    separators, so they survive the round trip. See ``INI_TEXT_FIELDS``.

``ensure_default_ini``
    Creates a missing file from a table of defaults, back-fills entries a
    newer GUI has added, and repairs pre-``SafeIni`` raw values.
"""

import os
import urllib.parse

# Fields holding free text that can contain the ',' / ':' / ' ' this format
# splits on -- an absolute Windows path like "C:\Users\me\log.txt" is the
# common case. Percent-encoded on write, decoded on read.
INI_TEXT_FIELDS = ("logfilename", "working_folder", "base_linux_datafolder", "scan_name")

# Fields holding a list of *strings* (e.g. softglue channel letters "B","C","D").
# The format's native multi-token form is a list of floats, which raises on
# both read and write for these, so they are encoded as one token too.
INI_LIST_FIELDS = ("softglue_channels",)

# Fields that are conceptually whole numbers -- counts, indices, enum
# selections and 0/1 flags -- returned as int rather than float.
#
# Every value in this format is stored as "%0.4f", so without this they all
# come back as floats, and a float reaches places that will not take one:
# PyQt5 rejects it outright (setChecked(1.0) raises TypeError), range() and
# list indexing raise, and "%04d" formatting of a scan number is a silent
# accident waiting to happen. The stub that stood in for a working .ini
# reader handed out real ints, so none of this was ever exercised until the
# reader started working.
INI_INT_FIELDS = (
    "countsperexposure",
    "saxsmode",
    "scan_number",
    "_pulses_per_step",
    "_qds_unit",
    "_qds_x_sensor",
    "_qds_y_sensor",
    "_save_us_optics",
    "_check_optics_prescan",
    "_xmotor",
    "_ymotor",
)

# A blank value cannot round-trip: ``val.split()`` on "" yields [], which the
# parser reads back as an empty *list* rather than an empty string. Empty text
# is written as this non-blank placeholder instead.
_EMPTY = "-"


# ---------------------------------------------------------------------------
# Quoting helpers
# ---------------------------------------------------------------------------

def quote_text(value):
    """Encode a text value so it holds no ',' ':' or whitespace."""
    s = str(value)
    return urllib.parse.quote(s, safe="/") if s else _EMPTY


def unquote_text(value):
    """Inverse of :func:`quote_text`."""
    return "" if value == _EMPTY else urllib.parse.unquote(value)


def quote_list(values):
    """Encode a list of strings as a single comma-joined, percent-encoded token."""
    return urllib.parse.quote(",".join(str(v) for v in values) or _EMPTY, safe="")


def unquote_list(raw):
    """Inverse of :func:`quote_list`."""
    decoded = urllib.parse.unquote(raw)
    return [] if decoded in ("", _EMPTY) else decoded.split(",")


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------

def _parse_value(raw):
    """Parse one on-disk value: float, list of floats, or text.

    Matches the original package's rules -- a single token that parses as a
    number becomes a float, several whitespace-separated numbers become a list
    of floats, anything else stays a string.
    """
    tokens = raw.split()
    if len(tokens) == 1:
        try:
            return float(tokens[0])
        except ValueError:
            return tokens[0]
    try:
        return [float(t) for t in tokens]
    except ValueError:
        return raw.strip()


def _format_value(value):
    """Render a Python value back into the on-disk form."""
    if isinstance(value, (list, tuple)):
        if len(value) > 1:
            return "".join("%0.4f " % v for v in value)
        value = value[0] if value else ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # bool is an int subclass; "%0.4f" would be right but 1.0000/0.0000
        # reads back far more predictably than "True"/"False".
        return "%0.4f" % float(value)
    try:
        return "%0.4f" % value
    except (TypeError, ValueError):
        return str(value)


def parse_line(line):
    """Split one line into ``(description, key, raw_value)``, or None.

    Bounded splits, so a description or value containing a further ',' or ':'
    no longer desynchronises the parse.
    """
    if "," not in line or ":" not in line:
        return None
    desc, rest = line.split(",", 1)
    if ":" not in rest:
        return None
    key, raw_value = rest.split(":", 1)
    key = key.strip()
    if not key:
        return None
    return desc.strip(), key, raw_value.strip()


class Ini:
    """One flat .ini file, exposed as attributes on the instance.

    ``readini()`` sets an attribute per key; ``writeini()`` writes each key
    back, preserving the descriptions and the line order already in the file.
    """

    def __init__(self, ininame):
        self.ini_name = ininame
        self.name = ""
        # key -> description, learned on read and used to rewrite the file in
        # its original order. Kept off the attribute namespace the .ini keys
        # live in by its leading underscore.
        self._ini_layout = []

    # -- internals ---------------------------------------------------------

    def _read_layout(self, path):
        """Return [(description, key, raw_value)] for every parseable line."""
        layout = []
        try:
            with open(path) as f:
                for line in f:
                    parsed = parse_line(line)
                    if parsed is not None:
                        layout.append(parsed)
        except OSError:
            pass
        return layout

    # -- public API --------------------------------------------------------

    def readini(self, ini_name=""):
        path = ini_name or self.ini_name
        layout = self._read_layout(path)
        self._ini_layout = [(desc, key) for desc, key, _raw in layout]
        for _desc, key, raw in layout:
            setattr(self, key, _parse_value(raw))

    def writeini(self, ini_name=""):
        """Rewrite the file from the current attribute values.

        The layout is taken from the file on disk so that a value changed by
        another process is not clobbered by a stale in-memory ordering, and so
        that keys this object never read through are preserved.

        The file therefore drives the write: an attribute with no line in the
        .ini is NOT persisted. That is why every key the GUI relies on must be
        listed in its defaults table -- ensure_default_ini writes those on
        first run and back-fills them on upgrade, so by the time anything
        assigns to them the lines already exist.
        """
        path = ini_name or self.ini_name
        layout = self._read_layout(path) or [
            (desc, key, "") for desc, key in self._ini_layout
        ]

        lines = []
        written = set()
        for desc, key, raw in layout:
            if hasattr(self, key):
                lines.append("%s, %s : %s" % (desc, key, _format_value(getattr(self, key))))
            else:
                # Not an attribute of ours -- keep the file's value as-is.
                lines.append("%s, %s : %s" % (desc, key, raw))
            written.add(key)

        for desc, key in self._ini_layout:
            if key not in written and hasattr(self, key):
                lines.append("%s, %s : %s" % (desc, key, _format_value(getattr(self, key))))
                written.add(key)

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")


class SafeIni(Ini):
    """:class:`Ini` with the text and list fields percent-encoded on disk.

    Every other line of the codebase keeps seeing plain, real values: the
    encoding is applied immediately before the write and undone immediately
    after the read.
    """

    def readini(self, ini_name=""):
        super().readini(ini_name)
        for field in INI_INT_FIELDS:
            val = getattr(self, field, None)
            if isinstance(val, float):
                setattr(self, field, int(round(val)))
        for field in INI_TEXT_FIELDS:
            val = getattr(self, field, None)
            if isinstance(val, str):
                setattr(self, field, unquote_text(val))
            elif isinstance(val, list) and not val:
                # The blank-value-reads-as-empty-list quirk. Shouldn't occur
                # once a value has gone through quote_text, but a hand-edited
                # or not-yet-migrated .ini can still hit it.
                setattr(self, field, "")
        for field in INI_LIST_FIELDS:
            val = getattr(self, field, None)
            if isinstance(val, str):
                setattr(self, field, unquote_list(val))

    def writeini(self, ini_name=""):
        saved = {}
        for field in INI_TEXT_FIELDS:
            val = getattr(self, field, None)
            if isinstance(val, str):
                saved[field] = val
                setattr(self, field, quote_text(val))
        for field in INI_LIST_FIELDS:
            val = getattr(self, field, None)
            if isinstance(val, list):
                saved[field] = val
                setattr(self, field, quote_list(val))
        try:
            super().writeini(ini_name)
        finally:
            for field, val in saved.items():
                setattr(self, field, val)


# ---------------------------------------------------------------------------
# First-run / upgrade handling
# ---------------------------------------------------------------------------

def ensure_default_ini(path, entries):
    """Create `path` from `entries` if absent, back-fill, and repair raw values.

    `entries` is a list of ``(description, key, value)`` triples, already in
    on-disk form. Entries already in the file keep their saved value; only
    absent ones are added, so a GUI upgrade that needs a new field no longer
    requires the .ini to be hand-edited first.

    Also repairs any INI_TEXT_FIELDS / INI_LIST_FIELDS line still holding a
    raw, pre-:class:`SafeIni` value that would otherwise be mis-parsed.
    """
    text = ""
    if os.path.exists(path):
        with open(path) as f:
            text = f.read()

    lines = text.splitlines()
    present = set()
    changed = False
    for i, line in enumerate(lines):
        parsed = parse_line(line)
        if parsed is None:
            continue
        desc, key, raw_value = parsed
        present.add(key)

        if key in INI_TEXT_FIELDS:
            if raw_value == "" or any(c in raw_value for c in ":, \t"):
                lines[i] = "%s, %s : %s" % (desc, key, quote_text(raw_value))
                changed = True
        elif key in INI_LIST_FIELDS:
            tokens = raw_value.split()
            if len(tokens) != 1 or any(c in raw_value for c in ":,"):
                lines[i] = "%s, %s : %s" % (desc, key, quote_list(tokens))
                changed = True

    missing = [e for e in entries if e[1] not in present]
    if not missing and not changed:
        return

    lines.extend(
        "%s, %s : %s" % (description, name, value) for description, name, value in missing
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
