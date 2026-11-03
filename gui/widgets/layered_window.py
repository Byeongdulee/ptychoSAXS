"""A main window that borrows the widgets it lacks from another window.

rungui's scan engine reads several hundred widgets by name -- menu actions
that hold detector and capture settings, the per-motor scan fields, the
motor position labels. A reduced view drops most of them, but the settings
they hold still have to exist and keep their values while that view is
shown. So the reduced view is loaded into a LayeredMainWindow backed by the
full window, which stays alive (hidden): any widget or attribute the reduced
view does not define resolves to the full window's, and everything it does
define is its own.

Only Python-side lookups fall back -- `window.some_widget` and
`window.findChild(...)`. Qt itself never sees the backing window's widgets
as children of this one, so layouts, painting and geometry are unaffected.
"""

from PyQt5.QtWidgets import QMainWindow


class LayeredMainWindow(QMainWindow):
    def __init__(self, backing, parent=None):
        super().__init__(parent)
        self._backing = backing

    @property
    def backing(self):
        return self.__dict__.get("_backing")

    def __getattr__(self, name):
        # Only reached when ordinary lookup has already failed, so a widget
        # this window's own .ui defines always wins.
        backing = self.__dict__.get("_backing")
        if backing is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(backing, name)

    def findChild(self, *args, **kwargs):
        found = super().findChild(*args, **kwargs)
        backing = self.backing
        if found is None and backing is not None:
            found = backing.findChild(*args, **kwargs)
        return found

    def ownChild(self, *args, **kwargs):
        """findChild without the fallback: only this window's own widgets."""
        return super().findChild(*args, **kwargs)
