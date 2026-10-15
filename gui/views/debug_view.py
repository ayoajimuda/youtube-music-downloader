"""Debug: a live log of what the GUI and the downloader are doing."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from gui.resources import make_icon
from gui.views.widgets import button, label
from managers import log_manager


class DebugView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 36, 40, 24)
        outer.setSpacing(16)

        head = QHBoxLayout()
        head.addWidget(label("Debug Logs", "H1"))
        head.addStretch(1)
        self.logs_btn = button("  Open logs folder", self.open_logs)
        self.export_btn = button("  Export", self.export)
        self.copy_btn = button("  Copy", self.copy)
        self.clear_btn = button("  Clear", self.clear)
        for widget in (self.logs_btn, self.export_btn, self.copy_btn, self.clear_btn):
            head.addWidget(widget)
        outer.addLayout(head)
        outer.addWidget(label("Errors, warnings and notes from the downloader appear here as "
                              "they happen. They're also saved in the logs folder.", "Faint",
                              wrap=True))

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setPlaceholderText("no logs yet…")
        outer.addWidget(self.console, 1)

    def refresh_icons(self):
        dim = self.win.theme.c["text_dim"]
        self.logs_btn.setIcon(make_icon("folder", dim, 16))
        self.export_btn.setIcon(make_icon("download", dim, 16))
        self.copy_btn.setIcon(make_icon("copy", dim, 16))
        self.clear_btn.setIcon(make_icon("trash", dim, 16))

    def append(self, text: str):
        self.console.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")

    def export(self):
        name, _ = QFileDialog.getSaveFileName(self, "Export logs", "debug.log",
                                              "Log files (*.log *.txt)")
        if name:
            Path(name).write_text(self.console.toPlainText(), encoding="utf-8")

    def copy(self):
        QApplication.clipboard().setText(self.console.toPlainText())

    def clear(self):
        self.console.clear()

    def open_logs(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_manager.LOG_DIR)))
