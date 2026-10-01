"""Retry the downloads recorded in failed_downloads.json.

Module-level, like the config and rate limiter modules: call configure() once
at startup, then run() whenever the user picks "retry failed downloads".

The project's other modules are passed in rather than imported, so file and
package names don't matter:

    downloader  a YoutubeMusicDownloader (uses run_download, output_directory,
                yt_dlp_sleep_min/max, rate_limit_backoff, rate_limit_max_wait)
    log         the logging module (read_failures, record_failure, log_success,
                and clear_failures if present)
    batch_file  optional: successes are written back into the file each link
                came from, so re-running that file skips them
    limiter     optional: the rate limiter module (acquire, penalize)

Usage:
    import retry_manager
    retry_manager.configure(downloader, log_manager, batch_file, rate_limiter)
    summary = retry_manager.run()
"""

import random
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from colorama import Fore, Style, init

init(autoreset=True)

# Failures another attempt won't fix. Matched against the stored last_error,
# which carries the downloader's classification (e.g. "- Video is private").
PERMANENT_MARKERS = (
    "video is private", "private video", "members-only", "members only",
    "copyright", "video unavailable", "has been removed",
    "account associated with this video has been terminated",
)

# Waiting doesn't clear a bot check; only fresh cookies do. Stop the run.
BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot", "requires authentication")

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
    # Non-throttled attempts after which a link is written off. Throttled
    # attempts don't count: they were about the host, not the link.
    "max_attempts": 5,
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
              max_attempts: int = 5, max_throttle_streak: int = 3,
              output_subfolder: str = "Retries", show_progress: bool = False) -> None:
    """Hand the module the pieces it works with. Call once at startup."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if max_throttle_streak < 1:
        raise ValueError("max_throttle_streak must be at least 1")
    _state.update(downloader=downloader, log=log, batch_file=batch_file,
                  limiter=limiter, max_attempts=max_attempts,
                  max_throttle_streak=max_throttle_streak,
                  output_subfolder=output_subfolder, show_progress=show_progress)


def _require() -> Tuple[Any, Any]:
    downloader, log = _state["downloader"], _state["log"]
    if downloader is None or log is None:
        raise RuntimeError("retry_manager.configure(downloader, log) must be called first")
    return downloader, log


def _contains(text: str, markers) -> bool:
    low = (text or "").lower()
    return any(m in low for m in markers)


# ==================== Selection ====================
def real_attempts(entry: dict) -> int:
    """Attempts that failed for reasons other than throttling."""
    total = int(entry.get("attempt_count", entry.get("attempts", 0)) or 0)
    throttled = int(entry.get("throttled_attempts", 0) or 0)
    return max(0, total - throttled)


def is_permanent(entry: dict) -> bool:
    """True if the last error was one another attempt won't fix."""
    return _contains(entry.get("last_error", ""), PERMANENT_MARKERS)


def pending(include_permanent: bool = False,
            include_exhausted: bool = False) -> Tuple[List[dict], List[dict], List[dict]]:
    """(to_retry, permanent, exhausted). to_retry is sorted most-promising first."""
    _, log = _require()
    queue, permanent, exhausted = [], [], []
    for entry in log.read_failures().values():
        if not include_permanent and is_permanent(entry):
            permanent.append(entry)
        elif not include_exhausted and real_attempts(entry) >= _state["max_attempts"]:
            exhausted.append(entry)
        else:
            queue.append(entry)
    # Throttled links first (most likely to work now), then fewest real
    # attempts, then the ones that failed longest ago.
    queue.sort(key=lambda e: (not e.get("throttled"), real_attempts(e),
                              e.get("last_failed", "")))
    return queue, permanent, exhausted


# ==================== One attempt ====================
def _template(item_type: str) -> str:
    downloader, _ = _require()
    root = Path(downloader.output_directory) / _state["output_subfolder"]
    if item_type == "album":
        return str(root / "%(artist)s/%(album)s/%(artist)s - %(title)s.%(ext)s")
    if item_type == "playlist":
        return str(root / "%(playlist)s/%(artist)s - %(title)s.%(ext)s")
    return str(root / "%(artist)s - %(title)s.%(ext)s")


def _attempt(entry: dict) -> Tuple[bool, str, bool]:
    """(success, error, throttled). One yt-dlp run; this module owns the retries."""
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
    # Older logger without clear_failures: same read-modify-write, same lock.
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
def _print_plan(queue, permanent, exhausted) -> None:
    print(f"\n{Fore.CYAN}Failed downloads on record:{Style.RESET_ALL} "
          f"{len(queue) + len(permanent) + len(exhausted)}")
    print(f"  {Fore.GREEN}To retry:{Style.RESET_ALL} {len(queue)}")
    throttled = sum(1 for e in queue if e.get("throttled"))
    if throttled:
        print(f"    {Fore.YELLOW}{throttled} last failed to throttling "
              f"(likely to work now){Style.RESET_ALL}")
    if permanent:
        print(f"  {Fore.RED}Skipped, permanent error:{Style.RESET_ALL} {len(permanent)}")
    if exhausted:
        print(f"  {Fore.RED}Skipped, {_state['max_attempts']}+ failed attempts:"
              f"{Style.RESET_ALL} {len(exhausted)}")
    for entry in queue[:10]:
        tag = f"{Fore.YELLOW} [throttled]{Style.RESET_ALL}" if entry.get("throttled") else ""
        print(f"      - {str(entry.get('title') or entry['url'])[:55]} "
              f"{Style.DIM}({real_attempts(entry)} attempts){Style.RESET_ALL}{tag}")
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

    include_permanent  also retry private/removed/copyright failures
    include_exhausted  also retry links past max_attempts
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
            print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(title or url)[:65]}")

            ok, error, throttled = _attempt(entry)

            if ok:
                streak = 0
                summary.recovered.append(url)
                recovered_urls.append(url)
                if entry.get("source"):
                    per_source.setdefault(entry["source"], {})[url] = "success"
                log.log_success(f"Recovered on retry: {title or url}", url=url,
                                metadata=entry.get("metadata"),
                                item_type=item_type, console=False)
                print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
            else:
                summary.still_failing.append(url)
                log.record_failure(url, title, error, entry.get("source", ""),
                                   throttled=throttled, item_type=item_type,
                                   metadata=entry.get("metadata"))
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
    return summary