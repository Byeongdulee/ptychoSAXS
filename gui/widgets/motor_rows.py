"""Reusable "Motor name / Current / Move to / Tweak" row table.

Builds a QGridLayout inside a host QWidget declared in the .ui file, so the
table reflows with the window instead of being pinned to fixed pixels. Each
column keeps a fixed minimum width and no stretch, and a trailing empty
column soaks up the rest of the container's width -- so the table reads as a
compact, left-anchored block instead of spreading its fields across the
whole page.

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

from PyQt5.QtWidgets import (
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
)

ALL_COLUMNS = ("name", "current", "moveto", "tweak")

_HEADER_TEXT = {
    "name": "Motor name",
    "current": "Current",
    "moveto": "Move to",
    "tweak": "Tweak",
}

# Grid columns, in order. "tweak" expands into the three cells after it.
_COLUMN_CELLS = {
    "name": ("name",),
    "current": ("current",),
    "moveto": ("moveto",),
    "tweak": ("tweak_l", "tweak_s", "tweak_r"),
}

# Keeps the << / >> buttons square-ish however wide the table gets.
_TWEAK_BUTTON_WIDTH = 34

# Minimum pixel width for each cell. None of these columns stretch (see
# __init__) -- a trailing spacer column soaks up whatever width is left over,
# so the table stays a compact block on the left instead of spreading its
# fields across the whole page.
_CELL_MIN_WIDTH = {
    "name": 70,
    "current": 90,
    "moveto": 90,
    "tweak_l": _TWEAK_BUTTON_WIDTH,
    "tweak_s": 70,
    "tweak_r": _TWEAK_BUTTON_WIDTH,
}

# Pale red, matching CRL_3dprint's rejected-move indication.
_INVALID_STYLE = "background-color: #ffcccc;"


def _fmt_step(value):
    """Render a step size without trailing zeros: 0.25, 2, 10, 180."""
    return "%g" % float(value)


def cell_order(columns):
    """The grid cells, left to right, for the requested logical columns."""
    cells = []
    for column in columns:
        cells.extend(_COLUMN_CELLS[column])
    return cells


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
             build both widgets into a small QStackedWidget so the step can
             switch between fixed and editable at runtime (the iterative
             page's phi step does this at step 4).
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

        self._cells = cell_order(self.columns)
        self._grid = QGridLayout(container)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)
        for column, cell in enumerate(self._cells):
            self._grid.setColumnStretch(column, 0)
            self._grid.setColumnMinimumWidth(column, _CELL_MIN_WIDTH[cell])
        # Trailing spacer column: no widget is ever placed in it, but giving
        # it the only nonzero stretch factor makes it claim all the leftover
        # width, so the real columns stay at their natural size instead of
        # spreading out to fill the container.
        self._grid.setColumnStretch(len(self._cells), 1)

        if header:
            self._build_header()
        first_row = 1 if header else 0
        for index, row in enumerate(self.rows):
            self._build_row(row, first_row + index)
        # Soak up leftover vertical space so rows keep their natural height
        # instead of stretching apart when the page is tall.
        self._grid.setRowStretch(first_row + len(self.rows), 1)

    # -- construction ------------------------------------------------------

    def _name(self, slot, key):
        return "%s_%s_%s" % (self.prefix, slot, key)

    def _column_of(self, cell):
        return self._cells.index(cell)

    def _build_header(self):
        for column in self.columns:
            cells = _COLUMN_CELLS[column]
            label = QLabel(_HEADER_TEXT[column], self.container)
            label.setObjectName("%s_hdr_%s" % (self.prefix, column))
            # The tweak header spans the whole "<<  step  >>" group.
            self._grid.addWidget(label, 0, self._column_of(cells[0]),
                                 1, len(cells))
            self._headers[column] = label

    def _build_row(self, row, grid_row):
        made = {}

        if "name" in self.columns:
            widget = QLabel(row.label, self.container)
            widget.setObjectName(self._name("name", row.key))
            self._grid.addWidget(widget, grid_row, self._column_of("name"))
            made["name"] = widget

        if "current" in self.columns:
            widget = QLabel("", self.container)
            widget.setObjectName(self._name("current", row.key))
            self._grid.addWidget(widget, grid_row, self._column_of("current"))
            made["current"] = widget

        if "moveto" in self.columns:
            widget = QLineEdit(self.container)
            widget.setObjectName(self._name("moveto", row.key))
            widget.setToolTip("Absolute position. Hit enter to move.")
            widget.returnPressed.connect(
                lambda key=row.key: self._emit_moveto(key))
            self._grid.addWidget(widget, grid_row, self._column_of("moveto"))
            made["moveto"] = widget

        if "tweak" in self.columns:
            for slot, text, sign in (("tweak_l", "<<", -1), ("tweak_r", ">>", 1)):
                button = QPushButton(text, self.container)
                button.setObjectName(self._name(slot, row.key))
                button.setMaximumWidth(_TWEAK_BUTTON_WIDTH)
                button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                button.clicked.connect(
                    lambda _checked=False, key=row.key, s=sign:
                    self._emit_tweak(key, s))
                self._grid.addWidget(button, grid_row, self._column_of(slot))
                made[slot] = button

            self._build_step_cell(row, grid_row, made)

        self._widgets[row.key] = made

    def _build_step_cell(self, row, grid_row, made):
        """The step-size cell: a line edit, a fixed label, or both."""
        text = _fmt_step(row.step)
        column = self._column_of("tweak_s")

        if row.swappable:
            # Both widgets in a 2-page stack, so the cell keeps one consistent
            # size and the grid does not reflow when the step becomes editable.
            stack = QStackedWidget(self.container)
            stack.setObjectName(self._name("tweakstack", row.key))
            editor = QLineEdit(text)
            editor.setObjectName(self._name("tweakedit", row.key))
            fixed = QLabel(text)
            fixed.setObjectName(self._name("tweaklbl", row.key))
            fixed.setToolTip("Fixed step size for this step of the workflow.")
            stack.addWidget(fixed)   # index 0
            stack.addWidget(editor)  # index 1
            stack.setCurrentIndex(1 if row.tweak == "edit" else 0)
            self._grid.addWidget(stack, grid_row, column)
            made["tweakstack"] = stack
            made["tweakedit"] = editor
            made["tweaklbl"] = fixed
            return

        if row.tweak == "edit":
            editor = QLineEdit(text, self.container)
            editor.setObjectName(self._name("tweakedit", row.key))
            self._grid.addWidget(editor, grid_row, column)
            made["tweakedit"] = editor
        else:
            fixed = QLabel(text, self.container)
            fixed.setObjectName(self._name("tweaklbl", row.key))
            fixed.setToolTip("Fixed step size for this step of the workflow.")
            self._grid.addWidget(fixed, grid_row, column)
            made["tweaklbl"] = fixed

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
        spec = self._specs[key]
        editor = self.widget(key, "tweakedit")
        fixed = self.widget(key, "tweaklbl")
        if spec.swappable:
            stack = self.widget(key, "tweakstack")
            source = editor if stack.currentIndex() == 1 else fixed
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
        stack = self.widget(key, "tweakstack")
        if stack is not None:
            stack.setCurrentIndex(1 if editable else 0)

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
