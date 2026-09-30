"""Retrying downloads: the struggling ones, and the ones already written off.

The record of what failed lives in logs_manager (history/failed_downloads.json).
This module decides what to do about it:

  1. attempt()            - a link failing right now, retried in place with
                            exponential backoff
  2. run_queue()          - work through everything on record
  3. import_failed_log()  - recover links that only exist in the text
                            failed.log (a crash, an older version) and put
                            them on record

Module-level: call configure(downloader) once at startup, then call the
functions directly, or assign the module to an attribute
(self.retry_queue = retry_manager) so existing call sites keep working.
"""

import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from colorama import Fore, Style

import batch_file
import logs_manager
from assist_methods import cleanup_directory
from tools.EnhancedMenu import Enhanced_Menu

# After this many throttled links in a row, stop: every remaining link would
# fail the same way and burn its full retry budget doing it.
THROTTLE_STREAK_LIMIT = 3

# A link that has failed this often without ever being throttled is probably
# dead (private, deleted, region-locked) rather than unlucky.
GIVE_UP_AFTER = 5

_lock = threading.RLock()
_downloader = None
_cookies = None
_state = {"backoff_base": 300, "backoff_max": 1800}


def configure(downloader, cookies=None,
              backoff_base: int = 300, backoff_max: int = 1800) -> None:
    """
    Point the module at the downloader. Call once at startup.

    backoff_base / backoff_max: after N consecutive throttled links the wait
    is backoff_base * 2^(N-1) seconds, capped at backoff_max.
    """
    global _downloader, _cookies
    with _lock:
        _downloader = downloader
        _cookies = cookies
        _state["backoff_base"] = backoff_base
        _state["backoff_max"] = backoff_max


def _require_downloader():
    if _downloader is None:
        raise RuntimeError("retry_manager.configure(downloader) has not been called")
    return _downloader


# ==================== The record (kept by logs_manager) ====================
def read() -> Dict[str, dict]:
    """Every failed link on record, as {url: entry}."""
    return logs_manager.read_failures()


def add_failure(url: str, title: str = "", error: str = "", source: str = "",
                throttled: bool = False, item_type: str = "track") -> None:
    """Put a link on record, or bump the one already there."""
    logs_manager.record_failure(url, title, error, source,
                                throttled=throttled, item_type=item_type)


def clear(urls: Iterable[str]) -> int:
    """Drop links that have since succeeded."""
    return logs_manager.clear_failures(urls)


def count() -> int:
    return logs_manager.failure_count()


def throttled_count() -> int:
    """How many recorded links last failed because of throttling."""
    return sum(1 for e in read().values() if e.get("throttled"))


def queue_path() -> Path:
    return logs_manager.FAILED_FILE


def hopeless(entry: dict) -> bool:
    """Failed often, never because of throttling: the link itself is the problem."""
    return (int(entry.get("attempt_count", 0)) >= GIVE_UP_AFTER
            and not int(entry.get("throttled_attempts", 0)))


def give_up(url: str) -> None:
    """Drop one link and mark it skipped in its source file, so re-runs pass it by."""
    entry = read().get(url)
    clear([url])
    if entry and entry.get("source"):
        source = Path(entry["source"])
        if source.is_file():
            batch_file.mark_statuses(source, {url: "skipped"})


def prune() -> int:
    """Drop every link that has failed too often on its own merits."""
    doomed = [url for url, entry in read().items() if hopeless(entry)]
    for url in doomed:
        give_up(url)
    return len(doomed)


# ==================== Recovering links from failed.log ====================
def scan_failed_log(path: Optional[Path] = None) -> Dict[str, str]:
    """
    Pull every URL out of the text failed.log.

    Returns {url: the line it came from}. The log is free text, so this looks
    for links rather than parsing the message format, which has changed
    between versions.
    """
    path = Path(path) if path else logs_manager.LOG_PATHS["failed"]
    found: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return found
    for line in lines:
        url = batch_file.extract_url(line)
        if url:
            found[url] = line.strip()[-200:]
    return found


