"""The MCP server speaks stdio, exposes no tools, and touches nothing.

The last part is the one worth a test. An MCP host starts every configured
server when it launches and leaves it running, so if this connected eagerly,
opening an editor would power up the beamline.
"""

import json
import os
import select
import subprocess
import sys

import pytest

pytestmark = pytest.mark.timeout(90)


class McpProc:
    def __init__(self, proc):
        self.proc = proc
        self._next_id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._next_id += 1
            msg["id"] = self._next_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return None if notify else self._next_id

    def recv(self, timeout=20):
        import time
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if r:
                line = self.proc.stdout.readline()
                if not line:
                    raise AssertionError("mcp server closed stdout")
                if line.strip():
                    return json.loads(line)
        raise AssertionError("no reply from the mcp server")

    def call(self, method, params=None):
        self.send(method, params)
        return self.recv()


@pytest.fixture
def mcp(tmp_path):
    env = {**os.environ, "PTYCHOSAXS_RUNTIME_DIR": str(tmp_path)}
    env.pop("PTYCHOSAXS_MCP_MAY_START_SERVER", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ptychosaxs.mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env)
    wrapper = McpProc(proc)
    reply = wrapper.call("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"}})
    wrapper.initialize_reply = reply
    wrapper.send("notifications/initialized", notify=True)
    wrapper.runtime_dir = tmp_path
    yield wrapper
    try:
        proc.stdin.close()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def test_it_initializes(mcp):
    result = mcp.initialize_reply["result"]
    assert result["serverInfo"]["name"] == "ptychosaxs"
    assert result["serverInfo"]["version"], "version must not be blank"
    assert "tools" in result["capabilities"]


def test_it_exposes_no_tools_yet(mcp):
    assert mcp.call("tools/list", {})["result"]["tools"] == []


def test_calling_an_unknown_tool_is_an_error_not_a_crash(mcp):
    reply = mcp.call("tools/call", {"name": "nope", "arguments": {}})
    assert "error" in reply or reply["result"].get("isError") is True


def test_it_answers_ping(mcp):
    assert "result" in mcp.call("ping", {})


def test_a_notification_is_not_answered(mcp):
    """Replying to a notification is the classic hand-rolled-JSON-RPC bug."""
    mcp.send("notifications/initialized", notify=True)
    reply = mcp.call("ping", {})
    assert reply["id"] == mcp._next_id


def test_it_opens_no_connection_to_the_beamline(mcp):
    """With zero tools nothing should ever call get_client()."""
    mcp.call("tools/list", {})
    mcp.call("ping", {})
    assert not (mcp.runtime_dir / "ctl.sock").exists(), (
        "the MCP server contacted the beamline server before any tool was "
        "called")


def test_it_writes_nothing_but_protocol_to_stdout(mcp):
    """One stray print would desynchronise the host's parser."""
    for _ in range(3):
        reply = mcp.call("tools/list", {})
        assert reply.get("jsonrpc") == "2.0"


def test_the_mutating_gate_is_closed_by_default():
    from ptychosaxs.mcp import tools
    assert tools.MUTATING_ALLOWED is False
    with pytest.raises(tools.MutatingNotAllowed):
        tools.require_mutating("move the hexapod")
