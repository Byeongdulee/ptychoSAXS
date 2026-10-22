"""Reusable More/Less navigation across a ladder of .ui files.

caqtdm motor screens let an operator collapse a busy screen down to the
controls they are using, and expand it back, without losing track of where
the window was or how big it was. LevelSwitcher is that navigation, kept
independent of any one GUI's widgets: give it an ordered list of .ui paths
(most detailed first) and a function that builds and shows one level, and it
handles which level is current, persisting that choice, and swapping windows
without the app exiting mid-swap or a stray geometry landing on the wrong
level.

Usage (see optics_motors.py's motor_control for the full pattern):

    self._levels = LevelSwitcher(
        [os.path.join(UI_DIR, "optics_motors.ui"),
         os.path.join(UI_DIR, "optics_motors_small.ui"),
         os.path.join(UI_DIR, "optics_motors_tiny.ui")],
        self._build_view, "opticsMotorsWindow")
    self._levels.start()

`self._build_view(path)` loads the named .ui, wires up its widgets, and --
critically -- calls `self._levels.restore_geometry(ui)` after showing it (see
the ordering note on that method). It must not install its own closeEvent;
LevelSwitcher owns that so it can save each level's geometry under that
level's own key.
"""

import os

from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QPushButton


class LevelSwitcher:
    """Tracks the current level of a .ui ladder and switches between them.

    `settings_prefix` namespaces every key this stores: `<prefix>/level` for
    the remembered level, `<prefix>/geometry/<ui basename>` for each level's
    own saved geometry -- separate per level, so a size saved by a wide full
    view is never handed to a small or tiny one.
    """

    def __init__(self, levels, build, settings_prefix, settings=None,
                 more_button="pushButton_More", less_button="pushButton_Less",
                 remember=True):
        self.levels = list(levels)
        self.build = build
        self.settings_prefix = settings_prefix
        self.settings = settings or QSettings("ptychoSAXS", "ptychoSAXS")
        self.more_button = more_button
        self.less_button = less_button
        self.remember = remember
        self.index = 0
        self.ui = None

    # -- startup -------------------------------------------------------

    def start(self):
        """Build the remembered level (or the first one) and show it."""
        if self.remember:
            saved = self.settings.value("%s/level" % self.settings_prefix,
                                        0, type=int)
            self.index = max(0, min(saved, len(self.levels) - 1))
        self.ui = self._open(self.index, anchor=None)
        return self.ui

    # -- switching -------------------------------------------------------

    def switch_to(self, index):
        """Build and show `index`, then close the outgoing window.

        The new window is shown and wired up before the old one closes --
        closing the last visible window first would trip Qt's
        quitOnLastWindowClosed and exit the app mid-switch.
        """
        index = max(0, min(index, len(self.levels) - 1))
        if index == self.index:
            return self.ui
        old_ui = self.ui
        anchor = old_ui.frameGeometry().topLeft() if old_ui is not None else None

        self.index = index
        if self.remember:
            self.settings.setValue("%s/level" % self.settings_prefix, index)

        self.ui = self._open(index, anchor)

        if old_ui is not None:
            old_ui.close()      # runs its own closeEvent -- saves its geometry
            old_ui.deleteLater()
        return self.ui

    def _open(self, index, anchor):
        # Set before build() runs: the caller's build() is expected to call
        # restore_geometry() on its way to returning, which reads this.
        self._pending_anchor = anchor
        path = self.levels[index]
        ui = self.build(path)
        self._install_close_handler(ui, index)
        self._connect_nav_buttons(ui)
        return ui

    def _install_close_handler(self, ui, index):
        """Save `ui`'s geometry under `index`'s own key when it closes.

        `index` is captured by value in the closure so a later switch -- which
        changes self.index before this handler ever fires -- cannot make an
        outgoing window's close save its geometry under the new level's key.
        """
        key = self._geometry_key(index)
        settings = self.settings

        def on_close(event):
            settings.setValue(key, ui.saveGeometry())
            event.accept()

        ui.closeEvent = on_close

    def _connect_nav_buttons(self, ui):
        """Wire whichever of More/Less this level's .ui actually has."""
        more = ui.findChild(QPushButton, self.more_button)
        less = ui.findChild(QPushButton, self.less_button)
        if more is not None:
            more.clicked.connect(lambda: self.switch_to(self.index - 1))
        if less is not None:
            less.clicked.connect(lambda: self.switch_to(self.index + 1))

    # -- geometry -------------------------------------------------------

    def _geometry_key(self, index):
        basename = os.path.basename(self.levels[index])
        return "%s/geometry/%s" % (self.settings_prefix, basename)

    def restore_geometry(self, ui):
        """Apply this level's saved geometry, then anchor to the prior window.

        Call this once the caller has already shown `ui`, sized it to its
        designed layout and run any resize/font scaling that depends on
        that initial size -- restoring geometry first would hand the
        resizer a size that does not match what it snapshotted.
        """
        saved = self.settings.value(self._geometry_key(self.index))
        if saved is not None:
            ui.restoreGeometry(saved)
        anchor = getattr(self, "_pending_anchor", None)
        if anchor is not None:
            ui.move(anchor)
        self._pending_anchor = None
