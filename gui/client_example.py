import client_json as client_json
cmd = 'set scanname coal_8_ detectors 1,2,3,6 saxsmode 1 testmode 0'
argv = cmd.split(' ')
client_json.send_command(argv)