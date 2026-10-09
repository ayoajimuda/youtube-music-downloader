"""Choose where downloaded music is saved.

The folder is checked before it's saved: created if it doesn't exist (after
asking), and tested by actually writing to it, so a read-only drive or a
typo shows up here rather than halfway through a download.

Usage:
    from tools.choose_download_directory import choose_download_directory, set_download_directory
    choose_download_directory(downloader)                     # interactive
    ok, message = set_download_directory("D:/Music", downloader)
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from managers import config_manager, log_manager
from menu.colorful_menu import Enhanced_Menu

KEEP = "keep"
DEFAULT = "default"
TYPE = "type"
BROWSE = "browse"
BACK = "back"


# ==================== Checking a folder ====================
def clean_path(text: str) -> Path:
    """User input as an absolute path: quotes from drag-and-drop removed, ~ and %VARS% expanded."""
    text = (text or "").strip().strip('"').strip("'")
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


def _writable(folder: Path) -> bool:
    """Actually try writing, since os.access isn't reliable on Windows."""
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".write_test_"):
            return True
    except OSError:
        return False


def check_folder(path: Path) -> Optional[str]:
    """None if the folder can be used as it is (or created), else the reason it can't."""
    if path.exists() and not path.is_dir():
        return f"{path} is a file, not a folder."
    if path.is_dir():
        return None if _writable(path) else f"Can't write to {path} (read-only or no permission)."
    # Doesn't exist yet: it can be created if its nearest existing parent is writable.
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir():
        return f"{parent} doesn't exist (is the drive connected?)."
    return None if _writable(parent) else f"Can't create folders in {parent}."


def set_download_directory(path, downloader=None, create: bool = True) -> Tuple[bool, str]:
    """Check, create (if allowed), save and apply a download folder. (success, message)."""
    path = clean_path(str(path))
    problem = check_folder(path)
    if problem:
        return False, problem
    if not path.is_dir():
        if not create:
            return False, f"{path} doesn't exist."
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return False, f"Couldn't create {path}: {error}"
    old = config_manager.load()["output_directory"]
    ok, message = config_manager.update_config("output_directory", str(path))
    if not ok:
        return False, message
    if downloader is not None:
        downloader.output_directory = path
    if str(path) != old:
        log_manager.log_info(f"Download folder changed: {old} -> {path}", console=False)
    return True, f"Music will be saved to: {path}"


# ==================== Picking a folder ====================
def browse_for_folder(start: Path) -> Tuple[bool, Optional[str]]:
    """
    Open the system's folder picker. (available, chosen folder or None if cancelled).
    Not available without a desktop (e.g. over SSH) or without tkinter.
    """
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
    except Exception:
        return False, None
    try:
        root.withdraw()
        root.attributes("-topmost", True)           # in front of the terminal window
        chosen = filedialog.askdirectory(initialdir=str(start if start.is_dir() else Path.home()),
                                         title="Choose where to save music", mustexist=False)
    finally:
        root.destroy()
    return True, chosen or None


def choose_download_directory(downloader=None) -> Optional[Path]:
    """Interactive: show the current folder and let the user pick a new one."""
    import questionary

    current = config_manager.resolve_path(config_manager.load()["output_directory"])
    default = clean_path(config_manager.DEFAULT_CONFIG["output_directory"])
    Enhanced_Menu.print_key_value("Current", current, width=8,
                                  note="" if current.is_dir() else "(doesn't exist yet)")
    print()

    choices = [questionary.Choice(f"Keep {current}", value=KEEP),
               questionary.Choice("Type or paste a folder path", value=TYPE),
               questionary.Choice("Browse for a folder (opens a window)", value=BROWSE)]
    if default != current:
        choices.insert(1, questionary.Choice(f"Use the default ({default})", value=DEFAULT))
    choice = questionary.select("Where should music be saved?",
                                choices=choices + [questionary.Choice("Back", value=BACK)]).ask()
    if choice in (None, KEEP, BACK):
        return None

    if choice == DEFAULT:
        target = default
    elif choice == BROWSE:
        available, chosen = browse_for_folder(current)
        if not available:
            Enhanced_Menu.print_status("The folder picker isn't available here; type the path "
                                       "instead.", "warning")
            choice = TYPE
        elif not chosen:
            return None                                     # cancelled in the window
        else:
            target = clean_path(chosen)
    if choice == TYPE:
        def valid(text: str):
            if not text.strip():
                return True                                 # empty = cancel
            problem = check_folder(clean_path(text))
            return True if problem is None else problem

        answer = questionary.path("Folder (empty to cancel):", default=str(current),
                                  only_directories=True, validate=valid).ask()
        if not answer or not answer.strip():
            return None
        target = clean_path(answer)

    if target == current:
        Enhanced_Menu.print_status("That's already the download folder.", "info")
        return None
    if not target.is_dir() and not questionary.confirm(
            f"{target} doesn't exist. Create it?", default=True).ask():
        return None

    ok, message = set_download_directory(target, downloader)
    Enhanced_Menu.print_status(message, "success" if ok else "error")
    if ok and current.is_dir() and any(current.iterdir()):
        Enhanced_Menu.print_status(f"Music already in {current} stays there; only new "
                                   "downloads go to the new folder.", "info")
    return target if ok else None