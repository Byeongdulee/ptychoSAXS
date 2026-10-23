"""The tools this server exposes. Deliberately none yet.

To add one, write it inside register_all and give it a docstring -- the SDK
uses the signature for the input schema and the docstring for the
description, so there is no schema to hand-maintain:

    @app.tool(name="get_positions", title="Read motor positions")
    def get_positions() -> str:
        \"\"\"Current position of every configured axis, in its own units.\"\"\"
        return json.dumps(get_client().motion.positions())

`get_client()` returns a connected BeamlineClient, opening the connection on
first use. Everything it can do is in ptychosaxs/link/methods.py.

Two rules worth keeping:

* **Read-only first.** An MCP host is a process a model drives, and the
  hexapod is not reversible. A tool that moves anything goes behind
  MUTATING_ALLOWED, so turning it on is a deliberate act by whoever runs
  the host.
* **Return text, not objects.** JSON in a string is readable in a
  transcript and survives a model paraphrasing it; a bare float does not
  say what it measured.
"""

import os

#: Opt-in for tools that move hardware. Checked by require_mutating().
MUTATING_ALLOWED = os.environ.get("PTYCHOSAXS_MCP_ALLOW_MUTATING") == "1"


class MutatingNotAllowed(RuntimeError):
    """A tool that moves hardware was called without the opt-in."""


def require_mutating(what):
    """Call at the top of any tool that changes the instrument's state."""
    if not MUTATING_ALLOWED:
        raise MutatingNotAllowed(
            f"{what} would move hardware, and this MCP server is read-only. "
            f"Set PTYCHOSAXS_MCP_ALLOW_MUTATING=1 to permit it.")


def register_all(app, get_client):
    """Register every tool on `app`. Returns the names registered.

    Empty today. See the module docstring for the shape of a new one.
    """
    registered = []
    return registered
