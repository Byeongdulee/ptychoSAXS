import py12inifunc
import gui.client_json as client_json
import time

inifilename = "pty-co-saxs.ini"
parameters = py12inifunc.ini(inifilename)
macrofilename = 'examplescan.txt'
iscommandsent = False

while 1:
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
    time.sleep(5)