"""The control surface for the 12-ID-C instrument.

Every action a button can trigger lives here, as a plain Python call. No Qt,
no sockets, no JSON: this layer is what the server exposes and what a test
drives directly.

The split this package exists to make is the one that was already implicit.
gui.rungui.ptyco_main_control was the de facto service locator -- the 894
`self.w.*` references in gui/handlers/scan_handler.py resolve overwhelmingly
to parameters, detectors, messages, motors and a dozen flags, none of which
are widgets. BeamlineSession owns those on purpose; the ~180 genuine widget
reads become explicit arguments instead.
"""
