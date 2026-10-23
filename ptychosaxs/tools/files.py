import os
from epics import caget, caput, PV
from threading import Thread

main_dir='/mnt/Sector_12/12id-c/12IDC_PTYCHO/2023_Mar'  #Pilatus2M computer
fw_path=os.path.join(main_dir,'ptycho')
print('fw_path:', fw_path)
beamlinePV = "12idc:"

def folder_ready(beamlinePV):
	fw_dir=os.path.join(fw_path,'scan{:03d}'.format(caget(f'{beamlinePV}saveData_scanNumber'))) #change here
	create_dir(fw_dir)
	caput(f"{beamlinePV}data:userDir", fw_dir)

def create_dir(dirname):
	if not os.path.exists(dirname):  #create directory
		os.makedirs(dirname, mode=0o777)
		os.chmod(dirname,0o777)
		print("Creating directory:"+dirname)
	else:
		print("The directory already exists!")