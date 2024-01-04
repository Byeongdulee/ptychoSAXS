import socket
import sys
import json
UDP_IP = "127.0.0.1"
UDP_PORT = 20002

# Input Format: command [key value]
# example: 
#   mv X 1
#   mv X 1 Y 2
#   mvr X 1
#   setrange axis X L -1 R 1 N 0.01 t 2
#   setrange axis Y L -1 R 1 N 0.01 t 2
#   run2d
#   run2d xmotor 0 
#   run2d [xmotor 0 ymotor 1 scanname test]
#   run3d [xmotor 0 ymotor 1 phimotor 6 scanname test]
#   setfolder folder c:\data
#   toggle controllerfly on
#   toggle keepprevscan off
#   toggle reversescan on
d = {}
d['command']=sys.argv[1]
data = {}
for i in range(int((len(sys.argv)-2)/2)):
    data[sys.argv[2*i+2]] = sys.argv[2*i+3]
d['data'] = data
msg = json.dumps(d)
print(msg)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(bytes(msg, 'utf-8'), (UDP_IP, UDP_PORT))