"""Retry the downloads recorded in failed_downloads.json.

Module-level: call configure() once at startup (main_menu.build_downloader does),
then run() whenever the user picks "retry failed downloads".

How a run works:
  - Links are tried most-promising first: the fewest failed runs, oldest first.
  - Each link gets up to `max_retries` attempts (from Settings), waiting
    `retry_delay`, then twice that, and so on between them. A throttled link,
    a bot check or a permanent error ("Video is private") stops early, since
    trying again straight away won't help.
  - Links that keep failing are skipped after `give_up_after` failed runs
    (include_exhausted=True retries them anyway).
  - Three throttled links in a row, or a bot check, ends the run; the rest stay
    queued. Successes leave the queue, and are marked done in the batch file
    they came from.

Older code written for the Spotify project can keep calling retry_failed(),
add_failed_track(), clear_failed_tracks() and get_failed_count().
"""

import json
import random
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from colorama import Fore, Style, init

init(autoreset=True)

# Failures another attempt won't fix. Matched against the error from this run's
# attempt, so a private or removed video isn't tried over and over.
PERMANENT_MARKERS = (
    "video is private", "private video", "members-only", "members only",
    "copyright", "video unavailable", "has been removed",
    "account associated with this video has been terminated",
)

# Waiting doesn't clear a bot check; only fresh cookies do. Stop the run.
BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot", "requires authentication")

# Artist/title entries (from the old Spotify-era queue) are retried as a YouTube
# search for "<artist> - <title>"; yt-dlp downloads the top result.
SEARCH_PREFIX = "ytsearch1:"

_STOP_MESSAGES = {
    "cancelled": "Cancelled.",
    "throttled": "Still being throttled - stopped. Try again later; the rest stay queued.",
    "bot_check": "YouTube wants a sign-in (bot check). Refresh your cookies, then retry.",
    "interrupted": "Interrupted - remaining links stay queued.",
}

# One run at a time: two runs draining the same queue would download the same
# links twice and fight over failed_downloads.json.
_run_lock = threading.Lock()

_state: Dict[str, Any] = {
    "downloader": None,
    "log": None,
    "batch_file": None,
    "limiter": None,
    # Failed runs after which a link is skipped. Throttled runs don't count:
    # they were about the host, not the link.
    "give_up_after": 5,
    "max_throttle_streak": 3,
    "output_subfolder": "Retries",
    "show_progress": False,
}


@dataclass
class RetrySummary:
    """What happened to each link in one run."""
    recovered: List[str] = field(default_factory=list)
    still_failing: List[str] = field(default_factory=list)
    skipped_permanent: List[str] = field(default_factory=list)
    skipped_exhausted: List[str] = field(default_factory=list)
    not_attempted: List[str] = field(default_factory=list)
    stopped_reason: str = ""      # "", "cancelled", "throttled", "bot_check", "interrupted"

    @property
    def ok(self) -> bool:
        """True when every link that was tried came back and nothing stopped the run."""
        return not self.still_failing and not self.stopped_reason


# ==================== Setup ====================
def configure(downloader, log, batch_file=None, limiter=None, *,
              give_up_after: int = 5, max_throttle_streak: int = 3,
              output_subfolder: str = "Retries", show_progress: bool = False,
              max_attempts: Optional[int] = None) -> None:
    """
    Hand the module the pieces it works with. Call once at startup.

    downloader  the YoutubeMusicDownloader (run_download, output_directory,
                max_retries, retry_delay, yt_dlp_sleep_min/max, rate_limit_*)
    log         managers.log_manager
    batch_file  downloader.batch_downloader (optional)
    limiter     downloader.rate_limiter (optional)

    max_attempts is the old name for give_up_after.
    """
    give_up_after = max_attempts if max_attempts is not None else give_up_after
    if give_up_after < 1:
        raise ValueError("give_up_after must be at least 1")
    if max_throttle_streak < 1:
        raise ValueError("max_throttle_streak must be at least 1")
    _state.update(downloader=downloader, log=log, batch_file=batch_file,
                  limiter=limiter, give_up_after=give_up_after,
                  max_throttle_streak=max_throttle_streak,
                  output_subfolder=output_subfolder, show_progress=show_progress)


def is_configured() -> bool:
    return _state["downloader"] is not None and _state["log"] is not None


def _require() -> Tuple[Any, Any]:
    if not is_configured():
        raise RuntimeError("retry_manager.configure(downloader, log) must be called first "
                           "(main_menu.build_downloader() does this at startup)")
    return _state["downloader"], _state["log"]


def _contains(text: str, markers) -> bool:
    low = (text or "").lower()
    return any(m in low for m in markers)


# ==================== Old queue entries ====================
def search_url(artist: str, track: str) -> str:
    """A yt-dlp search 'URL' for an artist/title pair."""
    query = " - ".join(part.strip() for part in (artist or "", track or "") if part.strip())
    return f"{SEARCH_PREFIX}{query}"


