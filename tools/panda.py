
# panda box...
import asyncio
#import sys
from pandablocks.blocking import BlockingClient
from pandablocks.commands import Put
pandaip = "164.54.122.90"
from pandablocks.asyncio import AsyncioClient
from pandablocks.hdf import write_hdf_files
import h5py

def disarm_panda():

    with BlockingClient(pandaip) as client:
        client.send(Put("BITS.A", 0))


pandafn = "C:/Users/s12idc/Documents/GitHub/panda-capture.h5"

async def arm_and_hdf():
    # Create a client and connect the control and data ports
    async with AsyncioClient(pandaip) as client:
        try:
            # Put to 2 fields simultaneously
            await asyncio.gather(
                client.send(Put("BITS.A", 1)),
            )
            # Listen for data, arming the PandA at the beginning
            
            await write_hdf_files(client, file_names=iter((pandafn,)), arm=True)
        except:
            pass

def get_pandadata():
    h = h5py.File(pandafn, "r")
    d = h["INENC2.VAL.Value"][()]
    return d
