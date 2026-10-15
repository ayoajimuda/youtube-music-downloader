"""Check that everything the downloader needs is installed, and say how to fix what isn't.

Also home to find_program() and run_program(), which the other tools use so
they all respect the ffmpeg/yt-dlp locations set in Settings.

Usage:
    from tools.dependency_check import dependency_check
    all_ok = dependency_check()            # prints a report, returns True if nothing required is missing
    report = check_dependencies()          # the same, as data
"""

import importlib.metadata
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from menu.colorful_menu import Enhanced_Menu

# Which Settings key can point at each program.
CONFIG_KEYS = {"yt-dlp": "ytdlp_path", "ffmpeg": "ffmpeg_path", "ffprobe": "ffmpeg_path"}

_SYSTEM = platform.system()          # "Windows", "Darwin", "Linux"


def _install_hint(windows: str, mac: str, linux: str) -> str:
    return {"Windows": windows, "Darwin": mac}.get(_SYSTEM, linux)


# (name, version flag, what it's for, required, how to install)
PROGRAMS = (
    ("yt-dlp", "--version", "does the downloading", True,
     f"{Path(sys.executable).name} -m pip install -U yt-dlp"),
    ("ffmpeg", "-version", "converts audio and embeds thumbnails", True,
     _install_hint("winget install Gyan.FFmpeg", "brew install ffmpeg",
                   "sudo apt install ffmpeg  (or your distro's package manager)")),
    ("ffprobe", "-version", "reads audio details (comes with ffmpeg)", True,
     "installed together with ffmpeg"),
    ("deno", "--version", "JavaScript runtime some YouTube formats need", False,
     _install_hint("winget install DenoLand.Deno", "brew install deno",
                   "curl -fsSL https://deno.land/install.sh | sh")),
    ("node", "--version", "alternative JavaScript runtime", False,
     _install_hint("winget install OpenJS.NodeJS.LTS", "brew install node",
                   "sudo apt install nodejs")),
)

# (import name, pip name, what it's for, required)
PACKAGES = (
    ("colorama", "colorama", "coloured output", True),
    ("tqdm", "tqdm", "progress bars", True),
    ("requests", "requests", "update checks and cookie tests", True),
    ("questionary", "questionary", "arrow-key menus", True),
    ("browser_cookie3", "browser-cookie3", "reading cookies straight from a browser", False),
    ("PySide6", "PySide6", "the desktop window (gui_main.py)", False),
)


# ==================== Shared helpers ====================
def _configured_location(name: str) -> str:
    key = CONFIG_KEYS.get(name)
    if not key:
        return ""
    try:
        from managers import config_manager
        return config_manager.load().get(key) or ""
    except Exception:
        return ""


def bundled_bin_dirs() -> List[Path]:
    """Where a packaged build (PyInstaller) keeps the programs it ships with."""
    if not getattr(sys, "frozen", False):
        return []
    dirs = []
    if getattr(sys, "_MEIPASS", None):
        dirs.append(Path(sys._MEIPASS) / "bin")
    dirs.append(Path(sys.executable).resolve().parent / "bin")
    return dirs


def find_program(name: str) -> Tuple[Optional[str], str]:
    """
    (path, where it came from) for a program, or (None, reason).

    A location set in Settings wins; it can be the program itself or the
    folder holding it (an ffmpeg location also finds ffprobe next to it).
    Then a packaged app's own bin/ folder, then PATH.
    """
    configured = _configured_location(name)
    if configured:
        from managers import config_manager
        path = config_manager.resolve_path(configured)
        exe = name + (".exe" if os.name == "nt" else "")
        if path.is_dir():
            candidates = [path / exe, path / name]
        elif name == "ffprobe" and path.stem.lower() != "ffprobe":
            candidates = [path.parent / exe, path.parent / name]    # ffprobe sits beside ffmpeg
        else:
            candidates = [path]                                     # the program itself, any name
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate), "Settings"
        return None, f"not found at the location in Settings ({configured})"
    exe = name + (".exe" if os.name == "nt" else "")
    for folder in bundled_bin_dirs():
        candidate = folder / exe
        if candidate.is_file():
            return str(candidate), "bundled with the app"
    found = shutil.which(name)
    return (found, "PATH") if found else (None, "not found on PATH")


