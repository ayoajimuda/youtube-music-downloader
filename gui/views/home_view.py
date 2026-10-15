"""Home: paste a link, see what it is, download it (all of it, or one track)."""

from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                               QPushButton, QTableWidgetItem, QVBoxLayout, QWidget)

from downloader.base_downloader import is_youtube_url
from gui.dialogs import messages
from gui.resources import make_icon
from gui.views.widgets import Card, button, label, plain_table
from gui.workers import DownloadWorker, ImageWorker, MetadataWorker, output_template
from gui.workers.download_worker import COLLECTIONS

LARGE_COLLECTION = 50          # ask before downloading more than this many tracks
STATUS_TEXT = {"downloading": "Downloading…", "downloaded": "Downloaded",
               "skipped": "Already have it", "failed": "Failed (queued for retry)",
               "unavailable": "Unavailable"}


class HomeView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.meta = None
        self.rows = {}                 # track url -> table row
        self.statuses = {}             # track url -> last status
        self.meta_worker = None
        self.img_worker = None
        self.dl_worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 36, 40, 24)
        outer.setSpacing(18)
        outer.addWidget(label(win.app_name, "H1"))
        outer.addWidget(label("Paste a YouTube or YouTube Music link: a track, album, playlist "
                              "or channel. Downloads use your Settings and the same folders, "
                              "logs and retry queue as the terminal program.", "Sub", wrap=True))

        # Link bar
        row = QHBoxLayout()
        row.setSpacing(10)
        self.search_icon = QLabel()
        self.search_icon.setFixedWidth(20)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://music.youtube.com/playlist?list=…")
        self.url_edit.returnPressed.connect(self.fetch)
        self.fetch_btn = button("  Fetch", self.fetch, "accent")
        row.addWidget(self.search_icon)
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.fetch_btn)
        outer.addLayout(row)

        # Status + progress
        self.status = label("", "Faint", wrap=True)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.status)
        outer.addWidget(self.progress)

        # Details card
        self.meta_card = Card()
        self.meta_card.setVisible(False)
        card = QHBoxLayout(self.meta_card)
        card.setContentsMargins(18, 18, 18, 18)
        card.setSpacing(18)
        self.cover = QFrame()
        self.cover.setObjectName("Cover")
        self.cover.setFixedSize(150, 150)
        cover_layout = QVBoxLayout(self.cover)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        self.cover_img = QLabel()
        self.cover_img.setAlignment(Qt.AlignCenter)
        self.cover_img.setScaledContents(True)
        cover_layout.addWidget(self.cover_img)
        card.addWidget(self.cover)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.kind_lbl = label("PLAYLIST", "Accent")
        self.meta_title = label("—", "H2", wrap=True)
        self.meta_sub = label("", "Sub")
        self.meta_count = label("", "Faint")
        self.meta_dest = label("", "Faint", wrap=True)
        buttons = QHBoxLayout()
        self.dl_all = button("  Download All", self.download_all, "accent")
        self.pause_btn = button("  Pause", self.toggle_pause,
                                tooltip="Finish the tracks in progress, then wait")
        self.pause_btn.setVisible(False)
        self.stop_btn = button("  Stop", self.stop_download, "danger",
                               tooltip="Finish the tracks in progress, start no new ones")
        self.stop_btn.setVisible(False)
        self.open_btn = button("Open on YouTube", self.open_source)
        self.folder_btn = button("  Open folder", self.open_folder)
        buttons.addWidget(self.dl_all)
        buttons.addWidget(self.pause_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.folder_btn)
        buttons.addStretch(1)
        for widget in (self.kind_lbl, self.meta_title, self.meta_sub, self.meta_count,
                       self.meta_dest):
            info.addWidget(widget)
        info.addStretch(1)
        info.addLayout(buttons)
        card.addLayout(info, 1)
        outer.addWidget(self.meta_card)

        # Tracks
        self.table = plain_table(["#", "Title", "Album", "Duration", "Status", ""], stretch=(1, 2))
        self.table.setVisible(False)
        outer.addWidget(self.table, 1)
        # Keeps everything at the top while there's no table; with stretch 0 it gives way
        # to the table (stretch 1) once that's shown.
        outer.addStretch(0)

    # ==================== Theme ====================
    def refresh_icons(self):
        theme = self.win.theme
        self.search_icon.setPixmap(make_icon("search", theme.c["text_faint"], 20).pixmap(20, 20))
        self.fetch_btn.setIcon(make_icon("fetch", theme.on_accent, 18))
        self.dl_all.setIcon(make_icon("download", theme.on_accent, 18))
        self.stop_btn.setIcon(make_icon("stop", "#ef4444", 16))
        paused = bool(self.dl_worker and self.dl_worker.is_paused)
        self.pause_btn.setIcon(make_icon("play" if paused else "pause", theme.c["text_dim"], 16))
        self.folder_btn.setIcon(make_icon("folder", theme.c["text_dim"], 16))
        if self.meta:
            self._fill_table(self.meta["tracks"])

    # ==================== Fetch ====================
    def busy(self) -> bool:
        return bool(self.dl_worker and self.dl_worker.isRunning())

    def fetch(self):
        url = self.url_edit.text().strip().strip('"').strip("'")
        if not is_youtube_url(url):
            self.status.setText("Enter a YouTube or YouTube Music link.")
            return
        if self.meta_worker and self.meta_worker.isRunning():
            return
        self.status.setText("Reading the link…")
        self.fetch_btn.setEnabled(False)
        self.meta_worker = MetadataWorker(self.win.downloader, url)
        self.meta_worker.log.connect(self.win.log)
        self.meta_worker.done.connect(self._meta_done)
        self.meta_worker.error.connect(self._meta_error)
        self.meta_worker.start()

    def _meta_error(self, message):
        self.fetch_btn.setEnabled(True)
        self.status.setText(message)
        self.win.log("[error] " + message)

    def _meta_done(self, meta):
        self.fetch_btn.setEnabled(True)
        if self.busy():
            self.status.setText("Fetched. Wait for the current download to finish before "
                                "starting this one.")
        else:
            self.status.setText("Ready to download.")
        self.meta = meta
        self.statuses = {}
        self.kind_lbl.setText(meta["kind"].upper())
        self.meta_title.setText(meta["title"])
        self.meta_sub.setText(meta["subtitle"])
        count = f"{meta['count']} track{'s' if meta['count'] != 1 else ''}"
        if meta["unavailable"]:
            count += f" ({meta['unavailable']} unavailable)"
        self.meta_count.setText(count)
        self.dl_all.setText("  Download" if meta["count"] == 1 else "  Download All")
        self._show_destination()
        self.meta_card.setVisible(True)
        self.cover_img.clear()
        if meta["cover_url"]:
            self.img_worker = ImageWorker(meta["cover_url"])
            self.img_worker.loaded.connect(self._cover_loaded)
            self.img_worker.start()
        self._fill_table(meta["tracks"])
        self.win.refresh_history()

    def _show_destination(self):
        if not self.meta:
            return
        template = output_template(self.win.downloader.output_directory,
                                   self.meta["kind"], self.meta["title"])
        folder = template.split("%(")[0].rstrip("/\\")      # the part before the file-name pattern
        self.meta_dest.setText(f"Saves to {folder}  ·  {self.win.downloader.audio_format} "
                               f"{self.win.downloader.audio_quality}")

    def _cover_loaded(self, data):
        pixmap = QPixmap()
        if pixmap.loadFromData(QByteArray(data)):
            self.cover_img.setPixmap(pixmap)

    # ==================== Track table ====================
    def _fill_table(self, tracks):
        theme = self.win.theme
        self.table.setRowCount(0)
        self.table.setVisible(True)
        self.rows = {}
        faint = QColor(theme.c["text_faint"])
        for number, track in enumerate(tracks, 1):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, 52)
            self.rows[track["url"]] = row
            name = track["name"] + (f"\n{track['artists']}" if track["artists"] else "")
            cells = [str(number), name, track["album"], track["duration"], ""]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not track["available"]:
                    item.setForeground(faint)
                self.table.setItem(row, col, item)
            if track["available"]:
                btn = QPushButton()
                btn.setProperty("variant", "dlbtn")
                btn.setIcon(make_icon("download", theme.on_accent, 16))
                btn.setFixedSize(34, 30)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setToolTip("Download this track")
                btn.clicked.connect(lambda _=False, t=track: self.download_one(t))
                wrap = QWidget()
                wrap_layout = QHBoxLayout(wrap)
                wrap_layout.setContentsMargins(4, 0, 8, 0)
                wrap_layout.addWidget(btn)
                self.table.setCellWidget(row, 5, wrap)
                if track["url"] in self.statuses:
                    self._set_status(track["url"], self.statuses[track["url"]])
            else:
                self._set_status(track["url"], "unavailable")

    def _set_status(self, url, status):
        self.statuses[url] = status
        row = self.rows.get(url)
        if row is None:
            return
        item = QTableWidgetItem(STATUS_TEXT.get(status, status))
        colour = (self.win.theme.c["text_faint"] if status == "unavailable"
                  else self.win.theme.status_colour(status))
        item.setForeground(QColor(colour))
        self.table.setItem(row, 4, item)

    # ==================== Download ====================
    def download_all(self):
        if not self.meta:
            return
        available = [t for t in self.meta["tracks"] if t["available"]]
        if not available:
            self.status.setText("Nothing to download: every track here is unavailable.")
            return
        if len(available) > LARGE_COLLECTION and not messages.confirm(
                self, "Large download",
                f"This {self.meta['kind']} has {len(available)} tracks. That can take a while "
                "and makes YouTube throttling more likely.\n\nDownload them all?"):
            return
        self._start(available)

    def download_one(self, track):
        if self.meta:
            self._start([track])

    def _start(self, tracks):
        if self.busy():
            self.status.setText("A download is already running.")
            return
        if self.win.batch_page.running() or self.win.history_page._retrying():
            self.status.setText("Wait for the Batch download or retry run to finish first.")
            return
        self.win.downloader.load_settings()      # pick up any change made in Settings
        self._show_destination()
        total = len(tracks)
        self.status.setText(f"Downloading {self.meta['title']}…")
        self.progress.setVisible(True)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.dl_all.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setVisible(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("  Pause")
        self.dl_worker = DownloadWorker(self.win.downloader, self.meta, tracks)
        self.dl_worker.log.connect(self.win.log)
        self.dl_worker.progress.connect(self._on_progress)
        self.dl_worker.track_status.connect(self._set_status)
        self.dl_worker.finished_stats.connect(self._on_finished)
        self.dl_worker.start()

    def stop_download(self):
        if self.busy():
            self.dl_worker.stop()
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.status.setText("Stopping after the tracks in progress…")

    def toggle_pause(self):
        if not self.busy():
            return
        dim = self.win.theme.c["text_dim"]
        if self.dl_worker.is_paused:
            self.dl_worker.resume()
            self.pause_btn.setText("  Pause")
            self.pause_btn.setIcon(make_icon("pause", dim, 16))
            self.status.setText("Downloading…")
        else:
            self.dl_worker.pause()
            self.pause_btn.setText("  Resume")
            self.pause_btn.setIcon(make_icon("play", dim, 16))
            self.status.setText("Paused: the tracks in progress will finish, then it waits.")
        self.win.set_status(self.status.text())

    def _on_progress(self, finished, total):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(finished)
        if not (self.dl_worker and (self.dl_worker._stop.is_set() or self.dl_worker.is_paused)):
            self.status.setText(f"Downloading… {finished}/{total}")
        self.win.set_status(f"Downloading {self.meta['title']}: {finished}/{total}")

    def _on_finished(self, stats):
        self.dl_all.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.pause_btn.setVisible(False)
        self.progress.setVisible(False)
        parts = [f"{stats['succeeded']} downloaded"]
        if stats["skipped"]:
            parts.append(f"{stats['skipped']} already downloaded")
        if stats["failed"]:
            parts.append(f"{stats['failed']} failed (added to the retry queue)")
        if stats["not_tried"]:
            parts.append(f"{stats['not_tried']} not started")
        message = "Done: " + ", ".join(parts) + "."
        if stats["throttled"]:
            message += (" YouTube is throttling: wait a while, then retry from History → "
                        "Retry queue. Cookies help too.")
        self.status.setText(message)
        self.win.set_status(message)
        self.win.log("[download] " + message)
        self.win.refresh_history()

    # ==================== Links ====================
    def open_source(self):
        if self.meta:
            QDesktopServices.openUrl(QUrl(self.meta["source_url"]))

    def open_folder(self):
        folder = self.win.downloader.output_directory
        if self.meta and self.meta["kind"] in COLLECTIONS and self.meta["kind"] != "album":
            template = output_template(folder, self.meta["kind"], self.meta["title"])
            candidate = template.split("%(")[0].rstrip("/\\")
            if Path(candidate).is_dir():
                folder = Path(candidate)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def shutdown(self):
        """Called when the window closes: let a running download finish its current tracks."""
        if self.busy():
            self.dl_worker.stop()
            self.dl_worker.wait()
