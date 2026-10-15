"""About: the project, other projects, and ways to support it.

Edit the LINKS and the cards below with your own details.
"""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLineEdit,
                               QStackedWidget, QVBoxLayout, QWidget)

from gui.resources import make_icon
from gui.views.widgets import Card, button, label, tab_button

LINKS = {
    "github": "https://github.com/",
    "issues": "https://github.com/",
    "kofi": "https://ko-fi.com/",
}
CRYPTO_ADDRESS = "YOUR-USDT-ADDRESS-HERE"


class AboutView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 36, 40, 24)
        outer.setSpacing(16)
        outer.addWidget(label("About", "H1"))

        tabs = QHBoxLayout()
        tabs.setSpacing(18)
        self.tab_proj = tab_button("Projects")
        self.tab_supp = tab_button("Support Me")
        self.tab_proj.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.tab_proj)
        group.addButton(self.tab_supp)
        self.tab_proj.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        self.tab_supp.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        tabs.addWidget(self.tab_proj)
        tabs.addWidget(self.tab_supp)
        tabs.addStretch(1)
        outer.addLayout(tabs)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        projects = QWidget()
        row = QHBoxLayout(projects)
        row.setSpacing(16)
        row.setAlignment(Qt.AlignTop)
        row.addWidget(self._project_card(
            win.app_name, f"v{win.app_version}",
            "Download YouTube and YouTube Music tracks, albums, playlists and channels with "
            "yt-dlp. The window and the terminal program share settings, logs, history and "
            "the retry queue.",
            ["Python", "PySide6", "yt-dlp"]))
        row.addWidget(self._project_card(
            "Your Other Project", "v1.0",
            "Describe another tool of yours here (edit gui/views/about_view.py).",
            ["Edit", "me"]))
        row.addStretch(1)
        self.stack.addWidget(projects)

        support = QWidget()
        row = QHBoxLayout(support)
        row.setSpacing(16)
        row.setAlignment(Qt.AlignTop)

        kofi = Card()
        kofi.setFixedWidth(360)
        layout = QVBoxLayout(kofi)
        layout.setContentsMargins(20, 24, 20, 24)
        layout.setSpacing(10)
        layout.addWidget(label("Support via Ko-fi", "H3"))
        layout.addWidget(label("Enjoying the project? You can support ongoing development by "
                               "buying me a coffee.", "Sub", wrap=True))
        layout.addStretch(1)
        self.kofi = button("  Support me on Ko-fi",
                           lambda: QDesktopServices.openUrl(QUrl(LINKS["kofi"])), "accent")
        layout.addWidget(self.kofi)

        crypto = Card()
        crypto.setFixedWidth(360)
        layout = QVBoxLayout(crypto)
        layout.setContentsMargins(20, 24, 20, 24)
        layout.setSpacing(10)
        layout.addWidget(label("Crypto (USDT TRC20)", "H3"))
        layout.addWidget(label("Crypto donations are also accepted. Copy the address below.",
                               "Sub", wrap=True))
        layout.addStretch(1)
        address_row = QHBoxLayout()
        self.address = QLineEdit(CRYPTO_ADDRESS)
        self.address.setReadOnly(True)
        address_row.addWidget(self.address, 1)
        address_row.addWidget(button("Copy", lambda: QApplication.clipboard().setText(
            self.address.text())))
        layout.addLayout(address_row)

        row.addWidget(kofi)
        row.addWidget(crypto)
        row.addStretch(1)
        self.stack.addWidget(support)

    @staticmethod
    def _project_card(name, version, description, tags):
        card = Card()
        card.setFixedWidth(340)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label(name, "H3"))
        top.addStretch(1)
        top.addWidget(label(version, "Accent"))
        layout.addLayout(top)
        layout.addWidget(label(description, "Sub", wrap=True))
        layout.addStretch(1)
        layout.addWidget(label("  •  ".join(tags), "Faint"))
        return card

    def refresh_icons(self):
        self.kofi.setIcon(make_icon("coffee", self.win.theme.on_accent, 16))
