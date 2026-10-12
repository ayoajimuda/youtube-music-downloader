"""Save a YouTube / YouTube Music playlist as a JSON file in the export folder.

The batch downloader reads this file directly ("every link in a file") and
adds "status": "success" to each track it downloads. Unavailable tracks have
no link, so they're never attempted. Exporting again into the same file adds
only the new tracks, so those statuses are kept.

Usage:
    from tools.playlist_to_txt import export_playlist, playlist_to_txt
    result = export_playlist(url)                  # no prompts
    playlist_to_txt(downloader)                    # interactive; offers to download it afterwards
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import parse_qs, urlsplit

from managers import config_manager
from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import find_program, run_program

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
FETCH_TIMEOUT = 600          # big playlists take a while to list


class PlaylistError(Exception):
    """The playlist couldn't be read; the message says why in plain words."""


@dataclass
class Track:
    video_id: str
    title: str
    artist: str
    url: str
    available: bool = True

    def record(self) -> dict:
        if not self.available:                 # no link: the downloader won't try it
            return {"title": self.title, "id": self.video_id}
        return {"artist": self.artist, "title": self.title, "url": self.url}


@dataclass
class PlaylistExport:
    path: Path
    title: str
    total: int
    added: int
    unavailable: int
    already_listed: int
    tracks: List[Track] = field(default_factory=list)


# ==================== Reading the playlist ====================
def default_folder() -> Path:
    return config_manager.EXPORT_DIR


def safe_filename(name: str, fallback: str = "playlist") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(". ")
    return name[:100].rstrip(". ") or fallback


def looks_like_youtube(url: str) -> bool:
    try:
        return (urlsplit(url.strip()).hostname or "").lower() in YOUTUBE_HOSTS
    except ValueError:
        return False


def _cookie_args() -> List[str]:
    """Use the active cookies when cookies are on, so private playlists work too."""
    if not config_manager.load().get("use_cookies"):
        return []
    try:
        from managers import cookie_manager
        active = cookie_manager.get_active_cookie_file()
    except Exception:
        active = None
    return ["--cookies", str(active.resolve())] if active else []


def _explain(output: str) -> str:
    low = output.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube wants a sign-in (bot check). Add cookies in the Cookies menu, then retry."
    if "private" in low and "playlist" in low:
        return "This playlist is private. Turn cookies on with the account that owns it."
    if "does not exist" in low or "404" in low:
        return "That playlist doesn't exist (check the link)."
    if "429" in low or "too many requests" in low:
        return "YouTube is throttling requests. Wait a while and try again."
    errors = [l for l in output.splitlines() if l.strip().upper().startswith("ERROR")]
    return (errors[-1] if errors else output.strip()[-300:]) or "yt-dlp failed without a message"


def fetch_playlist(url: str) -> dict:
    """The playlist's details from yt-dlp, without downloading anything."""
    ytdlp, where = find_program("yt-dlp")
    if not ytdlp:
        raise PlaylistError(f"yt-dlp is needed for this ({where}). Run the dependency check.")
    code, output = run_program(
        [ytdlp, "--flat-playlist", "--dump-single-json", "--no-warnings", *_cookie_args(),
         url.strip()], timeout=FETCH_TIMEOUT)
    if code is None:
        raise PlaylistError(output)
    if code != 0:
        raise PlaylistError(_explain(output))
    start = output.find("{")
    try:
        info = json.loads(output[start:output.rfind("}") + 1]) if start >= 0 else {}
    except ValueError:
        raise PlaylistError("yt-dlp returned something that isn't playlist data.")
    if info.get("_type") != "playlist":
        raise PlaylistError("That link is a single video, not a playlist. Use 'Download a "
                            "track' for single videos.")
    return info


def _host_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return "music.youtube.com" if host == "music.youtube.com" else "www.youtube.com"


def tracks_from(info: dict, source_url: str) -> List[Track]:
    host = _host_for(source_url)
    tracks = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        title = (entry.get("title") or "").strip()
        artist = (entry.get("channel") or entry.get("uploader") or "").strip()
        if artist.endswith(" - Topic"):               # YouTube Music's auto-generated channels
            artist = artist[: -len(" - Topic")]
        available = title.lower() not in UNAVAILABLE_TITLES and \
            entry.get("availability") not in ("private", "needs_auth", "premium_only")
        tracks.append(Track(entry["id"], title or entry["id"], artist if available else "",
                            f"https://{host}/watch?v={entry['id']}", available))
    return tracks


# ==================== Writing the file ====================
def _ids_in(data: dict) -> Set[str]:
    """Video IDs already in an export (unavailable ones included)."""
    ids = set()
    for track in data.get("tracks") or []:
        if isinstance(track, dict):
            ids.update(VIDEO_ID.findall(str(track.get("url", ""))))
    for track in data.get("unavailable") or []:
        if isinstance(track, dict) and track.get("id"):
            ids.add(str(track["id"]))
    return ids


