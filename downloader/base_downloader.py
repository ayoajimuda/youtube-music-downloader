""" Base downloader for the youtube music downloader. Contains only the download functions"""


import json
import hashlib
import os
import random
import re
import subprocess
import threading
import time

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlsplit
from tqdm import tqdm
from colorama import init, Fore, Style

from downloader import batch_downloader, rate_limiter
from downloader.assist_methods import cleanup_directory, parse_size
from managers import config_manager, log_manager
from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import find_program, run_program

init(autoreset=True)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
INFO_TIMEOUT = 120        # seconds to read a link's details before downloading
STOP_AFTER_THROTTLED = 3  # throttled links in a row before a batch stops


class _NullBar:
    """No-op stand-in for tqdm so worker threads don't render nested bars."""
    total = None
    n = 0

    def set_description(self, *args, **kwargs):
        pass

    def set_postfix_str(self, *args, **kwargs):
        pass

    def refresh(self):
        pass

    def close(self):
        pass


# ==================== Link helpers ====================
def is_youtube_url(url: str) -> bool:
    """True for youtube.com / music.youtube.com / youtu.be links."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in YOUTUBE_HOSTS


def extract_playlist_id(url: str) -> Optional[str]:
    """The playlist/album ID from a link (list=..., or a YouTube Music /browse/ album)."""
    parts = urlsplit((url or "").strip())
    listed = parse_qs(parts.query).get("list")
    if listed and listed[0]:
        return listed[0]
    match = re.search(r"/browse/([A-Za-z0-9_-]+)", parts.path)
    return match.group(1) if match else None


def safe_name(name: str, fallback: str = "_") -> str:
    """A string that's safe as a file or folder name on any OS."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(". ")
    return name[:150].rstrip(". ") or fallback


