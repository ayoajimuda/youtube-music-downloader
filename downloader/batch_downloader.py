"""Reading and writing link lists held in a .txt or .csv file"""

import codecs
import csv
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Scan for full links plus YouTube links pasted without a scheme
URL_IN_TEXT = re.compile(
    r"(?:https?://|spotify:)\S+"
    r"|(?<![\w./@-])(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE)

LINK_TRAILING = ",;\"')]>"   # punctuation glued to links in prose, markdown or CSV cells
STATUS_TAG = re.compile(r"\s*#\s*status\s*=\s*(\w+)(?:\s*@[^#]*)?\s*$", re.IGNORECASE)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}   # hosts the batch downloader accepts

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

# Share/analytics parameters that don't change what gets downloaded.
TRACKING_PARAMS = {"si", "feature", "pp", "app", "ab_channel", "t", "start",
                   "index", "utm_source", "utm_medium", "utm_campaign"}

VIDEO_URL = "https://music.youtube.com/watch?v={}"

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

HEADER_SEARCH_ROWS = 10   # how far down a CSV to look for a header row

JSON_MAX_DEPTH = 50

# ==================== Normalisation ====================
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
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if k.lower() not in TRACKING_PARAMS]
    if any(k == "v" for k, _ in query):      # single video: don't pull its playlist
        query = [(k, v) for k, v in query if k != "list"]
    return urlunsplit(("https", host, parts.path, urlencode(query), ""))

def extract_url(text: str, allow_bare_id: bool = False) -> Optional[str]:
    """Bare 11-char IDs are only accepted when the caller knows the cell is an ID column."""
    if not text or text.lstrip().startswith("#"):
        return None
    match = URL_IN_TEXT.search(text)
    if match:
        return normalize_url(match.group())
    if allow_bare_id and VIDEO_ID.fullmatch(text.strip()):
        return VIDEO_URL.format(text.strip())
    return None

def normalize_status(value: str) -> str:
    v = (value or "").strip().lower()
    v = _STATUS_ALIASES.get(v, v)
    return v if v in KNOWN_STATUSES else ""

# ==================== Public API ====================
def parse(path: Path) -> List[Dict[str, str]]:
    """Return [{'url', 'title', 'status'}, ...] for every link in the file."""
    text, _ = _read(path)
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return _parse_csv(text)
    if suffix == ".json":
        return _parse_json(text)
    return _parse_txt(text)

def mark_statuses(path: Path, statuses: Dict[str, str]) -> bool:
    """Write {url: status} back into the file. False means the write failed."""
    if not statuses:
        return True
    path = Path(path)
    try:
        text, enc = _read(path)
        suffix = path.suffix.lower()
        if suffix == ".csv":
            out = _mark_csv(text, statuses)
        elif suffix == ".json":
            out = _mark_json(text, statuses)
        else:
            out = _mark_txt(text, statuses)
        if out is None:
            return False
        return _write(path, out, enc)
    except (OSError, ValueError):     # ValueError covers csv.Error and JSONDecodeError
        return False
    
# ==================== IO ====================
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
        return raw.decode("latin-1"), "latin-1"   # never fails

def _write(path: Path, text: str, enc: str) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(text.encode(enc))
        os.replace(tmp, path)     # atomic; fails if Excel has the file open
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False

# ==================== TXT ====================
def _parse_txt(text: str) -> List[Dict[str, str]]:
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

def _mark_txt(text: str, statuses: Dict[str, str]) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = []
    for line in text.splitlines():
        url = extract_url(line)
        if url in statuses:
            line = STATUS_TAG.sub("", line).rstrip() + f"  # status={statuses[url]} @ {stamp}"
        out.append(line)
    return eol.join(out) + eol

# ==================== CSV ====================
def _rows(text: str):
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delim = ","
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delim)), delim


def _pick(low: List[str], exact, loose=()) -> Optional[int]:
    for name in exact:
        if name in low:
            return low.index(name)
    for i, cell in enumerate(low):       # whole-word match, so "uri" won't hit "security"
        if any(re.search(rf"\b{re.escape(k)}\b", cell) for k in loose):
            return i
    return None

def _locate(rows):
    """(header_row_index, columns); header index is -1 when headerless."""
    for i, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        low = [c.strip().lower() for c in row]
        url_col = _pick(low, URL_HEADERS, URL_KEYWORDS)
        id_col = _pick(low, VIDEO_ID_HEADERS)
        if url_col is not None or id_col is not None:
            return i, {"url": url_col, "id": id_col,
                       "title": _pick(low, TITLE_HEADERS, TITLE_HEADERS_LOOSE),
                       "status": _pick(low, STATUS_HEADERS)}
    for row in rows[:HEADER_SEARCH_ROWS]:        # headerless: find a cell with a link
        for i, cell in enumerate(row):
            if URL_IN_TEXT.search(cell):
                return -1, {"url": i, "id": None, "title": None, "status": None}
    return None, None

def _cell(row, i) -> str:
    return row[i] if i is not None and i < len(row) else ""

