"""The Batch page's list of links to download, with signals so the table keeps up.

The BatchWorker takes items with claim_next() and reports back with
set_status() from its own thread; a lock keeps the list consistent, and Qt
delivers the signals to the GUI thread.
"""

import threading
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

PENDING, DOWNLOADING, DONE, FAILED, CANCELLED = (
    "pending", "downloading", "done", "failed", "cancelled")

SEARCH_PREFIX = "ytsearch1:"        # "Artist - Title" lines become a YouTube search


@dataclass
class QueueItem:
    url: str                         # a YouTube link, or ytsearch1:<query>
    title: str = ""
    source: str = ""                 # the batch file it came from ("" = pasted)
    status: str = PENDING
    error: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        return self.url[len(SEARCH_PREFIX):] if self.url.startswith(SEARCH_PREFIX) else self.url


class DownloadQueue(QObject):
    item_added = Signal(object)          # QueueItem
    item_updated = Signal(object)        # QueueItem
    cleared = Signal()
    counts_changed = Signal(dict)        # {pending, downloading, done, failed, total}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[QueueItem] = []
        self._lock = threading.RLock()

    # ---------- reading ----------
    @property
    def items(self) -> List[QueueItem]:
        return list(self._items)

    def counts(self) -> dict:
        with self._lock:
            counts = {s: 0 for s in (PENDING, DOWNLOADING, DONE, FAILED, CANCELLED)}
            for item in self._items:
                counts[item.status] += 1
            counts["total"] = len(self._items)
            return counts

    def has_pending(self) -> bool:
        return any(i.status == PENDING for i in self._items)

    def next_pending(self) -> Optional[QueueItem]:
        return next((i for i in self._items if i.status == PENDING), None)

    def get(self, item_id: str) -> Optional[QueueItem]:
        return next((i for i in self._items if i.id == item_id), None)

    def claim_next(self) -> Optional[QueueItem]:
        """Mark the next pending item as downloading and return it (worker thread)."""
        with self._lock:
            item = self.next_pending()
            if item:
                item.status = DOWNLOADING
        if item:
            self.item_updated.emit(item)
            self._changed()
        return item

    # ---------- changing ----------
    def add(self, url: str, title: str = "", source: str = "") -> Optional[QueueItem]:
        """Add a link (ignored if the same link is already queued and not finished)."""
        with self._lock:
            if any(i.url == url and i.status in (PENDING, DOWNLOADING) for i in self._items):
                return None
            item = QueueItem(url=url, title=title, source=source)
            self._items.append(item)
        self.item_added.emit(item)
        self._changed()
        return item

    def set_status(self, item_id: str, status: str, error: str = "") -> None:
        with self._lock:
            item = self.get(item_id)
            if item:
                item.status = status
                item.error = error
        if item:
            self.item_updated.emit(item)
            self._changed()

    def retry_failed(self) -> int:
        """Put failed and cancelled items back in line."""
        with self._lock:
            changed = [i for i in self._items if i.status in (FAILED, CANCELLED)]
            for item in changed:
                item.status, item.error = PENDING, ""
        for item in changed:
            self.item_updated.emit(item)
        count = len(changed)
        self._changed()
        return count

    def cancel_pending(self) -> None:
        with self._lock:
            changed = [i for i in self._items if i.status == PENDING]
            for item in changed:
                item.status = CANCELLED
        for item in changed:
            self.item_updated.emit(item)
        self._changed()

    def clear_finished(self) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.status in (PENDING, DOWNLOADING)]
        self.cleared.emit()
        self._changed()

    def clear_all(self) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.status == DOWNLOADING]
        self.cleared.emit()
        self._changed()

    def _changed(self) -> None:
        self.counts_changed.emit(self.counts())
