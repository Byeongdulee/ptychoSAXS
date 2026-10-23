"""Talking to the shared beamline server.

A GUI imports this and nothing else from the control side. The only module
here that imports Qt is ptychosaxs.client.qt, so a script, a test or the MCP
server can drive the instrument with no Qt installed at all.
"""

from ptychosaxs.client.connection import BeamlineClient  # noqa: F401
