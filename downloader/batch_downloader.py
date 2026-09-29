"""Reading and writing link lists held in a .txt or .csv file"""

import codecs
import csv
import hashlib
import io
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit, parse_qsl, urlunsplit

# Scan for full links plus YouTube links pasted without a scheme
URL_IN_TEXT = re.compile(
    r"(?:https?://|spotify:)\S+"
    r"|(?<![\w./@-])(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE)

LINK_TRAILING = ",;\"')]>" # Punctuation that ends up glued to a link in prose, markdown or CSV cells.

STATUS_TAG = re.compile(r"\s*#\s*status\s*=\s*(\w+)(?:\s*@[^#]*)?\s*$", re.IGNORECASE)

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be" } # Acceptable hosts that the batch down

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
_STATUS_ALIASES = {"true": "success", "yes": "success", "y": "success", "1": "success",
                   "x": "success", "done": "success", "downloaded": "success", "ok": "success"}

# How far down a CSV to look for a header row sitting under a preamble.
HEADER_SEARCH_ROWS = 10


def normalize_url(raw: str) -> str:
    url = raw.strip().rstrip(LINK_TRAILING + ".!?")
    if not url or url.lower().startswith("spotify:"):
        return url
    
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return url
    
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_PARAMS]
    if any(k == "v" for k, _ in query):
        query = [(k, v) for k, v in query if k != "list"]
    return urlunsplit(("https", host, parts.path, urlencode(query), ""))


def extract_url(text: str) -> Optional[str]:
    if not text or text.lstrip().startswith("#"):
        return None
    match = URL_IN_TEXT.search(text)
    if match:
        return normalize_url(match.group())
    if VIDEO_ID.fullmatch(text.strip()):
        return VIDEO_URL.format(text.strip())
    return None

def normalize_status(value: str) -> str:
    v = (value or "").strip().lower()
    v = _STATUS_ALIASES.get(v, v)
    return v if v in KNOWN_STATUSES else ""


def parse(self, path: Path) -> List[Dict[str, str]]:
    text, _ = self._read(path)
    return (self._parse_csv(text) if path.suffix.lower() == ".csv"
            else self._parse_txt(text))

def mark_statuses(self, path: Path, statuses: Dict[str, str]) -> bool:
    if not statuses:
        return True
    try:
        text, enc = self._read(path)
        if path.suffix.lower() == ".csv":
            out = self._mark_csv(text, statuses)
        else:
            out = self._mark_txt(text, statuses)
        if out is None:
            return False
        return self._write(path, out, enc)
    except (OSError, csv.Error):
        return False

# ---------- io ----------
@staticmethod
def _read(path: Path):
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        enc = "utf-8-sig"
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        enc = "utf-16"
    else:
        enc = "utf-8"
    try:
        return raw.decode(enc), enc
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"   # never fails, round-trips

@staticmethod
def _write(path: Path, text: str, enc: str) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(text.encode(enc))
        os.replace(tmp, path)      # atomic; fails if Excel has the file open
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False

# ---------- txt ----------
def _parse_txt(self, text: str) -> List[Dict[str, str]]:
    entries = []
    for line in text.splitlines():
        url = extract_url(line)
        if not url:
            continue
        tag = STATUS_TAG.search(line)
        title = URL_IN_TEXT.sub("", STATUS_TAG.sub("", line)).strip(" -|,:\t")
        entries.append({"url": url, "title": title,
                        "status": normalize_status(tag.group(1)) if tag else ""})
    return entries

def _mark_txt(self, text: str, statuses: Dict[str, str]) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = []
    for line in text.splitlines():
        url = extract_url(line)
        if url in statuses:
            line = (STATUS_TAG.sub("", line).rstrip()
                    + f"  # status={statuses[url]} @ {stamp}")
        out.append(line)
    return eol.join(out) + eol

# ---------- csv ----------
@staticmethod
def _rows(text: str):
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delim = ","
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delim)), delim

@staticmethod
def _pick(low: List[str], exact, loose=()) -> Optional[int]:
    for name in exact:
        if name in low:
            return low.index(name)
    for i, cell in enumerate(low):
        if any(k in cell for k in loose):
            return i
    return None

def _locate(self, rows):
    """(header_row_index, columns); header index is -1 when headerless."""
    for i, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        low = [c.strip().lower() for c in row]
        url_col = self._pick(low, URL_HEADERS, URL_KEYWORDS)
        id_col = self._pick(low, VIDEO_ID_HEADERS)
        if url_col is not None or id_col is not None:
            return i, {"url": url_col, "id": id_col,
                        "title": self._pick(low, TITLE_HEADERS + TITLE_HEADERS_LOOSE),
                        "status": self._pick(low, STATUS_HEADERS)}
    for row in rows[:HEADER_SEARCH_ROWS]:          # headerless: find a cell with a link
        for i, cell in enumerate(row):
            if URL_IN_TEXT.search(cell):
                return -1, {"url": i, "id": None, "title": None, "status": None}
    return None, None

@staticmethod
def _row_url(row, cols) -> Optional[str]:
    def cell(i):
        return row[i] if i is not None and i < len(row) else ""
    return extract_url(cell(cols["url"])) or extract_url(cell(cols["id"]))

def _parse_csv(self, text: str) -> List[Dict[str, str]]:
    rows, _ = self._rows(text)
    header_idx, cols = self._locate(rows)
    if cols is None:
        return []
    entries = []
    for row in rows[header_idx + 1:]:
        url = self._row_url(row, cols)
        if not url:
            continue
        get = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        entries.append({"url": url, "title": get(cols["title"]),
                        "status": normalize_status(get(cols["status"]))})
    return entries

def _mark_csv(self, text: str, statuses: Dict[str, str]) -> Optional[str]:
    rows, delim = self._rows(text)
    header_idx, cols = self._locate(rows)
    if cols is None:
        return None
    width = max(len(r) for r in rows)
    if header_idx == -1:                            # give a headerless file a header
        header = [""] * width
        header[cols["url"]] = "url"
        rows.insert(0, header)
        header_idx = 0
    if cols["status"] is None:                      # add a status column
        header = rows[header_idx]
        header.extend([""] * (width - len(header)))
        cols["status"] = len(header)
        header.append("status")
    for row in rows[header_idx + 1:]:
        url = self._row_url(row, cols)
        if url in statuses:
            row.extend([""] * (cols["status"] + 1 - len(row)))
            row[cols["status"]] = statuses[url]
    eol = "\r\n" if "\r\n" in text else "\n"
    buf = io.StringIO(newline="")
    csv.writer(buf, delimiter=delim, lineterminator=eol).writerows(rows)
    return buf.getvalue()


def _backoff_seconds(self, streak: int) -> float:
    """Exponential wait after `streak` consecutive throttled links, capped."""
    return min(self.rate_limit_backoff * (2 ** (streak - 1)), self.rate_limit_max_wait)