"""Error codes shared by both ends of the link.

Codes are strings, not numbers. They are read in a log far more often than
they are switched on in code, and ERR_DUPLICATE_GUI in a log line needs no
lookup table. The actionable detail goes in `data` -- the pid of the GUI that
already holds the slot, the job id of the scan that is blocking shutdown.
"""


class LinkError(Exception):
    """Base for everything this package raises."""


class ProtocolError(LinkError):
    """A frame could not be parsed, or arrived out of order.

    Raised by wire.decode and by FrameReader. Always fatal to the connection:
    once framing is lost there is no way to resynchronise a byte stream.
    """


class ServerError(LinkError):
    """The server refused a request. Raised client-side from an error reply."""

    def __init__(self, code, message, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data or {}


class DuplicateGuiError(ServerError):
    """This GUI type is already open. Carries the holder's pid."""

    @property
    def pid(self):
        return self.data.get("pid")


# --- codes ------------------------------------------------------------------

ERR_PROTOCOL = "ERR_PROTOCOL"                # malformed frame, bad handshake order
ERR_UNKNOWN_METHOD = "ERR_UNKNOWN_METHOD"
ERR_BAD_PARAMS = "ERR_BAD_PARAMS"
ERR_NOT_READY = "ERR_NOT_READY"              # devices still being built
ERR_DUPLICATE_GUI = "ERR_DUPLICATE_GUI"
ERR_BUSY = "ERR_BUSY"                        # another job owns the hardware
ERR_DEVICE = "ERR_DEVICE"                    # the hardware said no
ERR_GUARD_DECLINED = "ERR_GUARD_DECLINED"    # a human answered "cancel"
ERR_ABORTED = "ERR_ABORTED"
ERR_TIMEOUT = "ERR_TIMEOUT"
ERR_SHUTTING_DOWN = "ERR_SHUTTING_DOWN"
ERR_FLY_IN_PROGRESS = "ERR_FLY_IN_PROGRESS"  # cannot be interrupted; wait it out
ERR_INTERNAL = "ERR_INTERNAL"

ALL_CODES = frozenset(
    v for k, v in list(globals().items()) if k.startswith("ERR_")
)

# Codes that map to a dedicated client-side exception class.
_SPECIAL = {ERR_DUPLICATE_GUI: DuplicateGuiError}


def from_reply(err: dict) -> ServerError:
    """Build the right exception from an error reply's `error` object."""
    code = err.get("code", ERR_INTERNAL)
    cls = _SPECIAL.get(code, ServerError)
    return cls(code, err.get("message", ""), err.get("data"))
