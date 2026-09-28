#from .motions import motors
# Not imported eagerly: instruments (in .ptychosaxs) pulls in pihexapod/acspy,
# which aren't needed by lightweight submodules like .optics. Code that needs
# it imports directly: `from ptychosaxs.ptychosaxs import instruments`.