"""The shared beamline server.

Deliberately empty. A client does `from ptychosaxs.server import lifecycle`
to find the socket path, and importing this package must not drag the daemon,
its threads or the device layer into the client's address space.
"""
