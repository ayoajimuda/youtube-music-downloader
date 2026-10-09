"""Ask the user for download settings, save them to the config, and apply them.

Module-level, like the config, rate limiter and retry manager modules: call
configure() once at startup, then get_user_preferences() from the menu.

The config module is the single source of truth. Nothing here keeps its own
copy of a setting: prompts start from config.load(), changes go through
config.update() (which validates them), and apply() copies the result onto the
downloader.

Usage:
    from tools import ask_user_preference as preferences
    preferences.configure(config, Enhanced_Menu, cookies)   # cookies optional
    preferences.load_into(downloader)                        # at startup
    preferences.get_user_preferences(downloader)             # full walkthrough
    preferences.edit_setting("max_retries", downloader)      # one setting
    downloader_cookie_path = preferences.cookie_file(downloader.use_cookies)
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from colorama import Fore, Style, init

init(autoreset=True)

FORMAT_HELP = {
    "mp3": "Most compatible (default)",
    "m4a": "Apple format, good quality",
    "flac": "Lossless audio",
    "opus": "Excellent compression",
    "ogg": "Open format",
    "wav": "Uncompressed",
}

QUALITY_HELP = {
    "320k": "High quality (default)",
    "256k": "Very good quality",
    "192k": "Good quality",
    "128k": "Standard quality",
    "8k-160k": "Lower qualities (enter the exact value, e.g. 96k)",
    "auto": "Let yt-dlp choose",
    "disable": "Don't set a bitrate",
}

# A bitrate means nothing for these, so the question is skipped.
LOSSLESS = ("flac", "wav")

# (config key, what it means). Bounds come from config.INT_BOUNDS.
ADVANCED = (
    ("max_retries", "Attempts per link"),
    ("retry_delay", "Retry delay (s)"),
    ("download_timeout", "Stall timeout (s)"),
    ("max_concurrent", "Parallel downloads"),
    ("yt_dlp_sleep_min", "Min pause (s)"),
    ("yt_dlp_sleep_max", "Max pause (s)"),
)

HELP_WORDS = ("?", "choice", "help", "options")

# Every setting a menu can edit, with a readable name, in display order.
LABELS = {
    "audio_format": "Audio format",
    "audio_quality": "Bitrate",
    "output_directory": "Output folder",
    "use_cookies": "Use cookies",
    **dict(ADVANCED),
}

_state: Dict[str, Any] = {"config": None, "menu": None, "cookies": None}


# ==================== Setup ====================
def configure(config, menu, cookies=None) -> None:
    """Hand the module the config module, the menu, and (optionally) the cookie module."""
    _state.update(config=config, menu=menu, cookies=cookies)


def is_configured() -> bool:
    return _state["config"] is not None and _state["menu"] is not None


def _require() -> Tuple[Any, Any]:
    config, menu = _state["config"], _state["menu"]
    if config is None or menu is None:
        raise RuntimeError("preferences.configure(config, menu) must be called first")
    return config, menu


# ==================== Prompt helpers ====================
def _status(message: str, level: str = "info") -> None:
    _state["menu"].print_status(message, level)


def _ask(prompt: str, default: str = "") -> str:
    """Free-text answer, stripped. None (Enter / no input) becomes ''."""
    value = _state["menu"].get_input(prompt, "str", default=default)
    return str(value or "").strip()


def _ask_yn(prompt: str, default: bool) -> bool:
    return bool(_state["menu"].get_input(prompt, "yn", default=default))


def _choose(label: str, help_rows: Dict[str, str], allowed: Sequence[str], current: str) -> str:
    """Pick one of `allowed`. Enter keeps the current value; '?' lists the options."""
    while True:
        answer = _ask(f"{label} (Enter to keep, '?' for options)", current).lower()
        if not answer or answer == current:
            return current
        if answer in HELP_WORDS:
            print(f"\n{Fore.CYAN}Available {label.lower()} options:{Style.RESET_ALL}")
            for name, text in help_rows.items():
                print(f"  {name:8} - {text}")
            print()
            continue
        if answer in allowed:
            return answer
        _status(f"'{answer}' isn't supported. Enter '?' to see the options.", "error")


def _ask_directory(current: str) -> str:
    """A folder the downloader can write to. Created if it doesn't exist."""
    while True:
        answer = _ask("Output directory (Enter to keep)", current)
        answer = answer.strip('"').strip("'")          # drag-and-drop adds quotes
        if not answer or answer == current:
            return current
        path = Path(os.path.expandvars(answer)).expanduser()
        if not path.is_absolute():
            path = path.resolve()   # stored absolute, so it doesn't depend on the launch folder
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            _status(f"Can't create {path}: {error}", "error")
            continue
        if not os.access(path, os.W_OK):
            _status(f"{path} isn't writable. Pick another folder.", "error")
            continue
        return str(path)


