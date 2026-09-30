"""Loading, defaulting, validating and saving the downloader's JSON config. """

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

VALID_FORMATS = ("mp3", "flac", "ogg", "opus", "m4a", "wav")
VALID_QUALITIES = ("auto", "disable", "8k", "16k", "24k", "32k", "40k", "48k",
                   "64k", "80k", "96k", "112k", "128k", "160k", "192k",
                   "224k", "256k", "320k")

# Keys that must be positive integers, with their sane bounds.
INT_BOUNDS = {
    "max_retries": (1, 10),
    "retry_delay": (0, 3600),
    "download_timeout": (10, 86400),
    "max_concurrent": (1, 16),
    "yt_dlp_sleep_min": (0, 3600),
    "yt_dlp_sleep_max": (0, 3600),
}

COMMON_DEFAULTS: Dict[str, Any] = {
    "audio_quality": "320k",
    "audio_format": "mp3",
    "max_retries": 3,
    "retry_delay": 10,
    "download_timeout": 120,
    "use_cookies": False,
    "max_concurrent": 2,
    "yt_dlp_sleep_min": 3,
    "yt_dlp_sleep_max": 7,
}

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "config_file": "config/YoutubeMusicDownloader.json",
    "defaults": {},
    "on_error": None,
}

# ==================== Setup ====================
def configure(config_file: str, defaults: Dict[str, Any],
              on_error: Optional[Callable[[str], None]] = None) -> None:
    """Point the module at a config file and its defaults. Call once at startup."""
    with _lock:
        _state["config_file"] = str(config_file)
        # Copy so a caller mutating the dict later can't retroactively change
        # what this module considers a default.
        _state["defaults"] = dict(defaults)
        _state["on_error"] = on_error

def youtube_defaults() -> Dict[str, Any]:
    return {
        **COMMON_DEFAULTS,
        "output_directory": str(Path.home() / "Music" / "Collection" / "YouTube"),
    }

def configure_youtube(config_file: str = "config/YoutubeMusicDownloader.json",
                      on_error: Optional[Callable[[str], None]] = None) -> None:
    """The usual setup: YouTube defaults and the standard config path."""
    configure(config_file, youtube_defaults(), on_error)

def config_path() -> str:
    """Where the config is stored."""
    return _state["config_file"]

def defaults() -> Dict[str, Any]:
    """A copy of the current defaults."""
    return dict(_state["defaults"] or youtube_defaults())


# ==================== Internals ====================
def _log_error(message: str) -> None:
    handler = _state.get("on_error")
    if handler is None:
        return
    try:
        handler(message)
    except Exception:
        pass

def _validate(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Replace any value that would break the downloader with its default.
    A hand-edited config with "audio_format": "mp4" should not propagate
    into a subprocess argument list.
    """
    fallbacks = defaults()
    clean = dict(config)

    fmt = clean.get("audio_format")
    if not isinstance(fmt, str) or fmt.lower() not in VALID_FORMATS:
        if fmt is not None:
            _log_error(f"Invalid audio_format {fmt!r}; using default")
        clean["audio_format"] = fallbacks.get("audio_format", "mp3")
    else:
        clean["audio_format"] = fmt.lower()

    quality = clean.get("audio_quality")
    if not isinstance(quality, str) or quality.lower() not in VALID_QUALITIES:
        if quality is not None:
            _log_error(f"Invalid audio_quality {quality!r}; using default")
        clean["audio_quality"] = fallbacks.get("audio_quality", "320k")
    else:
        clean["audio_quality"] = quality.lower()

    for key, (low, high) in INT_BOUNDS.items():
        if key not in clean:
            continue
        fallback = fallbacks.get(key, low)
        try:
            value = int(clean[key])
        except (TypeError, ValueError):
            _log_error(f"Invalid {key}={clean[key]!r}; using {fallback}")
            clean[key] = fallback
            continue
        if not (low <= value <= high):
            _log_error(f"{key}={value} out of range [{low}, {high}]; using {fallback}")
            clean[key] = fallback
        else:
            clean[key] = value

    # sleep_min must not exceed sleep_max, or yt-dlp errors out
    smin, smax = clean.get("yt_dlp_sleep_min"), clean.get("yt_dlp_sleep_max")
    if isinstance(smin, int) and isinstance(smax, int) and smin > smax:
        _log_error(f"yt_dlp_sleep_min ({smin}) > max ({smax}); swapping")
        clean["yt_dlp_sleep_min"], clean["yt_dlp_sleep_max"] = smax, smin

    clean["use_cookies"] = bool(clean.get("use_cookies", False))

    out_dir = clean.get("output_directory")
    if not out_dir or not isinstance(out_dir, str):
        clean["output_directory"] = fallbacks.get("output_directory", str(Path.home()))

    return clean

def _save_unlocked(config: Dict[str, Any]) -> None:
    config_file = _state["config_file"]
    try:
        directory = os.path.dirname(config_file) or "."
        os.makedirs(directory, exist_ok=True)

        payload = {**config}
        for key, value in payload.items():
            if isinstance(value, Path):
                payload[key] = str(value)

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
    except Exception as error:
        _log_error(f"Error saving configuration: {error}")

# ==================== Public API ====================
def load() -> Dict[str, Any]:
    """
    Load config from disk, merged over the defaults so keys added later
    don't break older config files. Creates the file with defaults if it
    doesn't exist. Falls back to defaults on any read/parse error.
    """
    with _lock:
        fallbacks = dict(_state["defaults"] or youtube_defaults())
        try:
            config_file = _state["config_file"]
            if os.path.exists(config_file):
                with open(config_file, "r", encoding="utf-8") as handle:
                    user_config = json.load(handle)
                if not isinstance(user_config, dict):
                    raise ValueError("config root is not a JSON object")
                return _validate({**fallbacks, **user_config})
            _save_unlocked(fallbacks)
            return fallbacks
        except Exception as error:
            _log_error(f"Error loading configuration: {error}")
            return fallbacks

def save(config: Dict[str, Any]) -> None:
    """Persist a config dict to disk as JSON, creating parent dirs as needed."""
    with _lock:
        _save_unlocked(config)

def update(**changes) -> Dict[str, Any]:
    """Load, apply changes, validate, save, and return the merged config."""
    config = load()
    config.update(changes)
    config = _validate(config)
    save(config)
    return config

def reset() -> Dict[str, Any]:
    """Overwrite the config file with defaults and return them."""
    fresh = defaults()
    save(fresh)
    return fresh