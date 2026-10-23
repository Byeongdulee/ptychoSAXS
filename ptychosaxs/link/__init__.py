"""The link between a ptychoSAXS client and the beamline server.

Everything here is shared by both ends and by the MCP process, and it imports
nothing from ptychosaxs.api, ptychosaxs.server or ptychosaxs.client -- the
client must be able to speak the protocol without dragging the daemon into its
address space.

Named "link" rather than "transport" because ptychosaxs/transport.py already
means something else in this codebase: which *driver* a motion stage is built
on (EPICS, direct, simulated). Two meanings of one word in one package would
be a lasting source of confusion.

Same house style as qds_bridge/: plain constants in config.py, one module that
owns the on-the-wire format, and documented failure modes rather than bare
excepts.
"""
