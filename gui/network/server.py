"""
Canonical UDP server using JSON protocol. Replaces server_json.py.
"""
#https://stackoverflow.com/questions/61842432/pyqt5-and-asyncio

import asyncio

from PyQt5 import QtCore
import json
#IP = "127.0.0.1"
IP = "10.54.122.103"
BIND_ADDR = "0.0.0.0"  # listen on all local interfaces, not a hardcoded host IP

async def create_server(loop):
    return await loop.create_datagram_endpoint(
        lambda: UDPserver(), local_addr=(BIND_ADDR, 20002)
    )

class UDPserver(QtCore.QObject):
    jsonReceived = QtCore.pyqtSignal(dict)
    def __init__(self, parent=None):
        super().__init__(parent)
        self._transport = None
        self._counter_message = 0
        print("UDP Sever Connected...")

    @property
    def transport(self):
        return self._transport

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data, addr):
        self._counter_message += 1
        print("#Num of Mssg Received: {}".format(self._counter_message))

        try:
            json_message = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            # Report `data`, not `json_message`: on a parse failure the latter
            # was never bound, so the old handler raised UnboundLocalError
            # inside its own error path and lost the bad datagram entirely.
            print(f"Invalid message from {addr}: {exc}; {bytes(data)[:200]!r}")
            return

        print("JSON message received: " + str(json_message))
        try:
            self.jsonReceived.emit(json_message)
        except Exception:
            # A failure in the scan dispatch downstream must not kill the
            # datagram handler, but it must not vanish either -- the old bare
            # `except` swallowed these and then tripped over json_message.
            import traceback
            print("Error dispatching message:\n" + traceback.format_exc())