def import_failed_log(path: Optional[Path] = None) -> int:
    """
    Put links found in failed.log on record, skipping ones already there.

    Returns how many were added. They get attempt_count 1 and no source file,
    since the log doesn't record which batch file a link came from.
    """
    found = scan_failed_log(path)
    if not found:
        return 0
    known = set(read())
    added = 0
    for url, line in found.items():
        if url in known:
            continue
        add_failure(url, error=f"from failed.log: {line}")
        added += 1
    return added


# ==================== Retrying one link ====================
def attempt(url: str, output_template: str, additional_args: list = None,
            item_type: str = "item", show_progress: bool = True) -> Tuple[bool, str, bool]:
    """
    Download one link, retrying up to downloader.max_retries times.

    The wait doubles each time (retry_delay, then 2x, then 4x): a link that is
    struggling usually needs a longer gap, not another immediate go.

    Returns (success, last_error, throttled) so callers that batch many links
    can report why each one failed without re-reading the logs, and can tell a
    bad link apart from a refusing host.
    """
    d = _require_downloader()
    last_error = ""
    last_throttled = False
    number = 0

    for number in range(1, d.max_retries + 1):
        if number > 1:
            wait = d.retry_delay * (2 ** (number - 2))
            if show_progress:
                Enhanced_Menu.print_status(
                    f"Attempt {number}/{d.max_retries} in {wait}s...", "info")
            time.sleep(wait)

        try:
            result = d.run_download(url, output_template, additional_args,
                                    show_progress=show_progress)
            # run_download returns code 0 or raises, so this is the success path.
            if result and result.returncode == 0:
                logs_manager.log_success(f"Downloaded {item_type}: {url}",
                                         console=show_progress)
                if item_type in ("album", "playlist"):
                    cleanup_directory(d.output_directory, logs_manager)
                return True, "", False

        except RuntimeError:
            raise                       # yt-dlp is missing: retrying won't help
        except subprocess.CalledProcessError as error:
            last_error = str(error)[:300]
            last_throttled = getattr(error, "throttled", False)
            if last_throttled:
                # A refusing host won't change its mind in ten seconds, and
                # each retry makes it worse. Stop and let the caller queue it.
                logs_manager.log_warning(f"Throttled on {url} - leaving it for later",
                                         console=show_progress)
                break
            if number < d.max_retries:
                logs_manager.log_warning(
                    f"Attempt {number} failed for {item_type}: {last_error[:100]}",
                    console=show_progress)
        except Exception as error:
            last_error = str(error)[:300]
            logs_manager.log_error(f"Unexpected error on attempt {number}: {error}",
                                   console=show_progress)

    logs_manager.log_failure(f"Failed after {number} attempt(s): {url}",
                             console=show_progress)
    return False, last_error, last_throttled


# ==================== Pacing ====================
def backoff_seconds(streak: int) -> float:
    """Exponential wait after `streak` consecutive throttled links, capped."""
    return min(_state["backoff_base"] * (2 ** (streak - 1)), _state["backoff_max"])


def should_stop(streak: int) -> bool:
    """True once enough throttled links in a row say the host is refusing us."""
    return streak >= THROTTLE_STREAK_LIMIT


def pause_between(streak: int) -> None:
    """
    Wait before the next link in a batch or retry loop.

    With no throttle streak this is a short random pause: yt-dlp's own
    --sleep-interval only applies within one invocation, not between them.
    With a streak it is the exponential backoff. Ctrl-C during the sleep
    raises into the caller's existing handler.
    """
    if streak:
        wait = backoff_seconds(streak)
        Enhanced_Menu.print_status(
            f"Throttled - waiting {wait:.0f}s before the next link", "warning")
        time.sleep(wait)
    else:
        d = _require_downloader()
        time.sleep(random.uniform(d.yt_dlp_sleep_min, d.yt_dlp_sleep_max))


