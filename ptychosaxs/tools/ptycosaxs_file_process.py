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
from concurrent.futures import ThreadPoolExecutor
#import glob
#import shutil

LINUX_FOLDER = "/net/micdata/data2/12IDC/test/ptycho/"
WIN_FOLDER = "X:/12IDC/test/ptycho/"
if os.name == 'nt':
    h5foldername = WIN_FOLDER
else:
    h5foldername = LINUX_FOLDER

# q = queue.Queue()

# class FileProcessor:
#     def __init__(self, hostname, username, password):
#         self.hostname = hostname
#         self.username = username
#         self.password = password
#         self.lock = threading.Lock()
#         self.ssh = paramiko.SSHClient()
#         self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
#         self.ssh.connect(hostname, username=username, password=password)
#         self.sftp = self.ssh.open_sftp()
#         self.N = -1
#         self.fn = []
#         # threads = []
#         # for i in range(5):
#         #     t = threading.Thread(target=self.processdata)
#         #     t.start()
#         #     threads.append(t)
#         # self.threads = threads
#         # for t in self.threads:
#         #     t.join()

#     def transfer_file(self, remote_file, local_file):
#         try:
#             self.sftp.get(remote_file, local_file)
#             logging.info(f"Transferred {remote_file} to {local_file}")
#         except Exception as e:
#             logging.error(f"Error transferring {remote_file}: {e}")

#     def delete_remote_file(self, remote_file):
#         try:
#             self.sftp.remove(remote_file)
#             logging.info(f"Deleted remote file {remote_file}")
#         except Exception as e:
#             logging.error(f"Error deleting {remote_file}: {e}")


#     def processdata(self):
#         while True:
#             task = q.get()
#             if task[0] is None:
#                 break
#             remote_file = task[0]
#             local_file = task[1]
#             time.sleep(1)
#             self.transfer_file(remote_file, local_file)
#             self.delete_remote_file(remote_file)
#             q.task_done()

#     def close(self):
#         for i in range(5):
#             self.q.put((None,))
#         self.sftp.close()
#         self.ssh.close()

# processor = FileProcessor("pilatus2m.xray.aps.anl.gov", "det", "Pilatus2")

# for i in range(5):
#     t = threading.Thread(target=processor.processdata)
#     t.start()
#     t.join()


# def handle_file_event(self, full_filename):
# #        print(full_filename)
#     local_file = os.path.basename(full_filename)
#     remote_file = os.path.join('/ramdisk/', local_file)

# #        dtn = local_file[:local_file.rfind('.tif')] # test_00120_00001
# #        indx = dtn[dtn.rfind("_")+1:] # 00001
#     print(remote_file, local_file)
#     q.put((remote_file, local_file))


# # EPICS Callbacks
# def fullfilename_callback(pvname, char_value, **kw):
#     processor.handle_file_event(char_value)

# # #def compressfiles():
# #     dataset_name = 'dp'
# #     fn2search = '*_00001.tif'
# #     tiffFilenamesList = glob.glob(fn2search)
# #     if len(tiffFilenamesList) == 0:
# #         return 0

# #     fn = tiffFilenamesList[0]
# #     print(fn)
# #     filename2compress = fn[:fn.rfind('_')]
# #     tiffFilenamesList = glob.glob("%s*.tif"%filename2compress)

# #     tiffFilenamesList = sorted(tiffFilenamesList)
# #     if len(tiffFilenamesList) == 0:
# #         return 0

# #     newfilename = "%s%s.h5" % (h5foldername, filename2compress)

# #     arr3d = []
# #     for i, fn in enumerate(tiffFilenamesList):
# #         try:
# #             print(fn)
# #             with Image.open(fn) as img:
# #                 img_array = np.array(img)
# #                 if i==0:
# #                     row,col = img_array.shape
# #                     #arr3d = np.zeros((row,col,len(tiffFilenamesList)))
# #                     #arr3d[:,:,0] = img_array
# #                     arr3d = np.zeros((len(tiffFilenamesList), row,col))
# #                     arr3d[0,:,:] = img_array
# #                 else:
# #                     #arr3d[:,:,i] = img_array
# #                     arr3d[i,:,:] = img_array
# #         except:
# #             pass
# #     # Compress the image data using LZ4
# # #   compressed_data = lz4.frame.compress(img_array.tobytes())
# #     if len(arr3d) == 0:
# #         return
# #     # Open the HDF5 file
# #     with h5py.File(newfilename, 'w') as hf:
# #         if dataset_name in hf:
# #             del hf[dataset_name]
# #         # Create a dataset with LZ4 compression filter
# #         dataset = hf.create_dataset(dataset_name, data=arr3d, compression="lzf")