def run_program(args: List[str], timeout: float = 30) -> Tuple[Optional[int], str]:
    """(exit code, stdout + stderr). Exit code is None if it couldn't run at all."""
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
    match = re.search(r"version\s+(\S+)", first)           # "ffmpeg version 7.0.1 Copyright"
    if match:
        version = match.group(1)
    else:
        parts = first.split()                               # "2025.09.26", "deno 2.1.4 (...)", "v22.1.0"
        version = parts[1] if len(parts) > 1 and parts[0].lower() == name else (parts[0] if parts else "")
    return (version or "unknown version")[:40], path


# ==================== Checking ====================
@dataclass
class Dependency:
    name: str
    kind: str                  # "program" or "package"
    purpose: str
    required: bool
    found: bool
    version: str = ""
    location: str = ""
    fix: str = ""


@dataclass
class DependencyReport:
    items: List[Dependency] = field(default_factory=list)

    @property
    def missing_required(self) -> List[Dependency]:
        return [d for d in self.items if d.required and not d.found]

    @property
    def has_js_runtime(self) -> bool:
        return any(d.found for d in self.items if d.name in ("deno", "node"))

    @property
    def ok(self) -> bool:
        return not self.missing_required


def check_dependencies() -> DependencyReport:
    report = DependencyReport()
    for name, flag, purpose, required, fix in PROGRAMS:
        version, detail = program_version(name, flag)
        report.items.append(Dependency(
            name, "program", purpose, required, found=version is not None,
            version=version or "", location=detail if version else "",
            fix="" if version else (f"{detail}. Install: {fix}" if "Settings" in detail
                                    else f"Install: {fix}")))
    for module, pip_name, purpose, required in PACKAGES:
        found = importlib.util.find_spec(module) is not None
        version = ""
        if found:
            try:
                version = importlib.metadata.version(pip_name)
            except importlib.metadata.PackageNotFoundError:
                version = "installed"
        report.items.append(Dependency(
            pip_name, "package", purpose, required, found, version=version,
            fix="" if found else f"Install: {Path(sys.executable).name} -m pip install {pip_name}"))
    return report


def print_report(report: DependencyReport) -> None:
    for kind, heading in (("program", "Programs"), ("package", "Python packages")):
        Enhanced_Menu.print_section(heading)
        for dep in (d for d in report.items if d.kind == kind):
            if dep.found:
                state = "ok"
                note = dep.version + (f"  ({dep.location})" if dep.location and
                                      dep.kind == "program" else "")
            else:
                state = "MISSING" if dep.required else "not installed"
                note = dep.purpose
            Enhanced_Menu.print_key_value(dep.name, f"{state:<14}", width=16, note=note)

    Enhanced_Menu.print_section("System")
    Enhanced_Menu.print_key_value("Python", f"{sys.version.split()[0]}  ({sys.executable})",
                                  width=16)
    Enhanced_Menu.print_key_value("OS", f"{platform.system()} {platform.release()}", width=16)
    print()

    if report.missing_required:
        Enhanced_Menu.print_status("Missing: " + ", ".join(d.name for d in report.missing_required),
                                   "error")
        for dep in report.missing_required:
            print(f"    {dep.name}: {dep.fix}")
    else:
        Enhanced_Menu.print_status("Everything required is installed.", "success")
    if not report.has_js_runtime:
        js = next(d for d in report.items if d.name == "deno")
        Enhanced_Menu.print_status("No JavaScript runtime found. If downloads fail with "
                                   "'No supported JS runtime', install Deno (recommended):\n"
                                   f"{js.fix}", "warning")
    optional = [d for d in report.items if not d.required and not d.found
                and d.name not in ("deno", "node")]
    for dep in optional:
        Enhanced_Menu.print_status(f"Optional: {dep.name} ({dep.purpose}). {dep.fix}", "info")


def dependency_check() -> bool:
    """Print the dependency report. True if nothing required is missing."""
    report = check_dependencies()
    print_report(report)
    return report.ok