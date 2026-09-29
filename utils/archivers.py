import re
import threading
from pathlib import Path
from typing import Callable, Optional, Set

@staticmethod
def safe_name(name, fallback: str = "Playlist") -> str:
    """Turn an arbitrary title into a filesystem-safe folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    return cleaned[:120] or fallback


def load_archive(self, archive_path: Path) -> Set[str]:
    """Return the set of video IDs already recorded in a yt-dlp archive file."""
    ids: Set[str] = set()
    try:
        if Path(archive_path).exists():
            with open(archive_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        ids.add(parts[-1])
    except OSError as e:
        self._on_error(f"Could not read archive {archive_path}: {e}")
    return ids

def append_archive(self, archive_path: Path, video_id: str, lock: threading.Lock):
    """Append a finished video ID to the archive, serialised across threads."""
    with lock:
        try:
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(f"youtube {video_id}\n")
        except OSError as e:
            self._on_error(f"Could not update archive {archive_path}: {e}")