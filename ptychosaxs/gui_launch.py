"""Starting a panel that may already be open.

Every migrated GUI does the same three things at startup: connect, discover
it is a second copy, and ask the one that is already running to come
forward. Having that in one place keeps the panels' main() functions short
and stops each from reinventing the awkward part.
"""

import sys


def request_focus_and_exit(gui_type):
    """Ask the panel already holding `gui_type` to raise its window.

    Reconnects as a plain cli client rather than reusing the refused
    connection. Our own was closed with the refusal, and reconnecting as a
    gui would simply be refused again for the same reason -- which is what
    made the first version of this quietly do nothing at all.
    """
    from ptychosaxs.client.connection import BeamlineClient

    try:
        asker = BeamlineClient(kind="cli", label=f"{gui_type} relaunch")
        asker.connect(start_server=False, timeout_s=5)
    except Exception as exc:
        print(f"(could not ask it to come forward: {exc})", file=sys.stderr)
        return False
    try:
        asker.system.request_focus(gui_type)
        return True
    except Exception as exc:
        print(f"(could not ask it to come forward: {exc})", file=sys.stderr)
        return False
    finally:
        asker.close()