# #         # Store metadata (optional)
# #         dataset.attrs['shape'] = arr3d.shape
# #         dataset.attrs['dtype'] = arr3d.dtype.str
# #     for i, fn in enumerate(tiffFilenamesList):
# #         if os.path.exists(fn):
# #             os.remove(fn)
# #     print("Compression done")
# #     return 1

# # timer = threading.Timer(1, compressfiles)
# # timer.start()

# if __name__ == "__main__":
#     ffnamePV = PV('S12-PILATUS1:cam1:FullFileName_RBV')
#     ffnamePV.add_callback(fullfilename_callback)
#     try:
#         while True:
#             time.sleep(0.1)
#     except KeyboardInterrupt:
#         #timer.cancel()
#         processor.close()


class TIFFileHandler:
    """
    Handles events for new .tif files from a remote directory and processes them locally.
    """
    def __init__(self, src_host, src_user, src_pass, src_dir, dest_dir, h5_path, max_workers=4):
        self.src_host = src_host
        self.src_user = src_user
        self.src_pass = src_pass
        self.src_dir = src_dir
        self.dest_dir = dest_dir
        self.h5_path = h5_path
        self.max_workers = max_workers #or os.cpu_count()  # Use all available CPU threads
        self.hdf5_appender = HDF5Appender(h5_path)

        # Ensure destination directory exists
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        # Set up threading and file tracking
        self.lock = threading.Lock()
        self.processed_files = set()

        # Thread pool for file handling
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        # Connect to the remote server
        self.src_ssh, self.src_sftp = self._connect_to_server(src_host, src_user, src_pass)

    def _connect_to_server(self, host, username, password):
        """
        Establishes an SSH connection and returns the SSH and SFTP clients.
        """
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=username, password=password)
        return ssh, ssh.open_sftp()

    def poll_remote_directory(self):
        """
        Polls the remote directory for new .tif files and triggers processing for new files.
        Stops cleanly on `KeyboardInterrupt` or connection errors.
        """
        try:
            #while not self.stop_event.is_set():
            while True:
                try:
                    logging.info("Checking for new files...")
                    file_list = self.src_sftp.listdir(self.src_dir)
                    tif_files = [f for f in file_list if f.endswith('.tif')]

                    for file_name in tif_files:
                        if file_name not in self.processed_files:
                            self.executor.submit(self.handle_new_file, file_name)

                    time.sleep(5)  # Adjust polling interval as needed
                except paramiko.SSHException as e:
                    logging.error(f"Connection error while polling: {e}")
                    #self.reconnect()  # Attempt to reconnect
                except Exception as e:
                    logging.error(f"Unexpected error while polling: {e}")
                    break
        except KeyboardInterrupt:
            logging.info("Polling interrupted by user.")

    # def handle_new_file(self, file_name):
    #     """
    #     Processes a new .tif file: copies to local and appends to HDF5.
    #     """
    #     with self.lock:
    #         # Mark the file as processed
    #         if file_name in self.processed_files:
    #             return

    #         # Remote and local file paths
    #         remote_src_file = os.path.join(self.src_dir, file_name)
    #         local_dest_file = os.path.join(self.dest_dir, file_name)
    #         try:
    #             try:
    #                 # Download the file
    #                 print(f"Downloading {remote_src_file} to {local_dest_file}...")
    #                 self.src_sftp.get(remote_src_file, local_dest_file)

    #                 # Append the file to the HDF5
    #                 # self.append_to_h5(local_dest_file)

    #                 # Mark the file as processed
    #                 self.processed_files.add(file_name)

    #                 # Optionally delete the remote file
    #                 self.src_sftp.remove(remote_src_file)
    #                 print(f"Processed and removed remote file: {remote_src_file}")
    #             except Exception as e:
    #                 print(f"Error handling file {file_name}: {e}")
    #         except KeyboardInterrupt:
    #             print("Exiting...")


    def handle_new_file(self, file_name):
        """
        Processes a new .tif file: copies to local and appends to HDF5.
        """
        with self.lock:
            # Mark the file as processed
            if file_name in self.processed_files:
                logging.info(f"File {file_name} has already been processed. Skipping.")
                return

            # Remote and local file paths
            remote_src_file = os.path.join(self.src_dir, file_name)
            local_dest_file = os.path.join(self.dest_dir, file_name)

            # Check if the remote file exists
            try:
                self.src_sftp.stat(remote_src_file)
            except FileNotFoundError:
                logging.error(f"Remote file does not exist: {remote_src_file}")
                return
            except Exception as e:
                logging.error(f"Error checking remote file: {remote_src_file}, {e}")
                return

            try:
                # Download the file
                logging.info(f"Downloading {remote_src_file} to {local_dest_file}...")
                self.src_sftp.get(remote_src_file, local_dest_file)

                # Append the file to the HDF5
                # self.append_to_h5(local_dest_file)
                self.hdf5_appender.append(local_dest_file)

                # Mark the file as processed
                self.processed_files.add(file_name)

                # Optionally delete the remote file
                self.src_sftp.remove(remote_src_file)
                logging.info(f"Processed and removed remote file: {remote_src_file}")
            except Exception as e:
                logging.error(f"Error handling file {file_name}: {e}")
    
    # def reconnect(self):
    #     """
    #     Attempts to reconnect to the remote server in case of a connection error.
    #     """
    #     logging.info("Reconnecting to remote server...")
    #     self.src_sftp.close()
    #     self.src_ssh.close()
    #     self.src_ssh, self.src_sftp = self._connect_to_server(
    #         self.src_host, self.src_user, self.src_pass
    #     )

    def append_to_h5(self, local_file):
        """
        Appends a single .tif file to the HDF5 file in a thread-safe manner.
        """
        logging.info(f"Starting append_to_h5 for {local_file}")
        with self.lock:  # Ensure thread-safe access to the HDF5 file
            try:
                logging.info(f"Opening HDF5 file: {self.h5_path}")
                with h5py.File(self.h5_path, 'a') as h5_file:
                    logging.info(f"Reading image file: {local_file}")
                    image = Image.open(local_file)
                    image_array = np.array(image)
                    dataset_name = os.path.basename(local_file)

                    if dataset_name in h5_file:
                        logging.info(f"Dataset {dataset_name} already exists. Skipping.")
                        return

                    logging.info(f"Creating dataset: {dataset_name}")
                    h5_file.create_dataset(
                        name=dataset_name,
                        data=image_array,
                        compression="gzip",
                        compression_opts=4
                    )
                    logging.info(f"Appended dataset {dataset_name} to HDF5.")
            except Exception as e:
                logging.error(f"Error in append_to_h5: {e}")

    def close(self):
        """
        Closes the SFTP and SSH connections and sets the stop event.
        """
        #self.stop_event.set()
        try:
            self.src_sftp.close()
            self.src_ssh.close()
            self.hdf5_appender.stop()
            self.executor.shutdown(wait=True)
        except Exception as e:
            logging.warning(f"Error while closing connections: {e}")
        logging.info("Connections closed. Exiting...")

