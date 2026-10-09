"""Check the downloaded music files: broken files, leftovers, missing tags and cover art.

Two levels:
  quick  sizes, leftovers and folders only (instant)
  full   also opens every audio file with ffprobe to make sure it plays, and
         checks for tags and cover art (a few seconds per hundred files)

Problems it finds:
  broken     audio that won't open, has no audio stream, or is empty/too short
  leftovers  files left by interrupted downloads (.part, .ytdl, .temp) and
             thumbnails that didn't get embedded
  empty      folders with nothing in them
  duplicates the same track saved in two formats (Song.mp3 and Song.opus)
  untagged   no title/artist tags; no cover art (full check only)

Nothing is deleted without asking. Broken files are moved to a "_broken"
folder rather than deleted, so they can be looked at first.

Usage:
    from tools.check_downloads import check_downloads, check_downloaded_files
    report = check_downloads()                 # data only (full check of the music folder)
    check_downloaded_files(downloader)         # interactive: report, then offer to tidy up
"""

import json
import os
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from managers import config_manager, log_manager
from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import find_program, run_program

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg", ".webm", ".aac"}
IMAGE_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png"}
LEFTOVER_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
BROKEN_FOLDER = "_broken"
MIN_SECONDS = 1.0          # anything shorter isn't a real track
MIN_BYTES = 10 * 1024      # an audio file under 10 KB is almost certainly incomplete
WORKERS = 6
SHOW = 8                   # examples listed per problem


@dataclass
class AudioFile:
    path: Path
    size: int
    duration: float = 0.0
    codec: str = ""
    has_tags: bool = False
    has_cover: bool = False
    problem: str = ""           # why it's broken; "" if fine


@dataclass
class DownloadsReport:
    folder: Path
    deep: bool
    audio: List[AudioFile] = field(default_factory=list)
    leftovers: List[Path] = field(default_factory=list)
    empty_folders: List[Path] = field(default_factory=list)
    duplicates: Dict[str, List[Path]] = field(default_factory=dict)
    ffprobe_missing: bool = False

    @property
    def broken(self) -> List[AudioFile]:
        return [f for f in self.audio if f.problem]

    @property
    def healthy(self) -> List[AudioFile]:
        return [f for f in self.audio if not f.problem]

    @property
    def untagged(self) -> List[AudioFile]:
        return [f for f in self.healthy if self.deep and not f.has_tags]

    @property
    def no_cover(self) -> List[AudioFile]:
        return [f for f in self.healthy if self.deep and not f.has_cover]

    @property
    def problems(self) -> int:
        return len(self.broken) + len(self.leftovers) + len(self.empty_folders) + \
            len(self.duplicates)


