from epics import caget, caput, PV

beamlinePV = '12idc:'

# detPV = ['S12-PILATUS1:cam1',]
# hdfPV = ['S12-PILATUS1:HDF1',]

# def det_ready():
#     for det in detPV:
#         caput(f'{det}:Acquire', 0)
#         caput(f'{det}:TriggerMode',3) #Trigger mode
#         caput(f'{det}:NumImages',1)
#     for det in hdfPV:
#         caput(f'{det}:EnableCallbacks',1)
#         caput(f'{det}:FileWriteMode',2)
#         caput(f'{det}:FileTemplate', '%s%s_data_%5.5d.h5')
#         caput(f'{det}:NDArrayPort', 'PIL')

# def scan_ready(expt, x_points, y_points):
#     Npoints = x_points*y_points
#     fw_dir = caget(f"{beamlinePV}data:userDir")
#     for det in detPV:
#         caput(f'{det}:AcquirePeriod', expt+0.005)
#         caput(f'{det}:NumImages',Npoints)  # number of data to collect
#     for det in hdfPV:
#         caput(f'{det}:FilePath',fw_dir) #changle fw path
#         caput(f'{det}:FileName','scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))
#         caput(f'{det}:FileNumber',1)
#         caput(f'{det}:NumCapture',x_points)

# def arm_detectors():
# 	for det in detPV:
# 	    caput(f'{det}:Acquire', 1)  # arming the detector.

from ad_pilatus import AD_Pilatus
pil = AD_Pilatus('S12-PILATUS1:')
pil.setNDArrayPort()
def fly_ready(expt, x_points, y_points):
	Npoints = x_points*y_points
	pil.SetExposureTime(expt)
	pil.setFileTemplate('%s%s_data_%5.5d.h5')
	pil.SetMultiFrames(Npoints, x_points)
	pil.StartCapture()

def set_scanNumberAsfilename():
	fw_dir = caget(f"{beamlinePV}data:userDir")
	pil.setFilePath(fw_dir)
	pil.setFileName('scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber')))

def wait_capturedone():
    pil.CCD_waitCaptureDone()
    pil.FileWrite(); 
    pil.CCD_waitFileWriting()
# when x_scan is done..
# run pil.CCD_waitCaptureDone()pil.FileWrite(); pil.CCD_waitFileWriting()