def classify_failure(output: str, timed_out: bool = False, timeout: int = 0) -> str:
    """A short, readable reason for a failed yt-dlp run (most specific first)."""
    low = (output or "").lower()
    if timed_out:
        return f"Stalled: no output for {timeout}s, stopped"
    if re.search(r"http error 403|\b403[: ]+forbidden", low):
        return "HTTP 403 (YouTube refused the stream: update yt-dlp, then check cookies / JS runtime)"
    if re.search(r"http error 429|too many requests", low):
        return "HTTP 429 (rate limited, slow down or wait)"
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube requires authentication (sign in to confirm you're not a bot - check cookies)"
    if "only images are available" in low:
        return "No audio stream (SABR/format restriction)"
    if "requested format is not available" in low:
        return "Requested format not available"
    if "javascript runtime" in low or "no supported js" in low:
        return "Missing JS runtime (install Deno or Node)"
    if "confirm your age" in low or ("age" in low and "restrict" in low):
        return "Age restricted (needs cookies from an adult account)"
    if "private video" in low:
        return "Video is private"
    if "members-only" in low or "members only" in low or "join this channel" in low:
        return "Members-only content"
    if "copyright" in low:
        return "Copyright restriction"
    if "video unavailable" in low or "not available in your" in low or "is not available" in low:
        return "Video unavailable / region-locked"
    if ("ffmpeg" in low or "ffprobe" in low) and "not found" in low:
        return "ffmpeg not found (install ffmpeg, or set its location in Settings)"
    if "postprocessing" in low or "ffmpeg" in low:
        return "FFmpeg conversion error"
    if not output:
        return "No output from yt-dlp"
    errors = [l.strip() for l in output.splitlines() if l.strip().upper().startswith("ERROR")]
    return (errors[-1] if errors else output.strip()[-300:])[:300]


class YoutubeMusicDownloader:
    """
    Downloader class that contains all the download functions
    """

    def __init__(self):
        self.__output_directory = Path.home() / "Music" / "Collection" / "YouTube"
        self.__audio_quality = "320k"
        self.__audio_format = "mp3"
        self.use_cookies = False
        self.max_retries = 2          # attempts for a struggling download
        self.retry_delay = 20         # seconds between attempts
        self.download_timeout = 120   # seconds of silence before a download is killed

        self.debug = False
        self.max_concurrent = 3       # downloads at once (playlists)
        self.yt_dlp_sleep_min = 3     # min seconds yt-dlp waits between requests
        self.yt_dlp_sleep_max = 7     # max seconds (random delay in this range)

        self.rate_limit_backoff = 300
        self.rate_limit_max_wait = 1800
        self._last_run_throttled = False

        # The modules this class works with. main_menu can replace any of them.
        self.log_manager = log_manager
        self.history = log_manager            # add_input lives in the log manager
        self.batch_file = batch_downloader

        self.archives_dir = Path(log_manager.HISTORY_DIR) / "archives"
        self.archives_dir.mkdir(parents=True, exist_ok=True)

        self.load_settings()

    # ==================== Settings ====================
    def load_settings(self, settings: Optional[dict] = None) -> None:
        """Take the saved settings from the config manager (or the dict given)."""
        settings = settings if settings is not None else config_manager.load()
        self.audio_format = settings["audio_format"]
        self.audio_quality = settings["audio_quality"]
        self.output_directory = config_manager.resolve_path(settings["output_directory"])
        self.use_cookies = bool(settings.get("use_cookies"))
        for key in ("max_retries", "retry_delay", "download_timeout", "max_concurrent",
                    "yt_dlp_sleep_min", "yt_dlp_sleep_max"):
            if key in settings:
                setattr(self, key, settings[key])

    # The settings menus still set the old name; keep both in step.
    @property
    def max_concurrency(self) -> int:
        return self.max_concurrent

    @max_concurrency.setter
    def max_concurrency(self, value: int):
        self.max_concurrent = value

    def get_user_preferences(self):
        """Ask for the download settings, save them, and apply them here."""
        from tools import ask_user_preference as preferences
        if not preferences.is_configured():
            from managers import cookie_manager
            preferences.configure(config_manager, Enhanced_Menu, cookie_manager)
        return preferences.get_user_preferences(self)

    # ==================== Public properties ====================
    @property
    def audio_format(self) -> str:
        """Current audio format (mp3, flac, etc.)."""
        return self.__audio_format

    @audio_format.setter
    def audio_format(self, value: str):
        if value in config_manager.VALID_FORMATS:
            self.__audio_format = value
        else:
            raise ValueError(f"Unsupported audio format: {value}")

    @property
    def audio_quality(self) -> str:
        """Current audio bitrate (320k, 192k, auto, etc.)."""
        return self.__audio_quality

    @audio_quality.setter
    def audio_quality(self, value: str):
        if value in config_manager.VALID_QUALITIES:
            self.__audio_quality = value
        else:
            raise ValueError(f"Unsupported audio quality: {value}")

    @property
    def output_directory(self) -> Path:
        """Output directory path."""
        return self.__output_directory

    @output_directory.setter
    def output_directory(self, path):
        self.__output_directory = Path(path)
        self.__output_directory.mkdir(parents=True, exist_ok=True)

    # ==================== yt-dlp helpers ====================
    def _ytdlp(self) -> str:
        """The yt-dlp program (location from Settings, or on PATH)."""
        path, where = find_program("yt-dlp")
        if not path:
            message = f"yt-dlp not found ({where}). Install it with: pip install -U yt-dlp"
            self.log_manager.log_error(message)
            raise RuntimeError(message)
        return path

    def _common_args(self) -> List[str]:
        """Arguments every yt-dlp call shares: cookies and the ffmpeg location."""
        args = []
        cookie_file = self._get_cookie_file()
        if cookie_file:
            args += ["--cookies", cookie_file]
        ffmpeg = config_manager.load().get("ffmpeg_path") or ""
        if ffmpeg:
            args += ["--ffmpeg-location", str(config_manager.resolve_path(ffmpeg))]
        return args

    def _get_cookie_file(self) -> Optional[str]:
        """Return a usable cookie-file path if cookies are enabled, else None."""
        if not self.use_cookies:
            return None
        from managers import cookie_manager          # late: it creates cookies/ on import
        active = cookie_manager.get_active_cookie_file()
        if active:
            return str(Path(active).resolve())
        folder = Path(cookie_manager.COOKIE_DIRECTORY)
        if folder.is_dir():
            candidates = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime,
                                reverse=True)
            if candidates:
                return str(candidates[0].resolve())
        self.log_manager.log_warning("Cookies are enabled but no cookie file was found. "
                                     "Add one in the Cookies menu.")
        return None

    def fetch_info(self, url: str, item_type: str = "track") -> Tuple[bool, str, Optional[dict]]:
        """
        Read a link's details without downloading. Returns (ok, message, info).
        Albums and playlists are listed flat (titles and IDs only), which is quick.
        """
        mode = ["--no-playlist"] if item_type == "track" else ["--flat-playlist"]
        code, output = run_program([self._ytdlp(), "--dump-single-json", "--no-warnings",
                                    *mode, *self._common_args(), url], timeout=INFO_TIMEOUT)
        if code is None:
            return False, output, None
        if code != 0:
            return False, classify_failure(output), None
        start = output.find("{")
        try:
            info = json.loads(output[start:output.rfind("}") + 1]) if start >= 0 else None
        except ValueError:
            info = None
        if not info:
            return False, "yt-dlp didn't return any details for that link", None
        if item_type != "track" and info.get("_type") != "playlist":
            return False, f"That link is a single video, not {'an album' if item_type == 'album' else 'a playlist'}", None
        return True, "ok", info

    # ========================= Download Functions ================================
    def run_download(self, url: str, output_template: str, additional_args=None, show_progress: bool = True):
        """
        Runs a yt-dlp download with a tqdm progress bar and a stall watchdog.

        Args:
            url (str): The link to download
            output_template (str): yt-dlp output template (folder + file name pattern)
            additional_args (list, optional): extra yt-dlp arguments
            show_progress (bool, optional): draw a progress bar. Defaults to True.

        Returns a CompletedProcess on success (or when it was already downloaded).
        Raises CalledProcessError with .reason and .throttled on failure, and
        RuntimeError if yt-dlp isn't installed.
        """
        if not output_template:
            raise ValueError("run_download requires an output template")

        output_directory = os.path.dirname(output_template)
        if output_directory:
            os.makedirs(output_directory, exist_ok=True)

        command = [
            self._ytdlp(),
            "-x",
            "-f", "bestaudio/best",
            "--audio-format", self.__audio_format,
        ]
        if self.__audio_quality not in ("auto", "disable"):
            command += ["--audio-quality", self.__audio_quality]

        command += [
            "-o", output_template,
            "--no-overwrites",
            "--add-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
            "--ppa", "ThumbnailsConvertor+ffmpeg_o:-c:v mjpeg -vf crop=ih:ih",
            "--newline",
            "--progress",
            "--console-title",
            "--retries", "10",
            "--fragment-retries", "10",
            "--extractor-retries", "15",
            "--buffer-size", "16K",
            "--http-chunk-size", "10M",
            "--sleep-interval", str(self.yt_dlp_sleep_min),
            "--max-sleep-interval", str(self.yt_dlp_sleep_max),
            # --quiet but not --no-warnings: the warnings are how throttling and
            # "already downloaded" are spotted below.
            "--quiet",
        ]
        command += self._common_args()
        if additional_args:
            command.extend(additional_args if isinstance(additional_args, list) else [additional_args])
        command.append(url)

        progress_bar = tqdm(
            desc="Downloading",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            leave=False,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]",
            dynamic_ncols=True,
        ) if show_progress else _NullBar()

        process = None
        watchdog = None
        throttled = False
        timed_out = threading.Event()
        self._last_run_throttled = False

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding='utf-8',
                errors='replace'
            )

            # download_timeout is "no output for N seconds", so long but
            # healthy downloads aren't killed mid-transfer.
            def _kill(proc):
                timed_out.set()
                try:
                    proc.kill()
                except Exception:
                    pass

            def _arm():
                t = threading.Timer(self.download_timeout, _kill, args=(process,))
                t.daemon = True
                t.start()
                return t

            watchdog = _arm()
            output_lines: List[str] = []
            try:
                for line in iter(process.stdout.readline, ''):
                    watchdog.cancel()
                    watchdog = _arm()

                    line = line.strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    if len(output_lines) > 1000:
                        output_lines = output_lines[-200:]

                    # Detection only: flag it so the caller can queue the link
                    # for a later retry and batch loops can back off.
                    if not throttled and rate_limiter.looks_throttled(line):
                        throttled = True
                        self.log_manager.log_warning("YouTube throttling detected", url=url,
                                                     console=False)

                    if "[download]" in line:
                        try:
                            percent_match = re.search(r'(\d+\.?\d*)%', line)
                            if percent_match:
                                percent = float(percent_match.group(1))
                                progress_bar.set_description(
                                    f"{Fore.CYAN}Downloading: {percent:.1f}%{Style.RESET_ALL}")

                            size_match = re.search(r'of\s+~?\s*([\d.]+\s*[KMGT]?i?B)', line)
                            if size_match and progress_bar.total is None:
                                total_bytes = parse_size(size_match.group(1))
                                if total_bytes:
                                    progress_bar.total = total_bytes

                            if progress_bar.total and percent_match:
                                progress_bar.n = int(progress_bar.total * float(percent_match.group(1)) / 100)

                            speed_match = re.search(r'at\s+([\d.]+\s*[KMGT]?i?B/s)', line)
                            eta_match = re.search(r'ETA\s+([\d:]+)', line)
                            postfix = ", ".join(x for x in (
                                f"Speed: {speed_match.group(1)}" if speed_match else "",
                                f"ETA: {eta_match.group(1)}" if eta_match else "") if x)
                            if postfix:
                                progress_bar.set_postfix_str(postfix)
                            progress_bar.refresh()
                        except Exception:
                            pass

                    if "100%" in line or "already been downloaded" in line or "[ExtractAudio]" in line:
                        if progress_bar.total and progress_bar.n < progress_bar.total:
                            progress_bar.n = progress_bar.total
                        progress_bar.set_description(f"{Fore.GREEN}Downloaded{Style.RESET_ALL}")
                        progress_bar.set_postfix_str("")
                        progress_bar.refresh()
            finally:
                if watchdog is not None:
                    watchdog.cancel()

            process.wait()
            progress_bar.close()
            full_output = "\n".join(output_lines)
            low = full_output.lower()

            if not throttled and rate_limiter.looks_throttled(full_output):
                throttled = True
            self._last_run_throttled = throttled

            # yt-dlp can report an error and still exit 0, so look for ERROR lines too.
            had_error_line = any(line.lstrip().upper().startswith("ERROR:")
                                 for line in output_lines)
            already_have = ("has already been recorded in the archive" in low
                            or "already been downloaded" in low)

            if process.returncode == 0 and not had_error_line and not timed_out.is_set():
                done = subprocess.CompletedProcess(args=command, returncode=0,
                                                   stdout=full_output, stderr="")
                done.throttled = throttled
                done.already_downloaded = already_have
                if already_have:
                    self.log_manager.log_info("Already downloaded (skipped)", url=url,
                                              console=False)
                return done

            reason = classify_failure(full_output, timed_out.is_set(), self.download_timeout)
            self.log_manager.log_error(f"Download failed: {reason}", url=url, console=False)
            failure = subprocess.CalledProcessError(process.returncode or 1, command,
                                                    output=full_output, stderr="")
            failure.throttled = throttled
            failure.reason = reason
            raise failure

        except FileNotFoundError:
            progress_bar.close()
            error_msg = "yt-dlp not found. Please install it with: pip install -U yt-dlp"
            self.log_manager.log_error(error_msg)
            raise RuntimeError(error_msg)
        except subprocess.CalledProcessError:
            progress_bar.close()
            raise
        except BaseException as e:            # includes Ctrl-C: don't leave yt-dlp running
            progress_bar.close()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            if not isinstance(e, KeyboardInterrupt):
                self.log_manager.log_error(f"Unexpected error in run_download: {e}", url=url)
            raise

    def _download_with_retry(self, url: str, output_template: str, additional_args: list = None,
                             item_type: str = "item", show_progress: bool = True,
                             metadata: Optional[dict] = None) -> Tuple[bool, str, bool]:
        """
        Download with up to max_retries attempts. Returns (success, last_error, throttled)
        so callers that batch many links can report why each one failed, and can
        tell a bad link apart from a refusing host. A throttled attempt isn't
        retried straight away: that only deepens the rate limit.
        """
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            if attempt > 1:
                if show_progress:
                    Enhanced_Menu.print_status(
                        f"Attempt {attempt}/{self.max_retries} in {self.retry_delay}s...", "info")
                time.sleep(self.retry_delay)
            try:
                result = self.run_download(url, output_template, additional_args,
                                           show_progress=show_progress)
                if not getattr(result, "already_downloaded", False):
                    self.log_manager.log_success(f"Downloaded {item_type}", url=url,
                                                 metadata=metadata, item_type=item_type,
                                                 console=False)
                return True, "", False
            except subprocess.CalledProcessError as e:
                last_error = getattr(e, "reason", "") or str(e)[:300]
                if show_progress:
                    Enhanced_Menu.print_status(f"Attempt {attempt} failed: {last_error}", "error")
                if getattr(e, "throttled", False):
                    self.log_manager.log_failure(f"Throttled while downloading {item_type}",
                                                 url=url, metadata=metadata, item_type=item_type,
                                                 error=last_error, console=False)
                    return False, last_error, True
            except RuntimeError:
                raise                         # yt-dlp is missing - retrying won't help
            except KeyboardInterrupt:
                raise
            except Exception as e:
                last_error = str(e)[:300]
                self.log_manager.log_error(f"Unexpected error in attempt {attempt}: {e}", url=url)
        self.log_manager.log_failure(f"Failed after {self.max_retries} attempts", url=url,
                                     metadata=metadata, item_type=item_type, error=last_error,
                                     console=False)
        return False, last_error, False

    # Older name for the same thing.
    _retry_logic = _download_with_retry

    def _backoff_seconds(self, streak: int) -> float:
        """How long to wait after `streak` throttled links in a row (doubling, capped)."""
        return min(self.rate_limit_max_wait, self.rate_limit_backoff * 2 ** max(0, streak - 1))

    @staticmethod
    def _penalize(seconds: float) -> None:
        """Tell the rate limiter to pause everyone. Never fails the download over it."""
        try:
            rate_limiter.penalize(seconds)
        except Exception:
            pass

    # ==================== Archives (already-downloaded tracks) ====================
    def _archive_path(self, url: str) -> Path:
        playlist_id = extract_playlist_id(url) or hashlib.sha1(url.encode()).hexdigest()[:16]
        return self.archives_dir / f"{safe_name(playlist_id)}.txt"

    @staticmethod
    def _read_archive(path: Optional[Path]) -> Set[str]:
        """Video IDs in a yt-dlp style archive ("youtube <id>" per line)."""
        if not path or not path.is_file():
            return set()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return set()
        return {line.split()[-1] for line in lines if line.strip()}

    @staticmethod
    def _add_to_archive(path: Optional[Path], video_id: str) -> None:
        if not path or not video_id:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"youtube {video_id}\n")
        except OSError:
            pass

    # ==================== Playlists (several at once) ====================
    def parallel_run_download(self, tasks, archive_path: Optional[Path], max_workers: int = 3,
                              desc: str = "Downloading", source: str = "") -> Dict[str, bool]:
        """
        Download several tracks at once.

        Args:
            tasks: [{"url", "id", "title", "template"}, ...]
            archive_path: tracks whose ID is in this file are skipped; finished
                          tracks are added to it, so a re-run carries on
            max_workers: downloads at the same time
            desc: label for the progress bar
            source: where the links came from (stored with failures)

        Returns:
            {url: True/False} for every task that was attempted.
        """
        done_ids = self._read_archive(archive_path)
        todo = [t for t in tasks if t.get("id") not in done_ids]
        results: Dict[str, bool] = {}
        if not todo:
            return results

        stop = threading.Event()
        streak = 0

        def work(task):
            if stop.is_set():
                return task, None, "", False
            rate_limiter.acquire()            # shared pacing across all workers
            if stop.is_set():
                return task, None, "", False
            ok, error, throttled = self._download_with_retry(
                task["url"], task["template"], ["--no-playlist"], item_type="track",
                show_progress=False, metadata={"title": task.get("title")})
            return task, ok, error, throttled

        bar = tqdm(total=len(todo), desc=desc, unit="track", dynamic_ncols=True)
        pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
        futures = [pool.submit(work, task) for task in todo]
        try:
            for future in as_completed(futures):
                task, ok, error, throttled = future.result()
                if ok is None:
                    continue                  # skipped after a stop
                results[task["url"]] = ok
                bar.update(1)
                name = (task.get("title") or task["url"])[:60]
                if ok:
                    streak = 0
                    self._add_to_archive(archive_path, task.get("id"))
                    tqdm.write(f"  {Fore.GREEN}done{Style.RESET_ALL}  {name}")
                    continue
                self.log_manager.record_failure(task["url"], task.get("title", ""), error, source,
                                                throttled=throttled, item_type="track")
                tqdm.write(f"  {Fore.RED}failed{Style.RESET_ALL} {name}  ({error[:80]})")
                if throttled:
                    streak += 1
                    self._penalize(self._backoff_seconds(streak))
                    if streak >= STOP_AFTER_THROTTLED and not stop.is_set():
                        stop.set()
                        tqdm.write(f"  {Fore.YELLOW}Throttled {streak} times in a row - "
                                   f"stopping. The rest can be downloaded later.{Style.RESET_ALL}")
                else:
                    streak = 0
        except KeyboardInterrupt:
            stop.set()
            for future in futures:
                future.cancel()
            tqdm.write(f"  {Fore.YELLOW}Stopping after the downloads in progress...{Style.RESET_ALL}")
            raise
        finally:
            pool.shutdown(wait=True)
            bar.close()
        return results

    def _run_playlist(self, url: str, metadata: dict, max_workers: int = 3) -> bool:
        """Download a playlist's tracks in parallel into <output>/<playlist name>/."""
        name = safe_name(metadata.get("title") or "Playlist", "Playlist")
        folder = self.__output_directory / name
        host = "music.youtube.com" if "music.youtube.com" in url else "www.youtube.com"
        tasks, unavailable = [], 0
        for entry in metadata.get("entries") or []:
            if not entry or not entry.get("id"):
                continue
            if (entry.get("title") or "").lower() in UNAVAILABLE_TITLES:
                unavailable += 1
                continue
            tasks.append({"id": entry["id"],
                          "url": f"https://{host}/watch?v={entry['id']}",
                          "title": entry.get("title") or entry["id"],
                          "template": str(folder / "%(artist)s - %(title)s.%(ext)s")})
        archive = self._archive_path(url)
        already = len(self._read_archive(archive) & {t["id"] for t in tasks})
        if unavailable:
            Enhanced_Menu.print_status(f"Skipping {unavailable} private/deleted video(s).", "info")
        if already:
            Enhanced_Menu.print_status(f"{already} track(s) already downloaded earlier.", "info")
        if len(tasks) == already:
            Enhanced_Menu.print_status("Nothing new to download in this playlist.", "success")
            return True

        Enhanced_Menu.print_status(f"Downloading {len(tasks) - already} track(s) into {folder}, "
                                   f"{max_workers} at a time...", "info")
        results: Dict[str, bool] = {}
        try:
            results = self.parallel_run_download(tasks, archive, max_workers,
                                                 desc=name[:30], source="")
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Interrupted. Run it again later and finished tracks "
                                       "will be skipped.", "warning")
        succeeded = sum(results.values())
        failed = len(results) - succeeded
        print()
        Enhanced_Menu.print_key_value("Downloaded", succeeded, width=12)
        if failed:
            Enhanced_Menu.print_key_value("Failed", failed, width=12,
                                          note="added to the retry queue")
        not_tried = len(tasks) - already - len(results)
        if not_tried:
            Enhanced_Menu.print_key_value("Not tried", not_tried, width=12,
                                          note="run the playlist again later")
        if succeeded:
            cleanup_directory(self.__output_directory, self.log_manager)
        return failed == 0 and not not_tried

    # ==================== Tracks, albums, playlists ====================
    def download_item(self, item_type: str, url_prompt: str, output_template: str = None, additional_args: list = None, confirm_large: bool = False, use_archive: bool = False, concurrent: bool = False, max_workers: int = 3) -> bool:
        """
        Unified download for tracks, albums and playlists.

        Args:
            item_type (str): "track", "album" or "playlist"
            url_prompt (str): what to ask for ("track URL", ...)
            output_template (str, optional): yt-dlp output template
            additional_args (list, optional): extra yt-dlp arguments
            confirm_large (bool, optional): ask before collections of more than 50 items
            use_archive (bool, optional): keep an archive so re-runs skip finished tracks
            concurrent (bool, optional): download the tracks in parallel (playlists)
            max_workers (int, optional): downloads at once when concurrent

        Returns:
            bool: True if the last download succeeded
        """
        while True:
            Enhanced_Menu.clear_screen()
            Enhanced_Menu.print_header(f"Download {item_type.title()}")

            url = Enhanced_Menu.get_input(
                f"Enter YouTube Music {url_prompt} (or 'back' to return)", "str")
            url = (url or "").strip().strip('"').strip("'")
            if not url:
                Enhanced_Menu.print_status("No URL provided", "error")
                Enhanced_Menu.pause()
                continue
            if url.lower() == 'back':
                return False

            if not is_youtube_url(url):
                Enhanced_Menu.print_status(
                    "Invalid YouTube URL. Enter a valid YouTube/YouTube Music URL", "error")
                Enhanced_Menu.pause()
                continue

            Enhanced_Menu.print_status("Checking the link...", "info")
            is_valid, message, metadata = self.fetch_info(url, item_type)
            if not is_valid or not metadata:
                Enhanced_Menu.print_status(f"Validation failed: {message}", "error")
                if not Enhanced_Menu.confirm("Try another link?", default=True):
                    return False
                continue

            self.history.add_input(url, item_type, metadata)

            # Display resource information
            Enhanced_Menu.print_status("Resource information:", "success")
            count = metadata.get("playlist_count") or len(metadata.get("entries") or [])
            if item_type == "track":
                title = metadata.get('title', 'Unknown')
                artist = metadata.get('artist') or metadata.get('uploader', 'Unknown Artist')
                album = metadata.get('album')
                print(f"  {Fore.CYAN}Track:{Style.RESET_ALL} {title}")
                print(f"  {Fore.CYAN}Artist:{Style.RESET_ALL} {artist}")
                if album:
                    print(f"  {Fore.CYAN}Album:{Style.RESET_ALL} {album}")
            elif item_type == "album":
                print(f"  {Fore.CYAN}Album:{Style.RESET_ALL} {metadata.get('title', 'Unknown Album')}")
                artist = metadata.get('artist') or metadata.get('uploader') or metadata.get('channel')
                if artist:
                    print(f"  {Fore.CYAN}Artist:{Style.RESET_ALL} {artist}")
                print(f"  {Fore.CYAN}Tracks:{Style.RESET_ALL} {count or '?'}")
            elif item_type == "playlist":
                print(f"  {Fore.CYAN}Playlist:{Style.RESET_ALL} {metadata.get('title', 'Unknown Playlist')}")
                print(f"  {Fore.CYAN}Videos:{Style.RESET_ALL} {count}")
            print()

            # Confirm large collections
            if confirm_large and (count or 0) > 50:
                Enhanced_Menu.print_status(
                    f"This {item_type} contains {count} items. This may take a while.", "warning")
                if not Enhanced_Menu.get_input("Continue with download? (y/n)", "yn", default=False):
                    Enhanced_Menu.print_status("Download cancelled", "info")
                    continue

            if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
                self.get_user_preferences()

            # ---------------- Concurrent (playlist) path ----------------
            if concurrent:
                success = self._run_playlist(url, metadata, max_workers)

            # ---------------- Single-call (track / album) path ----------------
            else:
                template = output_template or str(self.__output_directory /
                                                  "%(artist)s - %(title)s.%(ext)s")
                item_args = list(additional_args) if additional_args else []
                if item_type == "track":
                    item_args.append("--no-playlist")
                else:
                    # One unavailable track shouldn't abandon the rest of the collection.
                    item_args.append("--ignore-errors")
                if use_archive:
                    archive_path = self._archive_path(url)
                    item_args.extend(["--download-archive", str(archive_path)])
                    self.log_manager.log_info(f"Using archive: {archive_path}", url=url,
                                              console=False)

                Enhanced_Menu.print_status(f"Starting {item_type} download...", "info")
                success, error, throttled = self._download_with_retry(
                    url, template, item_args, item_type, metadata=metadata)
                if success:
                    Enhanced_Menu.print_status(f"Downloaded to {self.__output_directory}", "success")
                    if item_type in ("album", "playlist"):
                        cleanup_directory(self.__output_directory, self.log_manager)
                else:
                    self.log_manager.record_failure(url, metadata.get('title', ''), error, "",
                                                    throttled=throttled, item_type=item_type,
                                                    metadata=metadata)
                    Enhanced_Menu.print_status(
                        "Added to the retry queue: pick it up later from the Downloads menu"
                        + (" (YouTube is throttling - wait a while first)." if throttled else "."),
                        "warning")

            # ---------------- Post-download prompt (shared) ----------------
            if success:
                if Enhanced_Menu.get_input(f"\nDownload another {item_type}? (y/n): ", "yn", default=True):
                    continue
                return True
            if Enhanced_Menu.get_input(f"\nDownload failed. Try another {item_type}? (y/n): ", "yn", default=True):
                continue
            return False

    # Older name for the same thing.
    _download_item = download_item

    def download_track(self):
        """Download a single track."""
        return self.download_item(
            item_type="track",
            url_prompt="track URL",
            output_template=str(self.__output_directory / "%(artist)s - %(title)s.%(ext)s"),
            confirm_large=False,
        )

    def download_album(self):
        """Download an album."""
        return self.download_item(
            item_type="album",
            url_prompt="album URL",
            output_template=str(self.__output_directory /
                                "%(artist)s/%(album)s/%(artist)s - %(title)s.%(ext)s"),
            confirm_large=True,
            use_archive=True,
        )

    def download_playlist(self):
        """Download a playlist with concurrent downloads."""
        return self.download_item(
            item_type="playlist",
            url_prompt="playlist URL",
            output_template=None,        # computed per-playlist inside
            confirm_large=True,
            concurrent=True,
            max_workers=self.max_concurrent or 3,
        )

    # ==================== Batch files ====================
    def download_from_file(self, file_path: str = None) -> bool:
        """
        Download every link in a .txt, .csv or .json file, strictly one at a time.

        Sequential by design: a batch file is usually long, and firing several
        requests at once at the same host is what gets you throttled. Every
        success is written back into the source file, so a re-run picks up where
        the last one stopped; every failure lands in the retry queue with its
        error and attempt count.
        """
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Batch Download", "Download every link in a .txt, .csv or .json file")

        if file_path is None:
            raw = Enhanced_Menu.get_input(
                "Path to the .txt, .csv or .json file (or 'back' to return)", "str")
            raw = (raw or "").strip().strip('"').strip("'")
            if not raw or raw.lower() == "back":
                return False
            file_path = raw

        path = Path(file_path).expanduser()
        if not path.is_file():
            Enhanced_Menu.print_status(f"No such file: {path}", "error")
            return False

        entries = self.batch_file.parse(path)
        if not entries:
            Enhanced_Menu.print_status(f"No links found in {path.name}", "error")
            return False

        # De-duplicate (keeping order), drop non-YouTube links, and skip
        # anything the file already records as downloaded.
        seen = set()
        valid: List[Dict[str, str]] = []
        invalid = already_done = 0
        for entry in entries:
            url = entry["url"]
            if url in seen:
                continue
            seen.add(url)
            if not is_youtube_url(url):
                invalid += 1
                continue
            if entry.get("status") == "success":
                already_done += 1
                continue
            valid.append(entry)
        duplicates = len(entries) - len(seen)

        Enhanced_Menu.print_status(f"Read {path.name}:", "success")
        print(f"  {Fore.CYAN}Links found:{Style.RESET_ALL} {len(entries)}")
        if duplicates:
            print(f"  {Fore.YELLOW}Duplicates skipped:{Style.RESET_ALL} {duplicates}")
        if invalid:
            print(f"  {Fore.YELLOW}Not YouTube links, skipped:{Style.RESET_ALL} {invalid}")
        if already_done:
            print(f"  {Fore.CYAN}Already marked success:{Style.RESET_ALL} {already_done}")
        print(f"  {Fore.GREEN}To download:{Style.RESET_ALL} {len(valid)}")
        print()

        if not valid:
            Enhanced_Menu.print_status("Nothing left to download.", "info")
            return True

        if not Enhanced_Menu.get_input(f"Download these {len(valid)} links? (y/n)",
                                       "yn", default=True):
            Enhanced_Menu.print_status("Cancelled", "info")
            return False

        self.history.add_input(str(path), "batch")

        # Output folder: named after the file by default, so a batch stays together.
        folder_name = safe_name(path.stem, "Batch")
        if Enhanced_Menu.get_input(f"Save into a subfolder named '{folder_name}'? (y/n)",
                                   "yn", default=True):
            target = self.__output_directory / folder_name
        else:
            target = self.__output_directory
        target.mkdir(parents=True, exist_ok=True)
        output_template = str(target / "%(artist)s - %(title)s.%(ext)s")

        if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
            self.get_user_preferences()

        total = len(valid)
        succeeded = failed = 0
        pending: Dict[str, str] = {}          # url -> status, not yet flushed to file
        cleared: List[str] = []               # urls to remove from the retry queue
        failures: List[Tuple[str, str, str]] = []
        interrupted = throttled_out = False
        rate_limit_streak = 0
        started = time.monotonic()

        Enhanced_Menu.print_status(f"Starting batch download of {total} links...", "info")
        print()

        try:
            for index, entry in enumerate(valid, 1):
                url, title = entry["url"], entry.get("title", "")
                label = title or url
                print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(label)[:65]}")

                ok, error, throttled = self._download_with_retry(
                    url, output_template, ["--no-playlist"], item_type="track",
                    show_progress=False, metadata={"title": title} if title else None)

                if ok:
                    succeeded += 1
                    pending[url] = "success"
                    cleared.append(url)
                    rate_limit_streak = 0
                    print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
                else:
                    failed += 1
                    pending[url] = "failed"
                    failures.append((url, title, error))
                    self.log_manager.record_failure(url, title, error, str(path),
                                                    throttled=throttled, item_type="track")
                    if throttled:
                        rate_limit_streak += 1
                        print(f"      {Fore.RED}throttled -> retry queue{Style.RESET_ALL}")
                    else:
                        rate_limit_streak = 0
                        print(f"      {Fore.RED}failed: {error[:70]} -> retry queue{Style.RESET_ALL}")

                # Written per link rather than in batches: the file should say
                # what the screen just said, and an interrupted run shouldn't
                # lose the last few results.
                if self.batch_file.mark_statuses(path, pending):
                    pending = {}

                # Once YouTube starts refusing, every remaining link fails the
                # same way. Stop instead: the markers already written make the
                # re-run pick up here.
                if rate_limit_streak >= STOP_AFTER_THROTTLED:
                    throttled_out = True
                    Enhanced_Menu.print_status(
                        "Three throttled links in a row - stopping here. Wait a while, "
                        "then re-run this file; finished links will be skipped.", "warning")
                    break

                if index < total:
                    if rate_limit_streak:
                        wait = self._backoff_seconds(rate_limit_streak)
                        Enhanced_Menu.print_status(
                            f"Throttled - waiting {wait:.0f}s before the next link", "warning")
                        time.sleep(wait)      # Ctrl-C is caught by the handler below
                    else:
                        # yt-dlp's own --sleep-interval only applies within a
                        # single invocation, not between them.
                        time.sleep(random.uniform(self.yt_dlp_sleep_min, self.yt_dlp_sleep_max))

        except KeyboardInterrupt:
            interrupted = True
            print()
            Enhanced_Menu.print_status(
                "Interrupted. Re-run this file later and finished links will be skipped.",
                "warning")

        # Final flush of statuses, and drop anything that succeeded from the queue.
        self.batch_file.mark_statuses(path, pending)
        self.log_manager.clear_failures(cleared)

        elapsed = time.monotonic() - started
        stopped = interrupted or throttled_out
        print()
        Enhanced_Menu.print_header("Batch Download Complete" if not stopped
                                   else "Batch Download Stopped")
        print(f"  {Fore.GREEN}Succeeded:{Style.RESET_ALL} {succeeded}")
        if failed:
            print(f"  {Fore.RED}Failed:{Style.RESET_ALL} {failed}")
            for url, title, _ in failures[:10]:
                print(f"      {Fore.RED}- {str(title or url)[:60]}{Style.RESET_ALL}")
            if len(failures) > 10:
                print(f"      {Fore.RED}...and {len(failures) - 10} more{Style.RESET_ALL}")
            print(f"  {Fore.YELLOW}Queued for retry in:{Style.RESET_ALL} {self.log_manager.FAILED_FILE}")
        if stopped:
            print(f"  {Fore.YELLOW}Not attempted:{Style.RESET_ALL} {total - succeeded - failed}")
        print(f"  {Fore.CYAN}Statuses written to:{Style.RESET_ALL} {path}")
        print(f"  {Fore.CYAN}Elapsed:{Style.RESET_ALL} {elapsed / 60:.1f} min")

        if succeeded:
            cleanup_directory(self.__output_directory, self.log_manager)

        return failed == 0 and not stopped

    # ==================== Retry queue ====================
    def download_from_retry_queue(self) -> bool:
        """Re-attempt every link in the retry queue (see managers.retry_manager)."""
        from managers import retry_manager
        if not retry_manager.is_configured():
            retry_manager.configure(self, self.log_manager, self.batch_file, rate_limiter)
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Retry Queue", "Re-attempt previously failed links")
        summary = retry_manager.run(
            confirm=lambda question: Enhanced_Menu.get_input(question, "yn", default=True))
        if summary.recovered:
            cleanup_directory(self.__output_directory, self.log_manager)
        return summary.ok