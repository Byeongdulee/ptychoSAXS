"""Connection bookkeeping: who may connect, and what that opens.

Pure unit tests -- no socket, no daemon, no device layer.
"""

import os

import pytest

from ptychosaxs.server.registry import (
    REQUIREMENTS, ClientInfo, ConnectionRegistry, DuplicateGui,
)
import logging

ME = os.getpid()
DEAD = 999_999_999          # a pid that cannot exist


@pytest.fixture
def reg():
    return ConnectionRegistry(logging.getLogger("test"))


def info(conn_id, kind="gui", gui_type=None, pid=ME, label=""):
    return ClientInfo(conn_id, kind, gui_type, pid, label)


def test_one_gui_of_each_type_may_connect(reg):
    reg.register(info("c1", gui_type="rungui"))
    reg.register(info("c2", gui_type="optics_motors"))
    reg.register(info("c3", gui_type="crl_3dprint"))
    assert reg.live_count() == 3


def test_a_second_copy_of_the_same_gui_is_refused(reg):
    reg.register(info("c1", gui_type="rungui", label="first"))
    with pytest.raises(DuplicateGui) as exc:
        reg.register(info("c2", gui_type="rungui", label="second"))
    assert exc.value.holder.conn_id == "c1"
    assert reg.live_count() == 1


def test_the_refusal_names_the_process_holding_the_slot(reg):
    """The operator needs to know which window to go and find.

    The holder has to be a live pid: a dead one is reaped instead of
    refused, which is what the next test covers.
    """
    reg.register(info("c1", gui_type="rungui", pid=ME))
    with pytest.raises(DuplicateGui) as exc:
        reg.register(info("c2", gui_type="rungui"))
    assert exc.value.holder.pid == ME
    assert str(ME) in str(exc.value)


@pytest.mark.parametrize("kind", ["cli", "mcp", "observer"])
def test_non_gui_clients_may_connect_many_times(kind, reg):
    """Refusing a second CLI invocation would break scripting, and neither
    the CLI nor MCP owns a window or an ini file."""
    for n in range(4):
        reg.register(info(f"c{n}", kind=kind))
    assert reg.live_count() == 4


def test_a_crashed_gui_does_not_lock_its_type_out_forever(reg):
    """Otherwise a SIGKILLed panel could never be reopened without
    restarting the server."""
    reg.register(info("c1", gui_type="rungui", pid=DEAD))
    reg.register(info("c2", gui_type="rungui", pid=ME, label="reopened"))
    assert reg.holder_of("rungui").conn_id == "c2"
    assert reg.live_count() == 1


def test_disconnecting_frees_the_slot(reg):
    reg.register(info("c1", gui_type="rungui"))
    reg.unregister("c1")
    reg.register(info("c2", gui_type="rungui"))
    assert reg.holder_of("rungui").conn_id == "c2"


def test_device_groups_are_the_union_of_what_clients_need(reg):
    reg.register(info("c1", gui_type="rungui"))
    reg.register(info("c2", gui_type="optics_motors"))
    assert reg.required_groups() == set(REQUIREMENTS["rungui"])


def test_groups_are_given_back_when_the_client_leaves(reg):
    reg.register(info("c1", gui_type="rungui"))
    assert reg.required_groups()
    reg.unregister("c1")
    assert reg.required_groups() == set()


def test_a_group_stays_required_while_any_client_still_needs_it(reg):
    """rungui and sample_alignment both drive the motion group."""
    reg.register(info("c1", gui_type="rungui"))
    reg.register(info("c2", gui_type="sample_alignment"))
    reg.unregister("c1")
    assert "motion" in reg.required_groups()


def test_an_unknown_client_type_opens_nothing(reg):
    """An unrecognised client must not be able to power up a stage."""
    reg.register(info("c1", gui_type="something_new"))
    assert reg.required_groups() == set()


def test_a_client_whose_process_is_gone_is_reported_dead(reg):
    reg.register(info("c1", gui_type="rungui", pid=DEAD))
    dead = reg.dead_clients(heartbeat_s=5, max_misses=3)
    assert [i.conn_id for i, _why in dead] == ["c1"]


def test_a_live_client_is_not_reported_dead(reg):
    reg.register(info("c1", gui_type="rungui", pid=ME))
    assert reg.dead_clients(heartbeat_s=5, max_misses=3) == []
