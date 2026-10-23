"""Build the MCP server and its lazy connection to the beamline."""

import logging
import os

from mcp.server import MCPServer

from ptychosaxs.mcp import tools

log = logging.getLogger("ptychosaxs.mcp")

#: By default this joins a server some GUI already started, but will not
#: start one itself. An MCP host launching its configured servers must not
#: be what powers up a beamline.
MAY_START_SERVER = os.environ.get("PTYCHOSAXS_MCP_MAY_START_SERVER") == "1"

_client = None


def get_client():
    """A connected BeamlineClient, opened on first use.

    Lazy on purpose: see the package docstring. With no tools registered
    this is never called, so running this server creates no socket at all.
    """
    global _client
    if _client is None or not _client.connected:
        from ptychosaxs.client.connection import BeamlineClient
        client = BeamlineClient(kind="mcp", label="mcp")
        client.connect(start_server=MAY_START_SERVER)
        _client = client
    return _client


def close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _version():
    try:
        from importlib.metadata import version
        return version("ptychoSAXS")
    except Exception:
        return "0+unknown"


def build_app():
    app = MCPServer(
        name="ptychosaxs",
        version=_version(),
        instructions=(
            "Control and monitoring for the 12-ID-C ptycho-SAXS instrument. "
            "This server exposes no tools yet."
        ),
    )
    names = tools.register_all(app, get_client)
    log.info("mcp server built with %d tool(s)", len(names))
    return app


def main(argv=None) -> int:
    # Logging to stderr: MCP hosts surface that as the server's log, and
    # stdout belongs to the protocol. stdio_server() additionally repoints
    # fd 1 at stderr while it is serving, so a stray print from a handler
    # cannot corrupt the stream.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = build_app()
    try:
        app.run(transport="stdio")
    finally:
        close_client()
    return 0
