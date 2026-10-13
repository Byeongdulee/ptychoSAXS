import os
import sys
sys.path.append('..')
import gui.client_json as client_json
import time

# Same file the GUI uses, addressed absolutely so this script can be run from
# any directory (gui/rungui.py builds the identical path from ini_utils.INI_DIR).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(_REPO_ROOT, "gui"))
from beamline_ini import SafeIni

inifilename = os.path.join(_REPO_ROOT, "gui", "ini", "pty-co-saxs.ini")
# SafeIni, not the bare format reader: the GUI writes this file through the
# percent-encoding layer, so reading it without one returns encoded text.
parameters = SafeIni(inifilename)
macrofilename = 'examplescan.txt'
iscommandsent = False

while 1:
    #check every 5 seconds for status
    parameters.readini()
    if parameters.scan_time == -1:
        if len(macrofilename) > 0:

            # comment the first non-empty, non-comment line in the macro file
            with open(macrofilename, 'r', encoding='utf-8') as f:
                file_lines = f.readlines()

            for i, l in enumerate(file_lines):
                if l.strip() == '':
                    continue
                if l.lstrip().startswith('#'):
                    continue

                argv = l.split(' ')
                client_json.send_command(argv)
                iscommandsent = True
                file_lines[i] = '# ' + l
                break

            with open(macrofilename, 'w', encoding='utf-8') as f:
                f.writelines(file_lines)
    time.sleep(10)