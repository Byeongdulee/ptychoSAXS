"""Bind a QLineEdit to an EPICS motor-record field, the way caqtdm does.

A caqtdm motor screen's Drive and Tweak boxes are not free-text entry boxes:
they *display* a record field (.VAL, .TWV) and typing in them writes that
field back. Anything that changes the field elsewhere -- another screen, a
script, spec -- shows up in the box within one poll.

PVLineEdit is that binding, with two deliberate escapes:

* `on_commit` lets the caller perform the write its own way. The optics panel
  uses it for Drive so a typed target still goes through the ptychosaxs
  controller (which does the move bookkeeping) rather than a raw .VAL put,
  while the *display* still follows .VAL.
* `fmt`/`parse` carry the panel's mm/um display-unit conversion, so the PV
  side is always in the record's own units (mm) and the widget side is in
  whatever unit the panel is currently showing.

Several PVs can be bound to one box (see `pvs`): the first is the one read
back, and a commit writes all of them. The dpad tweak boxes need that --
one box drives two motors, and both motors' .TWV must follow it.
"""

# caqtdm paints writable text entries a pale blue; readbacks stay plain. The
# same cue here tells an operator at a glance which boxes accept typing.
CAQTDM_EDIT_BG = "#c8dcf0"

# ...and turns one yellow the moment it is typed into, until Enter sends the
# value or the box reverts to the record. Without it there is no way to tell
# a number the IOC has from a number only the screen has -- which is exactly
# the mistake an operator makes when they type a value and walk away.
CAQTDM_DIRTY_BG = "#ffff99"

_UNSET = object()


class MemoryPV:
    """Debug-mode stand-in: remembers what was put to it.

    debug_stubs.FakePV always reads back 0, which would make a bound box
    snap to 0 on the next poll and so hide the very behaviour this class is
    here to let someone test. Starting at None instead means a box keeps the
    text the .ui designed it with until something writes the field.
    """

    connected = True

    def __init__(self, pvname, value=None):
        self.pvname = pvname
        self._value = value

    def get(self):
        return self._value

    def put(self, value):
        self._value = value


class PVLineEdit:
    """One QLineEdit mirroring one record field.

    `poll` is driven by the owner's existing refresh timer rather than a CA
    monitor callback, so every widget touch stays on the Qt thread.
    """

    def __init__(self, edit, pvs, fmt, parse, on_commit=None, on_error=None,
                 colour=CAQTDM_EDIT_BG, dirty_colour=CAQTDM_DIRTY_BG):
        self.edit = edit
        self.pvs = [pv for pv in pvs if pv is not None]
        self.fmt = fmt
        self.parse = parse
        self.on_commit = on_commit
        self.on_error = on_error
        self._last = _UNSET
        self._colour = colour
        self._dirty_colour = dirty_colour
        self._dirty = False
        self._paint(False)
        # textEdited, not textChanged: it fires only for typing, so the
        # box does not mark itself dirty when poll() or write() rewrites it.
        edit.textEdited.connect(lambda _text: self._paint(True))
        edit.returnPressed.connect(self.commit)

    def _paint(self, dirty):
        """Show the box as holding a sent value, or an unsent typed one."""
        colour = self._dirty_colour if dirty else self._colour
        self._dirty = bool(dirty)
        if colour:
            self.edit.setStyleSheet("background-color: %s;" % colour)

    # -- reading -----------------------------------------------------------

    def poll(self):
        """Redraw from the PV unless the operator is typing in this box.

        Rewriting whenever the text disagrees with the field -- not only
        when the field changes -- is what reverts a half-typed value the
        operator abandoned by clicking away, same as caqtdm.
        """
        if not self.pvs or self.edit.hasFocus():
            return
        try:
            value = self.pvs[0].get()
        except Exception:
            return  # a dropped CA connection must not stall the refresh tick
        if value is None:
            return
        value = float(value)
        self._last = value
        text = self.fmt(value)
        if text != self.edit.text():
            self.edit.setText(text)
        # Focus has left, so whatever was typed here is either sent or about
        # to be overwritten by the line above -- either way it is no longer
        # an unsent edit.
        if self._dirty:
            self._paint(False)

    def refresh(self):
        """Re-render the last known value, e.g. after a display-unit change.

        False when nothing has been read or written yet (no EPICS, or a
        field nobody has touched) and the box is therefore still showing
        text this binding did not put there.
        """
        if self._last is _UNSET:
            return False
        self.edit.setText(self.fmt(self._last))
        self._paint(False)
        return True

    def value(self):
        """What the box currently holds, in PV units. None if unparseable."""
        try:
            return self.parse(self.edit.text().strip())
        except ValueError:
            return None

    # -- writing -----------------------------------------------------------

    def commit(self):
        value = self.value()
        if value is None:
            if self.on_error is not None:
                self.on_error(self.edit.text())
            else:
                self.refresh()
            return
        self.write(value)

    def scale(self, factor):
        """Multiply what the box holds and send it -- the x10 / x0.1 buttons.

        A box holding something unparseable is left alone rather than reset:
        the operator is mid-edit, and replacing their text with 10 x nothing
        would be worse than doing nothing.
        """
        value = self.value()
        if value is None:
            return
        self.write(value * factor)

    def write(self, value):
        """Push `value` (PV units) to the field and show it immediately."""
        self._last = value
        self.edit.setText(self.fmt(value))
        self._paint(False)   # sent, so no longer an unsent edit
        if self.on_commit is not None:
            self.on_commit(value)
            return
        for pv in self.pvs:
            try:
                pv.put(value)
            except Exception as exc:
                print("Could not write %s: %s" % (getattr(pv, "pvname", pv), exc))
