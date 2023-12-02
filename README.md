# ptychosaxs
The collection of 12-ID-C ptychoSAXS classes

## Install
After cloning the package, run setup.py.
```
python setup.py install
```

## Usage
```python
import ptychosaxs as pts
pts.hexapod.get_pos()
pts.phi.rpos
import time
for i in range(10):
    pts.qds.get_position()
    time.sleep(0.001)
```
To disconnect
```python
pts.disconnect()
```
To reconnect
```python
pts.connect(pts)
```
Test the flyscan
```python
pts.hexapod.set_traj(5, 0.01, -0.005, 50, 0.01) # total scantime: 5s, scan range: 0.01mm, scan start position: -0.005mm, points to wait until it reaches linear motion, trigger period: 0.01 s.
pts.fly_test() # this will save qds data into rpos and tpos variables.
pts.plot_qds_hex(timeshift= -180, filename="test2")
pts.plot_qds_hex(timeshift= -180) # this will plot data only
```