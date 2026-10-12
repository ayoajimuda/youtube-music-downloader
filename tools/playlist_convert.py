"""Save a YouTube / YouTube Music playlist to the export folder as JSON, TXT or CSV.

JSON  {"playlist": ..., "source": ..., "exported": ..., "updated": ..., "track_count": N,
       "tracks": [{"artist", "title", "url"}], "unavailable": [{"title", "id"}]}
TXT   # Playlist: Road Trip
      Daft Punk - One More Time | https://music.youtube.com/watch?v=...
      # [Private video] (unavailable, id xxxxxxxxxxx)
CSV   artist,title,url,status,note   (opens in Excel; unavailable rows have no url)

All three can be downloaded with "every link in a file": the batch downloader
marks each track it downloads (status=success), and private/deleted tracks
have no link, so they're never attempted. Exporting again into the same file
adds only the new tracks, so those marks are kept.

Usage:
    from tools.playlist_convert import export_playlist, playlist_convert
    result = export_playlist(url)                     # JSON, no prompts
    result = export_playlist(url, fmt="csv")          # or "txt"
    playlist_convert(downloader)                      # interactive: asks for the format
"""

import csv
import io
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
FORMATS = {
    "json": "JSON - structured, keeps playlist details",
    "txt": "TXT - one 'Artist - Title | link' per line, easy to edit",
    "csv": "CSV - a table that opens in Excel / Google Sheets",
}
CSV_HEADER = ["artist", "title", "url", "status", "note"]
UNAVAILABLE_ID = re.compile(r"unavailable,? (?:id )?\(?([A-Za-z0-9_-]{11})\)?")
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

    def line(self) -> str:
        if not self.available:
            return f"# {self.title} (unavailable, id {self.video_id})"
        name = f"{self.artist} - {self.title}" if self.artist else self.title
        name = name.replace("|", "/").strip() or self.video_id
        return f"{name} | {self.url}"

    def row(self) -> List[str]:
        if not self.available:
            return ["", self.title, "", "", f"unavailable ({self.video_id})"]
        return [self.artist, self.title, self.url, "", ""]


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
def _write_atomically(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    os.replace(tmp, path)


def format_of(path: Path, fmt: Optional[str] = None) -> str:
    """'json', 'txt' or 'csv': the one asked for, else from the file's extension."""
    fmt = (fmt or Path(path).suffix.lstrip(".") or "json").lower()
    if fmt not in FORMATS:
        raise PlaylistError(f"Can't export as .{fmt}; choose JSON, TXT or CSV.")
    return fmt


def _ids_in_text(text: str) -> Set[str]:
    """Video IDs already in a TXT or CSV export (unavailable ones included)."""
    return set(VIDEO_ID.findall(text)) | set(UNAVAILABLE_ID.findall(text))


def _ids_in_json(data: dict) -> Set[str]:
    ids = set()
    for track in data.get("tracks") or []:
        if isinstance(track, dict):
            ids.update(VIDEO_ID.findall(str(track.get("url", ""))))
    for track in data.get("unavailable") or []:
        if isinstance(track, dict) and track.get("id"):
            ids.add(str(track["id"]))
    return ids


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


# --- JSON ---
def _read_json_export(path: Path) -> Optional[dict]:
    """An existing JSON export, or None if it isn't one of ours."""
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tracks"), list) else None


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _new_json(title: str, source: str, stamp: str, tracks: List[Track]) -> str:
    available = [t for t in tracks if t.available]
    return _dump_json({
        "playlist": title, "source": source, "exported": stamp, "updated": stamp,
        "track_count": len(available),
        "tracks": [t.record() for t in available],
        "unavailable": [t.record() for t in tracks if not t.available],
    })


def _add_json(path: Path, new: List[Track], stamp: str) -> str:
    data = _read_json_export(path)
    data["tracks"] += [t.record() for t in new if t.available]
    gone = [t.record() for t in new if not t.available]
    if gone:
        data["unavailable"] = (data.get("unavailable") or []) + gone
    data["updated"] = stamp
    data["track_count"] = len(data["tracks"])
    return _dump_json(data)


# --- TXT ---
def _new_txt(title: str, source: str, stamp: str, tracks: List[Track]) -> str:
    unavailable = sum(1 for t in tracks if not t.available)
    header = [f"# Playlist: {title}",
              f"# Source: {source}",
              f"# Exported: {stamp} - {len(tracks)} tracks"
              + (f", {unavailable} unavailable (commented out)" if unavailable else ""),
              "# Export again to add new tracks; existing lines and their status are kept.",
              ""]
    return "\n".join(header + [t.line() for t in tracks]) + "\n"


def _add_txt(path: Path, new: List[Track], stamp: str) -> str:
    existing = _read_text(path).rstrip("\n")
    lines = [existing, "", f"# Added {stamp}: {len(new)} new track(s)"] + [t.line() for t in new]
    return "\n".join(lines) + "\n"


# --- CSV ---
def _csv_text(rows: List[List[str]]) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    return buf.getvalue()


def _new_csv(title: str, source: str, stamp: str, tracks: List[Track]) -> str:
    return _csv_text([CSV_HEADER] + [t.row() for t in tracks])


def _csv_is_ours(path: Path) -> bool:
    try:
        first = next(csv.reader(io.StringIO(_read_text(path))), [])
    except (OSError, csv.Error):
        return False
    return [c.strip().lower() for c in first[:len(CSV_HEADER)]] == CSV_HEADER


def _add_csv(path: Path, new: List[Track], stamp: str) -> str:
    existing = _read_text(path)
    if existing and not existing.endswith(("\n", "\r")):
        existing += "\r\n"
    return existing + _csv_text([t.row() for t in new])


WRITERS = {"json": (_new_json, _add_json), "txt": (_new_txt, _add_txt),
           "csv": (_new_csv, _add_csv)}


def _mergeable(path: Path, fmt: str) -> bool:
    if fmt == "json":
        return _read_json_export(path) is not None
    if fmt == "csv":
        return _csv_is_ours(path)
    return True                                   # any text file can be added to


def _known_ids(path: Path, fmt: str) -> Set[str]:
    if fmt == "json":
        return _ids_in_json(_read_json_export(path) or {})
    return _ids_in_text(_read_text(path))


def export_playlist(url: str, output: Optional[Path] = None, merge: bool = True,
                    info: Optional[dict] = None, fmt: Optional[str] = None) -> PlaylistExport:
    """
    Write a playlist to a JSON, TXT or CSV file and return what was written.

    output  file to write; defaults to export/<playlist title>.<fmt>
    merge   if the file exists, add only tracks it doesn't have yet (keeps statuses);
            False replaces it
    info    playlist data already fetched with fetch_playlist(), to skip a second lookup
    fmt     'json', 'txt' or 'csv'; defaults to output's extension, else JSON
    """
    fmt = format_of(Path(output) if output else Path(), fmt)
    info = info if info is not None else fetch_playlist(url)
    title = (info.get("title") or info.get("id") or "Playlist").strip()
    tracks = tracks_from(info, url)
    path = Path(output) if output else default_folder() / f"{safe_filename(title)}.{fmt}"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    source = info.get("webpage_url") or url.strip()
    write_new, add_to = WRITERS[fmt]
    # Excel only reads a CSV's accents/emoji right with the UTF-8 marker at the start.
    encoding = "utf-8-sig" if fmt == "csv" else "utf-8"

    if merge and path.is_file():
        if not _mergeable(path, fmt):
            raise PlaylistError(f"{path.name} isn't a playlist export, so new tracks can't be "
                                "added to it. Replace it or save under another name.")
        known = _known_ids(path, fmt)
        new = [t for t in tracks if t.video_id not in known]
        if new:
            _write_atomically(path, add_to(path, new, stamp), encoding)
        return PlaylistExport(path, title, len(tracks), len(new),
                              sum(1 for t in new if not t.available),
                              len(tracks) - len(new), tracks)

    _write_atomically(path, write_new(title, source, stamp, tracks), encoding)
    return PlaylistExport(path, title, len(tracks), len(tracks),
                          sum(1 for t in tracks if not t.available), 0, tracks)


# ==================== Interactive ====================
def playlist_convert(downloader=None) -> Optional[PlaylistExport]:
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

    fmt = questionary.select(
        "Save the playlist as:",
        choices=[questionary.Choice(label, value=key) for key, label in FORMATS.items()]
                + [questionary.Choice("Cancel", value="cancel")]).ask()
    if fmt in (None, "cancel"):
        return None
    suggested = default_folder() / f"{safe_filename(title)}.{fmt}"

    answer = questionary.path("Save as:", default=str(suggested)).ask()
    if not answer:
        return None
    path = Path(answer.strip().strip('"').strip("'")).expanduser()
    if path.suffix.lower() != f".{fmt}":
        path = path.with_suffix(f".{fmt}")

    merge = True
    if path.is_file():
        choice = questionary.select(
            f"{path.name} already exists.",
            choices=["Add only new tracks (keep what's there)", "Replace it", "Cancel"]).ask()
        if choice in (None, "Cancel"):
            return None
        merge = choice.startswith("Add")

    try:
        result = export_playlist(url, path, merge=merge, info=info_preview, fmt=fmt)
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
                                      note="private/deleted, no link (never downloaded)")

    if downloader is not None and result.total and questionary.confirm(
            "Download this playlist now?", default=False).ask():
        config_manager.update(last_batch_file=str(result.path.resolve()))
        downloader.download_from_file(str(result.path))
    return result


# The old name, so code written before the rename keeps working.
playlist_to_txt = playlist_convert