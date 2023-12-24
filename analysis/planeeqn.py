import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
import sys

R = 45 # mm
th_sensor = 72 # degree
th_sensor = np.deg2rad(th_sensor)

def tiltplane(th, a, b, off_set, th0, R0):
    # th in radian
    return off_set - (a*R0*np.sin(th + th0) + b*R0*np.cos(th + th0))

def fit(xdata, ydata):
    # input xdata should be in degree
    # input ydata should be in mm unit
    
    offmi = -0.5
    offma = 0.5
    ami = -0.01
    ama = 0.01
    bmi = -0.01
    bma = 0.01

    popt, pcov = curve_fit(tiltplane, xdata, ydata, bounds=([ami,bmi,offmi, th_sensor-3, R-2], [ama, bma, offma, th_sensor+3, R+2]))
    return popt, pcov

if __name__ == "__main__":
    dt = np.loadtxt(sys.argv[1])
    # conditioning data
    dt[:,0] = np.deg2rad(dt[:,0])
    dt[:,1] = dt[:,1]/1000000 # converting um into mm unit
    off_set = np.mean(dt[:,1])
    dt[:,1] = dt[:,1] - off_set

    # fit data.
    popt, pconv = fit(dt[:,0], dt[:,1])
    plt.plot(dt[:,0], dt[:,1], 'b', label='data')
    plt.plot(dt[:,0], tiltplane(dt[:,0], *popt), 'g--',
         label='fit: a=%0.3e, b=%0.3e, off_set=%0.3e mm, th0=%1.3f rad, R=%5.3fmm' % tuple(popt))
    print(f"Tilt U to {-np.rad2deg(popt[1])} and V to {-np.rad2deg(popt[0])} degrees.") 
    plt.xlabel('phi (radian)')
    plt.ylabel('y-y_mean')
    plt.legend()
    plt.show()
    sys.exit()