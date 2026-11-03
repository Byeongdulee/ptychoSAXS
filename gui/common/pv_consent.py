"""
Scan-number PV writes, which this GUI now performs unconditionally.

12idc:data:fileIndex is shared, and writing it renumbers runs for anything
else reading it. This module used to put a dialog in the way at startup and
let the answer govern the session. That prompt has been removed by request:
writes are always allowed, and nothing is asked.

The class survives the removal on purpose. `granted` still gates the single
write site in rungui.write_scan_number_pv, and `request()` / `revoke()` are
still called from rungui, so keeping the shape means the policy can be put
back by editing this file alone. The constants are likewise still exported
because the callers compare against CLOSED.

What is *not* lost with the dialog: ptychosaxs.pv_audit remains the record of
which PVs this GUI reads and writes, and where. If the renumbering ever needs
to be explained or re-gated, that inventory is the place to start.
"""

from ptychosaxs import pv_audit

ALLOW = "allow"
DECLINE = "decline"
CLOSED = "closed"


class PvWriteConsent:
    """Always-granted stand-in for the former startup consent prompt.

    Granted from construction, so a caller that reads `granted` without ever
    calling `request()` behaves the same as one that does.
    """

    def __init__(self, parent=None, ask=None):
        self._parent = parent
        # Retained so existing callers (and tests) can still inject a dialog
        # without a TypeError. Never invoked - that is the point of the change.
        self._ask = ask
        self._granted = True
        self._asked = True

    @property
    def granted(self) -> bool:
        """Whether writes are allowed. Always True."""
        return self._granted

    @property
    def asked(self) -> bool:
        return self._asked

    def revoke(self):
        """No-op: a configuration change no longer re-opens the question."""

    def request(self, reason="starting up") -> str:
        """Return ALLOW without asking anything. Never returns CLOSED."""
        return ALLOW
