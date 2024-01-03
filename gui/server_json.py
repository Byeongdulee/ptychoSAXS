#https://stackoverflow.com/questions/61842432/pyqt5-and-asyncio

import asyncio

from PyQt5 import QtCore
import json

class UDPserver(QtCore.QObject):
    rangeChanged = QtCore.pyqtSignal(str, float, float, float, float)
    runRequested = QtCore.pyqtSignal(int)
    mvRequested = QtCore.pyqtSignal(str, float)
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
        # print(f"Received: {message}")
        # message = data.decode()
        # cmd, data = message.split(
        #     ":"
        # )
        # try:
        #     if cmd == "setrange":
        #         axis, L, R, step, t = data.split("/")
        #         print(f"For axis: {axis}, {L}, {R}, {step}, and {t} are updated.")
        #         self.rangeChanged.emit(axis, float(L), float(R), float(step), float(t))
        #     if cmd == "mv":
        #         dt = data.split(';')
        #         print(dt)
        #         for data in dt:
        #             if len(data)>0:
        #                 axis, pos = data.split("/")
        #                 print(axis)
        #                 self.mvRequested.emit(axis, float(pos))
        #     if cmd == "run2d":
        #         self.runRequested.emit(2)
        #     if cmd == "run3d":
        #         self.runRequested.emit(3) 
        #     if cmd == "none":
        #         self.runRequested.emit(0)           
        # except ValueError as e:
        #     print(e)
        try:
            json_message = json.loads(data)
            print("JSON message received: " + str(json_message))
            return_message = None
            cmd = json_message['command']
            if cmd == 'setrange':
                data = json_message['data']
                axis = data['axis']
                L = data['L']
                R = data['R']
                step = data['step']
                t = data['t']
                self.rangeChanged.emit(axis, float(L), float(R), float(step), float(t))
                return_message = "complete"
            elif cmd == 'mv':
                for axis, pos in data.items():
                    self.mvRequested.emit(axis, float(pos))
                return_message = "complete"
            elif cmd == 'run2d':
                self.runRequested.emit(2)
                return_message = "complete"
            elif cmd == 'run3d':
                self.runRequested.emit(3)
                return_message = "complete"
            elif cmd == 'none':
                self.runRequested.emit(0)
                return_message = "complete"
            else:
                raise Exception()
        except:
            print("Invalid message: ", json_message)
            return_message = "Invalid message"