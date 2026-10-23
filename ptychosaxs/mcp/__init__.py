"""An MCP server exposing the instrument, over stdio.

It has no tools yet, on purpose -- this is the place for them, wired up and
tested, so adding the first one is an edit to ptychosaxs/mcp/tools.py rather
than a design exercise.

It is a client of the shared beamline server like any GUI, and it connects
lazily: an MCP host starts every configured server when it launches and
leaves it running, so connecting at import would mean opening an editor
powers up the beamline. With zero tools nothing ever connects.
"""