# ==================== Looking at one file ====================
def _is_leftover(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(LEFTOVER_SUFFIXES) or ".part-frag" in name or ".temp." in name


def probe(audio: AudioFile, ffprobe: str) -> AudioFile:
    """Open a file with ffprobe and fill in duration, codec, tags and cover art."""
    code, output = run_program([ffprobe, "-v", "error", "-print_format", "json",
                                "-show_format", "-show_streams", str(audio.path)], timeout=60)
    if code is None:
        audio.problem = output
        return audio
    try:
        start = output.find("{")
        info = json.loads(output[start:output.rfind("}") + 1]) if start >= 0 else {}
    except ValueError:
        info = {}
    if code != 0 or not info:
        errors = [l.strip() for l in output.splitlines() if l.strip() and not l.strip().startswith(("{", "}", '"'))]
        reason = (errors[-1] if errors else "unreadable file").replace(f"{audio.path}: ", "")
        audio.problem = f"won't open: {reason}"
        return audio

    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    sound = [s for s in streams if s.get("codec_type") == "audio"]
    if not sound:
        audio.problem = "no audio in the file"
        return audio
    audio.codec = sound[0].get("codec_name", "")
    try:
        audio.duration = float(fmt.get("duration") or sound[0].get("duration") or 0)
    except ValueError:
        audio.duration = 0.0
    if audio.duration < MIN_SECONDS:
        audio.problem = f"too short ({audio.duration:.1f}s)"
        return audio

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for stream in sound:                         # Opus/Ogg keep tags on the stream
        tags.update({k.lower(): v for k, v in (stream.get("tags") or {}).items()})
    audio.has_tags = bool(tags.get("title") and (tags.get("artist") or tags.get("album_artist")))
    audio.has_cover = any((s.get("disposition") or {}).get("attached_pic") for s in streams) \
        or "metadata_block_picture" in tags
    return audio


# ==================== Checking a folder ====================
def check_downloads(folder=None, deep: bool = True,
                    progress: Optional[Callable[[int, int], None]] = None) -> DownloadsReport:
    """
    Check every file under `folder` (the music folder from Settings by default).
    deep=True opens each audio file with ffprobe; progress(done, total) is called as it goes.
    """
    folder = Path(folder) if folder else \
        config_manager.resolve_path(config_manager.load()["output_directory"])
    report = DownloadsReport(folder, deep)
    if not folder.is_dir():
        return report

    by_folder_stem: Dict[tuple, List[Path]] = defaultdict(list)
    images: List[Path] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != BROKEN_FOLDER and not d.startswith(".")]
        root_path = Path(root)
        if not dirs and not files and root_path != folder:
            report.empty_folders.append(root_path)
        for name in files:
            path = root_path / name
            ext = path.suffix.lower()
            if _is_leftover(path):
                report.leftovers.append(path)
            elif ext in AUDIO_EXTENSIONS and not name.startswith("."):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                audio = AudioFile(path, size)
                if size == 0:
                    audio.problem = "empty file (0 bytes)"
                elif size < MIN_BYTES:
                    audio.problem = f"only {size} bytes; the download didn't finish"
                report.audio.append(audio)
                by_folder_stem[(root_path, path.stem.casefold())].append(path)
            elif ext in IMAGE_EXTENSIONS:
                images.append(path)

    # A thumbnail named like a track is one yt-dlp failed to embed.
    stems = {(p.parent, p.stem.casefold()) for paths in by_folder_stem.values() for p in paths}
    report.leftovers += [img for img in images if (img.parent, img.stem.casefold()) in stems]
    report.duplicates = {str(paths[0].with_suffix("")): sorted(paths)
                         for paths in by_folder_stem.values() if len(paths) > 1}

    if deep:
        ffprobe, _ = find_program("ffprobe")
        if not ffprobe:
            report.ffprobe_missing = True
        else:
            to_probe = [a for a in report.audio if not a.problem]
            done = 0
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for _ in pool.map(lambda a: probe(a, ffprobe), to_probe):
                    done += 1
                    if progress:
                        progress(done, len(to_probe))
    return report


# ==================== Output ====================
def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def _rel(report: DownloadsReport, path: Path) -> str:
    try:
        return str(path.relative_to(report.folder))
    except ValueError:
        return str(path)


def _examples(report: DownloadsReport, lines: List[str]) -> None:
    for line in lines[:SHOW]:
        print(f"      {line}")
    if len(lines) > SHOW:
        print(f"      ...and {len(lines) - SHOW} more")


def print_report(report: DownloadsReport) -> None:
    if not report.folder.is_dir():
        Enhanced_Menu.print_status(f"The music folder doesn't exist yet: {report.folder}", "info")
        return
    total_size = sum(a.size for a in report.audio)
    formats = Counter(a.path.suffix.lower().lstrip(".") for a in report.audio)
    Enhanced_Menu.print_key_value("Folder", report.folder, width=12)
    Enhanced_Menu.print_key_value("Audio files", f"{len(report.audio)}  ({_size(total_size)})",
                                  width=12, note=", ".join(f"{n} {f}" for f, n in formats.most_common()))
    if report.deep and report.healthy:
        minutes = round(sum(a.duration for a in report.healthy) / 60)
        shown = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
        Enhanced_Menu.print_key_value("Playing time", shown, width=12)
    print()

    if report.ffprobe_missing:
        Enhanced_Menu.print_status("ffprobe wasn't found, so files weren't opened (quick check "
                                   "only). Install ffmpeg to check playback.", "warning")
    if report.broken:
        Enhanced_Menu.print_status(f"{len(report.broken)} broken file(s):", "error")
        _examples(report, [f"{_rel(report, a.path)}  ({a.problem})" for a in report.broken])
    if report.leftovers:
        Enhanced_Menu.print_status(f"{len(report.leftovers)} leftover file(s) from interrupted "
                                   "downloads or failed thumbnail embedding:", "warning")
        _examples(report, [_rel(report, p) for p in report.leftovers])
    if report.duplicates:
        Enhanced_Menu.print_status(f"{len(report.duplicates)} track(s) saved in more than one "
                                   "format:", "warning")
        _examples(report, [" + ".join(p.suffix.lstrip(".") for p in paths) + f"  {_rel(report, Path(stem))}"
                           for stem, paths in report.duplicates.items()])
    if report.empty_folders:
        Enhanced_Menu.print_status(f"{len(report.empty_folders)} empty folder(s).", "info")
    if report.deep and not report.ffprobe_missing:
        if report.untagged:
            Enhanced_Menu.print_status(f"{len(report.untagged)} file(s) without title/artist "
                                       "tags.", "info")
            _examples(report, [_rel(report, a.path) for a in report.untagged])
        if report.no_cover:
            Enhanced_Menu.print_status(f"{len(report.no_cover)} file(s) without cover art "
                                       "(normal for .wav).", "info")
    if not report.problems:
        Enhanced_Menu.print_status("No broken or leftover files found.", "success")


