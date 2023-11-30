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
r, t = pts.fly_test(2)
pts.plotdata(t, r, col=0)
```