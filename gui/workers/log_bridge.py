"""Mirror the project's log messages (errors, warnings, notes) into the Debug page.

log_manager writes error.log / warning.log / info.log through Python's logging
module. A handler on those loggers forwards each line to a Qt signal, which is
safe to emit from any thread (download threads included).
"""

import logging

from PySide6.QtCore import QObject, Signal

LOGGERS = ("downloader.error", "downloader.warning", "downloader.info")


class LogBridge(QObject):
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        bridge = self

        class _Handler(logging.Handler):
            def emit(self, record):
                try:
                    bridge.message.emit(f"[{record.levelname.lower()}] {record.getMessage()}")
                except RuntimeError:
                    pass                    # the window is already gone

        self._handler = _Handler()
        for name in LOGGERS:
            logging.getLogger(name).addHandler(self._handler)

    def detach(self):
        for name in LOGGERS:
            logging.getLogger(name).removeHandler(self._handler)
