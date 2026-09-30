"""Reusable "Motor name / Current / Move to / Tweak" row table.

The ptychoSAXS .ui files position widgets absolutely rather than with Qt
layouts, and ProportionalResizer (resize_utils.py) rescales a window by forcing
QSizePolicy.Ignored and calling setGeometry() on every named child. A
QGridLayout inside such a window would fight that, so these rows are built with
explicit setGeometry() too, reproducing the main panel's exact column metrics
(read off lb1 / lb_1 / ed_1 / pb_tweak1L / ed_1_tweak / pb_tweak1R in
ptycoSAXS.ui, which are constant across all motor slots).

Build every table BEFORE constructing the window's ProportionalResizer -- it
snapshots geometry once, at construction, and widgets created afterwards are
never scaled.

Typical use:

    table = MotorRowTable(
        self.ui.container_rows_movesafe,
        [MotorRow("X", step=0.25, tweak="label"),
         MotorRow("Z", step=0.25, tweak="label")],
        columns=("name", "current", "tweak"),
        prefix="movesafe",
    )
    table.on_tweak(self._on_tweak)
    table.set_current("X", 1.234)
"""

from PyQt5.QtWidgets import QLabel, QLineEdit, QPushButton

# (slot, logical column, x, width, height, dy). dy is the offset from the row's
# top, needed because the main panel's 28px tweak buttons sit slightly higher
# than the 20/21px labels and line edits they sit between.
_SLOTS = (
    ("name", "name", 10, 41, 21, 0),
    ("current", "current", 60, 71, 21, 0),
    ("moveto", "moveto", 130, 81, 20, 0),
    ("tweak_l", "tweak", 220, 31, 28, -5),
    ("tweak_s", "tweak", 260, 61, 20, -1),
    ("tweak_r", "tweak", 330, 31, 28, -5),
)

# Right edge of the widest (all-columns) table, used to size the trailing slot.
_TABLE_END_X = 361

# Vertical distance between rows, matching the main panel (motor 1 at y=195,
# motor 7 at y=405).
ROW_PITCH = 35

ALL_COLUMNS = ("name", "current", "moveto", "tweak")

_HEADER_TEXT = {
    "name": "Motor name",
    "current": "Current",
    "moveto": "Move to",
    "tweak": "Tweak",
}

# Pale red, matching CRL_3dprint's rejected-move indication.
_INVALID_STYLE = "background-color: #ffcccc;"


def _fmt_step(value):
    """Render a step size without trailing zeros: 0.25, 2, 10, 180."""
    return "%g" % float(value)


def column_layout(columns):
    """Map each slot to its (x, width, height, dy) for the given columns.

    Omitted columns close up the gap they would have left, so a table without
    a "Move to" column puts its tweak group where "Move to" used to start
    rather than leaving a hole.
    """
    layout = {}
    shift = 0
    for index, (slot, column, x, width, height, dy) in enumerate(_SLOTS):
        next_x = _SLOTS[index + 1][2] if index + 1 < len(_SLOTS) else _TABLE_END_X
        if column in columns:
            layout[slot] = (x + shift, width, height, dy)
        else:
            shift -= next_x - x
    return layout


def table_size(columns, n_rows, header=True):
    """(width, height) a container needs to hold this table, for .ui sizing.

    Use this when adding a page: a full four-column table is 371 wide, one
    without "Move to" is 281, and the height is 35 * (rows + 1) with a header.
    """
    layout = column_layout(columns)
    right = max(x + width for x, width, _h, _dy in layout.values())
    rows = n_rows + (1 if header else 0)
    return right + 10, rows * ROW_PITCH


class MotorRow(object):
    """One row's specification.

    key      motor name as `pts` knows it ("X", "Z", "trans1", "trans2", "phi").
             Stays fixed for the row's lifetime -- renaming to transH/transD
             changes only the displayed label.
    label    displayed name; defaults to key.
    step     initial tweak step size.
    tweak    "edit" for a user-editable QLineEdit step, "label" for a fixed,
             read-only QLabel step.
    swappable
             build both widgets at the same rect and show one at a time, so
             the step can switch between fixed and editable at runtime (the
             iterative page's phi step does this at step 4).
    """

    def __init__(self, key, label=None, step=0.1, tweak="edit", swappable=False):
        self.key = key
        self.label = key if label is None else label
        self.step = step
        self.tweak = tweak
        self.swappable = swappable


