"""Small building blocks shared by the pages."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QToolButton, QVBoxLayout)

from gui.resources import make_icon


class NavButton(QToolButton):
    """A sidebar button whose icon follows the theme (accent when selected)."""

    def __init__(self, icon_name: str, tooltip: str, win):
        super().__init__()
        self.icon_name = icon_name
        self.win = win
        self.setObjectName("Nav")
        self.setCheckable(True)
        self.setToolTip(tooltip)
        self.setIconSize(QSize(22, 22))
        self.setCursor(Qt.PointingHandCursor)
        self.toggled.connect(self.refresh)

    def refresh(self, *_):
        theme = self.win.theme
        colour = theme.accent if self.isChecked() else theme.c["text_dim"]
        self.setIcon(make_icon(self.icon_name, colour, 22))


class Card(QFrame):
    def __init__(self):
        super().__init__()
        self.setProperty("card", True)


def label(text: str, style: str = "", wrap: bool = False) -> QLabel:
    """A QLabel with one of the stylesheet's roles: H1, H2, H3, Sub, Faint, Accent, Warning."""
    widget = QLabel(text)
    if style:
        widget.setObjectName(style)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, on_click=None, variant: str = "", tooltip: str = "") -> QPushButton:
    widget = QPushButton(text)
    if variant:
        widget.setProperty("variant", variant)
    widget.setCursor(Qt.PointingHandCursor)
    if tooltip:
        widget.setToolTip(tooltip)
    if on_click:
        widget.clicked.connect(on_click)
    return widget


def tab_button(text: str) -> QPushButton:
    """An underlined tab (History: Downloads / Fetches / Retry queue)."""
    widget = QPushButton(text)
    widget.setProperty("seg", "true")
    widget.setCheckable(True)
    widget.setCursor(Qt.PointingHandCursor)
    return widget


def plain_table(headers, stretch=(), selectable: bool = False) -> QTableWidget:
    """A read-only table in the app's style. `stretch` = columns that take the spare width."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setShowGrid(False)
    table.setFocusPolicy(Qt.NoFocus)
    if selectable:
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
    else:
        table.setSelectionMode(QAbstractItemView.NoSelection)
    header = table.horizontalHeader()
    for col in range(len(headers)):
        header.setSectionResizeMode(
            col, QHeaderView.Stretch if col in stretch else QHeaderView.ResizeToContents)
    return table


class StatCard(QFrame):
    """A small number-and-label card (Pending / Done / Failed)."""

    def __init__(self, value: str, caption: str, status: str = ""):
        super().__init__()
        self.setProperty("card", True)
        self.status = status                  # used to colour the number (see refresh)
        self.setMinimumWidth(110)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.value.setAlignment(Qt.AlignCenter)
        self.caption = QLabel(caption.upper())
        self.caption.setObjectName("StatLabel")
        self.caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set_value(self, value) -> None:
        self.value.setText(str(value))

    def refresh(self, theme) -> None:
        colour = theme.status_colour(self.status) if self.status else theme.c["text"]
        self.value.setStyleSheet(f"color: {colour};")


class DropZone(QFrame):
    """Drop files here, or click to browse. Emits files_dropped([paths])."""

    files_dropped = Signal(list)

    def __init__(self, title: str, hint: str, extensions=(".txt", ".csv", ".json"),
                 dialog_filter: str = "Link lists (*.txt *.csv *.json);;All files (*)"):
        super().__init__()
        self.extensions = tuple(e.lower() for e in extensions)
        self.dialog_filter = dialog_filter
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(120)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(6)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignCenter)
        title_label = QLabel(title)
        title_label.setObjectName("H3")
        title_label.setAlignment(Qt.AlignCenter)
        hint_label = QLabel(hint)
        hint_label.setObjectName("Faint")
        hint_label.setAlignment(Qt.AlignCenter)
        for widget in (self.icon, title_label, hint_label):
            layout.addWidget(widget)

    def refresh(self, theme) -> None:
        self.icon.setPixmap(make_icon("upload", theme.c["text_faint"], 30).pixmap(30, 30))

    def _set_active(self, active: bool) -> None:
        self.setObjectName("DropZoneActive" if active else "DropZone")
        self.style().unpolish(self)
        self.style().polish(self)

    def _wanted(self, paths):
        return [p for p in paths if p.lower().endswith(self.extensions)]

    def dragEnterEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()] if event.mimeData().hasUrls() else []
        if self._wanted(paths):
            event.acceptProposedAction()
            self._set_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_active(False)

    def dropEvent(self, event):
        self._set_active(False)
        files = self._wanted([u.toLocalFile() for u in event.mimeData().urls()])
        if files:
            event.acceptProposedAction()
            self.files_dropped.emit(files)
        else:
            event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            files, _ = QFileDialog.getOpenFileNames(self, "Choose link lists", "", self.dialog_filter)
            if files:
                self.files_dropped.emit(files)
