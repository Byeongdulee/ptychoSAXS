"""The flat .ini format the GUI persists its settings in.

This replaces the external py12inifunc package, which was imported behind a
try/except that fell back to a stub with no-op readini/writeini. On a machine
without it installed the GUI therefore forgot the working folder, log
filename, data basepaths, scan name and scan number on every restart, and
said nothing. These tests pin the round trip that was silently missing.
"""
import os

import pytest

from beamline_ini import (
    Ini,
    SafeIni,
    ensure_default_ini,
    parse_line,
    quote_list,
    quote_text,
    unquote_list,
    unquote_text,
)

DEFAULTS = [
    ("Working Folder Name", "working_folder", quote_text("")),
    ("Log File Name", "logfilename", quote_text("")),
    ("Base Name of Data Files in Linux", "base_linux_datafolder", "/net/micdata/data2/"),
    ("Scan Name", "scan_name", quote_text("")),
    ("Scan Number", "scan_number", "0.0000"),
    ("SoftGlue Channels", "softglue_channels", quote_list(["B", "C", "D"])),
    ("Reference Position X", "_ref_X", "0.0000"),
    ("Number of Frames per Exposure", "_pulses_per_step", "1.0000"),
    ("Scan Wait", "_step_acq_time", "1.0000"),
]


@pytest.fixture
def ini_path(tmp_path):
    return str(tmp_path / "pty-co-saxs.ini")


@pytest.fixture
def fresh(ini_path):
    ensure_default_ini(ini_path, DEFAULTS)
    obj = SafeIni(ini_path)
    obj.readini()
    return obj


# ---------------------------------------------------------------------------
# The round trip that was missing entirely
# ---------------------------------------------------------------------------

def test_settings_survive_a_restart(fresh, ini_path):
    fresh.working_folder = "/net/micdata/data2/12IDC/2026_Data/2026_3/02_test"
    fresh.logfilename = "/net/micdata/data2/12IDC/2026_Data/2026_3/02_test/log.txt"
    fresh.base_linux_datafolder = "/net/micdata/data2/"
    fresh.scan_name = "test"
    fresh.scan_number = 5
    fresh.writeini()

    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert reloaded.working_folder == fresh.working_folder
    assert reloaded.logfilename == fresh.logfilename
    assert reloaded.base_linux_datafolder == "/net/micdata/data2/"
    assert reloaded.scan_name == "test"
    assert reloaded.scan_number == 5


def test_a_windows_path_survives_the_round_trip(fresh, ini_path):
    """Drive letters, backslashes, spaces and commas all collide with the
    format's own separators, which is what the quoting layer is for."""
    messy = r"C:\Users\s12idc\My Documents\run, final.txt"
    fresh.logfilename = messy
    fresh.working_folder = r"Z:\12IDC\2026_Data\2026_3\02_test"
    fresh.writeini()

    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert reloaded.logfilename == messy
    assert reloaded.working_folder == r"Z:\12IDC\2026_Data\2026_3\02_test"


def test_empty_text_round_trips_as_empty_not_as_a_list(fresh, ini_path):
    """A blank value reads back as an empty *list* in this format, so empty
    text needs its own placeholder."""
    fresh.working_folder = ""
    fresh.writeini()
    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert reloaded.working_folder == ""


def test_softglue_channels_round_trip_as_strings(fresh, ini_path):
    fresh.softglue_channels = ["B", "C", "D"]
    fresh.writeini()
    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert reloaded.softglue_channels == ["B", "C", "D"]


def test_repeated_writes_are_stable(fresh, ini_path):
    fresh.working_folder = "/net/micdata/data2/12IDC/x"
    fresh.logfilename = r"C:\a b\c, d.txt"
    for _ in range(3):
        fresh.writeini()
        fresh.readini()
    assert fresh.working_folder == "/net/micdata/data2/12IDC/x"
    assert fresh.logfilename == r"C:\a b\c, d.txt"


def test_writeini_does_not_leave_values_encoded_in_memory(fresh):
    """The encoding is applied for the write only; callers keep seeing real
    values afterwards."""
    fresh.working_folder = "/net/micdata/data2/12IDC/x"
    fresh.writeini()
    assert fresh.working_folder == "/net/micdata/data2/12IDC/x"
    assert fresh.softglue_channels == ["B", "C", "D"]


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
#
# Everything is stored as "%0.4f", so counts, indices and 0/1 flags would all
# come back as floats. The stub that stood in for a working reader handed out
# real ints, so nothing downstream was ever built to cope with a float -- and
# PyQt5 will not take one: setChecked(1.0) raises TypeError outright.

def test_whole_number_fields_come_back_as_int(fresh, ini_path):
    fresh.scan_number = 6
    fresh._pulses_per_step = 1
    fresh.writeini()

    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert isinstance(reloaded.scan_number, int)
    assert isinstance(reloaded._pulses_per_step, int)
    assert reloaded.scan_number == 6


def test_a_flag_field_is_of_a_type_pyqt_setchecked_accepts(ini_path):
    """The exact crash was QCheckBox.setChecked(1.0) -> TypeError.

    Asserted as a type contract rather than by driving a real QCheckBox: this
    suite deliberately never starts a QApplication (see conftest), and
    PyQt5's rule here is purely about the type -- it takes bool and int and
    rejects float.
    """
    with open(ini_path, "w") as f:
        f.write("Save Upstream Optics Positions, _save_us_optics : 1.0000\n")
    obj = SafeIni(ini_path)
    obj.readini()
    assert isinstance(obj._save_us_optics, int)
    assert not isinstance(obj._save_us_optics, float)
    assert bool(obj._save_us_optics) is True