def _read_export(path: Path) -> Optional[dict]:
    """An existing export, or None if it's missing or isn't one of ours."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tracks"), list) else None


def _write_atomically(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def export_playlist(url: str, output: Optional[Path] = None, merge: bool = True,
                    info: Optional[dict] = None) -> PlaylistExport:
    """
    Write a playlist to a JSON file and return what was written.

    output  file to write; defaults to export/<playlist title>.json
    merge   if the file exists, add only tracks it doesn't have yet (keeps statuses);
            False replaces it
    info    playlist data already fetched with fetch_playlist(), to skip a second lookup
    """
    info = info if info is not None else fetch_playlist(url)
    title = (info.get("title") or info.get("id") or "Playlist").strip()
    tracks = tracks_from(info, url)
    path = Path(output) if output else default_folder() / f"{safe_filename(title)}.json"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    unavailable = sum(1 for t in tracks if not t.available)

    existing = _read_export(path) if merge and path.is_file() else None
    if merge and path.is_file() and existing is None:
        raise PlaylistError(f"{path.name} isn't a playlist export, so new tracks can't be "
                            "added to it. Replace it or save under another name.")
    if existing is not None:
        known = _ids_in(existing)
        new = [t for t in tracks if t.video_id not in known]
        if new:
            existing["tracks"] += [t.record() for t in new if t.available]
            gone = [t.record() for t in new if not t.available]
            if gone:
                existing["unavailable"] = (existing.get("unavailable") or []) + gone
            existing["updated"] = stamp
            existing["track_count"] = len(existing["tracks"])
            _write_atomically(path, existing)
        return PlaylistExport(path, title, len(tracks), len(new),
                              sum(1 for t in new if not t.available),
                              len(tracks) - len(new), tracks)

    data = {
        "playlist": title,
        "source": info.get("webpage_url") or url.strip(),
        "exported": stamp,
        "updated": stamp,
        "track_count": len(tracks) - unavailable,
        "tracks": [t.record() for t in tracks if t.available],
        "unavailable": [t.record() for t in tracks if not t.available],
    }
    _write_atomically(path, data)
    return PlaylistExport(path, title, len(tracks), len(tracks), unavailable, 0, tracks)


# ==================== Interactive ====================
def playlist_to_txt(downloader=None) -> Optional[PlaylistExport]:
    """Ask for a playlist link, export it, and offer to download it straight away."""
    import questionary

    def valid(text: str):
        if not text.strip():
            return True                                     # empty = cancel
        return True if looks_like_youtube(text) else "Paste a YouTube or YouTube Music link"

    url = questionary.text("Playlist link (empty to cancel):", validate=valid).ask()
    url = (url or "").strip()
    if not url:
        return None
    if "list=" not in url and "/playlist" not in url and "/browse/" not in url:
        Enhanced_Menu.print_status("That doesn't look like a playlist link (no 'list='). "
                                   "Trying anyway.", "warning")

    Enhanced_Menu.print_status("Reading the playlist... (large playlists take a minute)", "info")
    try:
        info_preview = fetch_playlist(url)
    except PlaylistError as error:
        Enhanced_Menu.print_status(str(error), "error")
        return None
    title = (info_preview.get("title") or "Playlist").strip()
    suggested = default_folder() / f"{safe_filename(title)}.json"

    answer = questionary.path("Save as:", default=str(suggested)).ask()
    if not answer:
        return None
    path = Path(answer.strip().strip('"').strip("'")).expanduser()
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")

    merge = True
    if path.is_file():
        choice = questionary.select(
            f"{path.name} already exists.",
            choices=["Add only new tracks (keep what's there)", "Replace it", "Cancel"]).ask()
        if choice in (None, "Cancel"):
            return None
        merge = choice.startswith("Add")

    try:
        result = export_playlist(url, path, merge=merge, info=info_preview)
    except PlaylistError as error:
        Enhanced_Menu.print_status(str(error), "error")
        return None
    except OSError as error:
        Enhanced_Menu.print_status(f"Couldn't save {path}: {error}", "error")
        return None

    Enhanced_Menu.print_status(f"Saved {result.path}", "success")
    Enhanced_Menu.print_key_value("Playlist", result.title, width=16)
    Enhanced_Menu.print_key_value("Tracks", result.total, width=16)
    if result.already_listed:
        Enhanced_Menu.print_key_value("Already in file", result.already_listed, width=16)
    Enhanced_Menu.print_key_value("Added", result.added, width=16)
    if result.unavailable:
        Enhanced_Menu.print_key_value("Unavailable", result.unavailable, width=16,
                                      note="private/deleted, listed under \"unavailable\"")

    if downloader is not None and result.total and questionary.confirm(
            "Download this playlist now?", default=False).ask():
        config_manager.update(last_batch_file=str(result.path.resolve()))
        downloader.download_from_file(str(result.path))
    return result