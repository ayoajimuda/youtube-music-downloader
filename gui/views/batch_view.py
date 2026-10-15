"""Batch: download a list of links, from files or pasted in.

Files (.txt, .csv, .json) are read with the same parser as the terminal
program's "download every link in a file", and get each result written back,
so loading the file again later only queues what's left. Pasted lines can be
links or "Artist - Title" searches.
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLineEdit, QPlainTextEdit,
                               QProgressBar, QScrollArea, QTableWidgetItem, QVBoxLayout, QWidget)

from downloader import batch_downloader
from downloader.base_downloader import is_youtube_url
from gui.dialogs import messages
from gui.resources import make_icon
from gui.views.widgets import Card, DropZone, StatCard, button, label, plain_table
from gui.workers.batch_worker import BatchWorker
from gui.workers.download_queue import (CANCELLED, DONE, DOWNLOADING, FAILED, PENDING,
                                        SEARCH_PREFIX, DownloadQueue)

STATUS_TEXT = {PENDING: "Waiting", DOWNLOADING: "Downloading…", DONE: "Downloaded",
               FAILED: "Failed", CANCELLED: "Cancelled"}
STATUS_COLOUR = {DONE: "downloaded", FAILED: "failed", DOWNLOADING: "downloading"}


def parse_pasted(text: str):
    """(entries, skipped). Each line: a YouTube link, or 'Artist - Title' to search for."""
    entries, skipped = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url = batch_downloader.extract_url(line)
        if url:
            if is_youtube_url(url):
                title = line.replace(url, "").strip(" -|,\t")
                entries.append({"url": url, "title": title})
            else:
                skipped += 1                         # a link, but not a YouTube one
        else:
            entries.append({"url": SEARCH_PREFIX + line, "title": line})
    return entries, skipped


class BatchView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.queue = DownloadQueue(self)
        self.worker = None
        self.rows = {}                      # item id -> table row

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        scroll.setWidget(host)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(host)
        outer.setContentsMargins(40, 36, 40, 24)
        outer.setSpacing(18)

        # ---------- header + counters ----------
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("Batch", "H1"))
        titles.addWidget(label("Download a list of links one at a time, from a file or pasted "
                               "in. Results are written back into the file.", "Sub", wrap=True))
        head.addLayout(titles, 1)
        self.pending_card = StatCard("0", "Waiting")
        self.done_card = StatCard("0", "Done", "downloaded")
        self.failed_card = StatCard("0", "Failed", "failed")
        for card in (self.pending_card, self.done_card, self.failed_card):
            head.addWidget(card)
        outer.addLayout(head)

        # ---------- inputs: file drop + paste ----------
        inputs = QHBoxLayout()
        inputs.setSpacing(16)
        self.drop = DropZone("Drop a .txt, .csv or .json file here",
                             "or click to browse. Links already marked done are skipped.")
        self.drop.files_dropped.connect(self.add_files)
        inputs.addWidget(self.drop, 1)

        paste_card = Card()
        paste = QVBoxLayout(paste_card)
        paste.setContentsMargins(16, 14, 16, 14)
        paste.setSpacing(8)
        paste.addWidget(label("Or paste links / searches, one per line", "H3"))
        self.paste_box = QPlainTextEdit()
        self.paste_box.setObjectName("Paste")
        self.paste_box.setPlaceholderText("https://music.youtube.com/watch?v=…\n"
                                          "Daft Punk - One More Time\n"
                                          "Muse - Starlight")
        self.paste_box.setMinimumHeight(80)
        self.paste_box.setMaximumHeight(120)
        paste.addWidget(self.paste_box, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.add_btn = button("  Add to list", self.add_pasted, "accent")
        row.addWidget(self.add_btn)
        paste.addLayout(row)
        inputs.addWidget(paste_card, 1)
        outer.addLayout(inputs)

        # ---------- controls ----------
        controls = Card()
        bar = QHBoxLayout(controls)
        bar.setContentsMargins(16, 12, 16, 12)
        bar.setSpacing(10)
        self.start_btn = button("  Start", self.start, "accent")
        self.pause_btn = button("  Pause", self.toggle_pause)
        self.cancel_btn = button("  Cancel", self.cancel, "danger",
                                 tooltip="Finish the current link, then stop")
        bar.addWidget(self.start_btn)
        bar.addWidget(self.pause_btn)
        bar.addWidget(self.cancel_btn)
        bar.addSpacing(12)
        bar.addWidget(label("Save into subfolder:", "Faint"))
        self.subfolder = QLineEdit()
        self.subfolder.setPlaceholderText("(the music folder itself)")
        self.subfolder.setToolTip("Leave blank to save straight into the download folder. "
                                  "Filled in with the file's name when you add a file.")
        bar.addWidget(self.subfolder, 1)
        bar.addSpacing(12)
        self.retry_btn = button("  Retry failed", self.retry_failed)
        self.clear_done_btn = button("Clear done", self.clear_done)
        self.clear_all_btn = button("Clear all", self.clear_all, "danger")
        for widget in (self.retry_btn, self.clear_done_btn, self.clear_all_btn):
            bar.addWidget(widget)
        outer.addWidget(controls)

        # ---------- the list ----------
        self.table = plain_table(["#", "Title / link", "From", "Status"], stretch=(1,))
        self.table.setMinimumHeight(220)
        outer.addWidget(self.table, 1)
        self.empty = label("The list is empty. Add a file or paste some links above.", "Faint")
        self.empty.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.empty)

        # ---------- overall progress ----------
        progress_card = Card()
        progress = QHBoxLayout(progress_card)
        progress.setContentsMargins(20, 14, 20, 14)
        progress.setSpacing(16)
        progress.addWidget(label("Overall progress", "H3"))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        progress.addWidget(self.progress, 1)
        self.percent = label("0%", "Accent")
        progress.addWidget(self.percent)
        outer.addWidget(progress_card)
        self.status = label("", "Faint", wrap=True)
        outer.addWidget(self.status)

        self.queue.item_added.connect(self._on_added)
        self.queue.item_updated.connect(self._on_updated)
        self.queue.cleared.connect(self._rebuild)
        self.queue.counts_changed.connect(self._on_counts)
        self._on_counts(self.queue.counts())

    # ==================== Theme ====================
    def refresh_icons(self):
        theme = self.win.theme
        dim = theme.c["text_dim"]
        self.drop.refresh(theme)
        for card in (self.pending_card, self.done_card, self.failed_card):
            card.refresh(theme)
        self.add_btn.setIcon(make_icon("plus", theme.on_accent, 16))
        self.start_btn.setIcon(make_icon("download", theme.on_accent, 16))
        paused = bool(self.worker and self.worker.is_paused)
        self.pause_btn.setIcon(make_icon("play" if paused else "pause", dim, 16))
        self.cancel_btn.setIcon(make_icon("x", "#ef4444", 16))
        self.retry_btn.setIcon(make_icon("retry", dim, 16))
        self._rebuild()

    # ==================== Adding ====================
    def add_files(self, files):
        added = already = invalid = 0
        for name in files:
            path = Path(name)
            try:
                entries = batch_downloader.parse(path)
            except Exception as error:
                messages.error(self, "Batch", f"Couldn't read {path.name}:\n{error}")
                continue
            for entry in entries:
                url = entry.get("url") or ""
                if not is_youtube_url(url):
                    invalid += 1
                elif entry.get("status") == "success":
                    already += 1
                elif self.queue.add(url, entry.get("title", ""), str(path.resolve())):
                    added += 1
            if not self.subfolder.text().strip():
                self.subfolder.setText(path.stem)
        parts = [f"Added {added} link{'s' if added != 1 else ''}"]
        if already:
            parts.append(f"{already} already marked done in the file")
        if invalid:
            parts.append(f"{invalid} not YouTube links (skipped)")
        self._say(", ".join(parts) + ".")

    def add_pasted(self):
        entries, skipped = parse_pasted(self.paste_box.toPlainText())
        if not entries:
            self._say("Nothing to add: paste YouTube links or 'Artist - Title' lines.")
            return
        added = sum(1 for e in entries if self.queue.add(e["url"], e["title"]))
        self.paste_box.clear()
        searches = sum(1 for e in entries if e["url"].startswith(SEARCH_PREFIX))
        text = f"Added {added} line{'s' if added != 1 else ''}"
        if searches:
            text += f" ({searches} as YouTube searches)"
        if skipped:
            text += f"; {skipped} non-YouTube link{'s' if skipped != 1 else ''} skipped"
        self._say(text + ".")

    # ==================== Table ====================
    def _row_cells(self, number, item):
        source = Path(item.source).name if item.source else "pasted"
        return [str(number), item.label, source, STATUS_TEXT.get(item.status, item.status)]

    def _paint_row(self, row, item):
        number = row + 1
        for col, text in enumerate(self._row_cells(number, item)):
            cell = QTableWidgetItem(text)
            if col == 1:
                cell.setToolTip(item.url if not item.error else f"{item.url}\n{item.error}")
            if col == 3:
                if item.status in STATUS_COLOUR:
                    cell.setForeground(QColor(self.win.theme.status_colour(STATUS_COLOUR[item.status])))
                else:
                    cell.setForeground(QColor(self.win.theme.c["text_faint"]))
                if item.error:
                    cell.setToolTip(item.error)
            self.table.setItem(row, col, cell)

    def _on_added(self, item):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 40)
        self.rows[item.id] = row
        self._paint_row(row, item)

    def _on_updated(self, item):
        row = self.rows.get(item.id)
        if row is not None:
            self._paint_row(row, item)

    def _rebuild(self):
        self.table.setRowCount(0)
        self.rows = {}
        for item in self.queue.items:
            self._on_added(item)

    def _on_counts(self, counts):
        self.pending_card.set_value(counts[PENDING] + counts[DOWNLOADING])
        self.done_card.set_value(counts[DONE])
        self.failed_card.set_value(counts[FAILED])
        total = counts["total"]
        finished = counts[DONE] + counts[FAILED] + counts[CANCELLED]
        percent = int(finished * 100 / total) if total else 0
        self.progress.setValue(percent)
        self.percent.setText(f"{percent}%")
        self.empty.setVisible(total == 0)
        self.table.setVisible(total > 0)
        self._update_buttons()
        self.win.update_status_bar()

    # ==================== Running ====================
    def running(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def _update_buttons(self):
        running = self.running()
        counts = self.queue.counts()
        self.start_btn.setEnabled(not running and counts[PENDING] > 0)
        self.pause_btn.setEnabled(running)
        self.cancel_btn.setEnabled(running)
        self.retry_btn.setEnabled(counts[FAILED] + counts[CANCELLED] > 0)
        self.clear_done_btn.setEnabled(counts[DONE] + counts[FAILED] + counts[CANCELLED] > 0)
        self.clear_all_btn.setEnabled(counts["total"] > 0 and not running)
        self.subfolder.setEnabled(not running)

    def start(self):
        if self.running() or not self.queue.has_pending():
            return
        if self.win.home.busy() or self.win.history_page._retrying():
            messages.info(self, "Batch", "Wait for the download on Home to finish first, so "
                          "the two don't compete (that's what gets YouTube throttling you).")
            return
        self.win.downloader.load_settings()
        self.worker = BatchWorker(self.win.downloader, self.queue, self.subfolder.text())
        self.worker.log.connect(self.win.log)
        self.worker.paused_changed.connect(self._on_paused)
        self.worker.finished_stats.connect(self._on_finished)
        self.worker.start()
        self._say("Downloading…")
        self._update_buttons()

    def toggle_pause(self):
        if not self.running():
            return
        if self.worker.is_paused:
            self.worker.resume()
        else:
            self.worker.pause()

    def _on_paused(self, paused):
        self.pause_btn.setText("  Resume" if paused else "  Pause")
        self.pause_btn.setIcon(make_icon("play" if paused else "pause",
                                         self.win.theme.c["text_dim"], 16))
        self._say("Paused after the current link." if paused else "Downloading…")

    def cancel(self):
        if self.running() and messages.confirm(
                self, "Cancel", "Stop after the current link?\n(The rest stay in the list.)",
                default_yes=False):
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self._say("Stopping after the current link…")

    def _on_finished(self, stats):
        self.pause_btn.setText("  Pause")
        self.worker.wait()
        self._update_buttons()
        text = f"Finished: {stats['done']} downloaded, {stats['failed']} failed"
        if stats["failed"]:
            text += " (also in the retry queue)"
        text += "."
        if stats["missing_ytdlp"]:
            text = "yt-dlp isn't installed, so nothing could be downloaded. See Settings → Dependencies."
        elif stats["throttled_stop"]:
            text += " Stopped: YouTube is throttling. Wait a while, then press Start again."
        elif stats["cancelled"]:
            text += " Cancelled; the rest are still waiting."
        self._say(text)
        self.win.log("[batch] " + text)
        self.win.refresh_history()
        self.refresh_icons()

    def retry_failed(self):
        count = self.queue.retry_failed()
        self._say(f"{count} link{'s' if count != 1 else ''} back in line. Press Start.")

    def clear_done(self):
        self.queue.clear_finished()

    def clear_all(self):
        if messages.confirm(self, "Clear the list", "Remove every link from the list?\n"
                            "(Files and downloads aren't touched.)", default_yes=False):
            self.queue.clear_all()

    def _say(self, text):
        self.status.setText(text)
        self.win.set_status(text)

    def shutdown(self):
        if self.running():
            self.worker.cancel()
            self.worker.wait()
