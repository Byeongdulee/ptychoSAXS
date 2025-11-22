import client_json as client_json
cmd = 'set scanname test detectors 2,5 saxsmode 1 testmode 1'
argv = cmd.split(' ')
client_json.send_command(argv)