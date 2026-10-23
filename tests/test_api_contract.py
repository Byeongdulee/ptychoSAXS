"""The three lists that have to agree.

link.methods is the canonical set; the server registers handlers against
it and the client's facades call into it. A name in one and not the others
is the same bug as a stage in transport.CHOICES with no builder behind it,
which tests/test_transport.py already rejects.
"""

from ptychosaxs.client import facades
from ptychosaxs.link import methods
from ptychosaxs.server import dispatch

#: Handled by the connection itself, before a client is registered.
HANDSHAKE_ONLY = {"system.hello"}


def test_every_declared_method_has_a_server_handler():
    assert dispatch.missing_handlers() == []


def test_no_handler_is_registered_for_an_undeclared_method():
    assert set(dispatch.HANDLERS) <= methods.NAMES


def test_every_method_is_reachable_from_a_client_facade():
    missing = methods.NAMES - facades.facade_methods() - HANDSHAKE_ONLY
    assert missing == set(), f"no facade for: {sorted(missing)}"


def test_no_facade_calls_a_method_that_does_not_exist():
    assert facades.facade_methods() <= methods.NAMES


def test_blocking_methods_are_declared_in_both_places():
    for name, handler in dispatch.HANDLERS.items():
        if handler.blocking:
            assert name in methods.BLOCKING, (
                f"{name} runs as a job but is not in methods.BLOCKING")


def test_methods_answerable_before_ready_are_all_real_methods():
    assert methods.PRE_READY <= methods.NAMES


def test_registering_an_undeclared_method_is_refused():
    """A typo must fail at import, not at the beamline."""
    import pytest
    with pytest.raises(KeyError):
        dispatch.method("motion.teleport")(lambda ctx, params: None)


def test_every_method_has_a_one_line_description():
    """`ptychosaxs-server --help` and the contract doc read from these."""
    for name, text in methods.ALL.items():
        assert text and text.strip().endswith("."), name
