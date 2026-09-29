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