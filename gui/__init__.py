"""Qt views for the 12-ID-C instrument.

Thin layer: widgets, layout and user interaction only. Control logic belongs
in ptychosaxs.api, reached through a ptychosaxs.client connection to the
shared beamline server.

Importing this package must not build a QApplication or touch hardware.
"""
