"""History: what was downloaded, what was looked up, and what's waiting to be retried.

Everything here is read from the project's own records (log_manager), so it
includes downloads made in the terminal program too.
"""

from datetime import datetime

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QHBoxLayout, QLineEdit,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from gui.dialogs import messages
from gui.resources import make_icon
from gui.views.widgets import button, label, plain_table, tab_button
from gui.workers import RetryWorker
from managers import log_manager

TABS = (("downloads", "Downloads"), ("fetches", "Fetches"), ("retry", "Retry queue"))
LIMIT = 1000                   # newest records shown per tab


def _when(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return timestamp or ""


def _title(entry: dict) -> str:
    meta = entry.get("metadata") or {}
    return entry.get("title") or meta.get("title") or entry.get("url", "")


class HistoryView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.tab = "downloads"
        self.retry_worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 36, 40, 24)
        outer.setSpacing(16)
        outer.addWidget(label("History", "H1"))

        tabs = QHBoxLayout()
        tabs.setSpacing(18)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.tab_buttons = {}
        for key, text in TABS:
            btn = tab_button(text)
            btn.clicked.connect(lambda _=False, k=key: self.switch(k))
            group.addButton(btn)
            tabs.addWidget(btn)
            self.tab_buttons[key] = btn
        self.tab_buttons["downloads"].setChecked(True)
        tabs.addStretch(1)
        self.retry_btn = button("  Retry all", self.retry_all, "accent",
                                tooltip="Try every queued link again, using your Settings")
        self.clear_btn = button("  Clear", self.clear)
        tabs.addWidget(self.retry_btn)
        tabs.addWidget(self.clear_btn)
        outer.addLayout(tabs)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search history…")
        self.search.textChanged.connect(self.render)
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All types", "Track", "Album", "Playlist", "Artist"])
        self.type_filter.currentIndexChanged.connect(self.render)
        self.type_filter.setFixedWidth(160)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.type_filter)
        outer.addLayout(filters)

        self.note = label("", "Faint", wrap=True)
        outer.addWidget(self.note)
        self.table = plain_table(["Type", "Title", "Status", "When"], stretch=(1,),
                                 selectable=True)
        outer.addWidget(self.table, 1)
        self.empty = label("Nothing here yet. Your activity will appear here.", "Faint")
        outer.addWidget(self.empty)

    def refresh_icons(self):
        theme = self.win.theme
        self.clear_btn.setIcon(make_icon("trash", theme.c["text_dim"], 16))
        self.retry_btn.setIcon(make_icon("retry", theme.on_accent, 16))

    def switch(self, tab: str):
        self.tab = tab
        self.render()

    # ==================== Data ====================
    def _rows(self):
        """(type, title, status, when, url, colour) for the current tab, newest first."""
        if self.tab == "downloads":
            entries = (log_manager.read_downloads("success", LIMIT)
                       + log_manager.read_downloads("failed", LIMIT))
            entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
            for e in entries:
                ok = e.get("status") == "success"
                status = "Downloaded" if ok else "Failed" + (f": {e['error']}" if e.get("error") else "")
                yield (e.get("item_type", ""), _title(e), status, _when(e.get("timestamp")),
                       e.get("url", ""), "downloaded" if ok else "failed")
        elif self.tab == "fetches":
            for e in log_manager.get_history(LIMIT):
                yield (e.get("type", ""), _title(e), "Looked up", _when(e.get("timestamp")),
                       e.get("url", ""), "")
        else:
            for e in log_manager.read_failures().values():
                attempts = int(e.get("attempt_count", 0) or 0)
                yield (e.get("item_type", ""), _title(e),
                       f"{attempts} failed attempt{'s' if attempts != 1 else ''}", "",
                       e.get("url", ""), "failed")

    def render(self):
        query = self.search.text().lower().strip()
        wanted = self.type_filter.currentText().lower()
        rows = []
        for row in self._rows():
            kind, title, status, when, url, colour = row
            if wanted != "all types" and kind != wanted:
                continue
            if query and query not in f"{title} {url}".lower():
                continue
            rows.append(row)

        in_retry = self.tab == "retry"
        self.retry_btn.setVisible(in_retry)
        self.retry_btn.setEnabled(in_retry and bool(rows) and not self._retrying())
        self.table.setHorizontalHeaderLabels(
            ["Type", "Title", "Status", "" if in_retry else "When"])
        self.note.setVisible(in_retry)
        if in_retry:
            self.note.setText("Downloads that failed wait here (data/failed_downloads.json). "
                              "Links that failed to throttling usually work after a wait.")

        self.table.setRowCount(0)
        self.empty.setVisible(not rows)
        self.table.setVisible(bool(rows))
        for kind, title, status, when, url, colour in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setRowHeight(r, 44)
            for col, text in enumerate((kind.title(), title, status, when)):
                item = QTableWidgetItem(text)
                item.setToolTip(url if col == 1 else text)
                if col == 2 and colour:
                    item.setForeground(QColor(self.win.theme.status_colour(colour)))
                self.table.setItem(r, col, item)

    # ==================== Actions ====================
    def clear(self):
        what = {"downloads": "the download records (success and failed)",
                "fetches": "the list of links you looked up",
                "retry": "the retry queue (those links won't be retried)"}[self.tab]
        if not messages.confirm(self, "Clear history", f"Delete {what}?", default_yes=False):
            return
        kinds = {"downloads": ("success", "failed"), "fetches": ("history",),
                 "retry": ("retry_queue",)}[self.tab]
        for kind in kinds:
            try:
                log_manager.clear_log(kind)
            except Exception as error:
                messages.error(self, "Clear history", f"Couldn't clear {kind}: {error}")
        self.render()

    def _retrying(self) -> bool:
        return bool(self.retry_worker and self.retry_worker.isRunning())

    def retry_all(self):
        if self._retrying():
            return
        if self.win.home.busy() or self.win.batch_page.running():
            messages.info(self, "Retry", "Wait for the current download to finish first.")
            return
        self.win.downloader.load_settings()
        self.retry_btn.setEnabled(False)
        self.retry_btn.setText("  Retrying…")
        self.retry_worker = RetryWorker()
        self.retry_worker.log.connect(self.win.log)
        self.retry_worker.finished_summary.connect(self._retry_done)
        self.retry_worker.start()

    def _retry_done(self, result):
        self.retry_btn.setText("  Retry all")
        self.render()
        text = (f"{result['recovered']} recovered, {result['still_failing']} still failing.")
        if result["stopped_reason"]:
            text += f"\nStopped: {result['stopped_reason']}."
        messages.info(self, "Retry finished", text)
        self.win.refresh_history()

    def shutdown(self):
        if self._retrying():
            self.retry_worker.wait()
