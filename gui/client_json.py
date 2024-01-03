import socket
import sys
import json
UDP_IP = "127.0.0.1"
UDP_PORT = 20002

# example: 
#   mv X 1
#   mv X 1 Y 2
#   setrange X -1 1 0.01 2
#   run2d
#   run3d
d = {}
d['command']=sys.argv[1]
data = {}
for i in range(int((len(sys.argv)-2)/2)):
    data[sys.argv[2*i+2]] = sys.argv[2*i+3]
d['data'] = data
msg = json.dumps(d)
print(msg)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(msg, (UDP_IP, UDP_PORT))