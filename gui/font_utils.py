"""Shared helper for applying a global font size across the GUI's windows.

Kept dependency-free (only PyQt5.QtWidgets) so it can be imported from both
rungui.py and optics_motors.py without re-executing rungui.py's module-level
QApplication/ptyco_main_control startup code.
"""

from PyQt5.QtWidgets import QWidget

# Matches the pointsize baked into most ptycoSAXS.ui widgets.
DEFAULT_FONT_SIZE = 11


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