def _row_url(row, cols) -> Optional[str]:
    return (extract_url(_cell(row, cols["url"]))
            or extract_url(_cell(row, cols["id"]), allow_bare_id=True))
    
def _parse_csv(text: str) -> List[Dict[str, str]]:
    rows, _ = _rows(text)
    header_idx, cols = _locate(rows)
    if cols is None:
        return []
    entries = []
    for row in rows[header_idx + 1:]:
        url = _row_url(row, cols)
        if not url:
            continue
        entries.append({"url": url,
                        "title": _cell(row, cols["title"]).strip(),
                        "status": normalize_status(_cell(row, cols["status"]))})
    return entries

def _mark_csv(text: str, statuses: Dict[str, str]) -> Optional[str]:
    rows, delim = _rows(text)
    header_idx, cols = _locate(rows)
    if cols is None:
        return None
    width = max((len(r) for r in rows), default=0)
    if header_idx == -1:                          # give a headerless file a header
        header = [""] * width
        header[cols["url"]] = "url"
        rows.insert(0, header)
        header_idx = 0
    if cols["status"] is None:                    # add a status column
        header = rows[header_idx]
        header.extend([""] * (width - len(header)))
        cols["status"] = len(header)
        header.append("status")
    for row in rows[header_idx + 1:]:
        url = _row_url(row, cols)
        if url in statuses:
            row.extend([""] * (cols["status"] + 1 - len(row)))
            row[cols["status"]] = statuses[url]
    eol = "\r\n" if "\r\n" in text else "\n"
    buf = io.StringIO(newline="")
    csv.writer(buf, delimiter=delim, lineterminator=eol).writerows(rows)
    return buf.getvalue()

# ==================== JSON ====================
def _dict_fields(node: dict) -> dict:
    """Pull url/title/status out of one JSON object, using the CSV header lists."""
    keys = list(node.keys())
    low = [str(k).lower() for k in keys]

    def value(i):
        return node[keys[i]] if i is not None else None

    url_i = _pick(low, URL_HEADERS, URL_KEYWORDS)
    id_i = _pick(low, VIDEO_ID_HEADERS)
    title_i = _pick(low, TITLE_HEADERS, TITLE_HEADERS_LOOSE)
    status_i = _pick(low, STATUS_HEADERS)

    url = None
    if isinstance(value(url_i), str):
        url = extract_url(value(url_i))
    if not url and isinstance(value(id_i), str):
        url = extract_url(value(id_i), allow_bare_id=True)

    title = value(title_i)
    status = value(status_i)
    return {
        "url": url,
        "title": str(title).strip() if isinstance(title, str) else "",
        "status": normalize_status(str(status)) if status is not None else "",
        "status_key": keys[status_i] if status_i is not None else None,
    }


def _visit(container, key, depth: int, found: list) -> None:
    """Walk the structure, recording (container, key) for every link entry."""
    item = container[key]
    if isinstance(item, str):
        # A string is only an entry as a list element; a string under a
        # dict key is metadata (a title, a note), not a link to download.
        if isinstance(container, list) and extract_url(item):
            found.append((container, key))
    elif isinstance(item, dict) and _dict_fields(item)["url"]:
        found.append((container, key))
    elif isinstance(item, (list, dict)) and depth < JSON_MAX_DEPTH:
        children = range(len(item)) if isinstance(item, list) else list(item.keys())
        for child in children:
            _visit(item, child, depth + 1, found)


def _load_json(text: str):
    """(holder, found). holder[0] is the parsed root; entries point into it."""
    holder = [json.loads(text)]
    found: list = []
    _visit(holder, 0, 0, found)
    return holder, found


def _parse_json(text: str) -> List[Dict[str, str]]:
    try:
        _, found = _load_json(text)
    except ValueError:
        return []                       # not valid JSON: report "no links found"
    entries = []
    for container, key in found:
        item = container[key]
        if isinstance(item, str):
            entries.append({"url": extract_url(item), "title": "", "status": ""})
        else:
            f = _dict_fields(item)
            entries.append({"url": f["url"], "title": f["title"], "status": f["status"]})
    return entries


def _json_indent(text: str):
    """Keep the file's existing indentation (None means compact, one line)."""
    if "\n" not in text.strip():
        return None
    match = re.search(r"\n([ \t]+)\S", text)
    return match.group(1) if match else 2


def _mark_json(text: str, statuses: Dict[str, str]) -> Optional[str]:
    holder, found = _load_json(text)     # raises ValueError on bad JSON
    if not found:
        return None
    for container, key in found:
        item = container[key]
        if isinstance(item, str):
            url = extract_url(item)
            if url in statuses:          # a bare string can't hold a status
                container[key] = {"url": item, "status": statuses[url]}
        else:
            f = _dict_fields(item)
            if f["url"] in statuses:
                item[f["status_key"] or "status"] = statuses[f["url"]]
    eol = "\r\n" if "\r\n" in text else "\n"
    out = json.dumps(holder[0], indent=_json_indent(text), ensure_ascii=False)
    return out.replace("\n", eol) + eol