import socket
import sys
import json
#UDP_IP = "127.0.0.1"
UDP_IP = "10.54.122.103"
UDP_PORT = 20002
# python client_json.py command [key value]
# Input Format: command [key value]
# example: 
#   mv X 1
#   mv X 1 Y 2
#   mvr X 1
#   setrange axis X L -1 R 1 N 0.01 t 2
#   setrange axis Y L -1 R 1 N 0.01 t 2
#   fly2d
#   fly2d xmotor 0 
#   fly2d xmotor 0 ymotor 1 scanname test
#   fly2d_snake xmotor 0 ymotor 1 scanname test
#   fly3d xmotor 0 ymotor 1 phimotor 6 scanname test
#   fly3d_snake xmotor 0 ymotor 1 phimotor 6 scanname test
#   stepscan3d xmotor 0 ymotor 1 phimotor 6 scanname test
#   stepscan2d scanname test
#   setfolder folder c:\data
#   toggle controllerfly on
#   toggle keepprevscan off
#   toggle reversescan on
def send_command(argv):
    d = {}
    d['command']=argv[0]
    data = {}
    for i in range(int((len(argv)-1)/2)):
        data[argv[2*i+1]] = argv[2*i+2]
    d['data'] = data
    msg = json.dumps(d)
    print(msg)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(bytes(msg, 'utf-8'), (UDP_IP, UDP_PORT))
    
if __name__ == "__main__":
    send_command(sys.argv[1:])