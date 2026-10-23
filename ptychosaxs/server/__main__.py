"""python -m ptychosaxs.server

Startup order is load-bearing and is spelled out in
ptychosaxs/server/lifecycle.py: take the flock before importing or building
anything, bind inside the lock, and only then build the hardware. Several
GUIs starting at once each spawn one of these; all but one must exit having
touched nothing.
"""

import argparse
import logging
import sys

from ptychosaxs.api.profile import Profile
from ptychosaxs.api.session import BeamlineSession
from ptychosaxs.link import config
from ptychosaxs.server import lifecycle, logging_setup, shutdown
from ptychosaxs.server.daemon import Daemon


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m ptychosaxs.server",
        description="The shared ptychoSAXS beamline server.")
    p.add_argument("--simulate", action="store_true",
                   help="drive simulated stand-ins instead of hardware")
    p.add_argument("--foreground", action="store_true",
                   help="also log to stderr and do not detach")
    p.add_argument("--linger", type=float, default=config.LINGER_S,
                   metavar="SECONDS",
                   help="how long to wait at zero clients before exiting "
                        f"(default {config.LINGER_S})")
    p.add_argument("--hexapod-transport", default=None,
                   choices=("epics", "direct", "simulated"))
    p.add_argument("--status", action="store_true",
                   help="report on the running server and exit")
    p.add_argument("--stop", action="store_true",
                   help="ask the running server to exit")
    p.add_argument("--force", action="store_true",
                   help="with --stop, kill even during a fly scan")
    p.add_argument("--json", action="store_true",
                   help="with --status, emit JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.status or args.stop:
        from ptychosaxs.server import admin
        return (admin.stop(force=args.force) if args.stop
                else admin.status(as_json=args.json))

    shutdown.install()

    # 1. Lock before anything else. A daemon that loses this race must exit
    #    without having imported, let alone opened, a single device.
    try:
        lock = lifecycle.ServerLock(config.lock_path(create_dir=True)).acquire()
    except lifecycle.AlreadyRunning:
        # Expected for every launcher but the first. Give the winner a
        # moment to bind so a racing launcher does not see us vanish before
        # there is a socket to connect to.
        import time
        for _ in range(50):
            if lifecycle.server_is_running():
                break
            time.sleep(0.1)
        return 0
    except PermissionError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1

    log = logging_setup.configure(
        config.log_path(create_dir=True),
        level=logging.DEBUG if args.verbose else logging.INFO,
        foreground=args.foreground)
    logging_setup.capture_prints()

    profile = Profile.from_env(simulated=args.simulate or None)
    if args.hexapod_transport:
        profile = Profile(simulated=profile.simulated,
                          hexapod_transport=args.hexapod_transport,
                          gonio_smaract=profile.gonio_smaract,
                          phi_acs=profile.phi_acs)

    lock.write_info(simulated=profile.simulated)
    log.info("starting: %s", profile.summary())

    # 2. Bind inside the lock, clearing a stale socket first.
    try:
        sock = lifecycle.bind_socket(log=log)
    except Exception:
        log.exception("could not bind the control socket")
        lock.release()
        return 1

    # 3. Only now build anything. The socket already answers system.hello
    #    with ready=false, so a panel can show what it is waiting for.
    session = BeamlineSession(profile=profile, log=log)
    daemon = Daemon(session, sock, lock, log, linger_s=args.linger)

    import threading
    threading.Thread(target=session.start, name="ptys-startup",
                     daemon=True).start()

    daemon.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
