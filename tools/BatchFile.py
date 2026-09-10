"""Reading and writing link lists held in a .txt or .csv file.

Tuned for YoutubeMusicDownloader.download_from_file. That method removes
duplicates by comparing the "url" strings, skips entries whose "status" is
"success", and passes statuses back to mark_statuses keyed by the same url.
"""

import codecs
import csv
import hashlib
import io
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

# Any full link, plus YouTube links pasted without a scheme ("youtu.be/ID").
# The lookbehind stops "notyoutube.com/..." from matching halfway through.
URL_IN_TEXT = re.compile(
    r"(?:https?://|spotify:)\S+"
    r"|(?<![\w./@-])(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE)

# Punctuation that ends up glued to a link in prose, markdown or CSV cells.
LINK_TRAILING = ",;\"')]>"

STATUS_TAG = re.compile(r"\s*#\s*status\s*=\s*(\w+)(?:\s*@[^#]*)?\s*$", re.IGNORECASE)

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

# Share/analytics parameters that don't change what gets downloaded.
TRACKING_PARAMS = {"si", "feature", "pp", "app", "ab_channel", "t", "start",
                   "index", "utm_source", "utm_medium", "utm_campaign"}

# Where a bare video ID from a CSV is sent. Same host the playlist mode uses.
VIDEO_URL = "https://music.youtube.com/watch?v={}"

# Column names recognised when reading/writing a link .csv. Exact names are
# tried first, then a looser keyword match, so exports from various tools
# work without an entry per tool.
URL_HEADERS = ("url", "link", "uri", "address", "video url", "youtube url",
               "youtube link", "video link")
URL_KEYWORDS = ("uri", "url", "link")
VIDEO_ID_HEADERS = ("video id", "video_id", "videoid", "video-id", "youtube id")
TITLE_HEADERS = ("title", "name", "track", "song")
TITLE_HEADERS_LOOSE = ("track name", "song name", "song title", "video title", "track title")
STATUS_HEADERS = ("status", "state", "downloaded", "result")
KNOWN_STATUSES = {"success", "failed", "pending", "skipped", ""}

# How far down a CSV to look for a header row sitting under a preamble.
HEADER_SEARCH_ROWS = 10


# ==================== Link helpers ====================
def _youtube_parts(link: str) -> Optional[Tuple[str, str, Dict[str, List[str]]]]:
    """(host, path, query) when `link` is a YouTube URL, otherwise None."""
    candidate = link if "://" in link else "https://" + link
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host not in YOUTUBE_HOSTS:
        return None
    return host, parts.path or "/", parse_qs(parts.query)


def is_youtube_link(link: str) -> bool:
    return _youtube_parts(link.strip().rstrip(LINK_TRAILING)) is not None


def canonical_url(link: str) -> str:
    """
    One stable spelling per YouTube resource. Non-YouTube links are returned
    unchanged apart from trailing punctuation.

    music.youtube.com links stay on music.youtube.com; every other YouTube
    host becomes www.youtube.com.
    """
    link = link.strip().lstrip("(<[\"'").rstrip(LINK_TRAILING)
    parts = _youtube_parts(link)
    if parts is None:
        return link
    host, path, query = parts
    base = "https://music.youtube.com" if host == "music.youtube.com" else "https://www.youtube.com"
    segments = [s for s in path.split("/") if s]

    video_id = None
    if host == "youtu.be":
        video_id = segments[0] if segments else None
    elif path.rstrip("/") == "/watch":
        video_id = (query.get("v") or [None])[0]
    elif len(segments) >= 2 and segments[0] in ("shorts", "live", "embed", "v"):
        video_id = segments[1]
    if video_id and VIDEO_ID.fullmatch(video_id):
        return f"{base}/watch?v={video_id}"

    list_id = (query.get("list") or [None])[0]
    if path.rstrip("/") == "/playlist" and list_id:
        return f"{base}/playlist?list={list_id}"

    # Album (/browse/MPREb_...), channel and other pages: keep the path and
    # any meaningful query, drop share-tracking noise.
    kept = {k: v for k, v in query.items() if k not in TRACKING_PARAMS}
    clean_path = path.rstrip("/") or "/"
    return base + clean_path + (f"?{urlencode(kept, doseq=True)}" if kept else "")


def link_kind(link: str) -> str:
    """"track", "album", "playlist", "channel", "other" for YouTube links; "" otherwise."""
    if not is_youtube_link(link):
        return ""
    url = canonical_url(link)
    if "/watch?v=" in url:
        return "track"
    if "/playlist?list=" in url:
        # Auto-generated album playlists on YouTube Music use this prefix.
        return "album" if "list=OLAK5uy_" in url else "playlist"
    path = urlsplit(url).path
    if path.startswith("/browse/MPREb_"):
        return "album"
    if path.startswith("/browse/VL"):
        return "playlist"
    if path.startswith(("/channel/", "/@", "/c/", "/user/")):
        return "channel"
    return "other"


def find_link(text: str) -> Optional[str]:
    """First YouTube link in `text`, else the first link of any kind."""
    links = [m.group(0).rstrip(LINK_TRAILING) for m in URL_IN_TEXT.finditer(text)]
    return next((l for l in links if is_youtube_link(l)), links[0] if links else None)


def _norm(cell) -> str:
    return str(cell).strip().lower()


def _cell_has_youtube_link(row: List[str], i: int) -> bool:
    link = find_link(str(row[i])) if i < len(row) else None
    return bool(link) and is_youtube_link(link)


def _row_has_link(row: List[str]) -> bool:
    return any(URL_IN_TEXT.search(str(c)) for c in row)


class CsvLayout(NamedTuple):
    url_idx: Optional[int]      # column holding links
    id_idx: Optional[int]       # column holding bare video IDs
    title_idx: Optional[int]
    artist_idx: Optional[int]
    status_idx: Optional[int]
    header_row: Optional[int]   # index into rows, None when headerless
    start: int                  # first data row


class BatchFile:
    """Parse a link file, and record each link's outcome back into it."""

    def __init__(self, on_error: Optional[Callable[[str], None]] = None,
                 backup_dir=None):
        """
        backup_dir: where to keep a one-off copy of each file before it is first
        written to. None disables backups entirely. Writes are atomic either
        way (temp file + os.replace), so this only guards against a bug in the
        marking logic, not against an interrupted write.
        """
        self._on_error = on_error or (lambda message: None)
        self.backup_dir = Path(backup_dir) if backup_dir else None

    # ---------------- Layout ----------------
    @staticmethod
    def _read_csv(path: Path) -> Tuple[List[List[str]], str, bool, str, bool]:
        """
        Read a CSV once for both parse and mark_statuses.

        Returns (rows, delimiter, has_header, line_terminator, has_bom). Blank
        rows are kept as [], so a rewrite leaves the file's spacing alone.
        """
        with open(path, "rb") as f:
            raw = f.read()
        has_bom = raw.startswith(codecs.BOM_UTF8)
        text = raw.decode("utf-8-sig")
        sample = text[:8192]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        try:
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            has_header = False
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
        terminator = "\r\n" if "\r\n" in sample else "\n"
        return rows, delimiter, has_header, terminator, has_bom

    @staticmethod
    def layout(rows: List[List[str]], has_header: bool) -> CsvLayout:
        """Work out which columns hold what, and where the data starts."""
        first = next((i for i, r in enumerate(rows) if r), None)
        if first is None:
            return CsvLayout(0, None, None, None, None, None, 0)

        # Header row. csv.Sniffer guesses row 0, which is wrong when a
        # preamble comes first, and it sometimes calls a lone link a header.
        # A row that holds a link is never the header, and the search stops
        # there, so a headerless link list can't lose rows to it.
        header_row = None
        for i in range(first, min(len(rows), first + HEADER_SEARCH_ROWS)):
            row = rows[i]
            if not row:
                continue
            if _row_has_link(row):
                break
            names = [_norm(c) for c in row]
            loose = (i == first and has_header)
            if any(n in URL_HEADERS or n in VIDEO_ID_HEADERS
                   or (loose and any(k in n for k in URL_KEYWORDS)) for n in names):
                header_row = i
                break
        if header_row is None and has_header and not _row_has_link(rows[first]):
            header_row = first

        url_candidates: List[int] = []
        id_idx = title_idx = artist_idx = status_idx = None
        start = first

        if header_row is not None:
            start = header_row + 1
            for i, cell in enumerate(rows[header_row]):
                name = _norm(cell)
                if name in VIDEO_ID_HEADERS:
                    if id_idx is None:
                        id_idx = i
                elif name in URL_HEADERS or any(k in name for k in URL_KEYWORDS):
                    url_candidates.append(i)
                elif status_idx is None and name in STATUS_HEADERS:
                    status_idx = i
                elif artist_idx is None and "artist" in name and "album" not in name:
                    # "Album Artist Name(s)" is not the performer we want to show.
                    artist_idx = i
                elif title_idx is None and (name in TITLE_HEADERS
                                            or name in TITLE_HEADERS_LOOSE
                                            or ("name" in name and "album" not in name
                                                and "artist" not in name)):
                    title_idx = i

        data = [r for r in rows[start:] if r]

        url_idx = None
        if url_candidates:
            # Several link-ish columns ("Thumbnail URL", "Video URL"): take the
            # one that actually holds YouTube links, preferring exact names.
            url_candidates.sort(key=lambda i: _norm(rows[header_row][i]) not in URL_HEADERS)
            sample = data[:50]
            url_idx = next((i for i in url_candidates
                            if any(_cell_has_youtube_link(r, i) for r in sample)),
                           url_candidates[0])
        elif id_idx is None:
            # No usable header: the link column is the first one holding a
            # YouTube link, else the first holding any link.
            any_link = None
            for row in data:
                for i, cell in enumerate(row):
                    link = find_link(str(cell))
                    if link and is_youtube_link(link):
                        url_idx = i
                        break
                    if link and any_link is None:
                        any_link = i
                if url_idx is not None:
                    break
            if url_idx is None:
                url_idx = any_link if any_link is not None else 0

        if status_idx is None and header_row is None and data:
            # Headerless file we have written to before: the last column will
            # contain nothing but known status words.
            last = max(len(r) for r in data) - 1
            if last > (url_idx or 0):
                values = [_norm(r[last]) for r in data if len(r) > last]
                if values and all(v in KNOWN_STATUSES for v in values):
                    status_idx = last

        return CsvLayout(url_idx, id_idx, title_idx, artist_idx, status_idx, header_row, start)

    @classmethod
    def csv_layout(cls, rows: List[List[str]], has_header: bool):
        """Previous 5-tuple view: (url_idx, title_idx, artist_idx, status_idx, start)."""
        lay = cls.layout(rows, has_header)
        url_idx = lay.url_idx if lay.url_idx is not None else (lay.id_idx or 0)
        return url_idx, lay.title_idx, lay.artist_idx, lay.status_idx, lay.start

    @staticmethod
    def _row_link(row: List[str], lay: CsvLayout) -> Optional[str]:
        """The canonical link a data row stands for, or None."""
        if lay.url_idx is not None and lay.url_idx < len(row):
            link = find_link(str(row[lay.url_idx]))
            if link:
                return canonical_url(link)
        if lay.id_idx is not None and lay.id_idx < len(row):
            video_id = str(row[lay.id_idx]).strip()
            if VIDEO_ID.fullmatch(video_id):
                return VIDEO_URL.format(video_id)
        if lay.header_row is None:
            # Headerless: a link anywhere in the row will do. With a header,
            # an empty link cell means the row has no link, and a thumbnail
            # URL in another column must not stand in for it.
            link = find_link(" ".join(str(c) for i, c in enumerate(row) if i != lay.status_idx))
            if link:
                return canonical_url(link)
        return None

    # ---------------- Reading ----------------
    def parse(self, path: Path) -> List[Dict[str, str]]:
        """
        Pull link records out of a .txt or .csv file.

        Returns a list of dicts:
            {"url": ..., "title": ..., "artist": ..., "status": ..., "kind": ...}
        url is canonical (see module docstring). status is "" for a link that
        has not been downloaded yet. kind is "track", "album", "playlist",
        "channel" or "other" for YouTube links and "" for anything else.

        .txt  - one link per line. Blank lines and lines starting with # are
                skipped. Anything else on the line becomes the label, so both a
                bare URL and "https://... - Artist - Song" work. A trailing
                "# status=success" marker is read back and stripped.
        .csv  - delimiter and header are sniffed, and a header below a
                preamble is found. Link columns (url/link/uri) and video-ID
                columns are both understood, along with title, artist and
                status columns. Without a header, the first cell holding a
                link wins and the first other cell becomes the label.
        """
        path = Path(path)
        entries: List[Dict[str, str]] = []
        try:
            if path.suffix.lower() in (".csv", ".tsv"):
                rows, _, has_header, _, _ = self._read_csv(path)
                lay = self.layout(rows, has_header)

                for row in rows[lay.start:]:
                    if not row:
                        continue
                    url = self._row_link(row, lay)
                    if not url:
                        continue

                    title = ""
                    if lay.title_idx is not None and lay.title_idx < len(row):
                        # The plain track name. The artist is returned as its
                        # own field rather than folded in here, or a caller
                        # that combines the two ends up with it twice.
                        title = str(row[lay.title_idx]).strip()
                    elif lay.header_row is None and len(row) > 1:
                        # No header to go on: use the first cell that isn't the
                        # link and isn't a status word we wrote ourselves.
                        title = next((str(c).strip() for c in row
                                      if str(c).strip() and not URL_IN_TEXT.search(str(c))
                                      and _norm(c) not in KNOWN_STATUSES), "")

                    artist = ""
                    if lay.artist_idx is not None and lay.artist_idx < len(row):
                        artist = str(row[lay.artist_idx]).strip()

                    status = ""
                    if lay.status_idx is not None and lay.status_idx < len(row):
                        status = _norm(row[lay.status_idx])

                    entries.append({"url": url, "title": title, "artist": artist,
                                    "status": status, "kind": link_kind(url)})
            else:
                with open(path, encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue

                        status = ""
                        tag = STATUS_TAG.search(line)
                        if tag:
                            status = tag.group(1).strip().lower()
                            line = line[:tag.start()].rstrip()

                        link = find_link(line)
                        if not link:
                            continue
                        url = canonical_url(link)
                        title = line.replace(link, "", 1).strip(" ,|-\t")
                        entries.append({"url": url, "title": title, "artist": "",
                                        "status": status, "kind": link_kind(url)})
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            self._on_error(f"Could not read {path}: {e}")
            return []
        return entries

    # ---------------- Writing ----------------
    def _backup_once(self, path: Path) -> None:
        """Copy the file into backup_dir the first time it is written to.

        The copy lives alongside the other bookkeeping rather than next to the
        user's file, so a link list doesn't sprout a .bak sibling in whatever
        folder they keep it in. A path hash is in the name so two files called
        links.txt in different folders don't collide.
        """
        if self.backup_dir is None:
            return
        try:
            digest = hashlib.md5(str(path.resolve()).encode()).hexdigest()[:8]
            backup = self.backup_dir / f"{path.stem}_{digest}{path.suffix}.bak"
            if not backup.exists():
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(path.read_bytes())
        except OSError as e:
            # A missing backup is not a reason to refuse to record progress.
            self._on_error(f"Could not back up {path}: {e}")

    def mark_statuses(self, path: Path, statuses: Dict[str, str]) -> bool:
        """
        Record the outcome of each link back into the source file.

        `statuses` maps url -> "success" / "failed". Keys may be in any form;
        they are compared canonically, so every spelling of the same video in
        the file gets the status. The file is rewritten via a temporary sibling
        and os.replace, so an interrupted write cannot leave a half-written
        list behind. Line endings and a UTF-8 BOM (which Excel relies on to
        read non-ASCII text) are preserved.
        """
        if not statuses:
            return True

        path = Path(path)
        tmp_path = path.with_name(path.name + ".tmp")
        wanted = {canonical_url(url): status for url, status in statuses.items()}

        try:
            self._backup_once(path)

            if path.suffix.lower() in (".csv", ".tsv"):
                rows, delimiter, has_header, terminator, has_bom = self._read_csv(path)
                if not any(rows):
                    return True

                lay = self.layout(rows, has_header)
                width = max(len(r) for r in rows[lay.start:] or [[]])
                if lay.header_row is not None:
                    width = max(width, len(rows[lay.header_row]))

                status_idx = lay.status_idx
                if status_idx is None:
                    status_idx = width
                    width += 1
                    if lay.header_row is not None:
                        header = list(rows[lay.header_row])
                        rows[lay.header_row] = header + [""] * (status_idx - len(header)) + ["status"]

                out_rows = []
                for i, row in enumerate(rows):
                    if not row or i < lay.start:
                        # Blank lines, preamble and the header pass through as-is.
                        out_rows.append(row)
                        continue
                    row = list(row) + [""] * (width - len(row))
                    url = self._row_link(row, lay)
                    if url and url in wanted:
                        row[status_idx] = wanted[url]
                    out_rows.append(row)

                # Written with standard quoting rather than the sniffed dialect,
                # which can come back with quoting disabled and then refuse any
                # cell containing the delimiter ("metalcore,metal,heavy metal").
                with open(tmp_path, "w", newline="",
                          encoding="utf-8-sig" if has_bom else "utf-8") as f:
                    csv.writer(f, delimiter=delimiter, quotechar='"',
                               quoting=csv.QUOTE_MINIMAL,
                               lineterminator=terminator).writerows(out_rows)

            else:
                # newline="" keeps the original endings visible instead of
                # letting universal-newline mode hide them; a CRLF list edited
                # on Windows shouldn't silently become LF because the run
                # happened under WSL.
                with open(path, "rb") as f:
                    has_bom = f.read(3) == codecs.BOM_UTF8
                with open(path, encoding="utf-8-sig", newline="") as f:
                    raw_text = f.read()
                newline = "\r\n" if "\r\n" in raw_text else "\n"

                out_lines = []
                for raw in raw_text.splitlines():
                    stripped = raw.strip()
                    if not stripped or stripped.startswith("#"):
                        out_lines.append(raw)
                        continue

                    # Only touch lines in this batch - anything else keeps the
                    # marker an earlier run gave it.
                    body = STATUS_TAG.sub("", raw.rstrip())
                    link = find_link(body)
                    url = canonical_url(link) if link else None
                    if url and url in wanted:
                        out_lines.append(f"{body}  # status={wanted[url]}")
                    else:
                        out_lines.append(raw)

                with open(tmp_path, "w", newline="",
                          encoding="utf-8-sig" if has_bom else "utf-8") as f:
                    f.write(newline.join(out_lines) + newline)

            os.replace(tmp_path, path)
            return True

        except (OSError, UnicodeDecodeError, csv.Error) as e:
            self._on_error(f"Could not update statuses in {path}: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return False