# class HDF5Appender:
#     def __init__(self, h5_path):
#         self.h5_path = h5_path
#         self.queue = queue.Queue()
#         self.thread = threading.Thread(target=self.run, daemon=True)
#         self.stop_event = threading.Event()
#         self.thread.start()

#     def run(self):
#         while not self.stop_event.is_set():
#             try:
#                 local_file = self.queue.get(timeout=1)
#                 self._append_to_h5(local_file)
#                 self.queue.task_done()
#             except queue.Empty:
#                 continue
#             except Exception as e:
#                 logging.error(f"Error in HDF5 appender: {e}")

#     def _append_to_h5(self, local_file):
#         try:
#             logging.info(f"Appending {local_file} to HDF5.")
#             with h5py.File(self.h5_path, 'a') as h5_file:
#                 image = Image.open(local_file)
#                 image_array = np.array(image)
#                 dataset_name = os.path.basename(local_file)

#                 if dataset_name in h5_file:
#                     logging.info(f"Dataset {dataset_name} already exists. Skipping.")
#                     return

#                 h5_file.create_dataset(
#                     name=dataset_name,
#                     data=image_array,
#                     compression="gzip",
#                     compression_opts=4
#                 )
#                 logging.info(f"Appended dataset {dataset_name} to HDF5.")
#         except Exception as e:
#             logging.error(f"Error in _append_to_h5: {e}")

