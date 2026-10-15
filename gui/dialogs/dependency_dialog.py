"""A window showing whether yt-dlp, ffmpeg and the Python packages are installed."""

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from gui.workers.dependency_worker import DependencyWorker


class DependencyDialog(QDialog):
    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self.theme = theme
        self.setWindowTitle("Dependency check")
        self.resize(760, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)
        heading = QLabel("Dependency check")
        heading.setObjectName("H2")
        self.summary = QLabel("Checking yt-dlp, ffmpeg and the Python packages…")
        self.summary.setObjectName("Sub")
        self.summary.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(self.summary)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Status", "Version / location", "How to fix"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.again = QPushButton("Check again")
        self.again.clicked.connect(self.start)
        close = QPushButton("Close")
        close.setProperty("variant", "accent")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.again)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.worker = None
        self.start()

    def start(self):
        if self.worker and self.worker.isRunning():
            return
        self.again.setEnabled(False)
        self.summary.setText("Checking yt-dlp, ffmpeg and the Python packages…")
        self.worker = DependencyWorker()
        self.worker.done.connect(self.show_report)
        self.worker.start()

    def show_report(self, report):
        self.again.setEnabled(True)
        ok_colour = QColor("#22c55e")
        bad_colour = QColor("#ef4444")
        faint = QColor(self.theme.c["text_faint"]) if self.theme else QColor("#888888")
        self.table.setRowCount(0)
        for dep in report.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            if dep.found:
                status, colour = "ok", ok_colour
                detail = dep.version + (f"  ({dep.location})" if dep.location else "")
            else:
                status = "MISSING" if dep.required else "not installed"
                colour = bad_colour if dep.required else faint
                detail = dep.purpose
            cells = [dep.name, status, detail, dep.fix]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 1:
                    item.setForeground(colour)
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()

        if report.missing_required:
            names = ", ".join(d.name for d in report.missing_required)
            self.summary.setText(f"Missing: {names}. Downloads need these; see 'How to fix'.")
        elif not report.has_js_runtime:
            self.summary.setText("Everything required is installed. No JavaScript runtime was "
                                 "found: if downloads fail with 'No supported JS runtime', "
                                 "install Deno.")
        else:
            self.summary.setText("Everything required is installed.")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.wait(20000)
        super().closeEvent(event)

    def done(self, result):                   # accept()/reject() go through here
        if self.worker and self.worker.isRunning():
            self.worker.wait(20000)
        super().done(result)
