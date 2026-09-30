"""Everything about retrying a download, in one place.

Merges the old RetryQueue (the JSON record of links that failed) with the retry
behaviour that used to live inside YoutubeMusicDownloader:

  * attempt()          - the per-link retry loop (was _download_with_retry)
  * pause_between()    - how long to wait between links, with throttle backoff
  * run_queue()        - drain the queue interactively (was download_from_retry_queue)
  * add_failure() etc. - the persistent queue itself

Module-level: call configure(downloader, cookies) once at startup, then call the
functions directly, or assign the module to an attribute
(self.retry_queue = retry_manager) so existing call sites keep working.

The module is handed the downloader and calls back into its public API
(run_download, the retry/sleep settings, output_directory, batch_file,
log_manager) and to the cookie service for the pre-run cookie report. It never
imports the downloader, so there is no circular import.

The queue owns its own lock, so callers on several threads needn't hold one.
Reads never raise: a queue file hand-edited into invalid JSON reports the
problem and comes back empty rather than taking a download run down with it.
"""

import datetime
import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from colorama import Fore, Style

from assist_methods import cleanup_directory
from tools.EnhancedMenu import Enhanced_Menu

# After this many throttled links in a row, callers should stop: every
# remaining link would fail the same way and burn its full retry budget.
THROTTLE_STREAK_LIMIT = 3

_lock = threading.Lock()
_downloader = None
_cookies = None

# After N consecutive throttled links the wait is base * 2^(N-1), capped at max.
_state = {
    "path": Path("history/retry_queue.json"),
    "backoff_base": 300,
    "backoff_max": 1800,
}


def configure(downloader, cookies, path="history/retry_queue.json",
              backoff_base: int = 300, backoff_max: int = 1800) -> None:
    """Point the module at the downloader and the cookie service. Call once at startup."""
    global _downloader, _cookies
    with _lock:
        _downloader = downloader
        _cookies = cookies
        _state["path"] = Path(path)
        _state["backoff_base"] = backoff_base
        _state["backoff_max"] = backoff_max
    _state["path"].parent.mkdir(parents=True, exist_ok=True)


def queue_path() -> Path:
    """Where the queue is stored. `path` on the old object."""
    return _state["path"]


def _require_downloader():
    if _downloader is None:
        raise RuntimeError("retry_manager.configure(downloader, cookies) has not been called")
    return _downloader


def _on_error(message: str) -> None:
    try:
        _downloader.log_manager.log_error(message)
    except Exception:
        pass


# ==================== Queue storage ====================
def read() -> Dict[str, dict]:
    """Load the queue as {url: entry}. Never raises."""
    path = _state["path"]
    try:
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        _on_error(f"Could not read retry queue: {error}")
        return {}

    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}
    return {e["url"]: e for e in items if isinstance(e, dict) and e.get("url")}


def count() -> int:
    """How many links are currently waiting."""
    return len(read())


def _write(queue: Dict[str, dict]) -> None:
    path = _state["path"]
    payload = {
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(queue),
        "items": sorted(queue.values(), key=lambda e: e.get("last_failed", "")),
    }
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError as error:
        _on_error(f"Could not write retry queue: {error}")


