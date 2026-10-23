"""Constants and paths shared by both ends of the link.

Plain data and path helpers. Imports nothing beyond the standard library, so
either end can read it without pulling in Qt, pyepics or a vendor SDK.
"""

import os

# --- protocol ---------------------------------------------------------------

# Bumped only when a message is removed or changes meaning. New methods, new
# events and new fields in an existing result are additive and do not bump it.
PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1

# A peer that never sends a newline must not be able to grow our read buffer
# without bound. The failure this guards against is a wedged writer, not an
# attacker -- both ends are the same user on the same machine.
MAX_FRAME_BYTES = 8 << 20

# --- server lifetime --------------------------------------------------------

# How long the server waits at zero clients before exiting. Short, because
# nobody runs scans across a logout here, but not zero: closing one panel and
# opening another should not tear down and rebuild every hardware connection.
LINGER_S = 0.1

HEARTBEAT_S = 5.0          # server -> client ping interval
HEARTBEAT_MISSES = 3       # consecutive misses before a client is declared dead
SUPERVISION_TICK_S = 0.5   # how often the daemon re-checks liveness and exit

PUBLISH_HZ = 10.0          # conflated position/status broadcast rate
OUT_QUEUE_MAX = 1000       # per-connection outbound queue depth

# --- filesystem -------------------------------------------------------------

ENV_RUNTIME_DIR = "PTYCHOSAXS_RUNTIME_DIR"

SOCKET_NAME = "ctl.sock"
LOCK_NAME = "server.lock"
LOG_NAME = "server.log"

DIR_MODE = 0o700
SOCKET_MODE = 0o600


def runtime_dir(create: bool = False) -> str:
    """Directory holding the socket, the lockfile and the log.

    XDG_RUNTIME_DIR first: it is already 0700, per-user, on a tmpfs, and it is
    destroyed when the user's last session ends -- the same lifetime the server
    has, since the last GUI closing shuts it down. /tmp is the fallback for a
    context with no login session. We create it 0700 and check its owner,
    because /tmp is world-writable and a directory we did not create is one
    somebody else could be reading the socket through.
    """
    override = os.environ.get(ENV_RUNTIME_DIR)
    if override:
        path = override
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        path = (os.path.join(xdg, "ptychosaxs") if xdg
                else f"/tmp/ptychosaxs-{os.getuid()}")

    if create:
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
        st = os.stat(path)
        if st.st_uid != os.getuid():
            raise PermissionError(
                f"{path} is owned by uid {st.st_uid}, not {os.getuid()}; "
                f"refusing to put a control socket there"
            )
        # makedirs' mode is subject to umask, and an existing directory keeps
        # whatever mode it had.
        os.chmod(path, DIR_MODE)
    return path


def socket_path(create_dir: bool = False) -> str:
    return os.path.join(runtime_dir(create_dir), SOCKET_NAME)


def lock_path(create_dir: bool = False) -> str:
    return os.path.join(runtime_dir(create_dir), LOCK_NAME)


def log_path(create_dir: bool = False) -> str:
    return os.path.join(runtime_dir(create_dir), LOG_NAME)
