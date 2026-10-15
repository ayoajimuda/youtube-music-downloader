"""The main window: a sidebar on the left, the pages on the right."""

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from gui import APP_NAME, APP_VERSION
from gui.dialogs import messages
from gui.resources import app_icon
from gui.styles import GuiSettings, Theme
from gui.views import (LINKS, AboutView, BatchView, DebugView, HistoryView, HomeView,
                       NavButton, SettingsView)
from gui.workers import LogBridge
from managers import config_manager, log_manager

PAGES = (("home", "Home"), ("list", "Batch"), ("history", "History"), ("settings", "Settings"),
         ("terminal", "Debug Logs"), ("grid", "About"))


class MainWindow(QMainWindow):
    def __init__(self, downloader):
        """`downloader` is a ready YoutubeMusicDownloader (gui_main.py builds it)."""
        super().__init__()
        self.app_name = APP_NAME
        self.app_version = APP_VERSION
        self.downloader = downloader
        self.gui_settings = GuiSettings()
        self.theme = Theme.from_settings(self.gui_settings)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon(self.theme.accent))
        self.resize(1180, 760)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---------- sidebar ----------
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(64)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(8, 16, 8, 16)
        side.setSpacing(8)
        self.nav_buttons = []
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, (icon, tip) in enumerate(PAGES):
            btn = NavButton(icon, tip, self)
            btn.clicked.connect(lambda _=False, i=index: self.show_page(i))
            self.nav_group.addButton(btn)
            self.nav_buttons.append(btn)
            side.addWidget(btn, alignment=Qt.AlignHCenter)
        side.addStretch(1)
        for icon, tip, key in (("github", "GitHub", "github"),
                               ("alert", "Report an issue", "issues"),
                               ("coffee", "Buy me a coffee", "kofi")):
            btn = NavButton(icon, tip, self)
            btn.setCheckable(False)
            btn.clicked.connect(lambda _=False, k=key: QDesktopServices.openUrl(QUrl(LINKS[k])))
            self.nav_buttons.append(btn)
            side.addWidget(btn, alignment=Qt.AlignHCenter)
        layout.addWidget(sidebar)

        # ---------- pages ----------
        self.stack = QStackedWidget()
        self._make_status_bar()                    # before the pages: they report to it
        self.debug_page = DebugView(self)          # first page made, so the others can log
        self.home = HomeView(self)
        self.batch_page = BatchView(self)
        self.history_page = HistoryView(self)
        self.settings_page = SettingsView(self)
        self.about_page = AboutView(self)
        self.pages = (self.home, self.batch_page, self.history_page, self.settings_page,
                      self.debug_page, self.about_page)
        for page in self.pages:
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)

        # The downloader's own errors/warnings/notes show up in Debug Logs too.
        self.log_bridge = LogBridge(self)
        self.log_bridge.message.connect(self.log)

        self.nav_buttons[0].setChecked(True)
        self.apply_theme()
        self.history_page.render()
        self.update_status_bar()
        self.log(f"{APP_NAME} v{APP_VERSION} ready. Settings: {config_manager.config_path()}")
        QTimer.singleShot(300, self.startup_checks)

    # ==================== Status bar ====================
    def _make_status_bar(self):
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)
        self.status_message = QLabel("Ready")
        bar.addWidget(self.status_message, 1)
        self.status_queue = QLabel("")
        self.status_cookies = QLabel("")
        self.status_ytdlp = QLabel("")
        for widget in (self.status_queue, self.status_cookies, self.status_ytdlp):
            bar.addPermanentWidget(widget)
        self._ytdlp_version = None

    def set_status(self, text: str):
        self.status_message.setText(text)

    def update_status_bar(self, recheck_ytdlp: bool = False):
        """
        Refresh the permanent bits: retry queue, cookies, yt-dlp version. The version
        means running yt-dlp, so it's only looked up again when asked (after Settings
        change or an update).
        """
        if not hasattr(self, "status_queue"):
            return
        waiting = len(log_manager.read_failures())
        self.status_queue.setText(f"Retry queue: {waiting}")
        from gui.dialogs.cookie_dialog import active_cookie_file
        cfg = config_manager.load()
        if not cfg.get("use_cookies"):
            self.status_cookies.setText("Cookies: off")
        else:
            self.status_cookies.setText("Cookies: on" if active_cookie_file()
                                        else "Cookies: on (no file)")
        if recheck_ytdlp or self._ytdlp_version is None:
            from tools.dependency_check import program_version
            version, _ = program_version("yt-dlp")
            self._ytdlp_version = version or ""
        self.status_ytdlp.setText(f"yt-dlp {self._ytdlp_version}" if self._ytdlp_version
                                  else "yt-dlp: not found")

    # ==================== Helpers the pages use ====================
    def log(self, text: str):
        self.debug_page.append(text)

    def refresh_history(self):
        self.history_page.render()
        self.update_status_bar()

    def busy(self) -> bool:
        """Is anything downloading right now (Home, Batch or a retry run)?"""
        return self.home.busy() or self.batch_page.running() or self.history_page._retrying()

    def settings_changed(self):
        """Settings were saved or reset: refresh what shows them."""
        self.home._show_destination()
        self.update_status_bar()

    def show_page(self, index: int):
        self.stack.setCurrentIndex(index)
        if self.stack.currentWidget() is self.history_page:
            self.history_page.render()          # pick up downloads made meanwhile
        elif self.stack.currentWidget() is self.settings_page:
            self.settings_page.load()

    def apply_theme(self):
        QApplication.instance().setStyleSheet(self.theme.qss())
        self.setWindowIcon(app_icon(self.theme.accent))
        for btn in self.nav_buttons:
            btn.refresh()
        for page in self.pages:
            if hasattr(page, "refresh_icons"):
                page.refresh_icons()

    # ==================== Startup ====================
    def startup_checks(self):
        """Non-fatal checks, reported in Debug Logs (and once on screen if yt-dlp is missing)."""
        ok, problems = config_manager.validate_config()
        for problem in ([] if ok else problems[:5]):
            self.log(f"[config] {problem} (the default is used; fix it in Settings)")

        from tools.dependency_check import find_program
        missing = []
        for name in ("yt-dlp", "ffmpeg"):
            path, where = find_program(name)
            if path:
                self.log(f"[startup] {name}: {path} ({where})")
            else:
                missing.append(name)
                self.log(f"[startup] {name} {where}")
        if missing:
            self.home.status.setText(f"{' and '.join(missing)} not found - downloads won't work "
                                     "until it's installed (Settings → Check dependencies).")

        waiting = len(log_manager.read_failures())
        if waiting:
            self.log(f"[startup] {waiting} failed download(s) in the retry queue "
                     "(History → Retry queue)")

    # ==================== Closing ====================
    def closeEvent(self, event):
        if self.busy() and not messages.confirm(
                self, "Quit", "A download is still running. Quit once the tracks in progress "
                "have finished?\n(Tracks not started yet can be downloaded later.)",
                default_yes=False):
            event.ignore()
            return
        self.home.shutdown()
        self.batch_page.shutdown()
        self.history_page.shutdown()
        self.settings_page.shutdown()
        self.log_bridge.detach()
        event.accept()