def test_a_sensor_index_field_can_index_a_numpy_array(ini_path):
    """_qds_x_sensor/_qds_y_sensor are used as column indices; a float raises."""
    np = pytest.importorskip("numpy")
    with open(ini_path, "w") as f:
        f.write("Axis of QDS X, _qds_x_sensor : 0.0000\n")
        f.write("Axis of QDS Y, _qds_y_sensor : 1.0000\n")
    obj = SafeIni(ini_path)
    obj.readini()
    data = np.zeros((3, 4))
    data[:, obj._qds_x_sensor]
    data[:, obj._qds_y_sensor]


def test_genuinely_fractional_fields_stay_float(fresh, ini_path):
    fresh._step_acq_time = 0.25
    fresh._ref_X = 1.3655
    fresh.writeini()
    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert isinstance(reloaded._step_acq_time, float)
    assert reloaded._step_acq_time == pytest.approx(0.25)
    assert reloaded._ref_X == pytest.approx(1.3655)


# ---------------------------------------------------------------------------
# Robustness the original lacked: one bad line used to break every field
# ---------------------------------------------------------------------------

def test_a_malformed_line_does_not_break_the_other_fields(ini_path):
    with open(ini_path, "w") as f:
        f.write("Working Folder Name, working_folder : /net/micdata/data2/x\n")
        f.write("this line has no separators at all\n")
        f.write("\n")
        f.write("Scan Number, scan_number : 7.0000\n")
    obj = SafeIni(ini_path)
    obj.readini()
    assert obj.working_folder == "/net/micdata/data2/x"
    assert obj.scan_number == 7


def test_a_description_containing_a_colon_is_not_mistaken_for_the_separator(ini_path):
    with open(ini_path, "w") as f:
        f.write("Unit of QDS (nm: um: mm), _qds_unit : 1.0000\n")
    obj = Ini(ini_path)
    obj.readini()
    assert obj._qds_unit == 1.0


def test_only_keys_present_in_the_file_are_written(ini_path):
    """The file drives the write, as it did in the original package.

    An attribute that has no line in the .ini is not persisted, which is why
    every key the GUI relies on has to be in INI_DEFAULT_ENTRIES -- that is
    what ensure_default_ini puts there on first run and back-fills on upgrade.
    """
    ensure_default_ini(ini_path, DEFAULTS)
    obj = SafeIni(ini_path)
    obj.readini()
    obj.never_declared_anywhere = 1.0
    obj.writeini()
    assert "never_declared_anywhere" not in open(ini_path).read()


def test_unknown_keys_in_the_file_are_preserved_across_a_write(ini_path):
    with open(ini_path, "w") as f:
        f.write("Something Else, written_by_another_tool : 3.0000\n")
        f.write("Scan Number, scan_number : 1.0000\n")
    obj = Ini(ini_path)
    obj.readini()
    del obj.written_by_another_tool
    obj.scan_number = 2
    obj.writeini()
    assert "written_by_another_tool : 3.0000" in open(ini_path).read()


# ---------------------------------------------------------------------------
# ensure_default_ini
# ---------------------------------------------------------------------------

def test_a_missing_file_is_created_with_every_default(ini_path):
    ensure_default_ini(ini_path, DEFAULTS)
    text = open(ini_path).read()
    for _desc, key, _value in DEFAULTS:
        assert key in text


def test_backfill_adds_new_keys_without_touching_saved_ones(ini_path):
    ensure_default_ini(ini_path, DEFAULTS)
    obj = SafeIni(ini_path)
    obj.readini()
    obj.scan_name = "keepme"
    obj.writeini()

    extended = DEFAULTS + [("A New Setting", "_brand_new", "4.0000")]
    ensure_default_ini(ini_path, extended)

    reloaded = SafeIni(ini_path)
    reloaded.readini()
    assert reloaded.scan_name == "keepme"
    assert reloaded._brand_new == 4.0


def test_backfill_is_idempotent(ini_path):
    ensure_default_ini(ini_path, DEFAULTS)
    first = open(ini_path).read()
    ensure_default_ini(ini_path, DEFAULTS)
    assert open(ini_path).read() == first


def test_a_raw_pre_quoting_value_is_repaired_in_place(ini_path):
    """An .ini written before the quoting layer existed holds a raw path with
    spaces and colons in it, which would otherwise mis-parse on the next read."""
    with open(ini_path, "w") as f:
        f.write("Log File Name, logfilename : C:\\Users\\me\\My Logs\\run.txt\n")
    ensure_default_ini(ini_path, DEFAULTS)
    obj = SafeIni(ini_path)
    obj.readini()
    assert obj.logfilename == r"C:\Users\me\My Logs\run.txt"


# ---------------------------------------------------------------------------
# Unit-level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "",
    "/net/micdata/data2/12IDC/x",
    r"Z:\12IDC\2026_Data",
    "has spaces, commas: and colons",
])
def test_text_quoting_round_trips(value):
    assert unquote_text(quote_text(value)) == value


@pytest.mark.parametrize("value", [[], ["B"], ["B", "C", "D"]])
def test_list_quoting_round_trips(value):
    assert unquote_list(quote_list(value)) == value


def test_quoted_text_never_contains_a_format_separator():
    for value in ("a b", "a,b", "a:b", r"C:\x y,z"):
        encoded = quote_text(value)
        assert not any(c in encoded for c in ",: \t")


def test_parse_line_rejects_lines_without_both_separators():
    assert parse_line("") is None
    assert parse_line("no separators") is None
    assert parse_line("only, a comma") is None
    assert parse_line("desc, key : value") == ("desc", "key", "value")