# ==================== Working through the record ====================
def run_queue(include_log: bool = False, prompt: bool = True,
              skip_hopeless: bool = True) -> bool:
    """
    Re-attempt every failed link on record.

    include_log    also pulls in anything in failed.log that isn't on record.
    prompt=False   runs without asking, for an unattended re-run.
    skip_hopeless  leaves links that have failed GIVE_UP_AFTER times on their
                   own merits, rather than spending the run on dead links.
    """
    d = _require_downloader()
    if prompt:
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Retry Failed Downloads",
                                   "Re-attempt links that didn't make it")

    if include_log:
        added = import_failed_log()
        if added:
            Enhanced_Menu.print_status(f"Recovered {added} link(s) from failed.log", "info")

    records = read()
    if not records:
        Enhanced_Menu.print_status("Nothing on record to retry.", "info")
        return True

    items = list(records.values())
    skipped = 0
    if skip_hopeless:
        keep = [e for e in items if not hopeless(e)]
        skipped = len(items) - len(keep)
        items = keep
    if not items:
        Enhanced_Menu.print_status(
            f"All {skipped} recorded links have failed {GIVE_UP_AFTER}+ times without "
            "throttling. Drop them from the menu, or retry with skip_hopeless off.", "warning")
        return True

    # Throttled links first: they are the ones most likely to work now.
    items.sort(key=lambda e: (not e.get("throttled"), int(e.get("attempt_count", 0))))
    throttled_total = sum(1 for e in items if e.get("throttled"))

    print(f"  {Fore.CYAN}To retry:{Style.RESET_ALL} {len(items)}")
    if throttled_total:
        print(f"  {Fore.YELLOW}Last failed to throttling:{Style.RESET_ALL} {throttled_total} "
              f"{Style.DIM}(likely to work now){Style.RESET_ALL}")
    if skipped:
        print(f"  {Fore.RED}Skipped as dead:{Style.RESET_ALL} {skipped} "
              f"{Style.DIM}({GIVE_UP_AFTER}+ attempts, never throttled){Style.RESET_ALL}")
    for entry in items[:10]:
        tag = f"{Fore.YELLOW} [throttled]{Style.RESET_ALL}" if entry.get("throttled") else ""
        print(f"      - {str(entry.get('title') or entry['url'])[:55]} "
              f"{Style.DIM}({entry.get('attempt_count', 0)} attempts){Style.RESET_ALL}{tag}")
    if len(items) > 10:
        print(f"      ...and {len(items) - 10} more")
    print()

    if prompt:
        if _cookies is not None:
            active = _cookies.get_active_cookie_file()
            Enhanced_Menu.print_status(
                f"Cookies: {active.name}" if active else "No cookie file active",
                "info" if active else "warning")
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
    started = time.monotonic()

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
                # Keep the source file in step with the record, so a link that
                # keeps failing doesn't look untried in the file it came from.
                if source:
                    per_source.setdefault(source, {})[url] = "failed"
                if throttled:
                    streak += 1
                    print(f"      {Fore.RED}still throttled{Style.RESET_ALL}")
                else:
                    streak = 0
                    print(f"      {Fore.RED}still failing{Style.RESET_ALL}")

            # Working through the list into a still-throttled host just raises
            # everyone's attempt count. Stop and come back later.
            if should_stop(streak):
                Enhanced_Menu.print_status(
                    "Still being throttled - stopping. The rest stay on record.", "warning")
                break

            if index < total:
                pause_between(streak)
    except KeyboardInterrupt:
        print()
        Enhanced_Menu.print_status("Interrupted - remaining links stay on record.", "warning")

    clear(cleared)
    # Mirror the outcomes back into whichever file each link came from.
    for source, statuses in per_source.items():
        source_path = Path(source)
        if source_path.is_file():
            batch_file.mark_statuses(source_path, statuses)

    print()
    Enhanced_Menu.print_header("Retry Complete")
    print(f"  {Fore.GREEN}Recovered:{Style.RESET_ALL} {succeeded}")
    if failed:
        print(f"  {Fore.RED}Still failing:{Style.RESET_ALL} {failed}")
    print(f"  {Fore.CYAN}Elapsed:{Style.RESET_ALL} {(time.monotonic() - started) / 60:.1f} min")

    if succeeded:
        cleanup_directory(d.output_directory, logs_manager)
    return failed == 0
