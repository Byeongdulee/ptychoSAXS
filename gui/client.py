import socket
import sys
UDP_IP = "sec12pc02.xray.aps.anl.gov"
UDP_PORT = 20002

# example: 
#   mv X 1
#   mv X 1 Y 2
#   setrange X -1 1 0.01 2
#   run2d
#   run3d

if len(sys.argv)>1:
    cmd = sys.argv[1]
    # run2d, run3d, setrange
    if len(sys.argv)>2:
        if cmd=="setrange":
            if len(sys.argv)==7:
                axis, L, R, st, t = sys.argv[2:7]
                data = f"{axis}/{L}/{R}/{st}/{t}"
        if cmd=="mv":
            data=""
            for i in range(int((len(sys.argv)-2)/2)):
                axis = sys.argv[2*i+2]
                pos = sys.argv[2*i+3]
                data = f"{data}{axis}/{pos};"
    else:
        data = ""
else:
    cmd = "none"
    data = ""
msg = f"{cmd}:{data}"
print(msg)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(bytes(msg, "utf-8"), (UDP_IP, UDP_PORT))