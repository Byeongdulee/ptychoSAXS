#https://stackoverflow.com/questions/61842432/pyqt5-and-asyncio

import asyncio

from PyQt5 import QtCore
import json

class UDPserver(QtCore.QObject):
    rangeChanged = QtCore.pyqtSignal(str, float, float, float, float)
    runRequested = QtCore.pyqtSignal(int)
    mvRequested = QtCore.pyqtSignal(str, float)
    jsonReceived = QtCore.pyqtSignal(dict)
    def __init__(self, parent=None):
        super().__init__(parent)
        self._transport = None
        self._counter_message = 0

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
            print("JSON message received: " + str(json_message))
            self.jsonReceived.emit(json_message)
            # return_message = None
            # cmd = json_message['command']
            # try:
            #     data = json_message['data']
            # except:
            #     data = {}
            # if cmd == 'setrange':
            #     axis = data['axis']
            #     L = data['L']
            #     R = data['R']
            #     step = data['step']
            #     t = data['t']
            #     self.rangeChanged.emit(axis, float(L), float(R), float(step), float(t))
            #     return_message = "complete"
            # elif cmd == 'mv':
            #     for axis, pos in data.items():
            #         self.mvRequested.emit(axis, float(pos))
            #     return_message = "complete"
            # elif cmd == 'run2d':
            #     self.runRequested.emit(2)
            #     return_message = "complete"
            # elif cmd == 'run3d':
            #     self.runRequested.emit(3)
            #     return_message = "complete"
            # elif cmd == 'none':
            #     self.runRequested.emit(0)
            #     return_message = "complete"
            # else:
            #     print(f"Invalid command {cmd} is recieved.")
        except:
            print("Invalid message: ", json_message)
            return_message = "Invalid message"