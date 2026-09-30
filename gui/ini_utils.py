"""Shared helpers for self-initialising .ini config files.

The GUI .ini files are per-installation state (saved motor positions, window
geometry, last-used paths), so they live in gui/ini/ and are not tracked in
git. Each GUI therefore has to be able to recreate its own .ini from defaults
on first start, and to back-fill any entry a newer version of the GUI has
added to an older file.
"""

import configparser
import os

# Directory every GUI .ini lives in, created on demand by ensure_ini_defaults.
INI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ini")


def ensure_ini_defaults(path, defaults):
    """Create `path` from `defaults` if missing, and add any absent entry.

    `defaults` is {section: {key: value}}. Values already present in the file
    are never overwritten - only missing sections and missing keys are added,
    so a user's saved settings survive a GUI upgrade that introduces new ones.

    Returns True if the file was created or modified.
    """
    cfg = configparser.ConfigParser()
    cfg.read(path)
    changed = not os.path.exists(path)

    for section, entries in defaults.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
            changed = True
        for key, value in entries.items():
            if not cfg.has_option(section, key):
                cfg.set(section, key, str(value))
                changed = True

    if changed:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            cfg.write(f)
    return changed
