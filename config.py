"""Loading, defaulting, validating and saving the downloader's JSON config.

One module, two kinds of caller:

  Repairing (the downloader, preferences, menus):
      load(), update(**changes), save(config), reset()
      An invalid value is replaced with its default and reported through
      on_error, so a hand-edited typo can never reach a yt-dlp command line.

  Reporting (the config menu's checks, scripts):
      validate_config(), update_config(key, value), apply_config_profile(name), ...
      These say what's wrong and change nothing when a value is invalid.

Every setting is described once, in CONFIG_SCHEMA. Defaults, bounds, the
valid-value lists and the menu labels all come from it.
"""

import json
import os
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ==================== Locations ====================
def get_app_dir() -> Path:
    """Project root when run as a script; the .exe's folder when frozen (PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent       # managers/ -> project root


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config" / "YoutubeMusicDownloader.json"

# ==================== Schema ====================
VALID_FORMATS = ("mp3", "flac", "ogg", "opus", "m4a", "wav")
VALID_QUALITIES = ("auto", "disable", "8k", "16k", "24k", "32k", "40k", "48k",
                   "64k", "80k", "96k", "112k", "128k", "160k", "192k",
                   "224k", "256k", "320k")

# type        the Python type the value must have
# default     used when the key is missing or its value is invalid
# choices     allowed values (compared lower-case)
# min / max   inclusive bounds for numbers
# path        "~" and %VARS% / $VARS are expanded
# must_exist  validate_config reports it if the path doesn't exist ("" means "find it on PATH")
# label/group how menus show it
CONFIG_SCHEMA: Dict[str, Dict[str, Any]] = OrderedDict([
    # --- Audio ---
    ("audio_format", {"type": str, "default": "mp3", "choices": VALID_FORMATS,
                      "label": "Audio format", "group": "Audio"}),
    ("audio_quality", {"type": str, "default": "320k", "choices": VALID_QUALITIES,
                       "label": "Bitrate", "group": "Audio"}),
    # --- Folders and programs ---
    ("output_directory", {"type": str, "required": True, "path": True,
                          "default": str(Path.home() / "Music" / "Collection" / "YouTube"),
                          "label": "Output folder", "group": "Folders and programs"}),
    ("ffmpeg_path", {"type": str, "default": "", "path": True, "must_exist": True,
                     "label": "ffmpeg program",
                     "group": "Folders and programs"}),
    ("ytdlp_path", {"type": str, "default": "", "path": True, "must_exist": True,
                    "label": "yt-dlp program",
                    "group": "Folders and programs"}),
    # --- Downloading ---
    ("max_retries", {"type": int, "default": 3, "min": 1, "max": 10,
                     "label": "Attempts per link", "group": "Downloading"}),
    ("retry_delay", {"type": int, "default": 10, "min": 0, "max": 3600,
                     "label": "Retry delay (s)", "group": "Downloading"}),
    ("download_timeout", {"type": int, "default": 120, "min": 10, "max": 86400,
                          "label": "Stall timeout (s)", "group": "Downloading"}),
    ("max_concurrent", {"type": int, "default": 2, "min": 1, "max": 16,
                        "label": "Parallel downloads", "group": "Downloading"}),
    ("yt_dlp_sleep_min", {"type": int, "default": 3, "min": 0, "max": 3600,
                          "label": "Min pause (s)", "group": "Downloading"}),
    ("yt_dlp_sleep_max", {"type": int, "default": 7, "min": 0, "max": 3600,
                          "label": "Max pause (s)", "group": "Downloading"}),
    ("use_cookies", {"type": bool, "default": False,
                     "label": "Use cookies", "group": "Downloading"}),
    # --- Library sync ---
    ("tracks_file", {"type": str, "default": "data/tracks.json", "path": True,
                     "label": "Tracks file", "group": "Library sync"}),
    ("playlists_file", {"type": str, "default": "data/playlists.json", "path": True,
                        "label": "Playlists file", "group": "Library sync"}),
    ("sync_write_tracks_json", {"type": bool, "default": True,
                                "label": "Write tracks file on sync", "group": "Library sync"}),
    ("auto_sync_enabled", {"type": bool, "default": False,
                           "label": "Auto-sync", "group": "Library sync"}),
    ("auto_sync_interval", {"type": int, "default": 3600, "min": 60, "max": 86400,
                            "label": "Auto-sync interval (s)", "group": "Library sync"}),
])

# Keys other modules store in the config that aren't user settings.
KNOWN_EXTRAS = ("cookie_file", "last_batch_file")

# Derived views, kept under their old names for existing callers.
DEFAULT_CONFIG: Dict[str, Any] = {k: r["default"] for k, r in CONFIG_SCHEMA.items()}
COMMON_DEFAULTS: Dict[str, Any] = {k: v for k, v in DEFAULT_CONFIG.items()
                                   if k != "output_directory"}
INT_BOUNDS: Dict[str, Tuple[int, int]] = {k: (r["min"], r["max"])
                                          for k, r in CONFIG_SCHEMA.items() if r["type"] is int}

# ==================== Profiles ====================
# Presets for how hard and how politely to download. Only keys in the schema.
CONFIG_PROFILES: Dict[str, Dict[str, Any]] = {
    "light": {"max_retries": 2, "retry_delay": 3,
              "yt_dlp_sleep_min": 3, "yt_dlp_sleep_max": 6},
    "advanced": {"max_retries": 6, "retry_delay": 10,
                 "yt_dlp_sleep_min": 5, "yt_dlp_sleep_max": 10},
    "minimal": {"max_retries": 1, "retry_delay": 0,
                "yt_dlp_sleep_min": 2, "yt_dlp_sleep_max": 4},
}
PROFILE_DESCRIPTIONS = {
    "light": "A couple of retries with short pauses. A good everyday default.",
    "advanced": "More retries and longer pauses. Slower, but best for long batches.",
    "minimal": "One attempt per link, short pauses. Fastest; most likely to be throttled.",
}

# ==================== State ====================
_lock = threading.RLock()      # reentrant: update() loads and saves under one hold
_state: Dict[str, Any] = {
    "config_file": str(CONFIG_PATH),
    "defaults": {},
    "on_error": None,
}

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off", ""}


# ==================== Setup ====================
def configure(config_file, defaults: Optional[Dict[str, Any]] = None,
              on_error: Optional[Callable[[str], None]] = None) -> None:
    """Point the module at a config file. Call once at startup."""
    with _lock:
        _state["config_file"] = str(config_file)
        # Copy so a caller mutating the dict later can't change the defaults.
        _state["defaults"] = dict(defaults or {})
        _state["on_error"] = on_error


def youtube_defaults() -> Dict[str, Any]:
    return dict(DEFAULT_CONFIG)


def configure_youtube(config_file=None, on_error: Optional[Callable[[str], None]] = None) -> None:
    """The usual setup: schema defaults and the standard config path."""
    configure(config_file or CONFIG_PATH, youtube_defaults(), on_error)


def config_path() -> str:
    """Where the config is stored."""
    return _state["config_file"]


def defaults() -> Dict[str, Any]:
    """A copy of the current defaults (schema defaults, overridden by configure())."""
    return {**DEFAULT_CONFIG, **_state["defaults"]}


def resolve_path(value: str) -> Path:
    """A path setting as an absolute Path; relative ones are relative to the app folder."""
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else APP_DIR / path


# ==================== Checking one value ====================
def _log_error(message: str) -> None:
    handler = _state.get("on_error")
    if handler is None:
        return
    try:
        handler(message)
    except Exception:
        pass


def _coerce(key: str, value: Any) -> Any:
    """Convert a value to the setting's type. Raises ValueError with a readable reason."""
    rules = CONFIG_SCHEMA[key]
    kind = rules["type"]
    if kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE      # "false" is False, unlike bool("false")
        raise ValueError("must be true or false")
    if kind is int:
        if isinstance(value, bool):                    # True is an int in Python; not here
            raise ValueError("must be a whole number")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.lstrip("-").isdigit():
                return int(text)
        raise ValueError("must be a whole number")
    # str
    if not isinstance(value, (str, Path)):
        raise ValueError("must be text")
    text = str(value).strip()
    if rules.get("choices"):
        text = text.lower()
    if rules.get("path") and text:
        text = os.path.expandvars(os.path.expanduser(text.strip('"').strip("'")))
    return text


