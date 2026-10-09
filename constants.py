"""Project-wide constants.

Values that another module owns (formats, file locations) are taken from that
module rather than copied, so the two can never disagree. Anything that can
change in Settings (like the music folder) is a function, so it always returns
the current value.
"""

from pathlib import Path

from managers import config_manager, log_manager

# ==================== Dependencies ====================
# pip names (what you'd type after "pip install")
PYTHON_DEPENDENCIES = ["colorama", "tqdm", "requests", "questionary"]
OPTIONAL_PYTHON_DEPENDENCIES = ["browser-cookie3"]      # reading cookies from a browser

SYSTEM_DEPENDENCIES = ["yt-dlp", "ffmpeg", "ffprobe"]
OPTIONAL_SYSTEM_DEPENDENCIES = ["deno", "node"]         # one JavaScript runtime is enough

# ==================== Audio ====================
AUDIO_FORMATS = config_manager.VALID_FORMATS             # what the downloader accepts
VALID_AUDIO_EXTENSIONS = {f".{fmt}" for fmt in AUDIO_FORMATS}
DEFAULT_AUDIO_FORMAT = config_manager.DEFAULT_CONFIG["audio_format"]

AUDIO_BITRATE_OPTIONS = {
    "64k": "Very low quality (speech/podcasts)",
    "96k": "Low quality (voice focus)",
    "128k": "Medium quality (most music)",
    "192k": "Good quality (balanced size)",
    "256k": "High quality",
    "320k": "Best quality (larger file size)",
    "auto": "Let yt-dlp choose (keeps the source quality)",
}

# ==================== Folders and files ====================
APP_DIR = config_manager.APP_DIR
CONFIG_FILE = config_manager.CONFIG_PATH
FAILED_FILE = log_manager.FAILED_FILE                   # the retry queue
LOG_DIR = log_manager.LOG_DIR
HISTORY_DIR = log_manager.HISTORY_DIR


def download_directory() -> Path:
    """The music folder currently set in Settings."""
    return config_manager.resolve_path(config_manager.load()["output_directory"])