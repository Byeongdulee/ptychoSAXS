"""The "Check Upstream Optics" report: a beamline diagram plus status labels.

Renders an optics_status.OpticsStatus. All of the reasoning -- which motors,
which ini section, what counts as In -- lives in optics_status.py; this file
only draws it. Used both from rungui's Check Upstream Optics button and from
the pre-scan guard's warning dialog.

The diagram is hand-drawn with QPainter rather than an .ui file or an SVG:
there is no designable content, and recolouring SVG nodes per status would
mean string-templating XML. Nothing else in this repo paints custom widgets,
so there is no existing convention to match.
"""

from PyQt5.QtCore import QRectF, Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PyQt5.QtCore import QPointF
from PyQt5.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtCore import QSettings

import optics_status
from font_utils import apply_saved_font_size

# Same hexes the optics panel's status pills use, so the two windows agree on
# sight. UNDEFINED is the one state the panel has no colour for.
STATE_COLOURS = {
    optics_status.IN:        "#00cc00",
    optics_status.OUT:       "#cc0000",
    optics_status.BETWEEN:   "#9e9e9e",
    optics_status.UNDEFINED: "#ff8c00",
}
_TEXT_ON_COLOUR = "#ffffff"

# Rows of the status table, in beam order. Keys are optics_status.BLOCKS keys.
REPORT_BLOCKS = ["bs", "zp", "osa", "saxsbs"]

GEOMETRY_KEY = "opticsStatusDialog/geometry"


def _colour(state):
    return QColor(STATE_COLOURS.get(state, STATE_COLOURS[optics_status.BETWEEN]))


