"""Cookies menu: everything managers.cookie_manager can do, plus on/off for downloads.

It also remembers the active cookie file in the config (as "cookie_file"), so
it's still active after a restart: call restore() once at startup.

Usage:
    from menu import cookies_menu
    cookies_menu.restore()             # at startup
    cookies_menu.run(downloader)       # from main_menu; on/off applies to the downloader
"""

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import questionary

from managers import config_manager, cookie_manager, log_manager
from menu.colorful_menu import Enhanced_Menu

BACK = "Back"
STALE_AFTER_DAYS = 14          # YouTube cookies older than this often stop working

_state: Dict[str, Any] = {"downloader": None}


# ==================== Persistence ====================
def _remember() -> None:
    """Store the active cookie file in the config so it survives a restart."""
    active = cookie_manager.get_active_cookie_file()
    stored = str(active.resolve()) if active else ""
    if config_manager.load().get("cookie_file", "") != stored:
        config_manager.update(cookie_file=stored)


def restore() -> Optional[Path]:
    """At startup: make the remembered cookie file active again, if it still exists."""
    stored = config_manager.load().get("cookie_file") or ""
    if stored and Path(stored).is_file():
        cookie_manager.set_active(Path(stored))
        return Path(stored)
    return None


def _use_cookies() -> bool:
    return bool(config_manager.load().get("use_cookies"))


def _set_use_cookies(value: bool) -> None:
    ok, message = config_manager.update_config("use_cookies", value)
    if not ok:
        Enhanced_Menu.print_status(message, "error")
        return
    if _state["downloader"] is not None:
        _state["downloader"].use_cookies = value
    log_manager.log_info(f"Cookies {'enabled' if value else 'disabled'} for downloads",
                         console=False)


# ==================== Display ====================
def _age_days(path: Path) -> Optional[float]:
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return None


def _age(path: Path) -> str:
    days = _age_days(path)
    if days is None:
        return "unknown age"
    if days < 1:
        return "less than a day old"
    whole = round(days)
    return f"{whole} day{'s' if whole != 1 else ''} old"


def _saved_files() -> List[Path]:
    """Cookie files in the cookie folder, newest first."""
    folder = Path(cookie_manager.COOKIE_DIRECTORY)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.txt") if p.is_file()),
                  key=lambda p: _age_days(p) or 0.0)


def show_status() -> None:
    active = cookie_manager.get_active_cookie_file()
    use = _use_cookies()
    print()
    if active:
        stale = (_age_days(active) or 0) > STALE_AFTER_DAYS
        Enhanced_Menu.print_key_value("Active file", f"{active.name} ({_age(active)})", width=12)
    else:
        stale = False
        Enhanced_Menu.print_key_value("Active file", "none", width=12)
    Enhanced_Menu.print_key_value("Downloads", "use cookies" if use else "don't use cookies",
                                  width=12)

    if use and not active:
        Enhanced_Menu.print_status("Cookies are on, but no file is active - downloads run "
                                   "without them. Extract or load one.", "warning")
    elif stale:
        Enhanced_Menu.print_status("This file is getting old. If downloads hit sign-in or "
                                   "bot checks, export fresh cookies.", "warning")
    print()


# ==================== Actions ====================
def _after_new_file() -> None:
    """A cookie file just became active: remember it and offer to turn cookies on."""
    _remember()
    if not _use_cookies() and questionary.confirm(
            "Cookies are off for downloads. Turn them on?", default=True).ask():
        _set_use_cookies(True)
        Enhanced_Menu.print_status("Downloads will use cookies.", "success")


def check_browsers() -> None:
    cookie_manager.get_status()


def extract_from_browser() -> None:
    browsers = list(cookie_manager.cookie_sources())
    if not browsers:
        Enhanced_Menu.print_status("browser_cookie3 isn't installed. Install it with "
                                   "'pip install browser-cookie3', or use manual export.", "error")
        return
    Enhanced_Menu.print_status("Close the browser first: an open browser locks its cookie "
                               "database. Firefox works most reliably on Windows.", "info")
    browser = questionary.select("Extract cookies from which browser?",
                                 choices=browsers + [BACK]).ask()
    if browser in (None, BACK):
        return
    # extract_cookies writes cookies/<browser>_cookies.txt and makes it active,
    # so there's no separate save step afterwards.
    if cookie_manager.extract_cookies(browser):
        _after_new_file()


def load_from_path() -> None:
    def is_file(text: str):
        name = text.strip().strip('"').strip("'")
        if not name:
            return True                     # empty = cancel
        return True if Path(name).expanduser().is_file() or \
            (Path(cookie_manager.COOKIE_DIRECTORY) / name).is_file() else "No such file"

    path = questionary.path("Path to a cookies.txt file (empty to cancel):",
                            validate=is_file).ask()
    if path and path.strip() and cookie_manager.load_cookies(path):
        _after_new_file()