def _legacy_items(data) -> List[dict]:
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [e for e in items if isinstance(e, dict) and not e.get("url")
            and (e.get("track") or e.get("title"))]


def import_legacy_failures(path=None) -> int:
    """
    Convert artist/title entries (the old Spotify-project format, with no link)
    into searchable entries in the current queue. Returns how many were added.

    With no path, converts any such entries already in failed_downloads.json,
    which the log manager would otherwise skip and then drop on its next write.
    """
    _, log = _require()
    path = Path(path) if path else Path(log.FAILED_FILE)
    try:
        text = path.read_text(encoding="utf-8").strip()
        data = json.loads(text) if text else []
    except (OSError, ValueError):
        return 0
    legacy = _legacy_items(data)
    if not legacy:
        return 0

    with log._lock:
        records = log.read_failures()
        added = 0
        for old in legacy:
            artist = str(old.get("artist") or "")
            track = str(old.get("track") or old.get("title") or "")
            url = search_url(artist, track)
            if url == SEARCH_PREFIX or url in records:
                continue
            attempts = int(old.get("attempt_count", 0) or 0)
            records[url] = {
                "url": url,
                "title": f"{artist} - {track}" if artist else track,
                "item_type": "track", "source": "", "attempt_count": attempts,
            }
            added += 1
        if added:
            log._write_failures(records)
    if added:
        print(f"{Fore.CYAN}Converted {added} older artist/title entr"
              f"{'y' if added == 1 else 'ies'} into YouTube searches.{Style.RESET_ALL}")
    return added


# ==================== Selection ====================
def real_attempts(entry: dict) -> int:
    """Failed runs on record (throttled failures aren't counted when stored)."""
    return int(entry.get("attempt_count", 0) or 0)


def is_permanent(entry: dict) -> bool:
    """
    No error is stored any more, so nothing is known to be permanent before
    trying. A permanent error still ends that link's attempts straight away,
    and the link is skipped once it has failed give_up_after times.
    """
    return False


def pending(include_permanent: bool = False,
            include_exhausted: bool = False) -> Tuple[List[dict], List[dict], List[dict]]:
    """(to_retry, permanent, exhausted). to_retry is sorted most-promising first."""
    _, log = _require()
    import_legacy_failures()
    queue, permanent, exhausted = [], [], []
    for entry in log.read_failures().values():
        if not include_permanent and is_permanent(entry):
            permanent.append(entry)
        elif not include_exhausted and real_attempts(entry) >= _state["give_up_after"]:
            exhausted.append(entry)
        else:
            queue.append(entry)
    queue.sort(key=real_attempts)        # stable: equal counts stay oldest first
    return queue, permanent, exhausted


# ==================== Attempts ====================
def _template(item_type: str) -> str:
    downloader, _ = _require()
    root = Path(downloader.output_directory) / _state["output_subfolder"]
    if item_type == "album":
        return str(root / "%(artist)s/%(album)s/%(artist)s - %(title)s.%(ext)s")
    if item_type == "playlist":
        return str(root / "%(playlist)s/%(artist)s - %(title)s.%(ext)s")
    return str(root / "%(artist)s - %(title)s.%(ext)s")


def _attempt(entry: dict) -> Tuple[bool, str, bool]:
    """(success, error, throttled) for one yt-dlp run."""
    downloader, _ = _require()
    item_type = entry.get("item_type") or "track"
    args = ["--ignore-errors"] if item_type in ("album", "playlist") else []
    if _state["limiter"]:
        _state["limiter"].acquire()
    try:
        downloader.run_download(entry["url"], _template(item_type), args,
                                show_progress=_state["show_progress"])
        return True, "", False
    except subprocess.CalledProcessError as error:
        # Prefer the downloader's classified reason, then the output tail;
        # str(error) is just the command line.
        reason = (getattr(error, "reason", "")
                  or (error.output or "")[-300:]
                  or str(error))
        return False, reason[:300], bool(getattr(error, "throttled", False))
    except RuntimeError:
        raise                       # yt-dlp is missing; no point continuing
    except Exception as error:      # anything else: record it, keep going
        return False, str(error)[:300], False


def _retry_settings() -> Tuple[int, float, float]:
    """(attempts per link, first delay, longest delay), from the downloader's settings."""
    downloader, _ = _require()
    attempts = max(1, int(getattr(downloader, "max_retries", 1) or 1))
    delay = max(0.0, float(getattr(downloader, "retry_delay", 0) or 0))
    cap = float(getattr(downloader, "rate_limit_max_wait", 1800) or 1800)
    return attempts, delay, cap


