"""Being the only server, and deciding when to stop being one.

The ordering in `claim` is the whole correctness argument, so it is spelled
out rather than left to the reader:

1. Take the flock *first*, before importing or building anything. Several
   GUIs starting together each spawn a daemon; the losers must exit having
   touched no hardware. The PI C-887 accepts exactly one TCP connection, so
   three daemons racing to open it is not a tidiness problem, it is two
   failed stages and a controller held by whichever process happens to win.
2. Unlink a stale socket and bind the new one *inside* the lock. If the
   unlink were outside it, two daemons could both unlink and both bind: the
   second one's inode wins, clients connect to it, and the first keeps
   running invisibly while holding the hexapod.
3. Never unlink the lockfile. Unlinking a file another process holds an
   flock on lets the next starter create a fresh inode and take "the lock"
   while the first still holds it -- the classic flock-unlink race.
"""

import errno
import fcntl
import json
import os
import socket
import time

from ptychosaxs.link import config


class AlreadyRunning(Exception):
    """Another daemon holds the lock. Not an error: the caller exits 0."""


class ServerLock:
    """An exclusive, lifetime-held flock identifying the one live daemon."""

    def __init__(self, path=None):
        self.path = path or config.lock_path(create_dir=True)
        self._fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(
                    f"another ptychoSAXS server holds {self.path}") from exc
            raise
        self._fd = fd
        return self

    def write_info(self, **extra):
        """Record who we are, for `ptychosaxs-server --status`."""
        if self._fd is None:
            return
        info = {"pid": os.getpid(), "started_at": time.time(), **extra}
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, json.dumps(info).encode())
        os.fsync(self._fd)

    def release(self):
        """Drop the lock. The file itself stays; see the module docstring."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def read_lock_info(path=None):
    """What the running daemon wrote, or None. Never raises."""
    path = path or config.lock_path()
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        return json.loads(raw) if raw.strip() else None
    except (OSError, ValueError):
        return None


def server_is_running(path=None) -> bool:
    """True if something is accepting on the control socket."""
    path = path or config.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(path)
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False


def bind_socket(path=None, log=None, backlog=64):
    """Bind the control socket, clearing a stale one first.

    Only safe to call while holding the ServerLock.
    """
    path = path or config.socket_path(create_dir=True)

    if os.path.lexists(path):
        if server_is_running(path):
            # We hold the lock, so nobody should be serving here. Something
            # started a daemon around the lockfile, and two processes are
            # about to fight over a controller that allows one client.
            raise RuntimeError(
                f"{path} is already being served although we hold the lock; "
                f"refusing to start a second daemon")
        if log:
            log.info("removing stale socket %s", path)
        os.unlink(path)

    old_umask = os.umask(0o177)          # -> socket lands 0600
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
    finally:
        os.umask(old_umask)
    sock.listen(backlog)
    return sock


class ExitPolicy:
    """When the last client has gone, may we stop?

    Three rules, in order:

    * A fly scan is never interrupted. Its trajectory runs in the C-887's
      wave generator and there is no host-side flag to stop it, so the
      server stays up until it finishes. A GUI trying to close during one is
      told to wait.
    * A step scan is asked to stop, cooperatively, and we wait for it to
      finish the step it is on and run its normal cleanup -- shutter closed,
      detectors disarmed, data written.
    * Otherwise, exit after a short grace period, so closing one panel and
      opening another does not rebuild every hardware connection.
    """

    def __init__(self, registry, jobs, log, linger_s=None):
        self._registry = registry
        self._jobs = jobs
        self._log = log
        self.linger_s = config.LINGER_S if linger_s is None else linger_s
        self._idle_since = None
        self._stop_requested = False
        self._warned_orphan = False
        self._asked_stop = False

    def request_shutdown(self):
        self._stop_requested = True

    @property
    def shutdown_requested(self):
        return self._stop_requested

    def blocking_reason(self):
        """Why a shutdown request cannot be honoured yet, or None."""
        if self._jobs.fly_active():
            return ("a fly scan is in progress and cannot be interrupted; "
                    "please wait for it to finish")
        return None

    def should_exit(self) -> bool:
        clients = self._registry.live_count()

        if clients > 0 and not self._stop_requested:
            self._idle_since = None
            self._asked_stop = False
            self._warned_orphan = False
            return False

        if self._jobs.fly_active():
            if not self._warned_orphan:
                self._log.warning(
                    "no clients left but %s is running; staying up, a fly "
                    "scan cannot be interrupted",
                    self._jobs.describe_active())
                self._warned_orphan = True
            return False

        if self._jobs.any_active():
            if not self._asked_stop:
                asked = self._jobs.request_stop()
                self._log.warning(
                    "no clients left; asked %s to stop",
                    ", ".join(asked) or "nothing cancellable")
                self._asked_stop = True
            return False

        if self._idle_since is None:
            self._idle_since = time.monotonic()
        return (time.monotonic() - self._idle_since) >= self.linger_s
