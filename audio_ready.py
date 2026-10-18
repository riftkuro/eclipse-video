from PySide6.QtCore import QIODevice, QObject
from PySide6.QtMultimedia import QAudioFormat, QAudioSink


class Silence(QIODevice):
    def __init__(self, unsigned=False, parent=None):
        super().__init__(parent)
        self.zero = b'\x80' if unsigned else b'\x00'
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self):
        return True

    def bytesAvailable(self):
        return 65536 + super().bytesAvailable()

    def readData(self, maximum):
        return self.zero * maximum


class AudioReady(QObject):
    def __init__(self, output, parent=None):
        super().__init__(parent)
        self.output = output
        self.sink = None
        self.silence = None
        output.deviceChanged.connect(self.restart)
        self.restart()

    def restart(self):
        self.close()
        device = self.output.device()
        if device.isNull():
            return
        fmt = device.preferredFormat()
        self.silence = Silence(fmt.sampleFormat() == QAudioFormat.SampleFormat.UInt8, self)
        self.sink = QAudioSink(device, fmt, self)
        self.sink.setBufferSize(fmt.bytesForDuration(40000))
        self.sink.start(self.silence)

    def close(self):
        if self.sink:
            self.sink.stop()
            self.sink.deleteLater()
            self.sink = None
        if self.silence:
            self.silence.close()
            self.silence.deleteLater()
            self.silence = None
