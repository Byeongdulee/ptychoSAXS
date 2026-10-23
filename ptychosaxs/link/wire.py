"""The one and only definition of what a message looks like on the link.

Newline-delimited JSON over a stream socket: one JSON object per line, UTF-8.

A stream socket, not datagrams. The protocol this replaces was UDP, which is
unreliable, unordered, caps a message at one datagram and has no reply path at
all -- gui/network/server.py captured its transport in connection_made and
then never sent anything back, so a client could not learn whether its command
had even arrived. AF_UNIX SOCK_STREAM gives the same delivery guarantees as
TCP with none of its costs here: no port to allocate, nothing reachable from
the network, file permissions for access control, and SO_PEERCRED so the
server can trust which process it is talking to.

Frame shapes, all carrying `v` (protocol version) and `t` (type):

    req  {"v":1,"t":"req","id":"c3-17","method":"motion.mvr","params":{...}}
    rep  {"v":1,"t":"rep","id":"c3-17","ok":true,"result":{...}}
         {"v":1,"t":"rep","id":"c3-17","ok":false,"error":{"code","message","data"}}
    evt  {"v":1,"t":"evt","seq":4412,"ts":...,"event":"motion.positions","data":{...}}
    ask  {"v":1,"t":"ask","id":"p-9","job":"job-42","kind":"confirm", ...}
    ans  {"v":1,"t":"ans","id":"p-9","choice":"Proceed"}

`seq` is monotonic per server, so a client that sees a gap knows its own
outbound queue dropped events rather than silently believing it saw them all.
"""

import json
import socket
import time

from ptychosaxs.link import config
from ptychosaxs.link.errors import ProtocolError

T_REQ = "req"
T_REP = "rep"
T_EVT = "evt"
T_ASK = "ask"
T_ANS = "ans"
FRAME_TYPES = frozenset({T_REQ, T_REP, T_EVT, T_ASK, T_ANS})


def encode(obj: dict) -> bytes:
    """Serialise one frame, newline-terminated.

    ensure_ascii keeps every byte in the ASCII range, so a literal newline can
    never appear inside a string and the framing cannot be broken by content.
    """
    line = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    if "\n" in line:                      # belt and braces; json escapes these
        raise ProtocolError("encoded frame contains a newline")
    data = (line + "\n").encode("ascii")
    if len(data) > config.MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame is {len(data)} bytes, over the "
            f"{config.MAX_FRAME_BYTES} limit"
        )
    return data


def decode(line: bytes) -> dict:
    """Parse one frame. Raises ProtocolError on anything unusable."""
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"bad JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"frame is {type(obj).__name__}, expected object")
    t = obj.get("t")
    if t not in FRAME_TYPES:
        raise ProtocolError(f"unknown frame type {t!r}")
    return obj


# --- constructors -----------------------------------------------------------

def request(req_id: str, method: str, params: dict | None = None) -> dict:
    return {"v": config.PROTOCOL_VERSION, "t": T_REQ, "id": req_id,
            "method": method, "params": params or {}}


def ok(req_id: str, result=None) -> dict:
    return {"v": config.PROTOCOL_VERSION, "t": T_REP, "id": req_id,
            "ok": True, "result": result if result is not None else {}}


def error(req_id, code: str, message: str, data: dict | None = None) -> dict:
    return {"v": config.PROTOCOL_VERSION, "t": T_REP, "id": req_id,
            "ok": False,
            "error": {"code": code, "message": message, "data": data or {}}}


def event(seq: int, name: str, data: dict | None = None) -> dict:
    return {"v": config.PROTOCOL_VERSION, "t": T_EVT, "seq": seq,
            "ts": time.time(), "event": name, "data": data or {}}


def ask(ask_id: str, kind: str, title: str, text: str, choices,
        default: str, job: str | None = None,
        timeout_s: float | None = None) -> dict:
    return {"v": config.PROTOCOL_VERSION, "t": T_ASK, "id": ask_id,
            "job": job, "kind": kind, "title": title, "text": text,
            "choices": list(choices), "default": default,
            "timeout_s": timeout_s}


def answer(ask_id: str, choice: str) -> dict:
    return {"v": config.PROTOCOL_VERSION, "t": T_ANS, "id": ask_id,
            "choice": choice}


# --- reading ----------------------------------------------------------------

class FrameReader:
    """Turn a stream socket into a sequence of frames.

    Iteration ends cleanly when the peer closes. A partial frame left in the
    buffer at EOF is an error: it means the peer died mid-write, and silently
    discarding it would turn a truncated command into a no-op.
    """

    def __init__(self, sock: socket.socket, max_bytes: int | None = None):
        self._sock = sock
        self._buf = bytearray()
        self._max = max_bytes or config.MAX_FRAME_BYTES

    def __iter__(self):
        while True:
            while b"\n" not in self._buf:
                try:
                    chunk = self._sock.recv(65536)
                except (ConnectionResetError, OSError):
                    chunk = b""
                if not chunk:
                    if self._buf:
                        raise ProtocolError(
                            f"peer closed mid-frame, {len(self._buf)} bytes "
                            f"of an unterminated frame discarded"
                        )
                    return
                self._buf.extend(chunk)
                if len(self._buf) > self._max:
                    raise ProtocolError(
                        f"no newline in {len(self._buf)} bytes; a peer that "
                        f"never terminates a frame must not grow this buffer"
                    )
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            yield decode(bytes(line))


def send(sock: socket.socket, obj: dict) -> None:
    """Write one frame. sendall, so a short write cannot split a frame."""
    sock.sendall(encode(obj))
