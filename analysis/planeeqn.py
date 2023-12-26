import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
import sys

R_cyl = 50 # radius of the cylinderical mirror in mm
R_sensor = 45 # horizontal position of the vertical sensor in mm
th_sensor = 72 # angle between the vertical sensor to the cradle X, degree
th_sensor = np.deg2rad(th_sensor)
POSITION_UNIT_NM = 0
POSITION_UNIT_UM = 1
POSITION_UNIT_MM = 2
POSITION_UNIT = POSITION_UNIT_NM
DATA_COLUMN = 1

def CylinderTopPlane(th, a, b, off_set, th0, R0):
    # th in radian
    return off_set - (a*R0*np.sin(th + th0) + b*R0*np.cos(th + th0))

def CylinderSidePlane(th, r, th0, off_set, Rc):
    # th in radian
    # r and th0 are the position of the cylinder center with respect to the rotation axis in polar coordinates
    xp = np.sqrt(Rc**2-(r*np.sin(th + th0))**2) + r*np.cos(th + th0)
    d = Rc-xp
    return off_set + d

def pol2cart(r, th):
    xc = r*np.cos(th)
    yc = r*np.sin(th)
    return xc, yc

def fit_wobble(xdata, ydata):
    # input xdata should be in degree
    # input ydata should be in mm unit
    
    offmi = -0.5
    offma = 0.5
    ami = -0.01
    ama = 0.01
    bmi = -0.01
    bma = 0.01

    popt, pcov = curve_fit(CylinderTopPlane, xdata, ydata, bounds=([ami,bmi,offmi, th_sensor-np.pi, R_sensor-2], 
                                                            [ama, bma, offma, th_sensor+np.pi, R_sensor+2]))
    return popt, pcov

def fit_eccentricity(xdata, ydata):
    # input xdata should be in degree
    # input ydata should be in mm unit
    
    offmi = -0.5
    offma = 0.5
    rmi = -0.1
    rma = 0.1
    th0mi = -np.pi
    th0ma = np.pi

    popt, pcov = curve_fit(CylinderSidePlane, xdata, ydata, bounds=([rmi,th0mi,offmi, R_cyl-0.01], 
                                                            [rma, th0ma, offma, R_cyl+0.01]))
    return popt, pcov

def get_wobble_fitcurve(x, popt):
    print(f"Tilt U to {-np.rad2deg(popt[1])} and V to {-np.rad2deg(popt[0])} degrees.")
    lbl = 'fit: a=%0.3e, b=%0.3e, \noff_set=%0.1f um, th0=%1.1f deg,\n R=%5.1fmm' % (popt[0],popt[1],
                                                                                     popt[2]*1000,popt[3]*180/np.pi,
                                                                                     popt[4])
    return CylinderTopPlane(x, *popt), lbl

def get_eccen_fitcurve(x, popt):
    xc, yc = pol2cart(popt[0], popt[1])
    print(f"xc is {xc}mm and yc is {yc} mm.")
    lbl = 'fit: xc=%0.2f um, yc=%0.2f um, \nr=%0.1f um, th=%0.1f deg, \noff_set=%0.1f um, Rc=%5.1fmm' % (xc*1000, yc*1000, 
                                                                      popt[0]*1000,popt[1]*180/np.pi,
                                                                      popt[2]*1000, popt[3])
    return CylinderSidePlane(x, *popt), lbl

def plot(xd, yd, curve, lbl):
    # dt should be two colum data [phi, pos]
    # where phi is the phi angle in radian and pos is the QDS position in mm.
    plt.figure()
    plt.plot(xd, yd, 'b', label='data')
    plt.plot(xd, curve, 'g--', label=lbl)
    plt.xlabel('phi (radian)')
    if 'xc=' in lbl:
        plt.ylabel('x-x_mean')
    else:
        plt.ylabel('y-y_mean')
    plt.legend()
    plt.show()

def loadata(filename="", datacolumn=1, xdata=[], ydata=[]):
    if len(filename) >0:
        dt = np.loadtxt(filename)
        xd = dt[:,0]
        yd = dt[:,datacolumn]
    else:
        xd = xdata
        yd = ydata
    # conditioning data
    xd = np.deg2rad(xd)
    if POSITION_UNIT==POSITION_UNIT_NM:
        yd = yd/1E6 # converting nm into mm unit
    if POSITION_UNIT==POSITION_UNIT_UM:
        yd = yd/1E3 # converting nm into mm unit
    off_set = np.mean(yd)
    yd = yd - off_set
    return xd, yd

if __name__ == "__main__":
    xd, yd = loadata(sys.argv[1])

    # fit data.
    popt, pconv = fit_wobble(xd, yd)
    plt.plot(xd, yd, 'b', label='data')
    plt.plot(xd, CylinderTopPlane(xd, *popt), 'g--',
         label='fit: a=%0.3e, b=%0.3e, off_set=%0.3e mm, th0=%1.3f rad, R=%5.3fmm' % tuple(popt))
    print(f"Tilt U to {-np.rad2deg(popt[1])} and V to {-np.rad2deg(popt[0])} degrees.") 
    plt.xlabel('phi (radian)')
    plt.ylabel('y-y_mean')
    plt.legend()
    plt.show()
    sys.exit()