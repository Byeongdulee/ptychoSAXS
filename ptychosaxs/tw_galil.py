import gclib
import sys
from threading import Lock
lock = Lock()
g = gclib.py()
g.GOpen('164.54.122.27')
c = g.GCommand
c('LD 3,3,3,3,3,3,3,3')
motornames = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
motorunits = ['step', 'step', 'step', 'step', 'step', 'step', 'step', 'step']

def wait_move(channel='A'):
    g.GMotionComplete(channel)
    return True

def get_pos(channel=''):
    pos = {}
    with lock:
        v = c('RP')
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
    with lock:
        c('PA%s=%i'% (channel, value))
        c('BG%s'%channel)
        if wait:
            wait_move()

def mvr(channel='A', value=0, wait=True):
    with lock:
        c('PR%s=%i'% (channel, value))
        c('BG%s'%channel)
        if wait:
            wait_move()

def set_pos(channel='A', value=0):
    with lock:
        c('DP%s=%i'% (channel, value))
        
def move(channel = 'A'):
    with lock:
        c('BG%s'%channel)
        g.GMotionComplete(channel)
        print(c('RP'))

def step(channel = 'A', step=0):
    with lock:
        cmd = 'PR%s=%i'%(channel, step)
        c(cmd)

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

