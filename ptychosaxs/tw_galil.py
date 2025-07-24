import gclib
import sys
from threading import Lock
lock = Lock()
IP = '10.54.122.161'
g = gclib.py()
g.GOpen(IP)
#c = g.GCommand
g.GCommand('LD 3,3,3,3,3,3,3,3')
motornames = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
motorunits = ['step', 'step', 'step', 'step', 'step', 'step', 'step', 'step']
#g.GClose()

def turn_on():
    #g.GOpen(IP)
    g.GCommand('SHABCDEFGH;')
    #g.GClose()

def wait_move(channel='A'):
    g.GMotionComplete(channel)
    return True

def stop(channel="A"):
    pass

def get_pos(channel=''):
    #g.GOpen(IP)
    pos = {}
    with lock:
        v = g.GCommand('RP')
    #g.GClose()
    plist = v.split(',')
    i = 0
    for key in motornames:
        pos[key] = int(plist[i])
        i = i + 1
    if len(channel) == 0:
        return pos
    else:
        return pos[channel]

def mv(channel='A', value=0, wait=True):
    #g.GOpen(IP)
    with lock:
        g.GCommand('PA%s=%i'% (channel, value))
        g.GCommand('BG%s'%channel)
        if wait:
            wait_move()
    #g.GClose()

def mvr(channel='A', value=0, wait=True):
    #g.GOpen(IP)
    with lock:
        g.GCommand('PR%s=%i'% (channel, value))
        g.GCommand('BG%s'%channel)
        if wait:
            wait_move()
    #g.GClose()

def set_pos(channel='A', value=0):
    #g.GOpen(IP)
    with lock:
        g.GCommand('DP%s=%i'% (channel, value))
    #g.GClose()
        
def move(channel = 'A'):
    #g.GOpen(IP)
    with lock:
        g.GCommand('BG%s'%channel)
        g.GMotionComplete(channel)
        print(g.GCommand('RP'))
    #g.GClose()

def step(channel = 'A', step=0):
    #g.GOpen(IP)
    with lock:
        cmd = 'PR%s=%i'%(channel, step)
        g.GCommand(cmd)
    #g.GClose()

def tweak(channel='A', sp=1000):
    prev_dir = "+"
    prg_step = int("%s%i"%(prev_dir, sp))
    step(channel, prg_step)
    while True:
        direction = input("Direction: ")
        if len(direction)==0:
            direction = prev_dir
        if direction == 's':
            break
        if direction == prev_dir:
            pass
        else:
            prev_dir = direction
            prg_step = int("%s%i"%(prev_dir, sp))
            step(channel, prg_step)
        move(channel)

# Using the special variable 
# __name__
if __name__=="__main__":
    tweak(sys.argv[1], int(sys.argv[2]))