def check_value(key: str, value: Any) -> Tuple[Any, Optional[str]]:
    """(cleaned value, None) if the value is acceptable for `key`, else (value, reason)."""
    if key not in CONFIG_SCHEMA:
        return value, f"unknown setting '{key}'"
    rules = CONFIG_SCHEMA[key]
    try:
        clean = _coerce(key, value)
    except ValueError as error:
        return value, str(error)
    if rules.get("choices") and clean not in rules["choices"]:
        return value, f"must be one of: {', '.join(rules['choices'])}"
    if rules.get("required") and clean == "":
        return value, "can't be empty"
    if "min" in rules and clean < rules["min"]:
        return value, f"must be at least {rules['min']}"
    if "max" in rules and clean > rules["max"]:
        return value, f"must be at most {rules['max']}"
    return clean, None


def _validate(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Replace any value that would break the downloader with its default.
    A hand-edited config with "audio_format": "mp4" should not propagate
    into a subprocess argument list.
    """
    fallbacks = defaults()
    clean = dict(config)
    for key in CONFIG_SCHEMA:
        if key not in clean:
            clean[key] = fallbacks[key]
            continue
        value, error = check_value(key, clean[key])
        if error:
            _log_error(f"Invalid {key}={clean[key]!r} ({error}); using {fallbacks[key]!r}")
            clean[key] = fallbacks[key]
        else:
            clean[key] = value

    # sleep_min must not exceed sleep_max, or yt-dlp errors out
    smin, smax = clean["yt_dlp_sleep_min"], clean["yt_dlp_sleep_max"]
    if smin > smax:
        _log_error(f"yt_dlp_sleep_min ({smin}) > max ({smax}); swapping")
        clean["yt_dlp_sleep_min"], clean["yt_dlp_sleep_max"] = smax, smin
    return clean


# ==================== Reading and writing ====================
def _move_aside(config_file: str) -> None:
    """Keep an unreadable config as <name>.bad, so a later save can't destroy it."""
    try:
        if os.path.exists(config_file):
            backup = config_file + ".bad"
            os.replace(config_file, backup)
            _log_error(f"Unreadable config moved to {backup}; starting from defaults")
    except OSError:
        pass


def _read_raw(config_file: str) -> Dict[str, Any]:
    """The file's contents as-is. Raises OSError / ValueError."""
    with open(config_file, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("config root is not a JSON object")
    return data


def _save_unlocked(config: Dict[str, Any]) -> bool:
    config_file = _state["config_file"]
    try:
        directory = os.path.dirname(config_file) or "."
        os.makedirs(directory, exist_ok=True)
        payload = {k: str(v) if isinstance(v, Path) else v for k, v in config.items()}

        # Write to a temp file in the same directory, then atomically replace.
        # A crash or full disk mid-write would otherwise leave a truncated
        # config that fails to parse on next launch.
        handle_fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, config_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as error:
        _log_error(f"Error saving configuration: {error}")
        return False


def _load_unlocked() -> Dict[str, Any]:
    config_file = _state["config_file"]
    fallbacks = defaults()
    if not os.path.exists(config_file):
        _save_unlocked(fallbacks)
        return fallbacks
    try:
        user_config = _read_raw(config_file)
    except (OSError, ValueError) as error:          # JSONDecodeError is a ValueError
        _log_error(f"Error loading configuration: {error}")
        _move_aside(config_file)
        return fallbacks
    # Merged over the defaults, so keys added later don't break older files.
    return _validate({**fallbacks, **user_config})


# ==================== Public API: repairing ====================
def load() -> Dict[str, Any]:
    """
    The config, merged over the defaults, with invalid values replaced.
    Creates the file with defaults if it doesn't exist. Never raises.
    """
    with _lock:
        return _load_unlocked()


def save(config: Dict[str, Any]) -> bool:
    """Validate and write a whole config. False if the write failed."""
    with _lock:
        return _save_unlocked(_validate({**defaults(), **config}))


def update(**changes) -> Dict[str, Any]:
    """Load, apply changes, validate, save, and return the result, all under one lock."""
    with _lock:
        config = _validate({**_load_unlocked(), **changes})
        _save_unlocked(config)
        return config


def reset(keep_extras: bool = True) -> Dict[str, Any]:
    """
    Put every setting back to its default and return the result.
    keep_extras keeps what other modules store (active cookie file, last batch file).
    """
    with _lock:
        fresh = defaults()
        if keep_extras:
            current = _load_unlocked()
            fresh.update({k: current[k] for k in KNOWN_EXTRAS if k in current})
        _save_unlocked(fresh)
        return fresh


# ==================== Public API: reporting ====================
def load_config() -> Dict[str, Any]:
    """Same as load(); kept for callers written against config.py."""
    return load()


def save_config(config: Dict[str, Any]) -> bool:
    """Same as save(); kept for callers written against config.py."""
    return save(config)


def get_config_value(key: str, default: Any = None) -> Any:
    """A single setting, or `default` if it isn't set."""
    return load().get(key, default)


def validate_config(config: Optional[Dict[str, Any]] = None) -> Tuple[bool, List[str]]:
    """
    Check a config without changing it. Returns (is_valid, problems).

    With no argument, checks the file as it is on disk. load() quietly
    repairs bad values, so checking load()'s result would always pass.
    """
    errors: List[str] = []
    if config is None:
        config_file = _state["config_file"]
        if not os.path.exists(config_file):
            return True, []          # will be created with defaults on first load
        try:
            config = _read_raw(config_file)
        except (OSError, ValueError) as error:
            return False, [f"The config file can't be read: {error}"]

    for key, rules in CONFIG_SCHEMA.items():
        if key not in config:
            if rules.get("required"):
                errors.append(f"Missing required setting: {key}")
            continue
        value, error = check_value(key, config[key])
        if error:
            errors.append(f"{key} {error} (got {config[key]!r})")
            continue
        if rules.get("must_exist") and value and not resolve_path(value).exists():
            errors.append(f"{key}: {value} doesn't exist")

    smin, smax = config.get("yt_dlp_sleep_min"), config.get("yt_dlp_sleep_max")
    if isinstance(smin, int) and isinstance(smax, int) and smin > smax:
        errors.append(f"yt_dlp_sleep_min ({smin}) is larger than yt_dlp_sleep_max ({smax})")

    for key in config:
        if key not in CONFIG_SCHEMA and key not in KNOWN_EXTRAS:
            errors.append(f"Unknown setting '{key}' has no effect (a typo, or left over "
                          "from an older version?)")
    return not errors, errors


def update_config(key: str, value: Any) -> Tuple[bool, str]:
    """
    Change one setting if the new value is valid. Returns (success, message).
    Text like "5" or "true" is converted to the setting's type.
    """
    clean, error = check_value(key, value)
    if error:
        return False, f"{key} {error}"
    with _lock:
        current = _load_unlocked()
        if key == "yt_dlp_sleep_min" and clean > current["yt_dlp_sleep_max"]:
            return False, (f"yt_dlp_sleep_min can't be larger than yt_dlp_sleep_max "
                           f"({current['yt_dlp_sleep_max']})")
        if key == "yt_dlp_sleep_max" and clean < current["yt_dlp_sleep_min"]:
            return False, (f"yt_dlp_sleep_max can't be smaller than yt_dlp_sleep_min "
                           f"({current['yt_dlp_sleep_min']})")
        current[key] = clean
        if not _save_unlocked(current):
            return False, "Couldn't save the config file"
    return True, f"Updated '{key}' to '{clean}'"


def reset_to_defaults() -> Tuple[bool, str]:
    """Reset every setting. Returns (success, message)."""
    reset()
    return True, "Configuration reset to defaults"


def settings_by_group() -> "OrderedDict[str, List[str]]":
    """{group: [keys]} in schema order, for menus."""
    groups: "OrderedDict[str, List[str]]" = OrderedDict()
    for key, rules in CONFIG_SCHEMA.items():
        groups.setdefault(rules.get("group", "Other"), []).append(key)
    return groups


# ==================== Profiles ====================
def list_profiles() -> Dict[str, Dict[str, Any]]:
    """Every profile and its settings."""
    return {name: dict(settings) for name, settings in CONFIG_PROFILES.items()}


def get_profile_info(profile_name: str) -> Optional[Dict[str, Any]]:
    settings = CONFIG_PROFILES.get(profile_name)
    return dict(settings) if settings else None


def get_config_profile(config: Optional[Dict[str, Any]] = None) -> str:
    """
    The profile the config currently matches, or "custom".
    Worked out from the values, so it stays right after a single setting changes.
    """
    config = config if config is not None else load()
    for name, settings in CONFIG_PROFILES.items():
        if all(config.get(k) == v for k, v in settings.items()):
            return name
    return "custom"


def apply_config_profile(profile_name: str) -> Tuple[bool, str]:
    """Apply a profile's settings. Returns (success, message)."""
    settings = CONFIG_PROFILES.get(profile_name)
    if settings is None:
        return False, f"Unknown profile: {profile_name}. Available: {', '.join(CONFIG_PROFILES)}"
    update(**settings)
    return True, f"Applied profile '{profile_name}'"