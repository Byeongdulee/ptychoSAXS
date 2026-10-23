"""The macro runner calls scan methods instead of clicking buttons.

Clicking was never the intent. The per-motor scan entry points took
motornumber=-1, meaning "work out which motor from self.sender()", so a
direct call had no sender to read -- and the only way in was to find the
button, force it enabled because the macro window disables them all,
click it, and put it back. gui/macro_window.py said as much in a comment.

The entry points now take the index explicitly, so the macro calls them.
Saved macros are untouched: they store button names, and the table is
keyed by button name.
"""


import pytest

from gui.macro_window import _scan_action


class FakeHandler:
    def __init__(self, log):
        self._log = log

    def takeshot(self):
        self._log.append(("takeshot",))


class FakeMain:
    """Records what the macro asked for, without a GUI."""

    def __init__(self):
        self.calls = []
        self.scan_handler = FakeHandler(self.calls)

    def stepscan(self, motornumber):
        self.calls.append(("stepscan", motornumber))

    def fly(self, motornumber):
        self.calls.append(("fly", motornumber))

    def stepscan2d(self):
        self.calls.append(("stepscan2d",))

    def fly2d(self, snake=False):
        self.calls.append(("fly2d", snake))

    def stepscan3d(self, *axes):
        self.calls.append(("stepscan3d", axes))

    def fly3d(self, snake=False):
        self.calls.append(("fly3d", snake))

    def _scan_axis_indices(self):
        return (0, 2, 6)


@pytest.fixture
def main():
    return FakeMain()


@pytest.mark.parametrize("button,expected", [
    ("pb_lup_step2d", ("stepscan2d",)),
    ("pb_SAXSscan_fly2d", ("fly2d", True)),
    ("pb_SAXSscan_fly3d", ("fly3d", True)),
    ("pb_takeshot", ("takeshot",)),
])
def test_each_scan_button_maps_to_what_it_calls(button, expected, main):
    _scan_action(main, button)()
    assert main.calls == [expected]


def test_the_3d_step_scan_gets_the_panel_s_chosen_axes(main):
    _scan_action(main, "pb_lup_step3d")()
    assert main.calls == [("stepscan3d", (0, 2, 6))]


@pytest.mark.parametrize("slot", [1, 2, 3, 7, 8, 9])
def test_a_per_motor_step_button_passes_its_own_motor(slot, main):
    """The index used to be recovered by parsing the sender's object name.
    A direct call has no sender, which is why this had to change."""
    _scan_action(main, f"pb_lup_{slot}")()
    assert main.calls == [("stepscan", slot - 1)]


@pytest.mark.parametrize("slot", [1, 2, 3, 7, 8, 9])
def test_a_per_motor_fly_button_passes_its_own_motor(slot, main):
    _scan_action(main, f"pb_SAXSscan_{slot}")()
    assert main.calls == [("fly", slot - 1)]


def test_motor_indices_are_zero_based_as_the_entry_points_expect():
    """1-based in the UI, 0-based in the scan code. Getting this backwards
    would scan the wrong stage, silently."""
    main = FakeMain()
    _scan_action(main, "pb_lup_1")()
    assert main.calls == [("stepscan", 0)]


@pytest.mark.parametrize("button", [
    "pb_exit", "pb_some_future_button", "", None, "pb_lup_step2d_extra",
])
def test_a_button_that_is_not_a_scan_has_no_action(button, main):
    """Those fall back to the old click, so a saved macro naming a button
    added since cannot silently do nothing."""
    assert _scan_action(main, button) is None


def test_every_static_scan_command_resolves(main):
    """Nothing in the command table may be left without an action."""
    from gui.macro_window import STATIC_COMMANDS

    for name, spec in STATIC_COMMANDS.items():
        if spec.get("kind") != "click":
            continue
        button = spec.get("button_name")
        assert _scan_action(main, button) is not None, (
            f"macro command {name!r} clicks {button!r} with no direct action")


def test_the_saved_macros_on_disk_all_resolve(main):
    """gui/macro_commands.json holds real saved programs. Every scan button
    they name must still be callable, or someone's macro stops working."""
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "gui" / "macro_commands.json"
    saved = json.loads(path.read_text())
    buttons = {c.get("button_name") for c in saved
               if isinstance(c, dict) and c.get("kind") == "click"}
    buttons.discard(None)
    assert buttons, "expected saved click commands to check"
    unresolved = sorted(b for b in buttons if _scan_action(main, b) is None)
    assert unresolved == [], (
        f"saved macros name buttons with no direct action: {unresolved}")


def test_the_runner_no_longer_forces_a_button_enabled_for_a_known_scan():
    """The enable/click/restore dance was the workaround. For a mapped
    button it must be gone entirely."""
    import inspect

    from gui import macro_window

    src = inspect.getsource(macro_window.MacroExecutionEngine._on_invoke_command)
    before_fallback = src.split("has no direct action")[0]
    assert "setEnabled(True)" not in before_fallback, (
        "a known scan button is still being force-enabled and clicked")
    assert "action()" in before_fallback