def add_failure(url: str, title: str, error: str, source: str,
                throttled: bool = False, item_type: str = "track") -> None:
    """
    Add or update one failed link. Attempts accumulate across runs.

    `throttled` records that the host was refusing traffic rather than the
    link being bad - the two are worth telling apart, since a throttled
    link will very likely work later while a dead one never will. It is
    counted separately so a link isn't written off after three attempts
    that were never really about it.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _lock:
        queue = read()
        entry = queue.get(url, {
            "url": url,
            "title": title,
            "source": source,
            "item_type": item_type,
            "attempts": 0,
            "throttled_attempts": 0,
            "first_failed": now,
        })
        entry["title"] = title or entry.get("title", "")
        entry["source"] = source or entry.get("source", "")
        entry["item_type"] = item_type or entry.get("item_type", "track")
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        if throttled:
            entry["throttled_attempts"] = int(entry.get("throttled_attempts", 0)) + 1
        entry["throttled"] = bool(throttled)
        entry["last_failed"] = now
        entry["last_error"] = (error or "")[:300]
        queue[url] = entry
        _write(queue)


def throttled_count() -> int:
    """How many queued links last failed because of throttling."""
    return sum(1 for e in read().values() if e.get("throttled"))


def clear(urls: Iterable[str]) -> None:
    """Drop links that have since succeeded."""
    urls = set(urls)
    if not urls:
        return
    with _lock:
        queue = read()
        remaining = {u: e for u, e in queue.items() if u not in urls}
        if len(remaining) != len(queue):
            _write(remaining)


# ==================== Retrying one link ====================
def attempt(url: str, output_template: str, additional_args: list = None,
            item_type: str = "item", show_progress: bool = True) -> Tuple[bool, str, bool]:
    """
    Download one link, retrying up to downloader.max_retries times.

    Returns (success, last_error, throttled) so callers that batch many
    links can report why each one failed without re-parsing the logs, and
    can tell a bad link apart from a refusing host.
    """
    d = _require_downloader()
    last_error = ""
    last_throttled = False
    for number in range(1, d.max_retries + 1):
        if show_progress:
            Enhanced_Menu.print_section(
                f"Downloading {item_type} (Attempt {number}/{d.max_retries})")
        if number > 1:
            if show_progress:
                print(f"Waiting {d.retry_delay} seconds before retry...")
            time.sleep(d.retry_delay)

        try:
            result = d.run_download(url, output_template, additional_args,
                                    show_progress=show_progress)
            # run_download only ever returns code 0 or raises, so this is the success path.
            if result and result.returncode == 0:
                d.log_manager.log_success(f"Successfully downloaded {item_type}: {url}")
                if item_type in ("album", "playlist"):
                    cleanup_directory(d.output_directory, d.log_manager)
                return True, "", False
        except subprocess.CalledProcessError as error:
            last_error = str(error)[:300]
            last_throttled = getattr(error, "throttled", False)
            if number < d.max_retries:
                d.log_manager.log_error(
                    f"Attempt {number} failed for {item_type}: {last_error[:100]}")
            else:
                d.log_manager.log_failure(f"Failed after {d.max_retries} attempts: {url}")
        except RuntimeError:
            # yt-dlp is missing - retrying will not help.
            raise
        except Exception as error:
            last_error = str(error)[:300]
            d.log_manager.log_error(f"Unexpected error in attempt {number}: {error}")
            if number == d.max_retries:
                d.log_manager.log_failure(f"Failed after {d.max_retries} attempts: {url}")
    return False, last_error, last_throttled


# ==================== Pacing between links ====================
def backoff_seconds(streak: int) -> float:
    """Exponential wait after `streak` consecutive throttled links, capped."""
    return min(_state["backoff_base"] * (2 ** (streak - 1)), _state["backoff_max"])


def should_stop(streak: int) -> bool:
    """True once enough throttled links in a row say the host is refusing us."""
    return streak >= THROTTLE_STREAK_LIMIT


def pause_between(streak: int) -> None:
    """
    Wait before the next link in a batch or retry loop.

    With no throttle streak this is a short random pause (yt-dlp's own
    --sleep-interval only applies within one invocation, not between them).
    With a streak it is the exponential backoff. Ctrl-C during the sleep
    raises KeyboardInterrupt into the caller's existing handler.
    """
    if streak:
        wait = backoff_seconds(streak)
        Enhanced_Menu.print_status(
            f"Throttled - waiting {wait:.0f}s before the next link", "warning")
        time.sleep(wait)
    else:
        d = _require_downloader()
        time.sleep(random.uniform(d.yt_dlp_sleep_min, d.yt_dlp_sleep_max))


# ==================== Draining the queue ====================
def run_queue() -> bool:
    """Re-attempt every link sitting in the retry queue."""
    d = _require_downloader()
    Enhanced_Menu.clear_screen()
    Enhanced_Menu.print_header("Retry Queue", "Re-attempt previously failed links")

    queue = read()
    if not queue:
        Enhanced_Menu.print_status("The retry queue is empty.", "info")
        return True

    # Throttled links first: they are the ones most likely to work now.
    items = sorted(queue.values(),
                   key=lambda e: (not e.get("throttled"), int(e.get("attempts", 0))))
    throttled_total = sum(1 for e in items if e.get("throttled"))

    print(f"  {Fore.CYAN}Queued links:{Style.RESET_ALL} {len(items)}")
    if throttled_total:
        print(f"  {Fore.YELLOW}Last failed to throttling:{Style.RESET_ALL} {throttled_total} "
              f"{Style.DIM}(likely to work now){Style.RESET_ALL}")
    for entry in items[:10]:
        tag = f"{Fore.YELLOW} [throttled]{Style.RESET_ALL}" if entry.get("throttled") else ""
        print(f"      - {str(entry.get('title') or entry['url'])[:55]} "
              f"{Style.DIM}({entry.get('attempts', 0)} attempts){Style.RESET_ALL}{tag}")
    if len(items) > 10:
        print(f"      ...and {len(items) - 10} more")
    print()
    if _cookies is not None:
        _cookies.preflight()

    if not Enhanced_Menu.get_input(f"Retry these {len(items)} links? (y/n)",
                                   "yn", default=True):
        Enhanced_Menu.print_status("Cancelled", "info")
        return False

    target = d.output_directory / "Retries"
    target.mkdir(parents=True, exist_ok=True)
    # An album queued as a whole still wants its artist/album folders.
    templates = {
        "album": str(target / "%(artist)s/%(album)s/%(artist)s - %(title)s.%(ext)s"),
        "playlist": str(target / "%(playlist)s/%(artist)s - %(title)s.%(ext)s"),
    }
    default_template = str(target / "%(artist)s - %(title)s.%(ext)s")

    succeeded = failed = 0
    per_source: Dict[str, Dict[str, str]] = {}
    cleared: List[str] = []
    streak = 0
    total = len(items)

    try:
        for index, entry in enumerate(items, 1):
            url = entry["url"]
            label = entry.get("title") or url
            print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(label)[:65]}")

            item_type = entry.get("item_type", "track")
            ok, error, throttled = attempt(
                url, templates.get(item_type, default_template),
                item_type=item_type, show_progress=False)

            source = entry.get("source")
            if ok:
                succeeded += 1
                cleared.append(url)
                streak = 0
                if source:
                    per_source.setdefault(source, {})[url] = "success"
                print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
            else:
                failed += 1
                add_failure(url, entry.get("title", ""), error, source or "",
                            throttled=throttled, item_type=item_type)
                if throttled:
                    streak += 1
                    print(f"      {Fore.RED}still throttled{Style.RESET_ALL}")
                else:
                    streak = 0
                    print(f"      {Fore.RED}still failing{Style.RESET_ALL}")

            # Draining the queue into a still-throttled host just re-queues
            # everything with a higher attempt count. Stop and come back.
            if should_stop(streak):
                Enhanced_Menu.print_status(
                    "Still being throttled - stopping. The rest stay queued.", "warning")
                break

            if index < total:
                pause_between(streak)
    except KeyboardInterrupt:
        print()
        Enhanced_Menu.print_status("Interrupted - remaining links stay queued.", "warning")

    clear(cleared)
    # Mirror the successes back into whichever file each link came from.
    for source, statuses in per_source.items():
        source_path = Path(source)
        if source_path.is_file():
            d.batch_file.mark_statuses(source_path, statuses)

    print()
    Enhanced_Menu.print_header("Retry Complete")
    print(f"  {Fore.GREEN}Recovered:{Style.RESET_ALL} {succeeded}")
    if failed:
        print(f"  {Fore.RED}Still queued:{Style.RESET_ALL} {failed}")

    if succeeded:
        cleanup_directory(d.output_directory, d.log_manager)
    return failed == 0