# ==================== Tidying up ====================
def delete_files(paths: List[Path]) -> int:
    deleted = 0
    for path in paths:
        try:
            path.unlink()
            deleted += 1
        except OSError as error:
            Enhanced_Menu.print_status(f"Couldn't delete {path.name}: {error}", "error")
    return deleted


def move_to_broken(report: DownloadsReport) -> int:
    """Move broken files into <music folder>/_broken, keeping their subfolders."""
    moved = 0
    for audio in report.broken:
        target = report.folder / BROKEN_FOLDER / audio.path.relative_to(report.folder)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio.path), str(target))
            moved += 1
        except OSError as error:
            Enhanced_Menu.print_status(f"Couldn't move {audio.path.name}: {error}", "error")
    return moved


def remove_empty_folders(folder: Path) -> int:
    """Remove empty folders, deepest first (so a folder emptied by this counts too)."""
    removed = 0
    for root, dirs, files in sorted(os.walk(folder), key=lambda w: len(w[0]), reverse=True):
        path = Path(root)
        if path == folder or BROKEN_FOLDER in path.relative_to(folder).parts:
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


def check_downloaded_files(downloader=None) -> Optional[DownloadsReport]:
    """Interactive: pick quick or full, show the report, then offer each clean-up."""
    import questionary

    folder = Path(getattr(downloader, "output_directory", "") or "") if downloader else None
    if not folder or not folder.is_dir():
        folder = config_manager.resolve_path(config_manager.load()["output_directory"])

    level = questionary.select(
        f"Check {folder}:",
        choices=[questionary.Choice("Full check (opens every file to make sure it plays)", value=True),
                 questionary.Choice("Quick check (sizes and leftover files only)", value=False),
                 questionary.Choice("Back", value="back")]).ask()
    # (questionary turns value=None into the title, so Back needs a real value)
    if level in (None, "back"):
        return None

    def progress(done: int, total: int) -> None:
        print(f"\r  Checking files... {done}/{total}", end="" if done < total else "\n", flush=True)

    report = check_downloads(folder, deep=level, progress=progress)
    print_report(report)
    if not report.problems:
        return report
    print()

    tidied = []
    if report.leftovers and questionary.confirm(
            f"Delete the {len(report.leftovers)} leftover file(s)?", default=True).ask():
        tidied.append(f"deleted {delete_files(report.leftovers)} leftover(s)")
    if report.broken and questionary.confirm(
            f"Move the {len(report.broken)} broken file(s) to {BROKEN_FOLDER}/ so they can be "
            "checked and downloaded again?", default=True).ask():
        tidied.append(f"moved {move_to_broken(report)} broken file(s) to {BROKEN_FOLDER}/")
    if report.duplicates:
        Enhanced_Menu.print_status("Duplicates are left alone: choose which format to keep and "
                                   "delete the other yourself.", "info")
    if (report.empty_folders or tidied) and questionary.confirm(
            "Remove empty folders?", default=True).ask():
        tidied.append(f"removed {remove_empty_folders(report.folder)} empty folder(s)")

    if tidied:
        summary = "; ".join(tidied)
        Enhanced_Menu.print_status(summary[0].upper() + summary[1:] + ".", "success")
        log_manager.log_info(f"Checked {report.folder}: {summary}", console=False)
    return report