#     def append(self, local_file):
#         self.queue.put(local_file)

#     def stop(self):
#         self.stop_event.set()
#         self.thread.join()

class HDF5Appender:
    """
    Handles appending datasets to an HDF5 file in a thread-safe manner.
    """
    def __init__(self, h5_path, max_workers=4):
        self.h5_path = h5_path
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)# or os.cpu_count())

    def append(self, local_file):
        """
        Submits a file to the thread pool for appending to HDF5.
        """
        self.executor.submit(self._append_to_h5, local_file)

    def _append_to_h5(self, local_file):
        """
        Appends a single .tif file to the HDF5 file.
        """
        with self.lock:  # Ensure only one thread writes to the HDF5 file
            try:
                logging.info(f"Appending {local_file} to HDF5.")
                with h5py.File(self.h5_path, 'a') as h5_file:
                    image = Image.open(local_file)
                    image_array = np.array(image)
                    dataset_name = os.path.basename(local_file)

                    if dataset_name in h5_file:
                        logging.info(f"Dataset {dataset_name} already exists. Skipping.")
                        return

                    h5_file.create_dataset(
                        name=dataset_name,
                        data=image_array,
                        compression="gzip",
                        compression_opts=4
                    )
                    logging.info(f"Appended dataset {dataset_name} to HDF5.")
            except Exception as e:
                logging.error(f"Error in _append_to_h5: {e}")

    def close(self):
        """
        Shuts down the executor.
        """
        self.executor.shutdown(wait=True)
        logging.info("HDF5Appender shut down.")



if __name__ == "__main__":
    # Remote source directory and local destination
    src_host = "remote.server.com"
    src_user = "user"
    src_pass = "password"
    src_dir = "/ramdisk/ptychosaxs_processor_test/"
    dest_dir = "C:/Users/s12idc/Documents/GitHub/ptychoSAXS/ptychosaxs_process_test/"
    h5_path = "C:/Users/s12idc/Documents/GitHub/ptychoSAXS/ptychosaxs_process_test/output_file.h5"
    #h5_path = os.path.join(h5foldername,'output.h5')

    logging.basicConfig(level=logging.INFO)

    handler = TIFFileHandler("pilatus2m.xray.aps.anl.gov", "det", "Pilatus2",src_dir,dest_dir,h5_path)

    # EPICS Callbacks
    def fullfilename_callback(pvname,char_value, **kw):
        handler.poll_remote_directory()

    # def fullfilename_callback(pvname, char_value, **kw):
    #     file_name = os.path.basename(char_value)
    #     logging.info(f"New file event received: {file_name}")
    #     handler.handle_new_file(file_name)
    
    # def fullfilename_callback(pvname, char_value, **kw):
    #     """
    #     Callback triggered when a new file is ready.
    #     Validates and processes the file.
    #     """
    #     try:
    #         # Extract the file name from the PV value
    #         file_name = os.path.basename(char_value)
    #         logging.info(f"New file event received: {file_name}")

    #         # Check if the file name is non-empty
    #         if not file_name:
    #             logging.warning("Received empty file name from PV.")
    #             return

    #         # Trigger file processing
    #         handler.handle_new_file(file_name)
    #     except Exception as e:
    #         logging.error(f"Error in fullfilename_callback: {e}")

    ffnamePV = PV('S12-PILATUS1:cam1:FullFileName_RBV')
    ffnamePV.add_callback(fullfilename_callback)

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Shutting down...")
        handler.close()




