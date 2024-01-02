import socket
import sys
UDP_IP = "127.0.0.1"
UDP_PORT = 20002
if len(sys.argv)>1:
    cmd = sys.argv[1]
    # run2d, run3d, setrange
    if len(sys.argv)>2:
        L, R, st, t = sys.argv[2:-1]
        data = f"{L}/{R}/{st}/{t}"
    else:
        data = ""
else:
    cmd = "none"
    data = ""
msg = f"{cmd}:{data}"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(bytes(msg, "utf-8"), (UDP_IP, UDP_PORT))