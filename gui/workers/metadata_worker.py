"""Read a link's details (title, cover, track list) without downloading anything.

Uses the downloader's own fetch_info(), so it follows the same Settings as the
terminal program: the yt-dlp location, cookies and the bundled ffmpeg.
"""

import re

from PySide6.QtCore import QThread, Signal

from downloader.base_downloader import UNAVAILABLE_TITLES
from managers import log_manager


# ==================== Link helpers ====================
def extract_list_id(url: str):
    match = re.search(r"[?&]list=([\w-]+)", url or "")
    return match.group(1) if match else None


def url_kind(url: str) -> str:
    """'track', 'album', 'playlist' or 'artist', from the link alone."""
    low = (url or "").lower()
    if any(p in low for p in ("/channel/", "/@", "/c/", "/user/", "/artist/")):
        return "artist"
    list_id = extract_list_id(url)
    if list_id:
        # YouTube Music's auto-generated album playlists start with OLAK5uy.
        return "album" if list_id.startswith("OLAK5uy") else "playlist"
    if "/playlist" in low or "/browse/" in low:
        return "playlist"
    return "track"


def fmt_duration(seconds) -> str:
    try:
        minutes, secs = divmod(int(float(seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    except (TypeError, ValueError):
        return "--:--"


def best_thumb(obj) -> str:
    if not obj:
        return ""
    thumbs = obj.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url", "") or ""
    return obj.get("thumbnail", "") or ""


def _artist(entry: dict) -> str:
    name = (entry.get("artist") or entry.get("channel") or entry.get("uploader") or "").strip()
    return name[: -len(" - Topic")] if name.endswith(" - Topic") else name


# ==================== Worker ====================
class MetadataWorker(QThread):
    """
    Emits done(meta) or error(message). meta is:
        {kind, title, subtitle, cover_url, count, unavailable, source_url, raw,
         tracks: [{id, name, artists, album, duration, cover_url, url, available}]}
    """
    done = Signal(dict)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, downloader, url: str):
        super().__init__()
        self.downloader = downloader
        self.url = url.strip()

    def run(self):
        kind = url_kind(self.url)
        item_type = "track" if kind == "track" else "playlist"
        self.log.emit(f"[fetch] {self.url} ({kind})")
        try:
            ok, message, info = self.downloader.fetch_info(self.url, item_type)
        except RuntimeError as error:                 # yt-dlp isn't installed
            self.error.emit(str(error))
            return
        except Exception as error:
            self.error.emit(f"Couldn't read that link: {type(error).__name__}: {error}")
            return
        if not ok or not info:
            self.error.emit(message or "Couldn't read that link.")
            return

        try:
            log_manager.add_input(self.url, kind, info)
        except Exception:
            pass                                      # history is a convenience

        host = "music.youtube.com" if "music.youtube.com" in self.url else "www.youtube.com"
        if info.get("_type") == "playlist":
            tracks = []
            for entry in info.get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                title = entry.get("title") or entry["id"]
                available = title.lower() not in UNAVAILABLE_TITLES and \
                    entry.get("availability") not in ("private", "needs_auth", "premium_only")
                tracks.append(dict(
                    id=entry["id"], name=title, artists=_artist(entry) if available else "",
                    album=info.get("title", "") if kind == "album" else "",
                    duration=fmt_duration(entry.get("duration")),
                    cover_url=best_thumb(entry),
                    url=f"https://{host}/watch?v={entry['id']}", available=available))
            if not tracks:
                self.error.emit("No tracks found at that link.")
                return
            title = info.get("title") or kind.title()
            subtitle = (info.get("channel") or info.get("uploader") or "") if kind == "album" \
                else (info.get("channel") or info.get("uploader") or "")
            cover = best_thumb(info) or tracks[0]["cover_url"]
        else:                                         # a single track
            title = info.get("title") or "Track"
            subtitle = _artist(info)
            cover = best_thumb(info)
            tracks = [dict(id=info.get("id", ""), name=title, artists=subtitle,
                           album=info.get("album", "") or "",
                           duration=fmt_duration(info.get("duration")), cover_url=cover,
                           url=info.get("webpage_url") or self.url, available=True)]

        unavailable = sum(1 for t in tracks if not t["available"])
        self.log.emit(f"[fetch] {title}: {len(tracks)} track(s)"
                      + (f", {unavailable} unavailable" if unavailable else ""))
        self.done.emit(dict(kind=kind, title=title, subtitle=subtitle, cover_url=cover,
                            count=len(tracks), unavailable=unavailable, tracks=tracks,
                            source_url=self.url, raw=info))
