"""Downloads menu: routes to YoutubeMusicDownloader's download methods.

The downloader does the work (and asks for links itself); this menu picks
what to run, chooses batch files, and shows what downloads will use.

Usage:
    from menu import downloader_menu
    downloader_menu.run(downloader)       # from main_menu
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import questionary

from managers import config_manager, cookie_manager, log_manager
from menu.colorful_menu import Enhanced_Menu
from utils.track_checker import check_downloaded_files

BACK = "Back"
LOSSLESS = ("flac", "wav")
BATCH_TYPES = (".txt", ".csv", ".json")


# ==================== Status ====================
def status_block(downloader) -> None:
    """What the next download will use, plus anything waiting in the retry queue."""
    fmt = getattr(downloader, "audio_format", "?")
    quality = "lossless" if fmt in LOSSLESS else getattr(downloader, "audio_quality", "?")
    print()
    Enhanced_Menu.print_key_value("Format", f"{fmt} ({quality})", width=11)
    Enhanced_Menu.print_key_value("Saving to", getattr(downloader, "output_directory", "?"),
                                  width=11)
    if getattr(downloader, "use_cookies", False):
        active = cookie_manager.get_active_cookie_file()
        cookies = f"on ({active.name})" if active else "on, but no cookie file is active"
    else:
        cookies = "off"
    Enhanced_Menu.print_key_value("Cookies", cookies, width=11)
    queued = len(log_manager.read_failures())
    if queued:
        Enhanced_Menu.print_key_value("Retry queue", f"{queued} link(s) waiting", width=11)
    print()


# ==================== Batch files ====================
def _batch_module(downloader):
    module = getattr(downloader, "batch_file", None)
    if module is None:
        from downloader import batch_downloader as module
    return module


def _pending(downloader, path: Path) -> Tuple[int, int]:
    """(links in the file, links not yet marked success). (0, 0) if unreadable."""
    try:
        entries = _batch_module(downloader).parse(path)
    except (OSError, ValueError):
        return 0, 0
    urls = {e["url"]: e.get("status", "") for e in entries}
    return len(urls), sum(1 for status in urls.values() if status != "success")


def _source_files() -> List[Tuple[str, Path]]:
    """The tracks/playlists files from the config, and the last batch file, if they exist."""
    config = config_manager.load()
    sources = []
    for key, label in (("tracks_file", "tracks file"), ("playlists_file", "playlists file"),
                       ("last_batch_file", "last batch file")):
        value = config.get(key) or ""
        if not value:
            continue
        path = config_manager.resolve_path(value)
        if path.is_file() and all(path != p for _, p in sources):
            sources.append((label, path))
    return sources


def _run_batch(downloader, path: Path) -> None:
    # Remembered first, so an interrupted run still shows up as "last batch file".
    config_manager.update(last_batch_file=str(path.resolve()))
    downloader.download_from_file(str(path))


def download_from_file(downloader) -> None:
    def valid(text: str):
        name = text.strip().strip('"').strip("'")
        if not name:
            return True                                    # empty = cancel
        path = Path(name).expanduser()
        if not path.is_file():
            return "No such file"
        if path.suffix.lower() not in BATCH_TYPES:
            return "Use a .txt, .csv or .json file"
        return True

    answer = questionary.path("Path to a .txt, .csv or .json file of links (empty to cancel):",
                              validate=valid).ask()
    name = (answer or "").strip().strip('"').strip("'")
    if name:
        _run_batch(downloader, Path(name).expanduser())


def download_pending_from(downloader, path: Path) -> None:
    total, pending = _pending(downloader, path)
    if not total:
        Enhanced_Menu.print_status(f"No links found in {path.name}. Batch files need YouTube "
                                   "links (a url column or one link per line).", "warning")
        return
    if not pending:
        Enhanced_Menu.print_status(f"All {total} links in {path.name} are already downloaded.",
                                   "success")
        return
    _run_batch(downloader, path)


# ==================== Menu ====================
def _choices(downloader) -> List[Tuple[str, Callable[[], None]]]:
    items: List[Tuple[str, Callable[[], None]]] = [
        ("Download a track", downloader.download_track),
        ("Download an album", downloader.download_album),
        ("Download a playlist", downloader.download_playlist),
        ("Download every link in a file (.txt, .csv, .json)",
         lambda: download_from_file(downloader)),
    ]
    for label, path in _source_files():
        total, pending = _pending(downloader, path)
        if total:
            items.append((f"Download pending from {label} ({path.name}: {pending} of {total} "
                          f"left)", lambda p=path: download_pending_from(downloader, p)))
    queued = len(log_manager.read_failures())
    retry = f"Retry failed downloads ({queued} waiting)" if queued else "Retry failed downloads"
    items.append((retry, downloader.download_from_retry_queue))
    items.append(("Check downloaded files (broken files, leftovers)",
                  lambda: check_downloaded_files(downloader)))

    def settings():
        from menu import config_menu
        config_menu.run(downloader)
    items.append(("Download settings", settings))
    return items


def downloads_menu(downloader) -> None:
    """Show the downloads menu until the user picks Back (or presses Ctrl-C)."""
    while True:
        status_block(downloader)
        items = _choices(downloader)
        choices = [questionary.Choice(label, value=index) for index, (label, _) in enumerate(items)]
        choices.append(questionary.Choice(BACK, value=BACK))
        choice = questionary.select("📥 Downloads — What would you like to do?",
                                    choices=choices).ask()
        if choice in (None, BACK):                       # None = Ctrl-C
            return
        label, action = items[choice]
        try:
            action()
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Cancelled", "warning")
        except Exception as error:                       # report it; stay in the menu
            Enhanced_Menu.print_status(f"{label} failed: {type(error).__name__}: {error}",
                                       "error")
            log_manager.log_error(f"Downloads menu: {label} failed: {error}", console=False)


def run(downloader) -> None:
    """Entry point for main_menu."""
    downloads_menu(downloader)