def _try_link(entry: dict) -> Tuple[bool, str, bool]:
    """Up to max_retries attempts with exponential backoff. (success, error, throttled)."""
    attempts, delay, cap = _retry_settings()
    error, throttled = "", False
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            wait = min(cap, delay * 2 ** (attempt - 2))
            print(f"      {Style.DIM}attempt {attempt}/{attempts}"
                  f"{f' in {wait:.0f}s' if wait else ''}{Style.RESET_ALL}")
            if wait:
                time.sleep(wait)
        ok, error, throttled = _attempt(entry)
        if ok:
            return True, "", False
        # Trying again straight away won't help with any of these.
        if throttled or _contains(error, BOT_CHECK_MARKERS) or \
                _contains(error, PERMANENT_MARKERS):
            break
    return False, error, throttled


# ==================== Pacing ====================
def _backoff_seconds(streak: int) -> float:
    downloader, _ = _require()
    base = float(getattr(downloader, "rate_limit_backoff", 300))
    cap = float(getattr(downloader, "rate_limit_max_wait", 1800))
    return min(cap, base * 2 ** (streak - 1))


def _pause(throttle_streak: int) -> None:
    downloader, _ = _require()
    limiter = _state["limiter"]
    if throttle_streak:
        wait = _backoff_seconds(throttle_streak)
        print(f"      {Fore.YELLOW}Throttled - waiting ~{wait:.0f}s before the next link"
              f"{Style.RESET_ALL}")
        if limiter:
            limiter.penalize(wait)          # the next acquire() does the waiting
        else:
            time.sleep(wait * random.uniform(1.0, 1.1))
    elif not limiter:
        low = getattr(downloader, "yt_dlp_sleep_min", 3)
        high = getattr(downloader, "yt_dlp_sleep_max", 7)
        time.sleep(random.uniform(low, max(low, high)))


# ==================== Bookkeeping ====================
def _clear(urls: List[str]) -> None:
    """Remove recovered links from failed_downloads.json."""
    if not urls:
        return
    _, log = _require()
    clear = getattr(log, "clear_failures", None)
    if clear:
        clear(urls)
        return
    with log._lock:
        records = log.read_failures()
        for url in urls:
            records.pop(url, None)
        log._write_failures(records)


def _mirror_to_sources(per_source: Dict[str, Dict[str, str]]) -> None:
    """Mark recovered links as done in the batch file each one came from."""
    batch_file = _state["batch_file"]
    if not batch_file:
        return
    for source, statuses in per_source.items():
        path = Path(source)
        if path.is_file():
            batch_file.mark_statuses(path, statuses)


# ==================== Output ====================
def _name(entry: dict) -> str:
    title = entry.get("title") or entry["url"]
    return title[len(SEARCH_PREFIX):] if title.startswith(SEARCH_PREFIX) else title


def _print_plan(queue, permanent, exhausted) -> None:
    attempts, delay, _ = _retry_settings()
    print(f"\n{Fore.CYAN}Failed downloads on record:{Style.RESET_ALL} "
          f"{len(queue) + len(permanent) + len(exhausted)}")
    print(f"  {Fore.GREEN}To retry:{Style.RESET_ALL} {len(queue)}  "
          f"{Style.DIM}(up to {attempts} attempt{'s' if attempts != 1 else ''} each"
          f"{f', {delay:.0f}s apart and doubling' if attempts > 1 and delay else ''})"
          f"{Style.RESET_ALL}")
    if permanent:
        print(f"  {Fore.RED}Skipped, permanent error:{Style.RESET_ALL} {len(permanent)}")
    if exhausted:
        print(f"  {Fore.RED}Skipped, failed {_state['give_up_after']}+ times:"
              f"{Style.RESET_ALL} {len(exhausted)}")
    for entry in queue[:10]:
        search = f"{Style.DIM} [search]{Style.RESET_ALL}" if \
            entry["url"].startswith(SEARCH_PREFIX) else ""
        print(f"      - {_name(entry)[:55]} "
              f"{Style.DIM}(failed {real_attempts(entry)}x){Style.RESET_ALL}{search}")
    if len(queue) > 10:
        print(f"      ...and {len(queue) - 10} more")
    print()


def _print_summary(summary: RetrySummary) -> None:
    print()
    print(f"{Fore.CYAN}Retry run finished{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}Recovered:{Style.RESET_ALL} {len(summary.recovered)}")
    if summary.still_failing:
        print(f"  {Fore.RED}Still failing:{Style.RESET_ALL} {len(summary.still_failing)}")
    if summary.not_attempted:
        print(f"  {Fore.YELLOW}Not attempted:{Style.RESET_ALL} {len(summary.not_attempted)}")
    if summary.stopped_reason:
        print(f"  {Fore.YELLOW}{_STOP_MESSAGES[summary.stopped_reason]}{Style.RESET_ALL}")


