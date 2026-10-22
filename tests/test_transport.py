"""
pytest suite for driver selection.

Three switches exist: which transport the hexapod uses, and whether the
goniometer and phi are driven by their vendor libraries instead of their
IOCs. What is worth testing is mostly the refusals - a mistyped switch,
and a driver whose library is not installed here - because the design
promise is that either costs one stage rather than the whole GUI.

Out of scope: real IOCs, real vendor libraries, Qt. transport.py imports
none of them, and the tests that touch motions_ver2 drive it with every
stage simulated.
"""
import pytest

from ptychosaxs import transport as t


# ---------------------------------------------------------------------------
# The hexapod's transport - the only genuine epics/direct choice
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("want", [t.EPICS, t.DIRECT])
def test_hexapod_transport_is_honoured(want):
    assert t.resolve("hexapod", env={t.ENV_HEXAPOD_TRANSPORT: want}) == (want, "")


def test_hexapod_defaults_to_epics_when_unset():
    assert t.resolve("hexapod", env={})[0] == t.DEFAULT_HEXAPOD_TRANSPORT


def test_the_default_is_something_the_hexapod_actually_supports():
    assert t.DEFAULT_HEXAPOD_TRANSPORT in t.CHOICES["hexapod"]


def test_a_mistyped_transport_says_so_rather_than_simulating_silently():
    driver, why = t.resolve("hexapod", env={t.ENV_HEXAPOD_TRANSPORT: "epcis"})
    assert driver == t.SIMULATED
    assert "not a hexapod transport" in why
    assert t.EPICS in why and t.DIRECT in why


def test_hexapod_can_be_simulated_through_the_switch():
    assert t.resolve("hexapod", env={t.ENV_HEXAPOD_TRANSPORT: t.SIMULATED})[0] == t.SIMULATED


# ---------------------------------------------------------------------------
# The gonio's driver - a boolean, and EPICS is not a transport for it
# ---------------------------------------------------------------------------

def test_gonio_defaults_to_its_ioc():
    assert t.resolve("gonio", env={}) == (t.EPICS, "")


@pytest.mark.parametrize("set_to", ["1", "true", "TRUE", "yes", "on"])
def test_gonio_switches_to_the_smaract_sdk(set_to):
    assert t.resolve("gonio", env={t.ENV_GONIO_SMARACT: set_to}) == (t.DIRECT, "")


@pytest.mark.parametrize("set_to", ["0", "false", "no", "off", ""])
def test_anything_else_leaves_the_gonio_on_epics(set_to):
    assert t.resolve("gonio", env={t.ENV_GONIO_SMARACT: set_to}) == (t.EPICS, "")


def test_the_hexapod_switch_does_not_touch_the_gonio():
    """They are separate concepts - the gonio has no transport."""
    env = {t.ENV_HEXAPOD_TRANSPORT: t.DIRECT}
    assert t.resolve("gonio", env=env)[0] == t.EPICS


# ---------------------------------------------------------------------------
# phi's driver - its IOC by default, acspy on request
# ---------------------------------------------------------------------------

def test_phi_defaults_to_its_ioc():
    assert t.resolve("phi", env={}) == (t.EPICS, "")


@pytest.mark.parametrize("set_to", ["1", "true", "TRUE", "yes", "on"])
def test_phi_switches_to_acspy(set_to):
    assert t.resolve("phi", env={t.ENV_PHI_ACS: set_to}) == (t.DIRECT, "")


@pytest.mark.parametrize("set_to", ["0", "false", "no", "off", ""])
def test_anything_else_leaves_phi_on_epics(set_to):
    assert t.resolve("phi", env={t.ENV_PHI_ACS: set_to}) == (t.EPICS, "")


def test_the_hexapod_switch_does_not_touch_phi():
    """They are separate concepts - phi has no transport."""
    env = {t.ENV_HEXAPOD_TRANSPORT: t.DIRECT}
    assert t.resolve("phi", env=env)[0] == t.EPICS


# ---------------------------------------------------------------------------
# Shared rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", t.STAGES)
def test_debug_overrides_every_switch(stage):
    env = {t.ENV_HEXAPOD_TRANSPORT: t.DIRECT, t.ENV_GONIO_SMARACT: "1"}
    assert t.resolve(stage, simulated=True, env=env) == (t.SIMULATED, "")


def test_an_unknown_stage_does_not_raise():
    driver, why = t.resolve("beamstop", env={})
    assert driver == t.SIMULATED
    assert "not a known stage" in why


def test_the_real_environment_is_used_when_none_is_passed(monkeypatch):
    monkeypatch.setenv(t.ENV_HEXAPOD_TRANSPORT, t.DIRECT)
    assert t.resolve("hexapod")[0] == t.DIRECT


@pytest.mark.parametrize("stage", t.STAGES)
def test_every_choice_is_described(stage):
    """These end up in the startup log, so they have to name something."""
    for driver in t.CHOICES[stage]:
        text = t.describe(stage, driver)
        assert text and text != driver


def test_simulated_is_described_without_a_stage_name():
    assert t.describe("hexapod", t.SIMULATED) == "a simulated stand-in"


# ---------------------------------------------------------------------------
# The builders fall back rather than propagate
# ---------------------------------------------------------------------------

def _all_simulated(monkeypatch):
    for flag in ("HEXAPOD", "ACS", "GONIO"):
        monkeypatch.setenv(f"PTYCHOSAXS_DEBUG_{flag}", "1")


def test_a_missing_library_falls_back_instead_of_raising(monkeypatch, capsys):
    """The acspy phi needs acspy. Where it is absent the stage is simulated
    and the GUI still starts - that is the whole design."""
    _all_simulated(monkeypatch)
    monkeypatch.setenv(t.ENV_PHI_ACS, "1")
    import motions_ver2

    def _explode(driver):
        if driver == t.DIRECT:
            raise t.TransportUnavailable("acspy is not installed")
        return "simulated-phi"

    monkeypatch.setitem(motions_ver2._BUILDERS, "phi", _explode)
    controller, got = motions_ver2.build_controller("phi", simulated=False)
    assert (controller, got) == ("simulated-phi", t.SIMULATED)
    assert "acspy is not installed" in capsys.readouterr().out


def test_a_mistyped_switch_reason_reaches_the_operator(monkeypatch, capsys):
    _all_simulated(monkeypatch)
    monkeypatch.setenv(t.ENV_HEXAPOD_TRANSPORT, "epcis")
    import motions_ver2

    monkeypatch.setitem(motions_ver2._BUILDERS, "hexapod", lambda d: d)
    _controller, got = motions_ver2.build_controller("hexapod", simulated=False)
    assert got == t.SIMULATED
    assert "not a hexapod transport" in capsys.readouterr().out


def test_builders_and_choices_cover_the_same_stages():
    """A stage in one and not the other is either a switch with nothing
    behind it, or a driver nobody can select."""
    import motions_ver2

    assert set(motions_ver2._BUILDERS) == set(t.CHOICES) == set(t.STAGES)
