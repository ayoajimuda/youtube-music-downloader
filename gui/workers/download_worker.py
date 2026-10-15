"""Download tracks through the project's downloader, reporting each one to the GUI.

Each track goes through YoutubeMusicDownloader._download_with_retry(), the same
path the terminal program uses for playlists, so the GUI gets everything that
comes with it: Settings (format, bitrate, retries, timeouts, pacing, cookies),
the shared rate limiter, the download logs, and the retry queue for failures.

Collections (albums, playlists, channels) also use the playlist archive in
history/archives/, so tracks already downloaded are skipped, whether they were
downloaded here or in the terminal program.
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from downloader import base_downloader, rate_limiter
from downloader.base_downloader import STOP_AFTER_THROTTLED, safe_name
from managers import log_manager

COLLECTIONS = ("album", "playlist", "artist")


def output_template(output_dir: Path, kind: str, title: str) -> str:
    """Where files go: the same layout as the terminal program."""
    out = Path(output_dir)
    if kind == "album":
        return str(out / "%(artist)s" / "%(album)s" / "%(artist)s - %(title)s.%(ext)s")
    if kind == "playlist":
        return str(out / safe_name(title or "Playlist", "Playlist") / "%(artist)s - %(title)s.%(ext)s")
    if kind == "artist":
        return str(out / safe_name(title or "Channel", "Channel") / "%(title)s.%(ext)s")
    return str(out / "%(artist)s - %(title)s.%(ext)s")


class DownloadWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int)               # finished, total
    track_status = Signal(str, str)           # track url, downloading/downloaded/skipped/failed
    finished_stats = Signal(dict)

    def __init__(self, downloader, meta: dict, tracks: Optional[List[dict]] = None):
        """
        meta    what MetadataWorker returned
        tracks  the tracks to download (default: every available one in meta)
        """
        super().__init__()
        self.downloader = downloader
        self.meta = meta
        self.tracks = [t for t in (tracks if tracks is not None else meta["tracks"])
                       if t.get("available", True)]
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()                   # set = running, clear = paused

    def stop(self):
        """Finish the downloads in progress, start no new ones."""
        self._stop.set()
        self._resume.set()                   # a paused run has to wake up to stop

    @property
    def is_paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self):
        """Let the tracks in progress finish, then wait before starting more."""
        self._resume.clear()

    def resume(self):
        self._resume.set()

    # ------------------------------------------------------------------
    def run(self):
        dl = self.downloader
        kind, title = self.meta["kind"], self.meta["title"]
        template = output_template(dl.output_directory, kind, title)
        archive = dl._archive_path(self.meta["source_url"]) if kind in COLLECTIONS else None
        done_ids = dl._read_archive(archive)

        stats = dict(succeeded=0, skipped=0, failed=0, not_tried=0, throttled=False,
                     stopped=False, total=len(self.tracks))
        todo = []
        for track in self.tracks:
            if track.get("id") and track["id"] in done_ids:
                stats["skipped"] += 1
                self.track_status.emit(track["url"], "skipped")
            else:
                todo.append(track)
        finished = stats["skipped"]
        self.progress.emit(finished, stats["total"])
        if stats["skipped"]:
            self.log.emit(f"[download] {stats['skipped']} already downloaded earlier - skipped")
        if not todo:
            self.finished_stats.emit(stats)
            return

        workers = max(1, int(getattr(dl, "max_concurrent", 1) or 1)) if len(todo) > 1 else 1
        self.log.emit(f"[download] {len(todo)} track(s) into {Path(template).parent}"
                      f" ({workers} at a time)")
        streak = 0

        def work(track):
            self._resume.wait()               # paused: wait here
            if self._stop.is_set():
                return track, None, "", False
            rate_limiter.acquire()            # shared pacing with every other download
            if self._stop.is_set():
                return track, None, "", False
            self.track_status.emit(track["url"], "downloading")
            ok, error, throttled = dl._download_with_retry(
                track["url"], template, ["--no-playlist"], item_type="track",
                show_progress=False,
                metadata={"title": track.get("name"), "artist": track.get("artists")})
            return track, ok, error, throttled

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [pool.submit(work, track) for track in todo]
            for future in as_completed(futures):
                try:
                    track, ok, error, throttled = future.result()
                except RuntimeError as error:          # yt-dlp missing: stop everything
                    self.log.emit(f"[error] {error}")
                    self._stop.set()
                    continue
                if ok is None:                         # not started because of a stop
                    stats["not_tried"] += 1
                    continue
                name = track.get("name") or track["url"]
                finished += 1
                if ok:
                    streak = 0
                    stats["succeeded"] += 1
                    dl._add_to_archive(archive, track.get("id"))
                    self.track_status.emit(track["url"], "downloaded")
                    self.log.emit(f"[done] {name}")
                else:
                    stats["failed"] += 1
                    log_manager.record_failure(track["url"], name, error, "",
                                               throttled=throttled, item_type="track")
                    self.track_status.emit(track["url"], "failed")
                    self.log.emit(f"[failed] {name}: {error}")
                    if throttled:
                        stats["throttled"] = True
                        streak += 1
                        dl._penalize(dl._backoff_seconds(streak))
                        if streak >= STOP_AFTER_THROTTLED and not self._stop.is_set():
                            self.log.emit(f"[download] Throttled {streak} times in a row - "
                                          "stopping. The rest can be downloaded later.")
                            self._stop.set()
                    else:
                        streak = 0
                self.progress.emit(finished, stats["total"])
        finally:
            pool.shutdown(wait=True)

        stats["stopped"] = self._stop.is_set()
        if stats["succeeded"] and kind in COLLECTIONS:
            try:
                base_downloader.cleanup_directory(dl.output_directory, log_manager)
            except Exception:
                pass
        self.finished_stats.emit(stats)
