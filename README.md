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