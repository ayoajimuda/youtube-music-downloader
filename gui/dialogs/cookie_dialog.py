"""Choose the cookies.txt file yt-dlp should use.

The chosen file becomes the cookie manager's active file and is remembered in
the config ("cookie_file"), exactly as the terminal Cookies Menu does, so the
GUI and the terminal program share it.
"""

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QFileDialog

from gui.dialogs import messages
from managers import config_manager, cookie_manager

HOW_TO = ("Export cookies.txt from a browser signed in to YouTube: open a private window, "
          "sign in, export the youtube.com cookies with a 'cookies.txt' extension, then close "
          "the private window straight away so the cookies stay valid.")


def looks_like_cookie_file(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8-sig", errors="replace")[:4000]
    except OSError:
        return False
    return "Netscape" in head or ".youtube.com" in head


def active_cookie_file() -> Optional[Path]:
    return cookie_manager.get_active_cookie_file()


def use_cookie_file(path: Path) -> Path:
    """Make `path` the active cookie file and remember it."""
    path = Path(path).resolve()
    cookie_manager.set_active(path)
    config_manager.update(cookie_file=str(path))
    return path


def forget_cookie_file() -> None:
    cookie_manager.set_active(None)
    config_manager.update(cookie_file="")


def choose_cookie_file(parent, start: str = "") -> Optional[Path]:
    """Ask for a cookies.txt, check it, and make it the active one. None if cancelled."""
    if not start:
        current = active_cookie_file()
        start = str(current.parent if current else Path(cookie_manager.COOKIE_DIRECTORY).resolve())
    while True:
        name, _ = QFileDialog.getOpenFileName(parent, "Choose your cookies.txt", start,
                                              "Cookie files (*.txt);;All files (*)")
        if not name:
            return None
        path = Path(name)
        if not path.is_file():
            messages.error(parent, "Cookies", f"{path} doesn't exist.")
            continue
        if not looks_like_cookie_file(path) and not messages.confirm(
                parent, "Cookies",
                f"{path.name} doesn't look like a Netscape cookies.txt file, so yt-dlp may "
                "not be able to use it.\n\nUse it anyway?", default_yes=False):
            start = str(path.parent)
            continue
        return use_cookie_file(path)
