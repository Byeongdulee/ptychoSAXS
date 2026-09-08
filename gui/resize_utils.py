"""Reusable proportional-resize scaling for absolutely-positioned .ui windows.

ptycoSAXS.ui and setup_configuration.ui use absolute widget geometry rather
than Qt layouts, so resizing the top-level window doesn't move/resize their
children by itself. ProportionalResizer snapshots each named child widget's
geometry at construction time and rescales all of them by the same (sx, sy)
factors whenever the container is resized, which reproduces the original
arrangement scaled uniformly larger/smaller.
"""

from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtWidgets import QWidget, QSizePolicy


class ProportionalResizer(QObject):
    def __init__(self, container, parent=None):
        super().__init__(parent)
        self.container = container
        self.orig_size = container.size()
        self.orig_geoms = {}
        for w in container.findChildren(QWidget):
            if not w.objectName():
                continue
            self.orig_geoms[w] = w.geometry()
            # A few widgets carry static min/max-size constraints from the
            # original fixed-size design (meant to keep them from growing/
            # shrinking in a *static* layout). Those fight our explicit
            # setGeometry() calls below, so lift them. Likewise, force
            # sizePolicy to Ignored so nothing silently reasserts a
            # preferred sizeHint over the geometry we set.
            w.setMinimumSize(0, 0)
            w.setMaximumSize(16777215, 16777215)
            sp = w.sizePolicy()
            sp.setHorizontalPolicy(QSizePolicy.Ignored)
            sp.setVerticalPolicy(QSizePolicy.Ignored)
            w.setSizePolicy(sp)

        # installEventFilter works regardless of whether `container` is a
        # Python-subclassed widget (e.g. a dialog's root widget from
        # uic.loadUi) or a plain, non-subclassed instance (e.g.
        # QMainWindow.centralWidget(), which is just a bare QWidget) —
        # instance-level resizeEvent overrides only work for the former, and
        # silently do nothing for the latter, so an event filter is the only
        # approach reliable for both.
        container.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.container and event.type() == QEvent.Resize:
            self.rescale()
        return False

    def rescale(self):
        if self.orig_size.width() == 0 or self.orig_size.height() == 0:
            return
        sx = self.container.width() / self.orig_size.width()
        sy = self.container.height() / self.orig_size.height()
        for widget, orig_rect in self.orig_geoms.items():
            try:
                widget.setGeometry(
                    round(orig_rect.x() * sx),
                    round(orig_rect.y() * sy),
                    round(orig_rect.width() * sx),
                    round(orig_rect.height() * sy),
                )
            except RuntimeError:
                pass  # underlying C++ widget was deleted
