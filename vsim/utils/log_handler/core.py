import logging

from PyQt6.QtCore import pyqtSignal, QObject


class QtTextEditLogHandler(logging.Handler):
    """Logging handler that emits log records to a Qt signal (to update a QTextEdit)."""
    class Emitter(QObject):
        message = pyqtSignal(str)

    def __init__(self, level=logging.INFO):
        super().__init__(level)
        self.emitter = QtTextEditLogHandler.Emitter()
        fmt = '%(asctime)s [%(levelname)s] %(message)s'
        self.setFormatter(logging.Formatter(fmt, datefmt='%Y-%m-%d %H:%M:%S'))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.emitter.message.emit(msg)
        except Exception:
            self.handleError(record)
