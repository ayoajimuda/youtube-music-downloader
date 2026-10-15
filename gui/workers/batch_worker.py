"""Download the Batch page's queue, one link at a time.

Works like the terminal program's "download every link in a file":
  - strictly one at a time (many requests at once is what gets you throttled)
  - each link goes through the downloader's _download_with_retry(), so Settings,
    cookies, the rate limiter and the download logs all apply
  - links that came from a file get "success"/"failed" written back into it,
    so loading the file again carries on where it stopped
  - failures go into the retry queue; successes leave it
  - three throttled links in a row stops the run (the rest stay pending)

Pause finishes the current link and waits; Cancel finishes it and stops.
"""

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from downloader import batch_downloader, rate_limiter
from downloader.base_downloader import STOP_AFTER_THROTTLED, safe_name
from gui.workers.download_queue import DONE, FAILED, PENDING
from managers import log_manager


class BatchWorker(QThread):
    log = Signal(str)
    paused_changed = Signal(bool)
    finished_stats = Signal(dict)

    def __init__(self, downloader, queue, subfolder: str = ""):
        super().__init__()
        self.downloader = downloader
        self.queue = queue
        self.subfolder = subfolder.strip()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()                      # set = running, clear = paused

    # ---------- controls (GUI thread) ----------
    @property
    def is_paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self):
        self._resume.clear()
        self.paused_changed.emit(True)

    def resume(self):
        self._resume.set()
        self.paused_changed.emit(False)

    def cancel(self):
        self._stop.set()
        self._resume.set()                      # wake it if paused, so it can stop

    # ---------- work ----------
    def _template(self) -> str:
        folder = Path(self.downloader.output_directory)
        if self.subfolder:
            folder = folder / safe_name(self.subfolder, "Batch")
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / "%(artist)s - %(title)s.%(ext)s")

    def run(self):
        dl = self.downloader
        template = self._template()
        stats = dict(done=0, failed=0, throttled_stop=False, cancelled=False, missing_ytdlp=False)
        streak = 0
        self.log.emit(f"[batch] saving to {Path(template).parent}")

        while not self._stop.is_set():
            self._resume.wait()                 # paused: wait here between links
            if self._stop.is_set():
                break
            item = self.queue.claim_next()
            if item is None:
                break

            rate_limiter.acquire()
            self.log.emit(f"[batch] {item.label}")
            try:
                ok, error, throttled = dl._download_with_retry(
                    item.url, template, ["--no-playlist"], item_type="track",
                    show_progress=False, metadata={"title": item.title} if item.title else None)
            except RuntimeError as exc:         # yt-dlp is missing: nothing else will work
                self.queue.set_status(item.id, PENDING)
                self.log.emit(f"[error] {exc}")
                stats["missing_ytdlp"] = True
                break

            if ok:
                streak = 0
                stats["done"] += 1
                self.queue.set_status(item.id, DONE)
                log_manager.clear_failures([item.url])
            else:
                stats["failed"] += 1
                self.queue.set_status(item.id, FAILED, error)
                log_manager.record_failure(item.url, item.title or item.label, error,
                                           item.source, throttled=throttled, item_type="track")
                self.log.emit(f"[failed] {item.label}: {error}")
                if throttled:
                    streak += 1
                    dl._penalize(dl._backoff_seconds(streak))
                    if streak >= STOP_AFTER_THROTTLED:
                        stats["throttled_stop"] = True
                        self.log.emit(f"[batch] Throttled {streak} times in a row - stopping. "
                                      "The rest stay in the list; start again later.")
                        break
                else:
                    streak = 0

            if item.source:                     # written per link, like the terminal version
                try:
                    batch_downloader.mark_statuses(Path(item.source),
                                                   {item.url: "success" if ok else "failed"})
                except Exception as exc:
                    self.log.emit(f"[batch] couldn't update {Path(item.source).name}: {exc}")

        stats["cancelled"] = self._stop.is_set()
        self.finished_stats.emit(stats)