def _ask_int(key: str, description: str, current: int, low: int, high: int) -> int:
    while True:
        answer = _ask(f"{description} ({low}-{high}, Enter to keep)",
                      str(current))
        if not answer:
            return current
        try:
            value = int(answer)
        except ValueError:
            _status(f"'{answer}' isn't a whole number.", "error")
            continue
        if low <= value <= high:
            return value
        _status(f"Enter a number between {low} and {high}.", "error")


def _ask_cookies(current: bool) -> bool:
    print(f"\n{Fore.CYAN}Cookies can help with:{Style.RESET_ALL}")
    print("  Age-restricted content")
    print("  Region-restricted videos")
    print("  Private playlists")
    print("  YouTube's 'confirm you're not a bot' checks")
    use = _ask_yn("Use cookies for authentication?", current)
    if use and not cookie_file(True):
        _status("No cookie file is loaded yet. Downloads will run without cookies until "
                "you add one in the Cookie Manager.", "warning")
    return use


# ==================== Cookies ====================
def _cookie_directory() -> Path:
    cookies = _state["cookies"]
    return Path(getattr(cookies, "COOKIE_DIRECTORY", "cookies"))


def cookie_file(use_cookies: bool, log=None) -> Optional[str]:
    """
    The cookie file yt-dlp should use, or None.

    The cookie manager's active file wins; otherwise the newest .txt in the
    cookie folder. Returned as an absolute path so yt-dlp doesn't depend on
    the working directory.
    """
    if not use_cookies:
        return None
    cookies = _state["cookies"]
    getter = getattr(cookies, "get_active_cookie_file", None)
    active = getter() if getter else None
    if active and Path(active).is_file():
        return str(Path(active).resolve())

    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    folder = _cookie_directory()
    if folder.is_dir():
        candidates = sorted((p for p in folder.glob("*.txt") if p.is_file()),
                            key=modified, reverse=True)
        if candidates:
            return str(candidates[0].resolve())
    if log is not None:
        log.log_error("Cookies are enabled but no cookie file was found. "
                      "Use the Cookie Manager to add one.")
    return None


# ==================== Public API ====================
def prompt() -> Optional[Dict[str, Any]]:
    """
    Walk the user through the settings and save what changed.

    Returns the saved config, the unchanged config if nothing changed, or
    None if the user cancelled with Ctrl-C (nothing is saved in that case).
    """
    config, menu = _require()
    current = config.load()
    menu.print_header("Download Settings", "Configure your music conversion preferences")

    changes: Dict[str, Any] = {}
    try:
        # Format first: it decides whether the bitrate question applies.
        fmt = _choose("Audio format", FORMAT_HELP, config.VALID_FORMATS,
                      current["audio_format"])
        changes["audio_format"] = fmt
        if fmt in LOSSLESS:
            _status(f"{fmt} is lossless, so bitrate doesn't apply - skipping it.", "info")
        else:
            changes["audio_quality"] = _choose("Bitrate", QUALITY_HELP, config.VALID_QUALITIES,
                                               current["audio_quality"])

        changes["output_directory"] = _ask_directory(current["output_directory"])
        changes["use_cookies"] = _ask_cookies(bool(current.get("use_cookies")))

        if _ask_yn("Change advanced settings (retries, timeouts, pacing)?", False):
            for key, description in ADVANCED:
                low, high = config.INT_BOUNDS[key]
                changes[key] = _ask_int(key, description, int(current[key]), low, high)
    except KeyboardInterrupt:
        print()
        _status("Cancelled - nothing was changed.", "warning")
        return None

    changed = {k: v for k, v in changes.items() if current.get(k) != v}
    if not changed:
        _status("No changes.", "info")
        return current

    saved = config.update(**changed)
    print()
    for key in changed:
        print(f"  {Fore.CYAN}{key}:{Style.RESET_ALL} {current.get(key)} -> {saved.get(key)}")
    _status("Settings saved", "success")
    return saved


