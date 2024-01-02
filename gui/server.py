#https://stackoverflow.com/questions/61842432/pyqt5-and-asyncio

import asyncio

from PyQt5 import QtCore


class UDPserver(QtCore.QObject):
    rangeChanged = QtCore.pyqtSignal(str, float, float, float, float)
    runRequested = QtCore.pyqtSignal(int)

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

        try:
            if cmd == "setrange":
                axis, L, R, step, rt = data.split("/")
                self.rangeChanged.emit(axis, float(L), float(R), float(step), float(rt))
            if cmd == "run2d":
                self.runRequested.emit(2)
            if cmd == "run3d":
                self.runRequested.emit(3)            
        except ValueError as e:
            print(e)