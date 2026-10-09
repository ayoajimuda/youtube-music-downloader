"""Check for missing Python packages and programs, and say how to install them.

Also provides find_program(), run_program() and program_version(), which the
playlist, update and cookie tools import from here.
"""

import importlib.metadata
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from constants import (OPTIONAL_PYTHON_DEPENDENCIES, OPTIONAL_SYSTEM_DEPENDENCIES,
                       PYTHON_DEPENDENCIES, SYSTEM_DEPENDENCIES)
from menu.colorful_menu import Enhanced_Menu

# Packages are checked as installed packages (by pip name), so names like
# "browser-cookie3" work even though you import it as browser_cookie3.
JS_RUNTIMES = tuple(OPTIONAL_SYSTEM_DEPENDENCIES)       # one of them is enough

INSTALL = {
    "yt-dlp": f"{Path(sys.executable).name} -m pip install -U yt-dlp",
    "ffmpeg": {"Windows": "winget install Gyan.FFmpeg", "Darwin": "brew install ffmpeg"}
              .get(platform.system(), "sudo apt install ffmpeg"),
    "ffprobe": "comes with ffmpeg",
    "deno": {"Windows": "winget install DenoLand.Deno", "Darwin": "brew install deno"}
            .get(platform.system(), "curl -fsSL https://deno.land/install.sh | sh"),
}
CONFIG_KEYS = {"yt-dlp": "ytdlp_path", "ffmpeg": "ffmpeg_path", "ffprobe": "ffmpeg_path"}


# ==================== Shared helpers (used by other tools) ====================
def find_program(name: str) -> Tuple[Optional[str], str]:
    """(path, "Settings"/"PATH") or (None, reason). A location set in Settings wins."""
    configured = ""
    if name in CONFIG_KEYS:
        try:
            from managers import config_manager
            configured = config_manager.load().get(CONFIG_KEYS[name]) or ""
        except Exception:
            pass
    if configured:
        from managers import config_manager
        path = config_manager.resolve_path(configured)
        exe = name + (".exe" if os.name == "nt" else "")
        if path.is_dir():
            candidates = [path / exe, path / name]
        elif name == "ffprobe" and path.stem.lower() != "ffprobe":
            candidates = [path.parent / exe, path.parent / name]      # beside ffmpeg
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate), "Settings"
        return None, f"not found at the location in Settings ({configured})"
    found = shutil.which(name)
    return (found, "PATH") if found else (None, "not found on PATH")


def run_program(args: List[str], timeout: float = 30) -> Tuple[Optional[int], str]:
    """(exit code, output). Exit code is None if it couldn't run at all."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return None, f"{Path(args[0]).name} took longer than {timeout:.0f}s"
    except OSError as error:
        return None, f"{Path(args[0]).name} won't run: {error}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def program_version(name: str, flag: str = "--version") -> Tuple[Optional[str], str]:
    """(short version or None, path or reason)."""
    path, where = find_program(name)
    if not path:
        return None, where
    code, output = run_program([path, flag], timeout=15)
    if code is None:
        return None, output
    first = output.strip().splitlines()[0] if output.strip() else ""
    match = re.search(r"version\s+(\S+)", first)
    if match:
        return match.group(1), path
    parts = first.split()
    version = parts[1] if len(parts) > 1 and parts[0].lower() == name else (parts[0] if parts else "")
    return version or "unknown version", path


# ==================== The check ====================
def _requirements() -> List[str]:
    """Package names from requirements.txt in the project folder, if there is one."""
    from managers import config_manager
    path = config_manager.APP_DIR / "requirements.txt"
    names = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):           # comments, -r / -e / --index-url
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)   # stops at ==, >=, [extras], ;
        if match:
            names.append(match.group(0))
    return names


def _installed(package: str) -> bool:
    try:
        importlib.metadata.version(package)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def dependency_check() -> Tuple[List[str], List[str]]:
    """
    Check for missing Python packages and programs; print what's missing and
    how to install it. Returns (missing_python, missing_system), required only.
    """
    missing_python, missing_system = [], []
    optional_missing = []

    # --- Python packages (skipped in a built .exe: they're bundled inside it) ---
    if not getattr(sys, "frozen", False):
        packages = {name: True for name in PYTHON_DEPENDENCIES}
        packages.update({name: False for name in OPTIONAL_PYTHON_DEPENDENCIES})
        for name in _requirements():
            packages.setdefault(name, True)
        for name, required in packages.items():
            if not _installed(name):
                (missing_python if required else optional_missing).append(name)

    # --- Programs ---
    for name in SYSTEM_DEPENDENCIES:
        if not find_program(name)[0]:
            missing_system.append(name)
    if not any(find_program(name)[0] for name in JS_RUNTIMES):
        optional_missing.append("deno")

    # --- Output ---
    pip = f"{Path(sys.executable).name} -m pip install"
    if missing_python:
        Enhanced_Menu.print_status("Missing Python packages:", "error")
        for name in missing_python:
            print(f"   - {name}")
        print(f"   Install with: {pip} {' '.join(missing_python)}")
    else:
        Enhanced_Menu.print_status("All Python packages are installed.", "success")

    if missing_system:
        Enhanced_Menu.print_status("Missing programs:", "error")
        for name in missing_system:
            where = find_program(name)[1]
            print(f"   - {name} ({where}). Install: {INSTALL.get(name, '')}")
    else:
        Enhanced_Menu.print_status("All required programs are installed.", "success")

    for name in optional_missing:
        if name == "deno":
            Enhanced_Menu.print_status("Optional: no JavaScript runtime (Deno or Node). Some "
                                       f"YouTube formats need one. Install: {INSTALL['deno']}",
                                       "warning")
        else:
            Enhanced_Menu.print_status(f"Optional: {name} isn't installed ({pip} {name})", "info")

    return missing_python, missing_system