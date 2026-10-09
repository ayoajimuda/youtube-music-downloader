"""Save a YouTube / YouTube Music playlist as a .txt batch file.

Each track becomes one line the batch downloader understands:

    # Playlist: Road Trip
    # Source: https://music.youtube.com/playlist?list=PL...
    Daft Punk - One More Time | https://music.youtube.com/watch?v=FGBhQbmPwH8
    # [Private video] | https://music.youtube.com/watch?v=xxxxxxxxxxx   (unavailable, skipped)

Exporting again into the same file adds only the new tracks, so the
"# status=success" marks the downloader writes are kept.

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

    def line(self) -> str:
        name = f"{self.artist} - {self.title}" if self.artist else self.title
        name = name.replace("|", "/").strip() or self.video_id
        return f"{name} | {self.url}" if self.available else f"# {name} | {self.url}"


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
    return config_manager.APP_DIR / "playlists"


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
def _ids_in(path: Path) -> Set[str]:
    """Video IDs already in a file (commented-out lines included)."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return set()
    return set(VIDEO_ID.findall(text))


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def export_playlist(url: str, output: Optional[Path] = None, merge: bool = True,
                    info: Optional[dict] = None) -> PlaylistExport:
    """
    Write a playlist to a .txt batch file and return what was written.

    output  file to write; defaults to playlists/<playlist title>.txt
    merge   if the file exists, add only tracks it doesn't have yet (keeps statuses);
            False replaces it
    info    playlist data already fetched with fetch_playlist(), to skip a second lookup
    """
    info = info if info is not None else fetch_playlist(url)
    title = (info.get("title") or info.get("id") or "Playlist").strip()
    tracks = tracks_from(info, url)
    path = Path(output) if output else default_folder() / f"{safe_filename(title)}.txt"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    unavailable = sum(1 for t in tracks if not t.available)

    if merge and path.is_file():
        known = _ids_in(path)
        new = [t for t in tracks if t.video_id not in known]
        if new:
            existing = path.read_text(encoding="utf-8-sig", errors="replace").rstrip("\n")
            lines = [existing, "", f"# Added {stamp}: {len(new)} new track(s)"]
            lines += [t.line() for t in new]
            _write_atomically(path, "\n".join(lines) + "\n")
        return PlaylistExport(path, title, len(tracks), len(new),
                              sum(1 for t in new if not t.available),
                              len(tracks) - len(new), tracks)

    header = [f"# Playlist: {title}",
              f"# Source: {info.get('webpage_url') or url.strip()}",
              f"# Exported: {stamp} - {len(tracks)} tracks"
              + (f", {unavailable} unavailable (commented out)" if unavailable else ""),
              "# Export again to add new tracks; existing lines and their status are kept.",
              ""]
    _write_atomically(path, "\n".join(header + [t.line() for t in tracks]) + "\n")
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
    suggested = default_folder() / f"{safe_filename(title)}.txt"

    answer = questionary.path("Save as:", default=str(suggested)).ask()
    if not answer:
        return None
    path = Path(answer.strip().strip('"').strip("'")).expanduser()
    if path.suffix.lower() != ".txt":
        path = path.with_suffix(".txt")

    merge = True
    if path.is_file():
        choice = questionary.select(
            f"{path.name} already exists.",
            choices=["Add only new tracks (keep what's there)", "Replace it", "Cancel"]).ask()
        if choice in (None, "Cancel"):
            return None
        merge = choice.startswith("Add")

    result = export_playlist(url, path, merge=merge, info=info_preview)

    Enhanced_Menu.print_status(f"Saved {result.path}", "success")
    Enhanced_Menu.print_key_value("Playlist", result.title, width=16)
    Enhanced_Menu.print_key_value("Tracks", result.total, width=16)
    if result.already_listed:
        Enhanced_Menu.print_key_value("Already in file", result.already_listed, width=16)
    Enhanced_Menu.print_key_value("Added", result.added, width=16)
    if result.unavailable:
        Enhanced_Menu.print_key_value("Unavailable", result.unavailable, width=16,
                                      note="private/deleted, commented out")

    if downloader is not None and result.total and questionary.confirm(
            "Download this playlist now?", default=False).ask():
        config_manager.update(last_batch_file=str(result.path.resolve()))
        downloader.download_from_file(str(result.path))
    return result