"""Tools menu: dependency check, yt-dlp updates, cookie check, playlist export, audio format.

Usage:
    from menu import tools_menu
    tools_menu.run(downloader)            # from main_menu
"""

import os
import subprocess
import sys
from pathlib import Path

import questionary

from managers import cookie_manager, log_manager
from menu.colorful_menu import Enhanced_Menu

BACK = "Back"


# ==================== Small tools that live here ====================
def open_folder(path) -> None:
    path = Path(path)
    if not path.exists():
        Enhanced_Menu.print_status(f"Folder doesn't exist yet: {path}", "info")
        return
    Enhanced_Menu.print_status(f"Opening {path}", "info")
    try:
        if os.name == "nt":
            os.startfile(str(path))                       # Windows Explorer
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        Enhanced_Menu.print_status(f"Couldn't open it: {error}", "error")


def open_folders(downloader) -> None:
    from tools.playlist_to_txt import default_folder
    folders = [
        ("Music folder", getattr(downloader, "output_directory", None)),
        ("Exported playlists", default_folder()),
        ("Logs", log_manager.LOG_DIR),
        ("History and retry queue", log_manager.HISTORY_DIR),
        ("Cookies", cookie_manager.COOKIE_DIRECTORY),
    ]
    choices = [questionary.Choice(f"{label}  ({Path(path).resolve()})", value=Path(path))
               for label, path in folders if path]
    picked = questionary.select("Open which folder?", choices=choices + [BACK]).ask()
    if picked not in (None, BACK):
        open_folder(picked)


HELP = (
    ("Downloads suddenly failing?", "Check for yt-dlp updates. YouTube changes often, and an "
     "old yt-dlp is the most common cause of 403 errors."),
    ("'Sign in to confirm you're not a bot'?", "Run the cookie check. Export cookies from a "
     "private window and close it afterwards, so they last longer."),
    ("Throttled (HTTP 429)?", "Stop for a while. Throttled links go into the retry queue; "
     "retry them later from Downloads."),
    ("'No supported JS runtime'?", "Install Deno or Node.js; the dependency check shows how."),
    ("Downloading a whole playlist later?", "Convert it to a .txt file first. Downloading "
     "from the file marks each finished track, so an interrupted run carries on where it "
     "stopped, and exporting again adds only new tracks."),
    ("Which format?", "mp3 plays everywhere; opus or m4a keep YouTube's audio with the "
     "smallest files; flac if you want no further loss."),
)


def show_help() -> None:
    for question, answer in HELP:
        Enhanced_Menu.print_color(question, "highlight")
        for line in Enhanced_Menu.wrap_text(answer, width=60):
            print(f"  {line}")
        print()


# ==================== Menu ====================
def _items(downloader):
    from tools.choose_audio_format import choose_audio_format
    from tools.choose_download_directory import choose_download_directory
    from tools.cookie_check import cookie_checker
    from tools.dependency_check import dependency_check
    from tools.playlist_to_txt import playlist_to_txt
    from tools.ytdlp_update_checker import ytdlp_update_checker
    return [
        ("Check dependencies (yt-dlp, ffmpeg, ...)", dependency_check),
        ("Check for yt-dlp updates", ytdlp_update_checker),
        ("Check cookies", cookie_checker),
        ("Convert a playlist to a .txt file", lambda: playlist_to_txt(downloader)),
        ("Choose audio format", lambda: choose_audio_format(downloader)),
        ("Choose download folder", lambda: choose_download_directory(downloader)),
        ("Open a folder", lambda: open_folders(downloader)),
        ("Help and troubleshooting", show_help),
    ]


def tools_menu(downloader=None) -> None:
    """Show the tools menu until the user picks Back (or presses Ctrl-C)."""
    items = _items(downloader)
    while True:
        print()
        choices = [questionary.Choice(label, value=i) for i, (label, _) in enumerate(items)]
        choice = questionary.select("🛠 Tools — Select a tool:",
                                    choices=choices + [questionary.Choice(BACK, value=BACK)]).ask()
        if choice in (None, BACK):                        # None = Ctrl-C
            return
        label, action = items[choice]
        try:
            action()
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Cancelled", "warning")
        except Exception as error:                        # report it; stay in the menu
            Enhanced_Menu.print_status(f"{label} failed: {type(error).__name__}: {error}",
                                       "error")
            log_manager.log_error(f"Tools menu: {label} failed: {error}", console=False)


def run(downloader=None) -> None:
    """Entry point for main_menu."""
    tools_menu(downloader)