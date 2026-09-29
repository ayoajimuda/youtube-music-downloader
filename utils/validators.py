import os
import re
import spotipy
from dotenv import load_dotenv
load_dotenv()
from ytmusicapi import YTMusic
import json
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import urllib.parse

class Helpers:
    """ A Class for all static methods used by the program"""
    
    # ========================================= Youtube Functions =========================================
    @staticmethod
    def validate_youtube_url(url: str) -> bool:
        patterns = [
            r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$',
            r'^(https?://)?music\.youtube\.com/.+$',
            r'^(https?://)?youtube\.com/watch\?v=[\w-]+(&.*)?$',
            r'^(https?://)?youtube\.com/playlist\?list=[\w-]+(&.*)?$',
            r'^(https?://)?youtu\.be/[\w-]+$'
        ]
        for pattern in patterns:
            if re.match(pattern, url, re.IGNORECASE):
                try:
                    parsed = urllib.parse.urlparse(url)
                    if parsed.scheme in ('http', 'https', '') or parsed.netloc:
                        return True
                except:
                    continue
        return False

    @staticmethod
    def extract_youtube_id(url: str) -> str:
        patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+)',
            r'youtube\.com/playlist\?list=([\w-]+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def extract_youtube_playlist_id(url: str) -> Optional[str]:
        patterns = [
            r'[?&]list=([^&]+)',
            r'/playlist\?list=([^&]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def get_youtube_playlist_items(url: str, log_manager, cookie_file: str = None) -> List[Dict]:
        """Fetch ALL playlist entries.

        Primary path uses ytmusicapi (YouTube Music's own API), which paginates
        reliably past yt-dlp's ~100-200 flat-playlist ceiling. Falls back to
        yt-dlp flat extraction if ytmusicapi is unavailable or errors.
        """
        playlist_id = Helpers.extract_youtube_playlist_id(url)

        if playlist_id:
            try:

                yt = YTMusic()  # unauthenticated is fine for public playlists
                data = yt.get_playlist(playlist_id, limit=None)  # None = fetch everything
                tracks = data.get("tracks", []) if data else []

                items = []
                for t in tracks:
                    vid = t.get("videoId")
                    if not vid:
                        continue
                    if t.get("isAvailable") is False:  # skip removed/region-blocked
                        continue
                    artists = t.get("artists") or []
                    artist = artists[0].get("name") if artists else None
                    items.append({
                        "id": vid,
                        "title": t.get("title"),
                        "artist": artist,
                    })

                if items:
                    log_manager.log_success(
                        f"ytmusicapi retrieved {len(items)} tracks "
                        f"(reported total: {data.get('trackCount', '?')})"
                    )
                    return items

                log_manager.log_warning("ytmusicapi returned no tracks; falling back to yt-dlp")
            except ImportError:
                log_manager.log_warning(
                    "ytmusicapi not installed (pip install ytmusicapi); "
                    "falling back to yt-dlp (large playlists will be truncated)"
                )
            except Exception as e:
                log_manager.log_error(f"ytmusicapi failed ({e}); falling back to yt-dlp")

        # ---- Fallback: yt-dlp flat extraction (may truncate large playlists) ----
        command = [
            "yt-dlp", "--flat-playlist", "--dump-json", "--ignore-errors",
            "--extractor-retries", "15",
            "--extractor-args", "youtubetab:skip=webpage",
        ]
        if cookie_file:
            command += ["--cookies", cookie_file]
        command.append(url)
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=600, check=False)
        except subprocess.TimeoutExpired as e:
            log_manager.log_warning("Playlist fetch timed out; using partial results")
            result = None
            raw = e.stdout or ""
        except Exception as e:
            log_manager.log_error(f"Error fetching playlist items: {e}")
            return []
        else:
            raw = result.stdout or ""

        items = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items
    
    @staticmethod
    def validate_resource_youtube(url: str, timeout=30) -> Tuple[bool, str, Optional[Dict]]:
        """Validate YouTube URL and return metadata."""
        command = ["yt-dlp", "--skip-download", "--flat-playlist", "--dump-json", "--no-warnings", url]
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=timeout, check=False)
            if result.returncode == 0:
                try:
                    for line in result.stdout.strip().split('\n'):
                        if line:
                            metadata = json.loads(line)
                            if metadata.get('availability') == 'unavailable':
                                return False, "Video unavailable", metadata
                            return True, f"Available - {metadata.get('title', 'Unknown')}", metadata
                    return True, "Resource available", None
                except json.JSONDecodeError:
                    return True, "Resource available", None
            else:
                err = result.stderr.lower()
                if "unavailable" in err:
                    return False, "Resource unavailable", None
                elif "private" in err:
                    return False, "Restricted access", None
                elif "age restriction" in err:
                    return False, "Age restricted video", None
                elif "not found" in err:
                    return False, "Resource not found", None
                else:
                    return False, f"Validation failed: {err[:100]}", None
        except subprocess.TimeoutExpired:
            return False, "Validation timeout", None
        except Exception as e:
            return False, f"Validation error: {str(e)[:100]}", None

    # ========================================= Other functions =========================================
    @staticmethod
    def cleanup_directory(output_directory: Path, log_manager) -> None:
        """Remove empty directories under output_directory."""
        removed = 0
        for dir_path in sorted(output_directory.rglob('*'), reverse=True):
            if dir_path.is_dir() and not any(dir_path.iterdir()):
                dir_path.rmdir()
                removed += 1
        if removed:
            log_manager.log_success(f"Removed {removed} empty director{'y' if removed==1 else 'ies'}")

    @staticmethod
    def sanitize_filename(name: str) -> str:
        """Remove invalid characters for file/folder names."""
        name = re.sub(r'[<>:"/\\|?*]', '_', name).strip('. ')
        return name if name else "_"

    @staticmethod
    def parse_size(size_str: str) -> Optional[int]:
        if not size_str:
            return None
        size_str = size_str.strip().upper()
        units = {
            'B': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4,
            'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4,
            'KIB': 1024, 'MIB': 1024**2, 'GIB': 1024**3, 'TIB': 1024**4
        }
        match = re.match(r'([\d\.]+)\s*(\w*)', size_str)
        if not match:
            return None
        value, unit = match.groups()
        try:
            value = float(value)
            if not unit:
                return int(value)
            if unit in units:
                return int(value * units[unit])
        except ValueError:
            return None
        return None