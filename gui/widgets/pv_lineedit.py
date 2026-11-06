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

from PyQt5.QtCore import QEvent, QObject

# caqtdm paints writable text entries a pale blue; readbacks stay plain. The
# same cue here tells an operator at a glance which boxes accept typing.
CAQTDM_EDIT_BG = "#c8dcf0"

# ...and turns one yellow the moment it is typed into, until Enter sends the
# value or the box reverts to the record. Without it there is no way to tell
# a number the IOC has from a number only the screen has -- which is exactly
# the mistake an operator makes when they type a value and walk away.
CAQTDM_DIRTY_BG = "#ffff99"

# A value the owner refused, e.g. a move outside the soft limits.
CAQTDM_INVALID_BG = "#ffcccc"

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
        edit.returnPressed.connect(self.commit)
        # Colours and the drop-an-unsent-number-when-the-mouse-leaves rule
        # are CaqtdmStyleEdit's, shared with the boxes no record backs.
        self.style = CaqtdmStyleEdit(edit, colour, dirty_colour)

    # -- reading -----------------------------------------------------------

    def poll(self):
        """Redraw from the PV unless the box holds an unsent number.

        An unsent number is CaqtdmStyleEdit's to keep or drop (it goes when
        the mouse leaves the box); everything else follows the field, even
        with the cursor in the box, as in caqtdm.
        """
        if not self.pvs or self.edit.isModified():
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

    def refresh(self):
        """Re-render the last known value, e.g. after a display-unit change.

        False when nothing has been read or written yet (no EPICS, or a
        field nobody has touched) and the box is therefore still showing
        text this binding did not put there.
        """
        if self._last is _UNSET:
            return False
        self.edit.setText(self.fmt(self._last))
        self.style.mark_set()
        return True

    def has_value(self):
        """True once the field has been read, or a value written to it."""
        return self._last is not _UNSET

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
        self.style.mark_set()   # sent, so no longer an unsent edit
        if self.on_commit is not None:
            self.on_commit(value)
            return
        for pv in self.pvs:
            try:
                pv.put(value)
            except Exception as exc:
                print("Could not write %s: %s" % (getattr(pv, "pvname", pv), exc))



class CaqtdmStyleEdit(QObject):
    """The caqtdm look and feel of an entry box, with or without a record.

    PVLineEdit uses it for boxes bound to a record field. On its own it
    serves the boxes that move motors through a controller rather than a
    record field (the main panel's, CRL_3dprint's, sample alignment's), so
    there is nothing to poll. Either way: pale blue at rest, yellow once
    typed into, and Enter is what sets the value.

    As in caqtdm, an unsent number survives only while the pointer is over
    the box: moving the mouse off it puts back the last value that was set,
    and so does focus leaving while the pointer is elsewhere (a click on
    another widget). Tabbing away with the pointer still on the box keeps
    the typing. Both triggers are events on this one widget -- nothing
    polls -- so this costs nothing per tick.

    Whatever the box is wired to do on Enter stays wired; this only paints
    and reverts. Text set from code (copy current positions, x10 / x0.1, a
    restored setting) counts as set, which is why the tracking keys off
    QLineEdit.isModified -- true only for typing -- rather than a store of
    its own.

    Parented to the box, so it lives exactly as long as the box does.
    """

    def __init__(self, edit, colour=CAQTDM_EDIT_BG, dirty_colour=CAQTDM_DIRTY_BG):
        super().__init__(edit)
        self.edit = edit
        self._colour = colour
        self._dirty_colour = dirty_colour
        self._invalid = False
        self._set_text = edit.text()
        self._paint(False)
        edit.textEdited.connect(lambda _text: self._paint(True))
        edit.textChanged.connect(self._on_text_changed)
        edit.returnPressed.connect(self._on_return)
        edit.editingFinished.connect(self._on_editing_finished)
        edit.installEventFilter(self)

    def _paint(self, dirty):
        if dirty:
            colour = self._dirty_colour
        elif self._invalid:
            colour = CAQTDM_INVALID_BG
        else:
            colour = self._colour
        if colour:
            self.edit.setStyleSheet("background-color: %s;" % colour)

    def set_invalid(self, invalid):
        """Pale red at rest, for a value the owner refused (a move outside
        the soft limits), until the owner clears it. Typing still shows
        yellow on top of it."""
        self._invalid = bool(invalid)
        self._paint(self.edit.isModified())

    def mark_set(self):
        """Take what the box shows now as set, whether or not it changed."""
        self._set_text = self.edit.text()
        self.edit.setModified(False)
        self._paint(False)

    def _on_text_changed(self, text):
        if not self.edit.isModified():
            self._set_text = text
            self._paint(False)

    def _on_return(self):
        self.mark_set()

    def _on_editing_finished(self):
        # Enter has already cleared the modified flag, so this only acts on
        # focus leaving with an unsent number in it.
        if not self.edit.underMouse():
            self.revert()

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() == QEvent.Leave:
            self.revert()
        return False

    def revert(self):
        """Drop an unsent number, if the box holds one."""
        if not self.edit.isModified():
            return
        # Painted here too: setText emits nothing when the typing ended up
        # back at the same text.
        self.edit.setText(self._set_text)
        self.mark_set()


def sync_drive(edit, target, readback, fmt):
    """Show a motor's target (.VAL) in its drive box, as caqtdm does.

    The box follows `target` -- where the motor was last told to go, by
    anyone -- except while it holds an unsent number. When the backend has
    no target to report (None: no motor record behind it), the box takes
    the first readback instead, once, so it does not sit blank.
    """
    if edit is None or edit.isModified():
        return
    if target is not None:
        text = fmt(target)
        if text != edit.text():
            edit.setText(text)
        edit.setProperty("caqtdmSeeded", True)
    elif readback is not None:
        seed_once(edit, fmt(readback))


def seed_once(edit, text):
    """Fill a drive box with its motor's first readback.

    For a box with no target to show (see sync_drive). The first readback
    after it is built fills it, once: after that it holds whatever was last
    sent. An operator already typing there is left alone, and seeded on the
    next readback instead.
    """
    if edit is None or edit.property("caqtdmSeeded") or edit.isModified():
        return
    edit.setText(text)
    edit.setProperty("caqtdmSeeded", True)
