"""Fonts that grow and shrink with the window, the way caqtdm screens do.

ProportionalResizer already rescales the absolutely-positioned widgets of a
.ui window; this does the same for their point sizes, so text keeps its
proportion of the widget it sits in instead of staying put while the box
around it grows.

Each widget's point size is snapshotted once, at construction, and every
rescale is computed from that snapshot rather than from the current size --
scaling repeatedly off the live value would compound rounding error until
the relative sizes (a block title 4pt above the body text, say) drifted
apart.

This is opt-in and self-contained: see DYNAMIC_FONTS in optics_motors.py for
how to turn it back off.
"""

from PyQt5.QtCore import QEvent, QObject
from PyQt5.QtWidgets import QWidget


class FontScaler(QObject):
    """Scales every named child's font with `container`'s size."""

    MIN_POINT_SIZE = 5.0

    def __init__(self, container, parent=None):
        super().__init__(parent)
        self.container = container
        self.orig_size = container.size()
        self.orig_sizes = {}
        for widget in [container] + container.findChildren(QWidget):
            if not widget.objectName():
                continue  # anonymous Qt internals manage their own fonts
            size = widget.font().pointSizeF()
            if size > 0:  # a pixel-sized font reports -1; leave it alone
                self.orig_sizes[widget] = size
        container.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.container and event.type() == QEvent.Resize:
            self.rescale()
        return False

    def rescale(self):
        if self.orig_size.width() == 0 or self.orig_size.height() == 0:
            return
        # Height drives text size more than width does, but scaling purely
        # on height overflows the boxes when a window is made tall and
        # narrow. The smaller of the two factors is the one that always fits.
        scale = min(self.container.width() / self.orig_size.width(),
                    self.container.height() / self.orig_size.height())
        for widget, orig_size in self.orig_sizes.items():
            try:
                font = widget.font()
                font.setPointSizeF(max(self.MIN_POINT_SIZE, orig_size * scale))
                widget.setFont(font)
            except RuntimeError:
                pass  # underlying C++ widget was deleted
