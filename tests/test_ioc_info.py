"""
pytest suite for the IOC-identity report behind File > IOC info.

An IOC that restarted mid-session is invisible from the GUI: the records
still answer and autosave has put plausible numbers back. This report is how
a user finds out, so the thing worth testing is that it answers for every
IOC - including the ones that do not publish devIocStats, and the ones that
are not there at all - without raising on the GUI thread.

Out of scope: real Channel Access. Every PV here is a fake.
"""
import pytest

from ptychosaxs import ioc_info


class _FakePV:
    """Minimal epics.PV stand-in driven by a dict of {pvname: value}."""

    values = {}
    hosts = {}

    def __init__(self, pvname, *args, **kwargs):
        self.pvname = pvname
        self.connected = pvname in self.values
        self.host = self.hosts.get(pvname)

    def wait_for_connection(self, timeout=None):
        return self.connected

    def get(self, timeout=None):
        return self.values.get(self.pvname)


@pytest.fixture
def fake_pv():
    _FakePV.values = {}
    _FakePV.hosts = {}
    return _FakePV


_TABLE = [
    ("iocA", "Stage A", "iocA:m1.RBV"),
    ("iocB", "Stage B", "iocB:m1.RBV"),
    ("iocC", "Stage C", "iocC:m1.RBV"),
]


def test_devicocstats_is_used_where_it_is_loaded(fake_pv):
    fake_pv.values = {
        "iocA:HOSTNAME": "host-a",
        "iocA:STARTTOD": "10/05/2026 14:33:34",
        "iocA:ENGINEER": "Peterson",
        "iocA:UPTIME": "2 days",
    }
    row = ioc_info.ioc_status("iocA", "Stage A", "iocA:m1.RBV", pv_class=fake_pv)
    assert row["reachable"] is True
    assert row["host"] == "host-a"
    assert row["started"] == "10/05/2026 14:33:34"
    assert row["engineer"] == "Peterson"
    assert row["source"] == "devIocStats"


def test_falls_back_to_the_channel_host_without_devicocstats(fake_pv):
    """Three of the real IOCs do not load devIocStats. The host serving a
    known channel still answers the question that matters most."""
    fake_pv.values = {"iocB:m1.RBV": 0.0}
    fake_pv.hosts = {"iocB:m1.RBV": "host-b:5064"}
    row = ioc_info.ioc_status("iocB", "Stage B", "iocB:m1.RBV", pv_class=fake_pv)
    assert row["reachable"] is True
    assert row["host"] == "host-b:5064"
    assert row["started"] is None
    assert "no devIocStats" in row["source"]


def test_an_absent_ioc_is_a_row_not_an_exception(fake_pv):
    row = ioc_info.ioc_status("iocC", "Stage C", "iocC:m1.RBV", pv_class=fake_pv)
    assert row["reachable"] is False
    assert row["host"] is None


def test_a_pv_class_that_raises_does_not_propagate(fake_pv):
    class _Explodes:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("CA is not initialised")

    row = ioc_info.ioc_status("iocA", "Stage A", "iocA:m1.RBV", pv_class=_Explodes)
    assert row["reachable"] is False


def test_collect_returns_one_row_per_ioc_in_order(fake_pv):
    fake_pv.values = {"iocA:HOSTNAME": "host-a"}
    rows = ioc_info.collect(table=_TABLE, pv_class=fake_pv)
    assert [r["prefix"] for r in rows] == ["iocA", "iocB", "iocC"]


def test_the_real_table_covers_the_motor_iocs():
    prefixes = {prefix for prefix, _role, _probe in ioc_info.IOC_TABLE}
    assert {"12idbHXP", "12idePtycho", "12ideMCS1", "12ideMCS2"} <= prefixes


def test_hexapod_link_reports_up_down_and_unknown(fake_pv):
    from ptychosaxs.epics_hexapod import ASYN_CNCT_PV, ASYN_CONNECTED

    assert ioc_info.hexapod_link_status(pv_class=fake_pv) is None  # IOC absent
    fake_pv.values = {ASYN_CNCT_PV: ASYN_CONNECTED}
    assert ioc_info.hexapod_link_status(pv_class=fake_pv) is True
    fake_pv.values = {ASYN_CNCT_PV: 0}
    assert ioc_info.hexapod_link_status(pv_class=fake_pv) is False


def test_report_names_every_ioc_and_flags_a_down_controller(fake_pv):
    fake_pv.values = {"iocA:HOSTNAME": "host-a", "iocA:STARTTOD": "now"}
    rows = ioc_info.collect(table=_TABLE, pv_class=fake_pv)
    text = ioc_info.format_report(rows, link_ok=False)
    assert "iocA" in text and "host-a" in text
    assert "unreachable" in text  # iocB and iocC
    assert "DOWN" in text


def test_report_omits_the_controller_line_when_it_cannot_tell(fake_pv):
    rows = ioc_info.collect(table=_TABLE, pv_class=fake_pv)
    assert "Hexapod controller link" not in ioc_info.format_report(rows, link_ok=None)
