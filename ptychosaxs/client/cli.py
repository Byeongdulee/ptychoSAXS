"""python -m ptychosaxs.client.cli -- talk to the server from a shell.

Replaces gui/network/client.py, which sent a UDP datagram into the void and
could not tell you whether it arrived.
"""

import argparse
import json
import sys

from ptychosaxs.client.connection import BeamlineClient
from ptychosaxs.link import errors


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m ptychosaxs.client.cli",
        description="Query the shared ptychoSAXS beamline server.")
    p.add_argument("command",
                   choices=("status", "axes", "positions", "clients",
                            "position", "watch"),
                   help="what to do")
    p.add_argument("axis", nargs="?", help="for `position`")
    p.add_argument("--start", action="store_true",
                   help="start a server if none is running")
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = BeamlineClient(kind="cli", label=f"cli {args.command}")
    try:
        client.connect(start_server=args.start)
    except Exception as exc:
        print(f"could not connect: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "status":
            out = client.system.status()
        elif args.command == "axes":
            out = client.motion.axes()
        elif args.command == "positions":
            out = client.motion.positions()
        elif args.command == "clients":
            out = client.system.clients()
        elif args.command == "position":
            if not args.axis:
                print("position needs an axis", file=sys.stderr)
                return 2
            out = client.motion.position(args.axis)
        elif args.command == "watch":
            return _watch(client)
    except errors.ServerError as exc:
        print(f"server refused: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.command != "watch":
            client.close()

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
    else:
        _render(args.command, out)
    return 0


def _render(command, out):
    if command == "status":
        print(f"pid {out['server_pid']}  up {out['uptime_s']}s  "
              f"{'SIMULATED' if out['simulated'] else 'real hardware'}")
        print(f"state    {'ready' if out['ready'] else out['phase']}")
        groups = ", ".join(f"{g['name']}({g['refs']})"
                           for g in out.get("groups", []) if g["open"])
        print(f"devices  {groups or 'none open'}")
        for c in out.get("clients", []):
            print(f"  {c['label']:<18} {c['kind']:<9} pid {c['pid']}")
    elif command == "axes":
        for a in out:
            flag = "" if a["connected"] else "   (not connected)"
            print(f"  {a['name']:<8} {a['unit']:<5} {a['stage']}{flag}")
    elif command == "clients":
        for c in out:
            print(f"  {c['label']:<18} {c['kind']:<9} pid {c['pid']}")
    elif isinstance(out, dict):
        for k, v in sorted(out.items()):
            print(f"  {k:<8} {v}")
    else:
        print(out)


def _watch(client):
    """Stream events until Ctrl-C. Shows what every connected GUI sees."""
    import threading
    stop = threading.Event()
    client.on("*", lambda name, data: print(f"{name}: {data}", flush=True))
    print("watching events, Ctrl-C to stop", flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