def edit_setting(key: str, downloader=None) -> Optional[Dict[str, Any]]:
    """
    Ask for one setting, save it, and (if a downloader is given) apply it.

    Uses the same validated prompts as the full walkthrough. Returns the saved
    config, the unchanged config if the value didn't change, or None if the
    user cancelled with Ctrl-C.
    """
    config, _ = _require()
    if key not in LABELS:
        raise KeyError(f"Unknown setting: {key}")
    current = config.load()
    try:
        if key == "audio_format":
            value = _choose("Audio format", FORMAT_HELP, config.VALID_FORMATS, current[key])
        elif key == "audio_quality":
            if current["audio_format"] in LOSSLESS:
                _status(f"Bitrate doesn't apply to {current['audio_format']}; it's only used "
                        "if you switch to a lossy format.", "info")
            value = _choose("Bitrate", QUALITY_HELP, config.VALID_QUALITIES, current[key])
        elif key == "output_directory":
            value = _ask_directory(current[key])
        elif key == "use_cookies":
            value = _ask_cookies(bool(current.get(key)))
        else:
            low, high = config.INT_BOUNDS[key]
            value = _ask_int(key, LABELS[key], int(current[key]), low, high)
    except KeyboardInterrupt:
        print()
        _status("Cancelled - nothing was changed.", "warning")
        return None

    if value == current.get(key):
        _status("No change.", "info")
        return current
    saved = config.update(**{key: value})
    print(f"  {Fore.CYAN}{key}:{Style.RESET_ALL} {current.get(key)} -> {saved.get(key)}")
    if key in ("yt_dlp_sleep_min", "yt_dlp_sleep_max") and saved[key] != value:
        _status("Minimum and maximum sleep were swapped so minimum stays the smaller.", "info")
    _status("Setting saved", "success")
    if downloader is not None:
        apply(downloader, saved)
    return saved


def apply(downloader, settings: Optional[Dict[str, Any]] = None) -> None:
    """Copy config values onto a downloader. Loads the config if none is given."""
    config, _ = _require()
    settings = settings if settings is not None else config.load()

    downloader.audio_format = settings["audio_format"]
    downloader.audio_quality = settings["audio_quality"]
    try:
        downloader.output_directory = settings["output_directory"]   # setter creates it
    except OSError as error:
        _status(f"Can't use output directory {settings['output_directory']}: {error}", "error")
    downloader.use_cookies = bool(settings.get("use_cookies"))

    for key, _ in ADVANCED:
        if key in settings:
            setattr(downloader, key, settings[key])
    # The downloader currently reads both names; keep them in step until it
    # settles on max_concurrent.
    if "max_concurrent" in settings:
        downloader.max_concurrency = settings["max_concurrent"]


def load_into(downloader) -> Dict[str, Any]:
    """At startup: load the saved config and apply it to the downloader."""
    config, _ = _require()
    settings = config.load()
    apply(downloader, settings)
    return settings


def get_user_preferences(downloader=None) -> Optional[Dict[str, Any]]:
    """Prompt, save, and (if a downloader is given) apply. Returns the saved config."""
    saved = prompt()
    if saved is not None and downloader is not None:
        apply(downloader, saved)
    return saved