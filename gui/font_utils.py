"""Shared helper for applying a global font size across the GUI's windows.

Kept dependency-free (only PyQt5) so it can be imported from both rungui.py
and optics_motors.py — two entirely separate processes/QApplications —
without re-executing either module's top-level startup code. The two share
the persisted font size via the same QSettings("ptychoSAXS", "ptychoSAXS")
store, key "ui/fontSize".
"""

from PyQt5.QtCore import QSettings, QTimer
from PyQt5.QtWidgets import QWidget

# Matches the pointsize baked into most ptycoSAXS.ui widgets.
DEFAULT_FONT_SIZE = 11

_SETTINGS_KEY = "ui/fontSize"


def apply_font_size_to_tree(root, size):
    """Set `size` as the point size of root and every named descendant widget.

    Each widget's existing family/weight/italic are preserved. Anonymous
    internal sub-widgets (spin box arrows, combo box internals, ...) are
    skipped since Qt manages their geometry/font itself.
    """
    for w in [root] + root.findChildren(QWidget):
        if not w.objectName():
            continue
        f = w.font()
        f.setPointSize(size)
        w.setFont(f)


def apply_saved_font_size(target):
    """Defer-apply the persisted font size (if any) to `target`'s widget tree.

    Deferred via singleShot so it runs after any pending show/layout/paint
    events for a freshly created or just-resized window have drained —
    applying immediately can get silently overwritten by that backlog once
    the event loop actually processes it. This matters most right at
    startup: a window built before its QApplication's event loop starts
    (the usual case, since __init__ runs before app.exec_()) leaves a
    backlog of show/restoreGeometry/rescale events that only flush once the
    loop is running, and that flush can undo a font applied too early.
    """
    size = QSettings("ptychoSAXS", "ptychoSAXS").value(_SETTINGS_KEY, None)
    if size is not None:
        size = int(size)
        QTimer.singleShot(0, lambda: apply_font_size_to_tree(target, size))