class MotorRowTable(object):
    """Builds and owns one table of motor rows inside `container`."""

    def __init__(self, container, rows, columns=ALL_COLUMNS, prefix="mr",
                 header=True):
        self.container = container
        self.columns = tuple(columns)
        self.prefix = prefix
        self.rows = list(rows)
        self._widgets = {}  # key -> {slot: widget}
        self._headers = {}  # column -> header QLabel
        self._specs = {row.key: row for row in self.rows}
        self._tweak_cb = None
        self._moveto_cb = None

        layout = column_layout(self.columns)
        if header:
            self._build_header(layout)
        # Without a header the first row still starts 5px down, because the
        # tweak buttons sit at dy=-5 and would otherwise be clipped off the
        # top of the container.
        first_row_y = ROW_PITCH if header else 5
        for index, row in enumerate(self.rows):
            self._build_row(row, layout, first_row_y + index * ROW_PITCH)

    # -- construction ------------------------------------------------------

    def _name(self, slot, key):
        return "%s_%s_%s" % (self.prefix, slot, key)

    def _build_header(self, layout):
        for column in self.columns:
            slot = "tweak_l" if column == "tweak" else column
            if slot not in layout:
                continue
            x, width, height, _dy = layout[slot]
            if column == "tweak":
                # Span the whole <<  step  >> group.
                right = layout["tweak_r"]
                width = right[0] + right[1] - x
            label = QLabel(_HEADER_TEXT[column], self.container)
            label.setObjectName("%s_hdr_%s" % (self.prefix, column))
            label.setGeometry(x, 0, width, height)
            self._headers[column] = label

    def _build_row(self, row, layout, y):
        made = {}

        if "name" in layout:
            x, width, height, dy = layout["name"]
            widget = QLabel(row.label, self.container)
            widget.setObjectName(self._name("name", row.key))
            widget.setGeometry(x, y + dy, width, height)
            made["name"] = widget

        if "current" in layout:
            x, width, height, dy = layout["current"]
            widget = QLabel("", self.container)
            widget.setObjectName(self._name("current", row.key))
            widget.setGeometry(x, y + dy, width, height)
            made["current"] = widget

        if "moveto" in layout:
            x, width, height, dy = layout["moveto"]
            widget = QLineEdit(self.container)
            widget.setObjectName(self._name("moveto", row.key))
            widget.setGeometry(x, y + dy, width, height)
            widget.setToolTip("Absolute position. Hit enter to move.")
            widget.returnPressed.connect(
                lambda key=row.key: self._emit_moveto(key))
            made["moveto"] = widget

        if "tweak_l" in layout:
            for slot, text, sign in (("tweak_l", "<<", -1), ("tweak_r", ">>", 1)):
                x, width, height, dy = layout[slot]
                button = QPushButton(text, self.container)
                button.setObjectName(self._name(slot, row.key))
                button.setGeometry(x, y + dy, width, height)
                button.clicked.connect(
                    lambda _checked=False, key=row.key, s=sign:
                    self._emit_tweak(key, s))
                made[slot] = button

            x, width, height, dy = layout["tweak_s"]
            text = _fmt_step(row.step)
            # A swappable row gets both widgets at the same rect; the hidden
            # one is still snapshotted by ProportionalResizer, so it scales
            # correctly even if it is never shown.
            if row.swappable or row.tweak == "edit":
                editor = QLineEdit(text, self.container)
                editor.setObjectName(self._name("tweakedit", row.key))
                editor.setGeometry(x, y + dy, width, height)
                made["tweakedit"] = editor
            if row.swappable or row.tweak == "label":
                fixed = QLabel(text, self.container)
                fixed.setObjectName(self._name("tweaklbl", row.key))
                fixed.setGeometry(x, y + dy, width, height)
                fixed.setToolTip("Fixed step size for this step of the workflow.")
                made["tweaklbl"] = fixed
            if row.swappable:
                made["tweakedit"].setVisible(row.tweak == "edit")
                made["tweaklbl"].setVisible(row.tweak == "label")

        self._widgets[row.key] = made

    # -- signals -----------------------------------------------------------

    def on_tweak(self, callback):
        """callback(key, sign, step) for every << / >> press."""
        self._tweak_cb = callback

    def on_moveto(self, callback):
        """callback(key, value) for every valid Move-to entry."""
        self._moveto_cb = callback

    def _emit_tweak(self, key, sign):
        if self._tweak_cb is None:
            return
        try:
            step = self.step(key)
        except (TypeError, ValueError):
            self.flag_invalid(key, True)
            return
        self.flag_invalid(key, False)
        self._tweak_cb(key, sign, step)

    def _emit_moveto(self, key):
        if self._moveto_cb is None:
            return
        widget = self.widget(key, "moveto")
        try:
            value = float(widget.text())
        except (TypeError, ValueError):
            self.flag_invalid(key, True)
            return
        self.flag_invalid(key, False)
        self._moveto_cb(key, value)

    # -- accessors ---------------------------------------------------------

    def keys(self):
        return [row.key for row in self.rows]

    def widget(self, key, slot):
        """The widget for one cell, or None when that column is absent."""
        return self._widgets.get(key, {}).get(slot)

    def set_current(self, key, value):
        widget = self.widget(key, "current")
        if widget is not None:
            widget.setText("%0.6f" % float(value))

    def step(self, key):
        """The active step size, reading whichever tweak widget is showing."""
        editor = self.widget(key, "tweakedit")
        fixed = self.widget(key, "tweaklbl")
        spec = self._specs[key]
        if spec.swappable:
            # isHidden(), not isVisible(): the latter is also False simply
            # because an ancestor has not been shown yet, which would pick the
            # wrong widget for any table read before the window appears.
            source = editor if editor is not None and not editor.isHidden() else fixed
        else:
            source = editor if editor is not None else fixed
        if source is None:
            return float(spec.step)
        return float(source.text())

    def set_step(self, key, value):
        """Set the step on both tweak widgets, so a later swap agrees."""
        text = _fmt_step(value)
        for slot in ("tweakedit", "tweaklbl"):
            widget = self.widget(key, slot)
            if widget is not None:
                widget.setText(text)

    def set_tweak_editable(self, key, editable):
        """Swap a swappable row between its fixed label and its line edit."""
        editor = self.widget(key, "tweakedit")
        fixed = self.widget(key, "tweaklbl")
        if editor is None or fixed is None:
            return
        editor.setVisible(bool(editable))
        fixed.setVisible(not editable)

    def set_label(self, key, text):
        """Change a row's displayed name (trans1 -> transH) without changing
        the key the rest of the code, and `pts`, address it by."""
        widget = self.widget(key, "name")
        if widget is not None:
            widget.setText(text)

    def set_row_enabled(self, key, enabled):
        """Enable/disable every cell in a row, name label included -- the same
        thing rungui's _set_motor_widgets_enabled does for the main panel."""
        for widget in self._widgets.get(key, {}).values():
            widget.setEnabled(bool(enabled))

    def set_rows_enabled(self, spec):
        """Apply a whole enable map at once.

        `spec` is either a {key: bool} mapping or a container of the keys that
        should be enabled (everything else is disabled) -- the latter matches
        AlignmentFlow.iterative_state()["motors"].
        """
        if hasattr(spec, "items"):
            for key, enabled in spec.items():
                self.set_row_enabled(key, enabled)
            return
        for key in self.keys():
            self.set_row_enabled(key, key in spec)

    def flag_invalid(self, key, invalid):
        """Tint a row's Current cell pale red after a rejected move, and clear
        it on the next accepted one."""
        widget = self.widget(key, "current")
        if widget is not None:
            widget.setStyleSheet(_INVALID_STYLE if invalid else "")
