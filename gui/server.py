#https://stackoverflow.com/questions/61842432/pyqt5-and-asyncio

import asyncio

from PyQt5 import QtCore


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
        message = data.decode()
        cmd, data = message.split(
            ":"
        )
        print(f"Received: {message}")
        try:
            if cmd == "setrange":
                axis, L, R, step, t = data.split("/")
                print(f"For axis: {axis}, {L}, {R}, {step}, and {t} are updated.")
                self.rangeChanged.emit(axis, float(L), float(R), float(step), float(t))
            if cmd == "mv":
                dt = data.split(';')
                print(dt)
                for data in dt:
                    if len(data)>0:
                        axis, pos = data.split("/")
                        print(axis)
                        self.mvRequested.emit(axis, float(pos))
            if cmd == "run2d":
                self.runRequested.emit(2)
            if cmd == "run3d":
                self.runRequested.emit(3) 
            if cmd == "none":
                self.runRequested.emit(0)           
        except ValueError as e:
            print(e)