class OpticsDiagram(QWidget):
    """Beam-path schematic, drawn right to left.

    The source is on the right and the beam travels leftwards -- X-rays, BS,
    ZP, OSA, sample, X-ray eye, SAXS beamstop, SAXS detector -- so the arrow
    points left. Everything is laid out in a fixed 1000x300 virtual space and
    scaled once, so the drawing code never deals in pixels.
    """

    VW, VH = 1000.0, 300.0
    AXIS_Y = 150.0

    # Virtual x of each element, right (upstream) to left (downstream).
    # Spacing is >= CAPTION_W so the captions underneath never collide --
    # that is what sets the gaps, not the optics' real separations.
    X_BEAM_FROM, X_BEAM_TO = 975.0, 900.0
    X_BS, X_ZP, X_OSA = 840.0, 715.0, 590.0
    X_SAMPLE, X_EYE, X_SAXSBS, X_DET = 460.0, 330.0, 200.0, 75.0

    CAPTION_W = 118.0    # must not exceed the smallest gap above
    CAPTION_DY = 72.0    # below the axis; clears the detector body

    BS_R = 13.0          # beamstop radius; also the ZP's inner hole
    ZP_R = 42.0
    OSA_HOLE_R = 4.5
    DET_HALF_H = 62.0

    def __init__(self, parent=None):
        super(OpticsDiagram, self).__init__(parent)
        self.status = None
        self.eye_in = None          # True / False / None (unknown)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_status(self, status, eye_in=None):
        self.status = status
        self.eye_in = eye_in
        self.update()

    # -- helpers -----------------------------------------------------------

    def _state(self, key):
        if self.status is None:
            return optics_status.UNDEFINED
        block = self.status.blocks.get(key)
        return block.state if block else optics_status.UNDEFINED

    def _brush(self, key):
        """Filled in the block's state colour; hollow when it is retracted."""
        state = self._state(key)
        if state == optics_status.OUT:
            return QBrush(Qt.NoBrush)
        return QBrush(_colour(state))

    def _caption(self, painter, x, text, sub=None):
        painter.setPen(QPen(QColor("#202020")))
        font = QFont(painter.font())
        font.setPointSizeF(15.0)
        font.setBold(True)
        painter.setFont(font)
        half = self.CAPTION_W / 2.0
        painter.drawText(QRectF(x - half, self.AXIS_Y + self.CAPTION_DY,
                                self.CAPTION_W, 22),
                         Qt.AlignHCenter | Qt.AlignVCenter, text)
        if sub:
            font.setBold(False)
            font.setPointSizeF(13.0)
            painter.setFont(font)
            painter.drawText(QRectF(x - half, self.AXIS_Y + self.CAPTION_DY + 22,
                                    self.CAPTION_W, 20),
                             Qt.AlignHCenter | Qt.AlignVCenter, sub)

    # -- painting ----------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#fbfbfb"))

        scale = min(self.width() / self.VW, self.height() / self.VH)
        painter.translate((self.width() - self.VW * scale) / 2.0,
                          (self.height() - self.VH * scale) / 2.0)
        painter.scale(scale, scale)

        self._draw_beam(painter)
        self._draw_detector(painter)
        self._draw_saxsbs(painter)
        self._draw_eye(painter)
        self._draw_sample(painter)
        self._draw_osa(painter)
        self._draw_zp(painter)
        self._draw_bs(painter)
        painter.end()

    def _draw_beam(self, painter):
        """The incoming X-rays: a leftward arrow entering from the right."""
        pen = QPen(QColor("#e8a000"), 3.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(self.X_BEAM_FROM, self.AXIS_Y),
                         QPointF(self.X_BEAM_TO + 14, self.AXIS_Y))
        head = QPolygonF([QPointF(self.X_BEAM_TO, self.AXIS_Y),
                          QPointF(self.X_BEAM_TO + 18, self.AXIS_Y - 9),
                          QPointF(self.X_BEAM_TO + 18, self.AXIS_Y + 9)])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#e8a000")))
        painter.drawPolygon(head)
        self._caption(painter, (self.X_BEAM_FROM + self.X_BEAM_TO) / 2.0, "X-rays")

        # Dashed beam axis through the rest of the instrument, so the
        # elements read as sitting on one beam path.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#d8d8d8"), 1.5, Qt.DashLine))
        painter.drawLine(QPointF(self.X_BEAM_TO, self.AXIS_Y),
                         QPointF(self.X_DET, self.AXIS_Y))

    def _draw_bs(self, painter):
        painter.setPen(QPen(QColor("#303030"), 2.0))
        painter.setBrush(self._brush("bs"))
        painter.drawEllipse(QPointF(self.X_BS, self.AXIS_Y), self.BS_R, self.BS_R)
        self._caption(painter, self.X_BS, "BS", self._label("bs"))

    def _draw_zp(self, painter):
        """An annulus whose hole is the same size as the beamstop."""
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        path.addEllipse(QPointF(self.X_ZP, self.AXIS_Y), self.ZP_R, self.ZP_R)
        path.addEllipse(QPointF(self.X_ZP, self.AXIS_Y), self.BS_R, self.BS_R)
        painter.setPen(QPen(QColor("#303030"), 2.0))
        painter.setBrush(self._brush("zp"))
        painter.drawPath(path)
        self._caption(painter, self.X_ZP, "ZP", self._label("zp"))

    def _draw_osa(self, painter):
        """A block with a very small circular aperture punched through it."""
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        path.addRect(QRectF(self.X_OSA - 13, self.AXIS_Y - 46, 26, 92))
        path.addEllipse(QPointF(self.X_OSA, self.AXIS_Y),
                        self.OSA_HOLE_R, self.OSA_HOLE_R)
        painter.setPen(QPen(QColor("#303030"), 2.0))
        painter.setBrush(self._brush("osa"))
        painter.drawPath(path)
        self._caption(painter, self.X_OSA, "OSA", self._label("osa"))

    def _draw_sample(self, painter):
        painter.setPen(QPen(QColor("#303030"), 2.0))
        painter.setBrush(QBrush(QColor("#7fb2e5")))
        painter.drawRect(QRectF(self.X_SAMPLE - 9, self.AXIS_Y - 38, 18, 76))
        self._caption(painter, self.X_SAMPLE, "sample")

    def _draw_eye(self, painter):
        """Small removable detector -- ghosted when it is out of the beam."""
        inserted = self.eye_in is True
        painter.setPen(QPen(QColor("#303030" if inserted else "#b0b0b0"),
                            2.0, Qt.SolidLine if inserted else Qt.DashLine))
        painter.setBrush(QBrush(QColor("#b9a3d6")) if inserted else QBrush(Qt.NoBrush))
        painter.drawRect(QRectF(self.X_EYE - 17, self.AXIS_Y - 28, 34, 56))
        painter.drawEllipse(QPointF(self.X_EYE, self.AXIS_Y), 8.0, 8.0)
        if self.eye_in is None:
            sub = "unknown"
        else:
            sub = "In" if inserted else "Out"
        self._caption(painter, self.X_EYE, "X-ray eye", sub)

    def _draw_saxsbs(self, painter):
        painter.setPen(QPen(QColor("#303030"), 2.0))
        painter.setBrush(self._brush("saxsbs"))
        painter.drawRect(QRectF(self.X_SAXSBS - 9, self.AXIS_Y - 9, 18, 18))
        self._caption(painter, self.X_SAXSBS, "SAXS BS", self._label("saxsbs"))

    def _draw_detector(self, painter):
        painter.setPen(QPen(QColor("#303030"), 2.0))
        painter.setBrush(QBrush(QColor("#cfd8dc")))
        painter.drawRect(QRectF(self.X_DET - 17, self.AXIS_Y - self.DET_HALF_H,
                                34, self.DET_HALF_H * 2))
        self._caption(painter, self.X_DET, "SAXS det")

    def _label(self, key):
        if self.status is None:
            return None
        block = self.status.blocks.get(key)
        return block.label if block else None


