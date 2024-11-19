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
import queue
import glob
LINUX_FOLDER = "/net/micdata/data2/12IDC/test/ptycho/"
WIN_FOLDER = "X:/12IDC/test/ptycho/"
if os.name == 'nt':
    h5foldername = WIN_FOLDER
else:
    h5foldername = LINUX_FOLDER

q = queue.Queue()

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
        # threads = []
        # for i in range(5):
        #     t = threading.Thread(target=self.processdata)
        #     t.start()
        #     threads.append(t)
        # self.threads = threads
        # for t in self.threads:
        #     t.join()

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


    def processdata(self):
        while True:
            task = q.get()
            if task[0] is None:
                break
            remote_file = task[0]
            local_file = task[1]
            time.sleep(1)
            self.transfer_file(remote_file, local_file)
            self.delete_remote_file(remote_file)
            q.task_done()

    def close(self):
        for i in range(5):
            self.q.put((None,))
        self.sftp.close()
        self.ssh.close()

processor = FileProcessor("pilatus2m.xray.aps.anl.gov", "det", "Pilatus2")

for i in range(5):
    t = threading.Thread(target=processor.processdata)
    t.start()
    t.join()


def handle_file_event(self, full_filename):
#        print(full_filename)
    local_file = os.path.basename(full_filename)
    remote_file = os.path.join('/ramdisk/', local_file)

#        dtn = local_file[:local_file.rfind('.tif')] # test_00120_00001
#        indx = dtn[dtn.rfind("_")+1:] # 00001
    print(remote_file, local_file)
    q.put((remote_file, local_file))


# EPICS Callbacks
def fullfilename_callback(pvname, char_value, **kw):
    processor.handle_file_event(char_value)

def compressfiles():
    dataset_name = 'dp'
    fn2search = '*_00001.tif'
    tiffFilenamesList = glob.glob(fn2search)
    if len(tiffFilenamesList) == 0:
        return 0

    fn = tiffFilenamesList[0]
    print(fn)
    filename2compress = fn[:fn.rfind('_')]
    tiffFilenamesList = glob.glob("%s*.tif"%filename2compress)

    tiffFilenamesList = sorted(tiffFilenamesList)
    if len(tiffFilenamesList) == 0:
        return 0

    newfilename = "%s%s.h5" % (h5foldername, filename2compress)

    arr3d = []
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

timer = threading.Timer(1, compressfiles)
timer.start()

if __name__ == "__main__":
    ffnamePV = PV('S12-PILATUS1:cam1:FullFileName_RBV')
    ffnamePV.add_callback(fullfilename_callback)
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        timer.cancel()
        processor.close()