def choose_saved() -> None:
    files = _saved_files()
    if not files:
        Enhanced_Menu.print_status("No saved cookie files yet.", "info")
        return
    active = cookie_manager.get_active_cookie_file()
    active = active.resolve() if active else None
    choices = [questionary.Choice(
        f"{p.name}  ({_age(p)}){'  - active' if p.resolve() == active else ''}", value=p)
        for p in files]
    picked = questionary.select("Make which file active?",
                                choices=choices + [questionary.Choice(BACK, value=BACK)]).ask()
    if picked in (None, BACK):
        return
    if cookie_manager.load_cookies(str(picked)):
        _after_new_file()


def manual_export() -> None:
    Enhanced_Menu.print_status("Tip: export from a private/incognito window, then close it. "
                               "Cookies from a normal window are rotated by YouTube and stop "
                               "working within hours.", "info")
    if cookie_manager.manual_cookie_instructions():
        _after_new_file()


def test_active() -> None:
    cookie_manager.test_cookies()


def toggle_use_cookies() -> None:
    new_value = not _use_cookies()
    _set_use_cookies(new_value)
    if not new_value:
        Enhanced_Menu.print_status("Downloads will no longer use cookies.", "success")
        return
    Enhanced_Menu.print_status("Downloads will use cookies.", "success")
    if not cookie_manager.get_active_cookie_file():
        Enhanced_Menu.print_status("No cookie file is active yet - extract or load one.",
                                   "warning")


def backup_active() -> None:
    if not cookie_manager.get_active_cookie_file():
        Enhanced_Menu.print_status("No active cookie file to back up.", "info")
        return
    name = questionary.text("Name for the backup:", default="cookies").ask()
    if name is None:
        return
    cookie_manager.save_cookies(name.strip() or "cookies")


def deactivate() -> None:
    if not cookie_manager.get_active_cookie_file():
        Enhanced_Menu.print_status("No cookie file is active.", "info")
        return
    cookie_manager.set_active(None)
    _remember()
    Enhanced_Menu.print_status("No cookie file is active now. The file itself was kept.",
                               "success")


def delete_files() -> None:
    files = _saved_files()
    if not files:
        Enhanced_Menu.print_status("No cookie files to delete.", "info")
        return
    picked = questionary.checkbox(
        "Select files to delete (space to select, enter to confirm):",
        choices=[questionary.Choice(f"{p.name}  ({_age(p)})", value=p) for p in files]).ask()
    if not picked:
        return
    if not questionary.confirm(f"Permanently delete {len(picked)} cookie file(s)?",
                               default=False).ask():
        Enhanced_Menu.print_status("Nothing was deleted.", "info")
        return
    active = cookie_manager.get_active_cookie_file()
    deleted = 0
    for path in picked:
        try:
            path.unlink()
            deleted += 1
        except OSError as error:
            Enhanced_Menu.print_status(f"Couldn't delete {path.name}: {error}", "error")
    if active and not active.exists():
        cookie_manager.set_active(None)
    _remember()
    Enhanced_Menu.print_status(f"Deleted {deleted} cookie file(s).", "success")


# ==================== Menu ====================
ACTIONS = [
    ("Check which browsers have YouTube cookies", check_browsers),
    ("Extract cookies from a browser", extract_from_browser),
    ("Load a cookies.txt file", load_from_path),
    ("Choose from saved cookie files", choose_saved),
    ("How to export cookies manually", manual_export),
    ("Test the active cookies", test_active),
    (None, toggle_use_cookies),           # label depends on the current setting
    ("Back up the active file", backup_active),
    ("Stop using the active file", deactivate),
    ("Delete cookie files", delete_files),
]


def cookies_menu() -> None:
    """Show the cookies menu until the user picks Back (or presses Ctrl-C)."""
    while True:
        show_status()
        toggle = "Turn cookies OFF for downloads" if _use_cookies() \
            else "Turn cookies ON for downloads"
        choices = [questionary.Choice(label or toggle, value=index)
                   for index, (label, _) in enumerate(ACTIONS)]
        choices.append(questionary.Choice(BACK, value=BACK))
        choice = questionary.select("🍪 Cookies — What would you like to do?",
                                    choices=choices).ask()
        if choice in (None, BACK):                 # None = Ctrl-C
            return
        label, action = ACTIONS[choice]
        try:
            action()
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Cancelled", "warning")
        except Exception as error:                 # report it; don't drop out of the program
            Enhanced_Menu.print_status(f"{label or toggle} failed: "
                                       f"{type(error).__name__}: {error}", "error")


def run(downloader=None) -> None:
    """Entry point for main_menu. On/off changes apply to `downloader` if one is given."""
    _state["downloader"] = downloader
    cookies_menu()