class OpticsStatusDialog(QDialog):
    """Diagram plus a status row per block, refreshed while visible.

    `caget` and `eye_state` are injected so a debug-mode session or a test can
    supply state without EPICS. `problems`, when given, is shown as a warning
    banner -- that is how the pre-scan guard reuses this window.
    """

    REFRESH_MS = 1000

    def __init__(self, parent=None, caget=None, eye_state=None, problems=None,
                 ini_path=None):
        super(OpticsStatusDialog, self).__init__(parent)
        self.setWindowTitle("Upstream Optics Status")
        self._caget = caget
        self._ini_path = ini_path or optics_status.OPTICS_INI_PATH
        self._eye_state = eye_state
        self._problems = list(problems or [])

        layout = QVBoxLayout(self)
        self.diagram = OpticsDiagram(self)
        layout.addWidget(self.diagram, 1)

        self.banner = QLabel(self)
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        layout.addWidget(self.banner)

        self.grid = QGridLayout()
        self._value_labels = {}
        for column, key in enumerate(REPORT_BLOCKS):
            spec = optics_status.BLOCKS[key]
            title = QLabel(spec.title, self)
            title.setAlignment(Qt.AlignCenter)
            value = QLabel("----", self)
            value.setAlignment(Qt.AlignCenter)
            value.setMinimumWidth(90)
            self.grid.addWidget(title, 0, column)
            self.grid.addWidget(value, 1, column)
            self._value_labels[key] = value
        layout.addLayout(self.grid)

        self.footer = QLabel(self)
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet("color: #606060;")
        layout.addWidget(self.footer)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self.refresh)
        close = QPushButton("Close", self)
        close.clicked.connect(self.close)
        buttons.addWidget(refresh)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.resize(860, 460)
        apply_saved_font_size(self)
        geometry = QSettings("ptychoSAXS", "ptychoSAXS").value(GEOMETRY_KEY)
        if geometry is not None:
            self.restoreGeometry(geometry)

        # Polls while visible so the report tracks motors being nudged from
        # the optics panel, which runs in a separate process.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    # -- state -------------------------------------------------------------

    def refresh(self):
        try:
            status = optics_status.read_status(self._ini_path, caget=self._caget)
        except Exception as exc:          # never let the report itself raise
            self.footer.setText("Could not read optics status: %s" % exc)
            return
        self._render(status)

    def _render(self, status):
        eye_in = self._eye_state() if callable(self._eye_state) else self._eye_state
        self.diagram.set_status(status, eye_in=eye_in)

        for key, label in self._value_labels.items():
            block = status.blocks.get(key)
            state = block.state if block else optics_status.UNDEFINED
            text = block.label if block else "----"
            if block is not None and block.ambiguous:
                text += " (?)"
            label.setText(text)
            label.setStyleSheet(
                "background-color: %s; color: %s; padding: 3px; border-radius: 3px;"
                % (STATE_COLOURS.get(state, "#9e9e9e"), _TEXT_ON_COLOUR))

        if self._problems:
            self.banner.setText("Not ready for ptychography:\n- %s"
                                % "\n- ".join(self._problems))
            self.banner.setStyleSheet(
                "background-color: #fff3cd; color: #663c00; padding: 6px;"
                " border: 1px solid #e0c080; border-radius: 3px;")
            self.banner.setVisible(True)

        notes = ["Definitions: %s" % status.ini_path]
        if not status.ini_present:
            notes.append("file not found - open the optics GUI to create it")
        if status.ini_error:
            notes.append("read error: %s" % status.ini_error)
        if not status.epics_ok:
            notes.append("no live motor positions (EPICS unavailable)")
        notes.append("read %s" % status.read_at.strftime("%H:%M:%S"))
        self.footer.setText(" | ".join(notes))

    # -- Qt lifecycle ------------------------------------------------------

    def showEvent(self, event):
        super(OpticsStatusDialog, self).showEvent(event)
        self.refresh()
        self._timer.start(self.REFRESH_MS)

    def hideEvent(self, event):
        self._timer.stop()
        QSettings("ptychoSAXS", "ptychoSAXS").setValue(GEOMETRY_KEY,
                                                       self.saveGeometry())
        super(OpticsStatusDialog, self).hideEvent(event)
