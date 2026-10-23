"""Pre-scan guards as data.

The point of the split: the decision to ask is a pure function, so these
run with no Qt, no window and no instrument. The asking stays a
QMessageBox in the panel, rendered by one _ask method.

That is also what lets a scan be started by something with no screen. A
macro, the CLI and an MCP tool answer guards from a policy instead, which
generalises the special case the GUI already had -- _confirm_large_scan
skipped its dialog when macro_running was set.
"""

import pytest

from ptychosaxs.api import guards


# -- large scan --------------------------------------------------------------

def test_a_small_scan_asks_nothing():
    assert guards.large_scan(10, 1.0, 0.5) is None


def test_a_scan_at_the_threshold_asks_nothing():
    """The dialog is for scans *larger* than the threshold."""
    assert guards.large_scan(guards.LARGE_SCAN_THRESHOLD, 1.0, 0.5) is None


def test_a_large_scan_asks_before_committing_the_beam_time():
    g = guards.large_scan(400, 1.0, 0.5)
    assert g.kind == guards.CONFIRM
    assert g.choices == ("Proceed", "Cancel")
    assert "400 positions" in g.text


def test_a_large_scan_shows_what_it_will_cost():
    g = guards.large_scan(400, 1.0, 0.5)
    assert "10.0 min" in g.text, g.text      # 400 * 1.5 s = 600 s


def test_cancel_is_the_default_so_silence_is_safe():
    g = guards.large_scan(400, 1.0, 0.5)
    assert g.default == "Cancel"
    assert g.allows(g.default) is False


def test_an_unattended_run_is_not_asked():
    """Nobody is there to click Proceed during a macro."""
    assert guards.large_scan(400, 1.0, 0.5, unattended=True) is None


def test_the_threshold_is_adjustable_per_call():
    assert guards.large_scan(100, 1.0, 0.5, threshold=50) is not None


# -- warnings ----------------------------------------------------------------

def test_multi_pulse_without_the_hdf_plugin_is_refused():
    g = guards.multi_pulse_needs_hdf(4, hdf_plugin_enabled=False)
    assert g.kind == guards.WARN
    assert g.blocks
    assert "4" in g.text


def test_multi_pulse_with_the_plugin_is_fine():
    assert guards.multi_pulse_needs_hdf(4, hdf_plugin_enabled=True) is None


def test_a_single_pulse_never_needs_the_plugin():
    assert guards.multi_pulse_needs_hdf(1, hdf_plugin_enabled=False) is None


@pytest.mark.parametrize("mode,asks", [("scan", False), ("idle", True),
                                       ("", True), (None, True)])
def test_the_saxs_detector_must_be_in_scan_mode(mode, asks):
    assert (guards.saxs_detector_mode(mode) is not None) is asks


# -- velocity limits ---------------------------------------------------------

def test_a_velocity_inside_the_limits_passes():
    assert guards.velocity_limits({"X": 0.1, "phi": 10.0}) is None


def test_too_fast_is_refused_before_anything_moves():
    g = guards.velocity_limits({"X": 5.0})
    assert g.key == "velocity_too_high"
    assert g.blocks, "there is nothing to agree to here"
    assert "0.2000" in g.text, g.text


def test_too_slow_is_refused_too():
    g = guards.velocity_limits({"X": 0.000001})
    assert g.key == "velocity_too_low"


def test_the_sign_of_a_velocity_does_not_matter():
    """A fly scan runs in both directions; only the speed is limited."""
    assert guards.velocity_limits({"X": -5.0}).key == "velocity_too_high"


def test_an_axis_with_no_configured_limit_is_not_checked():
    assert guards.velocity_limits({"UNKNOWN_AXIS": 1e6}) is None


def test_limits_can_be_supplied_per_call():
    assert guards.velocity_limits({"X": 5.0}, maximums={"X": 10.0},
                                  minimums={}) is None


def test_the_limits_are_readable_without_importing_a_gui():
    """They used to be class attributes on a Qt handler, so the numbers
    that keep the hexapod inside what it will accept could only be read by
    importing a GUI."""
    assert guards.MAX_VELOCITY["phi"] == 37.0
    assert set(guards.MIN_VELOCITY) == set(guards.MAX_VELOCITY)


# -- answering without a screen ----------------------------------------------

def test_a_macro_may_agree_to_a_long_scan():
    g = guards.large_scan(400, 1.0, 0.5)
    assert guards.MACRO_POLICY.answer(g) == "Proceed"
    assert g.allows(guards.MACRO_POLICY.answer(g))


def test_a_macro_may_not_wave_away_a_hardware_warning():
    """Running lots of scans unattended is the point of a macro. Ignoring
    the stage telling you no is not."""
    g = guards.velocity_limits({"X": 5.0})
    assert not g.allows(guards.MACRO_POLICY.answer(g))


def test_the_strict_policy_agrees_to_nothing():
    g = guards.large_scan(400, 1.0, 0.5)
    assert guards.STRICT_POLICY.answer(g) == "Cancel"


def test_an_unknown_guard_gets_its_default_not_a_crash():
    """A policy written before a guard existed must fail safe."""
    g = guards.Guard(key="invented_later", kind=guards.CONFIRM,
                     title="?", text="?", choices=("Proceed", "Cancel"),
                     default="Cancel", proceed="Proceed")
    assert guards.MACRO_POLICY.answer(g) == "Cancel"


# -- the handler still agrees ------------------------------------------------

def test_the_scan_handler_routes_every_guard_through_one_renderer():
    """_ask is the single place a decision becomes a dialog."""
    import inspect
    from gui.handlers import scan_handler

    src = inspect.getsource(scan_handler.ScanHandler)
    for method in ("_confirm_large_scan", "_check_hdf_for_multi_pulse",
                   "_check_saxs_det_mode", "_check_velocity_limits"):
        body = src.split(f"def {method}(")[1].split("\n    def ")[0]
        assert "QMessageBox" not in body, (
            f"{method} still builds its own dialog")
        assert "self._ask(" in body, f"{method} does not go through _ask"


def test_the_handler_still_exposes_the_limits_it_always_did():
    from gui.handlers.scan_handler import ScanHandler
    assert ScanHandler.MAX_VELOCITY is guards.MAX_VELOCITY
    assert ScanHandler.LARGE_SCAN_THRESHOLD == guards.LARGE_SCAN_THRESHOLD
