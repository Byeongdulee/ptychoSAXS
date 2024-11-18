import logging
import os
import time
from epics import PV
import threading
import paramiko
from PIL import Image
logging.basicConfig(level=logging.INFO)
import h5py
import numpy as np
import os
LINUX_FOLDER = "/net/micdata/data2/12IDC/2024_Nov/ptycho/"
WIN_FOLDER = "z:/data2/12IDC/2024_Nov/ptycho/"
class FileProcessor:
    def __init__(self, hostname, username, password):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.lock = threading.Lock()
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(hostname, username=username, password=password)
        self.sftp = self.ssh.open_sftp()
        self.N = -1
        self.fn = []
        if os.name == 'nt':
            self.h5foldername = WIN_FOLDER
        else:
            self.h5foldername = LINUX_FOLDER

    def transfer_file(self, remote_file, local_file):
        try:
            self.sftp.get(remote_file, local_file)
            logging.info(f"Transferred {remote_file} to {local_file}")
        except Exception as e:
            logging.error(f"Error transferring {remote_file}: {e}")

    def delete_remote_file(self, remote_file):
        try:
            self.sftp.remove(remote_file)
            logging.info(f"Deleted remote file {remote_file}")
        except Exception as e:
            logging.error(f"Error deleting {remote_file}: {e}")

    def handle_file_event(self, full_filename):
        local_file = os.path.basename(full_filename)
        remote_file = os.path.join('/ramdisk/', local_file)

        dtn = local_file[:local_file.rfind('.tif')] # test_00120_00001
        indx = dtn[dtn.rfind("_")+1:] # 00001
        
        if previndex>0 and int(indx)==0:
            previndex = 0
            print("0 skiped")
            return

        bname = dtn[:dtn.rfind("_")] # test_00120
        N = bname[bname.rfind("_")+1:] # 00120
        if self.N != N:
            # thread
            t = threading.Thread(target=lambda: self.compressfiles(self.fn))
        else:
            self.fn.append(local_file)
            self.N = N
            t = threading.Thread(target=lambda: self.processdata(remote_file, local_file))
        t.start()
        t.join()

    def processdata(self, remote_file, local_file):
        with self.lock:
            self.transfer_file(remote_file, local_file)
            self.delete_remote_file(remote_file)

    def compressfiles(self, tiffFilenamesList, **kw):
        dataset_name = 'dp'

        if len(tiffFilenamesList) == 0:
            return 0

        fn = tiffFilenamesList[0]
        print(fn)
        filename2compress = fn[:fn.rfind('_')]
        newfilename = "%s%s.h5" % (self.h5foldername, filename2compress)

        arr3d = []
        tiffFilenamesList = sorted(tiffFilenamesList)
        for i, fn in enumerate(tiffFilenamesList):
            try:
                print(fn)
                with Image.open(fn) as img:
                    img_array = np.array(img)
                    if i==0:
                        row,col = img_array.shape
                        #arr3d = np.zeros((row,col,len(tiffFilenamesList)))
                        #arr3d[:,:,0] = img_array
                        arr3d = np.zeros((len(tiffFilenamesList), row,col))
                        arr3d[0,:,:] = img_array
                    else:
                        #arr3d[:,:,i] = img_array
                        arr3d[i,:,:] = img_array
            except:
                pass
        # Compress the image data using LZ4
    #   compressed_data = lz4.frame.compress(img_array.tobytes())
        if len(arr3d) == 0:
            return
        # Open the HDF5 file
        with h5py.File(newfilename, 'w') as hf:
            if dataset_name in hf:
                del hf[dataset_name]
            # Create a dataset with LZ4 compression filter
            dataset = hf.create_dataset(dataset_name, data=arr3d, compression="lzf")

            # Store metadata (optional)
            dataset.attrs['shape'] = arr3d.shape
            dataset.attrs['dtype'] = arr3d.dtype.str
        for i, fn in enumerate(tiffFilenamesList):
            if os.path.exists(fn):
                os.remove(fn)
        print("Compression done")
        return 1

    def close(self):
        self.sftp.close()
        self.ssh.close()

# EPICS Callbacks
def fullfilename_callback(pvname, char_value, **kw):
    processor.handle_file_event(char_value)

if __name__ == "__main__":
    processor = FileProcessor("pilatus2m.xray.aps.anl.gov", "det", "Pilatus2")
    ffnamePV = PV('S12-PILATUS1:cam1:FullFileName_RBV')
    ffnamePV.add_callback(fullfilename_callback)
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        processor.close()