# ==================== Run ====================
def run(include_permanent: bool = False, include_exhausted: bool = False,
        limit: Optional[int] = None, dry_run: bool = False,
        confirm: Optional[Callable[[str], bool]] = None) -> RetrySummary:
    """
    Retry every eligible link in failed_downloads.json, one at a time.

    include_permanent  kept for older callers; no effect (no errors are stored)
    include_exhausted  also retry links that have failed give_up_after times
    limit              retry at most this many links this run
    dry_run            print the plan, download nothing
    confirm            called with a question before starting; False cancels
    """
    _, log = _require()
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("A retry run is already in progress")
    try:
        return _run(log, include_permanent, include_exhausted, limit, dry_run, confirm)
    finally:
        _run_lock.release()


def _run(log, include_permanent, include_exhausted, limit, dry_run, confirm) -> RetrySummary:
    queue, permanent, exhausted = pending(include_permanent, include_exhausted)
    summary = RetrySummary(skipped_permanent=[e["url"] for e in permanent],
                           skipped_exhausted=[e["url"] for e in exhausted])
    if limit:
        queue = queue[:limit]

    _print_plan(queue, permanent, exhausted)
    if not queue or dry_run:
        summary.not_attempted = [e["url"] for e in queue]
        return summary
    if confirm and not confirm(f"Retry these {len(queue)} links? (y/n)"):
        summary.stopped_reason = "cancelled"
        summary.not_attempted = [e["url"] for e in queue]
        _print_summary(summary)
        return summary

    recovered_urls: List[str] = []
    per_source: Dict[str, Dict[str, str]] = {}
    streak = 0
    total = len(queue)

    try:
        for index, entry in enumerate(queue, 1):
            url = entry["url"]
            title = entry.get("title", "")
            item_type = entry.get("item_type") or "track"
            print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {_name(entry)[:65]}")

            ok, error, throttled = _try_link(entry)

            if ok:
                streak = 0
                summary.recovered.append(url)
                recovered_urls.append(url)
                if entry.get("source"):
                    per_source.setdefault(entry["source"], {})[url] = "success"
                log.log_success(f"Recovered on retry: {_name(entry)}", url=url,
                                item_type=item_type, console=False)
                print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
            else:
                summary.still_failing.append(url)
                log.record_failure(url, title, error, entry.get("source", ""),
                                   throttled=throttled, item_type=item_type)
                if _contains(error, BOT_CHECK_MARKERS):
                    summary.stopped_reason = "bot_check"
                    print(f"      {Fore.RED}bot check{Style.RESET_ALL}")
                    break
                streak = streak + 1 if throttled else 0
                print(f"      {Fore.RED}{'still throttled' if throttled else 'still failing'}"
                      f"{Style.RESET_ALL}")

            # Draining the queue into a host that's still refusing just
            # re-queues everything with a higher count. Stop and come back.
            if streak >= _state["max_throttle_streak"]:
                summary.stopped_reason = "throttled"
                break
            if index < total:
                _pause(streak)
    except KeyboardInterrupt:
        summary.stopped_reason = "interrupted"
        print()
    finally:
        # Runs even on Ctrl-C, so recovered links never stay queued.
        _clear(recovered_urls)
        _mirror_to_sources(per_source)

    tried = set(summary.recovered) | set(summary.still_failing)
    summary.not_attempted = [e["url"] for e in queue if e["url"] not in tried]
    _print_summary(summary)
    if hasattr(log, "log_info"):
        stopped = f", stopped ({summary.stopped_reason})" if summary.stopped_reason else ""
        log.log_info(f"Retry run: {len(summary.recovered)} recovered, "
                     f"{len(summary.still_failing)} still failing{stopped}", console=False)
    return summary


# ==================== Spotify-project names ====================
def retry_failed(config=None) -> RetrySummary:
    """
    Old entry point. The config argument is accepted and ignored: attempts and
    delays now come from the downloader's settings (Settings menu).
    """
    return run()


def add_failed_track(artist: str, track: str, error: Optional[str] = None,
                     config=None, url: str = "") -> dict:
    """Queue a failed track. Without a link, it's retried as a YouTube search."""
    _, log = _require()
    title = f"{artist} - {track}" if artist else track
    return log.record_failure(url or search_url(artist, track), title, error or "",
                              item_type="track",
                              metadata={"artist": artist, "title": track})


def clear_failed_tracks() -> None:
    """Empty the retry queue."""
    _, log = _require()
    if hasattr(log, "clear_log"):
        log.clear_log("retry_queue")
    else:
        with log._lock:
            log._write_failures({})
    if hasattr(log, "log_info"):
        log.log_info("Cleared failed downloads list.")


def get_failed_count() -> int:
    """How many links are in the retry queue."""
    _, log = _require()
    import_legacy_failures()
    return len(log.read_failures())