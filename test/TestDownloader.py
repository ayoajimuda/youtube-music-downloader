import re
import os
import random
import subprocess
import time
import hashlib
import threading

from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from colorama import init, Fore, Style

from tools.CookieManager import CookieManager
from tools.ConfigManager import DownloaderConfigManager
from tools.BatchFile import BatchFile
from tools.RetryQueue import RetryQueue
from tools.RateLimiter import youtube_limiter, looks_throttled
from tools.EnhancedMenu import Enhanced_Menu
from utils.utilities import DownloaderUtils
from utils.logger import Logs_Manager
from utils.validators import Helpers
from utils.history_logger import DownloadHistory
from utils.helpers import DownloadHelpers

init(autoreset=True)
os.makedirs("cookies", exist_ok=True)

# Formats yt-dlp can embed cover art into. wav is the odd one out.
VALID_FORMATS = ["mp3", "flac", "ogg", "opus", "m4a", "wav"]
THUMBNAIL_FORMATS = {"mp3", "flac", "ogg", "opus", "m4a"}
VALID_QUALITIES = ["auto", "disable", "8k", "16k", "24k", "32k", "40k", "48k", "64k",
                   "80k", "96k", "112k", "128k", "160k", "192k", "224k", "256k", "320k"]

# Printed by yt-dlp (via --print after_move:) once per fully finished file.
DONE_MARKER = "[ymd-done]"

# Output templates. Plain YouTube videos and search results often have no
# "artist" field, which used to produce "NA - Title.mp3"; fall back to the uploader.
ARTIST_TMPL = "%(artist,uploader|Unknown Artist)s"
TRACK_TMPL = f"{ARTIST_TMPL} - %(title)s.%(ext)s"
ALBUM_TMPL = f"{ARTIST_TMPL}/%(album|Unknown Album)s/{TRACK_TMPL}"
PLAYLIST_TMPL = f"%(playlist|Unknown Playlist)s/{TRACK_TMPL}"


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

class YoutubeMusicDownloader:
    """Downloader class that handles the downloading process."""

    def __init__(self):
        self.__output_directory = Path.home() / "Music" / "Collection" / "YouTube"
        self.__audio_quality = "320k"
        self.__audio_format = "mp3"
        self.__configuration_file = r"config/YoutubeMusicDownloader.json"

        # Order matters: the config manager takes log_manager.log_error as its
        # error sink, so the logger has to exist first.
        self.log_manager = Logs_Manager()
        # Spotify credential options belong to the spotdl downloader, not here.
        self.cookie_manager = CookieManager(show_spotify_options=False)
        self.utils = DownloaderUtils()
        self.history = DownloadHistory()
        self._config = DownloaderConfigManager.for_youtube(
            self.__configuration_file, on_error=self.log_manager.log_error)

        self.use_cookies = False
        self.debug = False
        self._cookie_notices = set()       # cookie warnings already logged this session

        self.max_retries = 3
        self.retry_delay = 10
        self.download_timeout = 120        # seconds of *silence* before a download is killed
        # Silence allowed once a file reaches 100%: ffmpeg conversion prints
        # nothing, and a long mix can take several minutes to encode.
        self.postprocess_timeout = 900

        self.max_concurrent = 2
        self.yt_dlp_sleep_min = 3          # min seconds yt-dlp waits between downloads
        self.yt_dlp_sleep_max = 7          # max seconds (random delay in this range)

        self.rate_limit_backoff = 300      # base wait (s) after YouTube throttles us
        self.rate_limit_max_wait = 1800    # ceiling for that wait

        # Set by run_download so a batch loop can tell "this link is bad" apart
        # from "YouTube is refusing everything right now".
        self._last_run_throttled = False

        self.archives_dir = Path("history/archives")
        self.archives_dir.mkdir(parents=True, exist_ok=True)

        # Collaborators. Each takes the logger's error sink rather than the
        # logger itself, so none of them depends on the downloader.
        self.file_helpers = DownloadHelpers(on_error=self.log_manager.log_error)
        self.batch_file = BatchFile(on_error=self.log_manager.log_error,
                                    backup_dir="history/backups")
        self.retry_queue = RetryQueue("history/retry_queue_youtube.json",
                                      on_error=self.log_manager.log_error)

        try:
            self.load_config()
        except Exception as e:
            self.log_manager.log_error(f"Error loading config: {e}")

        # Created after load_config, since that's what decides the final path.
        self.__output_directory.mkdir(parents=True, exist_ok=True)

    # ==================== Public properties ====================
    @property
    def audio_format(self) -> str:
        """Current audio format (mp3, flac, etc.)."""
        return self.__audio_format

    @audio_format.setter
    def audio_format(self, value: str):
        if value in VALID_FORMATS:
            self.__audio_format = value
        else:
            raise ValueError(f"Unsupported audio format: {value}")

    @property
    def audio_quality(self) -> str:
        """Current audio bitrate (320k, 192k, auto, etc.)."""
        return self.__audio_quality

    @audio_quality.setter
    def audio_quality(self, value: str):
        if value in VALID_QUALITIES:
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

    @property
    def retry_queue_count(self) -> int:
        """How many failed links are currently waiting in the retry queue."""
        return self.retry_queue.count

    # ==================== Configuration ====================
    def load_config(self):
        c = self._config.load()
        self.__output_directory = Path(c["output_directory"])
        self.__audio_quality = c["audio_quality"]
        self.__audio_format = c["audio_format"]
        self.use_cookies = c["use_cookies"]
        self.max_retries = c["max_retries"]
        self.retry_delay = c["retry_delay"]
        self.download_timeout = c["download_timeout"]
        self.max_concurrent = c["max_concurrent"]
        self.yt_dlp_sleep_min = c["yt_dlp_sleep_min"]
        self.yt_dlp_sleep_max = c["yt_dlp_sleep_max"]

    def save_config(self):
        self._config.save({
            "output_directory": self.__output_directory,
            "audio_quality": self.__audio_quality,
            "audio_format": self.__audio_format,
            "use_cookies": self.use_cookies,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
            "download_timeout": self.download_timeout,
            "max_concurrent": self.max_concurrent,
            "yt_dlp_sleep_min": self.yt_dlp_sleep_min,
            "yt_dlp_sleep_max": self.yt_dlp_sleep_max,
        })

    def get_user_preferences(self):
        """Take user input for the download settings."""
        Enhanced_Menu.print_header("Download Settings", "Configure your music conversion preferences")

        # Audio quality
        while True:
            audio_quality_input = Enhanced_Menu.get_input(
                "What bitrate would you like (enter 'choice' to see options): ",
                "str", default=self.__audio_quality)
            # Normalise first, so "320K" or "Choice" are accepted.
            audio_quality_input = (audio_quality_input or "").strip().lower()
            if not audio_quality_input:
                self.__audio_quality = "320k"
                break
            if audio_quality_input == 'choice':
                print(f"\n{Fore.CYAN}Available qualities:{Style.RESET_ALL}")
                print("  auto     - Let yt-dlp choose the best")
                print("  320k     - High quality (default)")
                print("  256k     - Very good quality")
                print("  192k     - Good quality")
                print("  128k     - Standard quality")
                print("  8k-160k  - Lower qualities")
                continue
            if audio_quality_input in VALID_QUALITIES:
                self.__audio_quality = audio_quality_input
                break
            Enhanced_Menu.print_status("Invalid bitrate. The downloader doesn't support these values", "error")

        # Audio format
        while True:
            audio_format_input = Enhanced_Menu.get_input(
                "What format would you like (enter 'choice' to see options): ",
                "str", default=self.__audio_format)
            audio_format_input = (audio_format_input or "").strip().lower()
            if not audio_format_input:
                self.__audio_format = "mp3"
                break
            if audio_format_input == 'choice':
                print(f"\n{Fore.CYAN}Available formats:{Style.RESET_ALL}")
                print("  mp3  - Most compatible (default)")
                print("  m4a  - Apple format, good quality")
                print("  flac - Lossless audio")
                print("  opus - Excellent compression")
                print("  ogg  - Open format")
                print("  wav  - Uncompressed (no embedded cover art)")
                continue
            if audio_format_input in VALID_FORMATS:
                self.__audio_format = audio_format_input
                break
            Enhanced_Menu.print_status("Invalid format. Downloader doesn't support this format", "error")

        # Output directory
        # Pressing Enter keeps the current directory rather than resetting it.
        output_path = Enhanced_Menu.get_input(
            f"Enter output directory (current: {self.__output_directory}): ", "str")
        output_path = (output_path or "").strip().strip('"').strip("'")
        if output_path:
            self.__output_directory = Path(output_path).expanduser()
        self.__output_directory.mkdir(parents=True, exist_ok=True)

        # Cookie choice
        Enhanced_Menu.print_status("Cookie Settings", "info")
        print(f"\n{Fore.CYAN}Cookies can help with:{Style.RESET_ALL}")
        print(" Age-restricted content")
        print(" Members-only content and your private playlists")
        print(" 'Sign in to confirm you're not a bot' errors")
        print(f"{Style.DIM} (Cookies can't get around region locks.){Style.RESET_ALL}")
        cookie_choice = Enhanced_Menu.get_input("Use cookies for authentication? (y/n): ", "yn",
                                                default=self.use_cookies)
        self.use_cookies = bool(cookie_choice)
        if self.use_cookies:
            self._cookie_notices.clear()
            path, report = self.cookie_manager.prepare_for_ytdlp()
            if path is None:
                detail = f" ({report.summary()})" if report else ""
                Enhanced_Menu.print_status(
                    f"No usable cookie file yet{detail} - add one in the Cookie Manager. "
                    "Until then downloads run without cookies.", "warning")
            elif not report.signed_in:
                Enhanced_Menu.print_status(
                    f"{path.name} has no signed-in YouTube session ({report.summary()}).", "warning")
            else:
                Enhanced_Menu.print_status(f"Using {path.name}: {report.summary()}", "success")

        self.save_config()
        Enhanced_Menu.print_status("Settings saved", "success")

    def _cookie_notice(self, message: str) -> None:
        """Log a cookie problem once per session, not once per track."""
        if message not in self._cookie_notices:
            self._cookie_notices.add(message)
            self.log_manager.log_error(message)

    def _get_cookie_file(self) -> Optional[str]:
        """
        The cookie file for yt-dlp when cookies are enabled, else None.

        Goes through the Cookie Manager, so every run uses the same remembered
        file. A file with a header yt-dlp rejects is repaired first, because
        otherwise every download would fail on it.
        """
        if not self.use_cookies:
            return None
        path, report = self.cookie_manager.prepare_for_ytdlp()
        if path is None:
            detail = f" ({report.summary()})" if report else ""
            self._cookie_notice(
                f"Cookies are enabled but there is no usable cookie file{detail}. "
                "Downloading without cookies - add one in the Cookie Manager.")
            return None
        if not report.signed_in:
            self._cookie_notice(
                f"Cookie file {path.name} has no signed-in YouTube session ({report.summary()}). "
                "Age-restricted and members-only tracks will still fail.")
        return str(path)

    # ==================== Core download ====================
    @staticmethod
    def _classify_failure(low: str) -> Tuple[str, bool]:
        """
        Map lower-cased yt-dlp error text to (reason, permanent).

        "permanent" means retrying the same link straight away cannot help, so
        the retry loop gives up at once and a collection can skip the item.
        Most specific checks come first. HTTP codes are matched as
        "http error 403" rather than a bare "403", because bare digits also
        show up in file sizes and video IDs.
        """
        checks = [
            (("does not look like a netscape", "must be netscape formatted"),
             "Cookie file is not in Netscape format (reload it in the Cookie Manager)", True),
            (("http error 403", "forbidden"),
             "HTTP 403 (YouTube refused the stream: update yt-dlp, then check cookies / JS runtime)",
             False),
            (("http error 429", "too many requests"),
             "HTTP 429 (rate limited, slow down or wait)", False),
            (("try again later", "rate-limited", "rate limited"),
             "YouTube is rate limiting this session (wait, then retry)", False),
            (("not a bot",),
             "YouTube wants bot verification (check cookies, or wait)", False),
            (("javascript runtime", "no supported js"),
             "Missing JS runtime (install Deno or Node)", False),
            (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
             "Age restricted (needs cookies from a signed-in account)", True),
            (("private video",), "Video is private", True),
            (("members-only", "members only", "join this channel"), "Members-only content", True),
            (("copyright",), "Copyright restriction", True),
            (("has been removed", "account associated with this video has been terminated"),
             "Video was removed", True),
            (("video unavailable", "not available in your", "blocked it in your country",
              "no longer available"),
             "Video unavailable / region-locked", True),
            (("only images are available",), "No audio stream (SABR/format restriction)", False),
            (("requested format is not available",), "Requested format not available", False),
            (("sign in",), "YouTube requires authentication (check cookies)", False),
            (("ffmpeg", "postprocessing"), "FFmpeg conversion error", False),
        ]
        for markers, reason, permanent in checks:
            if any(marker in low for marker in markers):
                return reason, permanent
        return "", False

    def run_download(self, url: str, output_template: str,
                     additional_args=None, show_progress: bool = True,
                     collection: bool = False):
        """
        Run a yt-dlp download with a tqdm progress bar and a stall watchdog.

        collection=True is for albums/playlists fetched in a single yt-dlp call.
        Such a run counts as a success when at least one item downloaded and
        every error was a permanent one (unavailable, private, ...), since
        retrying would only hit the same dead tracks again.
        """
        if not output_template:
            raise ValueError("run_download requires an output template")

        # Pace outbound requests. This has to be inside the method - as a bare
        # statement in the class body it would run once at import and never again.
        youtube_limiter.acquire()

        output_directory = os.path.dirname(output_template)
        if output_directory:
            os.makedirs(output_directory, exist_ok=True)

        command = [
            "yt-dlp",
            "-x",
            "-f", "bestaudio/best",
            "--audio-format", self.__audio_format,
        ]

        # --audio-quality only accepts 0-10 or a bitrate like 320K, not "auto"/"disable"
        if self.__audio_quality not in ("auto", "disable"):
            command += ["--audio-quality", self.__audio_quality]

        command += [
            "-o", output_template,
            "--no-overwrites",
            "--add-metadata",
        ]

        # yt-dlp raises a postprocessing error when asked to embed a thumbnail
        # into a container it can't tag (wav), which would fail every download.
        if self.__audio_format in THUMBNAIL_FORMATS:
            command += [
                "--embed-thumbnail",
                "--convert-thumbnails", "jpg",
                "--ppa", "ThumbnailsConvertor+ffmpeg_o:-c:v mjpeg -vf crop=ih:ih",
            ]

        command += [
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
            # One marker line per file that made it all the way through
            # post-processing, so collections can count real completions.
            "--print", f"after_move:{DONE_MARKER} %(filepath)s",
        ]

        if self.debug:
            command.append("--no-quiet")    # --print would otherwise imply --quiet
        else:
            command += ["--quiet", "--no-warnings"]

        cookie_file = self._get_cookie_file()
        cookie_copy = None

        if additional_args:
            if isinstance(additional_args, list):
                command.extend(additional_args)
            else:
                command.append(additional_args)
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
        throttled = False
        self._last_run_throttled = False

        # Watchdog state. One thread polls a timestamp, instead of a new Timer
        # thread per output line. The silence limit is download_timeout while
        # data is flowing, and the longer postprocess_timeout once a file hits
        # 100%, because ffmpeg conversion prints nothing under --quiet.
        watch = {"last": time.monotonic(), "limit": self.download_timeout, "killed": False}
        stop_watch = threading.Event()

        try:
            if cookie_file:
                # A private copy per process: yt-dlp rewrites its cookie file
                # on exit, and concurrent workers sharing one file can empty it
                # under each other. The manager folds refreshed cookies back.
                cookie_copy = self.cookie_manager.begin_run(cookie_file)
                command[1:1] = ["--cookies", str(cookie_copy or cookie_file)]

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
            )

            def _watchdog():
                while not stop_watch.wait(1.0):
                    if time.monotonic() - watch["last"] > watch["limit"]:
                        watch["killed"] = True
                        try:
                            process.kill()
                        except Exception:
                            pass
                        return

            threading.Thread(target=_watchdog, daemon=True).start()

            output_lines: List[str] = []
            error_lines: List[str] = []     # kept separately so pruning never drops them
            completed = 0

            for line in iter(process.stdout.readline, ''):
                watch["last"] = time.monotonic()

                line = line.strip()
                if not line:
                    continue

                if line.startswith(DONE_MARKER):
                    completed += 1
                    watch["limit"] = self.download_timeout
                    # Next item in a collection starts a fresh size estimate.
                    progress_bar.total = None
                    progress_bar.n = 0
                    continue

                output_lines.append(line)
                if len(output_lines) > 1000:
                    output_lines = output_lines[-200:]
                if line.lstrip().upper().startswith("ERROR:"):
                    error_lines.append(line)

                # Feed throttling signals back into the limiter so every
                # later request in this session slows down too.
                if not throttled and looks_throttled(line):
                    throttled = True
                    youtube_limiter.penalize(self.retry_delay * 6)
                    self.log_manager.log_error(
                        "YouTube throttling detected - pausing outbound requests")

                if "[download]" in line:
                    try:
                        percent_match = re.search(r'(\d+\.?\d*)%', line)
                        if percent_match:
                            percent = float(percent_match.group(1))
                            if percent < 100:
                                watch["limit"] = self.download_timeout
                            progress_bar.set_description(
                                f"{Fore.CYAN}Downloading: {percent:.1f}%{Style.RESET_ALL}")

                        size_match = re.search(r'of\s+([\d.]+\s*[KMGT]?i?B)', line)
                        if size_match and progress_bar.total is None:
                            total_bytes = Helpers.parse_size(size_match.group(1))
                            if total_bytes:
                                progress_bar.total = total_bytes

                        downloaded_match = (re.search(r'([\d.]+\s*[KMGT]?i?B)\s+at', line) or
                                            re.search(r'([\d.]+\s*[KMGT]?i?B)\s+ETA', line) or
                                            re.search(r'([\d.]+\s*[KMGT]?i?B)\s*/', line))
                        if downloaded_match:
                            downloaded_bytes = Helpers.parse_size(downloaded_match.group(1))
                            if downloaded_bytes:
                                progress_bar.n = downloaded_bytes

                        speed_match = re.search(r'at\s+([\d.]+\s*[KMGT]?i?B/s)', line)
                        if speed_match:
                            progress_bar.set_postfix_str(f"Speed: {speed_match.group(1)}")

                        eta_match = re.search(r'ETA\s+([\d:]+)', line)
                        if eta_match:
                            progress_bar.set_postfix_str(f"ETA: {eta_match.group(1)}")

                        progress_bar.refresh()
                    except Exception:
                        pass

                if "100%" in line or "already been downloaded" in line or "[Merger]" in line:
                    # Conversion is next, and it is silent.
                    watch["limit"] = max(self.download_timeout, self.postprocess_timeout)
                    if progress_bar.total and progress_bar.n < progress_bar.total:
                        progress_bar.n = progress_bar.total
                    progress_bar.set_description(f"{Fore.GREEN}Downloaded{Style.RESET_ALL}")
                    progress_bar.set_postfix_str("")
                    progress_bar.refresh()

            process.wait()
            stop_watch.set()

            full_output = "\n".join(output_lines)
            low = full_output.lower()

            # A 403 on the media stream is YouTube refusing to serve us, which
            # looks identical to a per-video problem in the exit code alone.
            # Checked against the whole output, since a marker that never
            # appeared on a streamed line would otherwise be missed here.
            if not throttled and looks_throttled(full_output):
                throttled = True
                youtube_limiter.penalize(self.retry_delay * 6)
            self._last_run_throttled = throttled

            def _done():
                result = subprocess.CompletedProcess(
                    args=command, returncode=0, stdout=full_output, stderr="")
                result.throttled = throttled
                result.completed = completed
                result.skipped = len(error_lines)
                return result

            # yt-dlp can report an error and still exit 0 (a failing
            # postprocessor can do it), so a zero exit code on its own is not
            # proof anything downloaded.
            if process.returncode == 0 and not error_lines and not watch["killed"]:
                return _done()

            # Archive skip / already-have-it is NOT a failure
            already_have = ("has already been recorded in the archive" in low
                            or "already been downloaded" in low
                            or "nothing to download" in low)
            if already_have and not error_lines and not watch["killed"]:
                self.log_manager.log_success(f"Already downloaded (skipped): {url}")
                return _done()

            # A collection with some dead tracks: --ignore-errors kept going
            # and yt-dlp exits 1 anyway. Accept it if the only errors are ones
            # a retry cannot fix and something actually came down.
            all_permanent = bool(error_lines) and all(
                self._classify_failure(err.lower())[1] for err in error_lines)
            if (collection and all_permanent and completed > 0
                    and not throttled and not watch["killed"]):
                self.log_manager.log_success(
                    f"Finished {url}: {completed} downloaded, "
                    f"{len(error_lines)} unavailable item(s) skipped")
                for err in error_lines:
                    self.log_manager.log_failure(f"Skipped in {url}: {err[:200]}")
                return _done()

            # Genuine failure - classify (specific first, catch-all keeps raw tail)
            error_text = "\n".join(error_lines) if error_lines else full_output
            reason, permanent = self._classify_failure(error_text.lower())
            if collection:
                # Only give up on a collection outright if every error was permanent.
                permanent = all_permanent
            if throttled:
                permanent = False

            error_msg = f"Download failed for {url} with code {process.returncode}"
            if process.returncode == 0 and error_lines:
                error_msg = f"Download failed for {url} - yt-dlp reported an error but exited 0"

            if watch["killed"]:
                permanent = False
                error_msg += f" - Killed after {watch['limit']}s with no output"
            elif collection and all_permanent:
                error_msg += (f" - Nothing new downloaded; {len(error_lines)} item(s) "
                              f"unavailable (e.g. {reason})")
            elif reason:
                error_msg += f" - {reason}"
                if cookie_file and any(k in reason for k in
                                       ("cookies", "bot verification", "authentication", "Age restricted")):
                    error_msg += (" - if these cookies worked before, YouTube has probably rotated them:"
                                  " verify or re-export them in the Cookie Manager")
            elif error_text:
                error_msg += f" - Error: {error_text[-400:]}"
            else:
                error_msg += " - No output from yt-dlp"

            self.log_manager.log_failure(error_msg)
            failure = subprocess.CalledProcessError(
                process.returncode or 1, command, output=full_output, stderr="")
            # Ride along on the exception so a caller in another thread reads
            # its own verdict rather than whatever a sibling thread just set.
            failure.throttled = throttled
            failure.permanent = permanent
            failure.reason = error_msg
            raise failure

        except FileNotFoundError:
            error_msg = "yt-dlp not found. Please install it with: pip install yt-dlp"
            self.log_manager.log_error(error_msg)
            raise RuntimeError(error_msg)
        except subprocess.CalledProcessError:
            # Already classified and logged above - don't let the generic
            # handler below relabel it as an "unexpected error".
            raise
        except Exception as e:
            self.log_manager.log_error(f"Unexpected error in run_download: {e}")
            raise
        finally:
            # Runs for KeyboardInterrupt too, which "except Exception" misses:
            # don't leave the bar drawn or yt-dlp running in the background.
            stop_watch.set()
            progress_bar.close()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
            if cookie_copy:
                self.cookie_manager.end_run(cookie_copy, cookie_file)

    def _download_with_retry(self, url: str, output_template: str, additional_args: list = None,
                             item_type: str = "item", show_progress: bool = True) -> Tuple[bool, str, bool]:
        """
        Unified retry logic. Returns (success, last_error, throttled) so callers
        that batch many links can report why each one failed without re-parsing
        the logs, and can tell a bad link apart from a refusing host.

        Permanent failures (private, removed, region-locked, ...) stop after
        the first attempt instead of burning max_retries and rate-limit budget.
        """
        last_error = ""
        last_throttled = False
        collection = item_type in ("album", "playlist")
        for attempt in range(1, self.max_retries + 1):
            if show_progress:
                Enhanced_Menu.print_section(
                    f"Downloading {item_type} (Attempt {attempt}/{self.max_retries})")
            if attempt > 1:
                if show_progress:
                    print(f"Waiting {self.retry_delay} seconds before retry...")
                time.sleep(self.retry_delay)

            try:
                result = self.run_download(url, output_template, additional_args,
                                           show_progress=show_progress, collection=collection)
                # run_download only ever returns code 0 or raises, so this is the success path.
                if result and result.returncode == 0:
                    skipped = getattr(result, "skipped", 0)
                    note = f" ({skipped} unavailable skipped)" if collection and skipped else ""
                    self.log_manager.log_success(f"Successfully downloaded {item_type}{note}: {url}")
                    if collection:
                        Helpers.cleanup_directory(self.__output_directory, self.log_manager)
                    return True, "", False
            except subprocess.CalledProcessError as e:
                # str(e) is just "Command [...] returned non-zero exit status",
                # which says nothing useful in the retry queue; use the reason.
                last_error = (getattr(e, "reason", "") or str(e))[:300]
                last_throttled = getattr(e, "throttled", False)
                if getattr(e, "permanent", False):
                    self.log_manager.log_failure(f"Not retrying (permanent failure): {url}")
                    break
                if attempt < self.max_retries:
                    self.log_manager.log_error(
                        f"Attempt {attempt} failed for {item_type}: {last_error[:100]}")
                else:
                    self.log_manager.log_failure(f"Failed after {self.max_retries} attempts: {url}")
            except RuntimeError:
                # yt-dlp is missing - retrying will not help.
                raise
            except Exception as e:
                last_error = str(e)[:300]
                self.log_manager.log_error(f"Unexpected error in attempt {attempt}: {e}")
                if attempt == self.max_retries:
                    self.log_manager.log_failure(f"Failed after {self.max_retries} attempts: {url}")
        return False, last_error, last_throttled

    def _download_items_concurrently(self, tasks, archive_path: Optional[Path],
                                     max_workers: int = 3, desc: str = "Downloading",
                                     source: str = "") -> Tuple[Dict[str, bool], int]:
        """
        The archive is pre-filtered up front and appended to under a narrow lock,
        so workers run genuinely in parallel rather than queueing on one mutex.

        tasks: list of (url, output_template, additional_args, video_id, title)
        returns: ({video_id: success_bool}, number of items queued for retry by this run)

        Items that fail because the host was throttling go to the retry queue.
        A throttled item is the one case where the link is probably fine and
        only the timing was wrong, so it is worth keeping hold of.
        """
        results: Dict[str, bool] = {}
        queued = 0
        result_lock = threading.Lock()
        archive_lock = threading.Lock()
        pbar_lock = threading.Lock()

        with tqdm(total=len(tasks), desc=desc, unit="item", dynamic_ncols=True) as pbar:
            def worker(url, tmpl, args, video_id, title):
                nonlocal queued
                success, error, throttled = self._download_with_retry(
                    url, tmpl, args, "item", show_progress=False)
                if success and archive_path is not None:
                    self.file_helpers.append_archive(archive_path, video_id, archive_lock)
                elif throttled:
                    # The link is probably fine and only the timing was wrong,
                    # so keep hold of it rather than letting it vanish.
                    self.retry_queue.add_failure(url, title, error, source,
                                                 throttled=True, item_type="track")
                with result_lock:
                    results[video_id] = success
                    if not success and throttled:
                        queued += 1
                with pbar_lock:
                    pbar.update(1)
                return success

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(worker, u, t, a, vid, ttl): vid
                           for u, t, a, vid, ttl in tasks}
                for future in as_completed(futures):
                    vid = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        self.log_manager.log_error(f"Worker crashed for {vid}: {e}")
                        with result_lock:
                            results.setdefault(vid, False)

        return results, queued

    def _download_item(self, item_type: str, url_prompt: str, relative_template: str = None,
                       additional_args: list = None, confirm_large: bool = False,
                       use_archive: bool = False, concurrent: bool = False,
                       max_workers: int = 3) -> bool:
        """Unified download for tracks, albums, and playlists.

        relative_template is joined onto the output directory only after the
        "Configure download settings?" prompt, so a directory changed there is
        actually used for this download.

        When concurrent=True (playlist mode) the resource is expanded into its
        individual items which are downloaded in parallel. Otherwise a single
        yt-dlp call handles the whole URL (track / album mode).
        """
        while True:
            Enhanced_Menu.clear_screen()
            Enhanced_Menu.print_header(f"Download {item_type.title()}")

            url = Enhanced_Menu.get_input(
                f"Enter YouTube Music {url_prompt} (or 'back' to return)", "str")
            url = (url or "").strip()
            if not url:
                Enhanced_Menu.print_status("No URL provided", "error")
                continue
            if url.lower() == 'back':
                return False

            if not Helpers.validate_youtube_url(url):
                Enhanced_Menu.print_status(
                    "Invalid YouTube URL. Enter a valid YouTube/YouTube Music URL", "error")
                continue

            # Metadata probes shell out to yt-dlp as well, so they belong
            # under the same bucket as the downloads.
            youtube_limiter.acquire()
            is_valid, message, metadata = Helpers.validate_resource_youtube(url)
            if not is_valid or not metadata:
                Enhanced_Menu.print_status(f"Validation failed: {message}", "error")
                continue

            self.history.add_input(url, item_type)

            # Display resource information
            Enhanced_Menu.print_status("Resource information:", "success")
            if item_type == "track":
                title = metadata.get('title', 'Unknown')
                artist = metadata.get('artist') or metadata.get('uploader', 'Unknown Artist')
                album = metadata.get('album', 'Unknown Album')
                print(f"  {Fore.CYAN}Track:{Style.RESET_ALL} {title}")
                print(f"  {Fore.CYAN}Artist:{Style.RESET_ALL} {artist}")
                if album != 'Unknown Album':
                    print(f"  {Fore.CYAN}Album:{Style.RESET_ALL} {album}")
            elif item_type == "album":
                album_title = metadata.get('title', 'Unknown Album')
                album_artist = metadata.get('artist') or metadata.get('uploader', 'Unknown Artist')
                track_count = metadata.get('playlist_count', '?')
                print(f"  {Fore.CYAN}Album:{Style.RESET_ALL} {album_title}")
                print(f"  {Fore.CYAN}Artist:{Style.RESET_ALL} {album_artist}")
                print(f"  {Fore.CYAN}Tracks:{Style.RESET_ALL} {track_count}")
            elif item_type == "playlist":
                playlist_title = metadata.get('title', 'Unknown Playlist')
                playlist_count = metadata.get('playlist_count', 0)
                print(f"  {Fore.CYAN}Playlist:{Style.RESET_ALL} {playlist_title}")
                print(f"  {Fore.CYAN}Videos:{Style.RESET_ALL} {playlist_count}")
            print()

            # Confirm large collections
            if confirm_large and metadata.get('playlist_count', 0) > 50:
                count = metadata['playlist_count']
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
                item_args = list(additional_args) if additional_args else []
                if item_type in ("album", "playlist"):
                    # One unavailable track shouldn't abandon the rest of the
                    # collection. Single links deliberately don't get this.
                    item_args.append("--ignore-errors")
                if use_archive:
                    playlist_id = Helpers.extract_youtube_playlist_id(url)
                    if playlist_id:
                        archive_path = self.archives_dir / f"{playlist_id}.txt"
                        item_args.extend(["--download-archive", str(archive_path)])
                        self.log_manager.log_success(f"Using archive: {archive_path}")
                    else:
                        self.log_manager.log_error(
                            f"Could not extract playlist ID from {url}, archive not used")

                output_template = str(self.__output_directory / relative_template)
                Enhanced_Menu.print_status(f"Starting {item_type} download...", "info")
                success, error, throttled = self._download_with_retry(
                    url, output_template, item_args, item_type)
                if throttled:
                    self.retry_queue.add_failure(url, metadata.get('title', ''), error,
                                                 "", throttled=True, item_type=item_type)
                    Enhanced_Menu.print_status(
                        "Throttled by YouTube - added to the retry queue so you can "
                        "pick it up later from the menu.", "warning")

            # ---------------- Post-download prompt (shared) ----------------
            if success:
                time.sleep(0.5)
                if Enhanced_Menu.get_input(f"\nDownload another {item_type}? (y/n): ", "yn", default=True):
                    continue
                return True
            else:
                if Enhanced_Menu.get_input(f"\nDownload failed. Try another {item_type}? (y/n): ", "yn", default=True):
                    continue
                return False

    def _run_playlist(self, url: str, metadata: Dict, max_workers: int) -> bool:
        """Expand a playlist and download its items in parallel."""
        youtube_limiter.acquire()
        items = Helpers.get_youtube_playlist_items(url, self.log_manager)
        if not items:
            Enhanced_Menu.print_status("Failed to retrieve playlist items.", "error")
            return False

        order = Enhanced_Menu.get_input(
            "Download order: (t)op-to-bottom or (b)ottom-to-top", "str", default="t")
        if (order or "t").lower().startswith('b'):
            items.reverse()

        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        playlist_id = Helpers.extract_youtube_playlist_id(url)
        archive_path = self.archives_dir / (
            f"{playlist_id}.txt" if playlist_id else f"playlist_{url_hash}.txt")

        playlist_folder = self.__output_directory / self.file_helpers.safe_name(
            metadata.get('title'), f"Playlist_{url_hash}")
        playlist_folder.mkdir(parents=True, exist_ok=True)
        collection_template = str(playlist_folder / TRACK_TMPL)

        done_ids = self.file_helpers.load_archive(archive_path)
        tasks = []
        skipped = 0
        for item in items:
            video_id = item.get('id')
            if not video_id:
                continue
            if video_id in done_ids:
                skipped += 1
                continue
            video_url = f"https://music.youtube.com/watch?v={video_id}"
            tasks.append((video_url, collection_template, [], video_id,
                          item.get('title') or ''))

        if skipped:
            Enhanced_Menu.print_status(f"Skipping {skipped} already-downloaded tracks", "info")

        if not tasks:
            Enhanced_Menu.print_status("Nothing new to download.", "warning")
            return True

        Enhanced_Menu.print_status(
            f"Starting concurrent download of {len(tasks)} videos "
            f"(max {max_workers} at a time)...", "info")
        results, queued = self._download_items_concurrently(
            tasks, archive_path, max_workers=max_workers, desc="Playlist Download",
            source=url)

        success_count = sum(1 for v in results.values() if v)
        failed_count = len(results) - success_count

        print("\n" + "=" * 55)
        Enhanced_Menu.print_header("Playlist Download Complete")
        print(f"  {Fore.GREEN}Successfully downloaded: {success_count}{Style.RESET_ALL}")
        if skipped:
            print(f"  {Fore.CYAN}Already had: {skipped}{Style.RESET_ALL}")
        if failed_count:
            print(f"  {Fore.RED}Failed: {failed_count}{Style.RESET_ALL}")
            if queued:
                print(f"  {Fore.YELLOW}Throttled tracks queued for retry: {queued}{Style.RESET_ALL}")
            print(f"{Fore.RED}  Re-run later - finished tracks will be skipped.{Style.RESET_ALL}")
        print("=" * 55)

        Helpers.cleanup_directory(self.__output_directory, self.log_manager)
        return failed_count == 0

    # ==================== Batch download from a file ====================
    def download_from_file(self, file_path: str = None) -> bool:
        """
        Download every link in a .txt or .csv file, strictly one at a time.

        Sequential by design: a batch file is usually long, and firing several
        requests at once at the same host is what gets you throttled. Every
        success is written back into the source file, so a re-run picks up where
        the last one stopped; every failure lands in history/retry_queue.json
        with its error and attempt count.
        """
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Batch Download", "Download every link in a .txt or .csv file")

        if file_path is None:
            raw = Enhanced_Menu.get_input(
                "Path to the .txt or .csv file (or 'back' to return)", "str")
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
            if not Helpers.validate_youtube_url(url):
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
        folder_name = self.file_helpers.safe_name(path.stem, "Batch")
        if Enhanced_Menu.get_input(f"Save into a subfolder named '{folder_name}'? (y/n)",
                                   "yn", default=True):
            target = self.__output_directory / folder_name
        else:
            target = self.__output_directory
        target.mkdir(parents=True, exist_ok=True)
        output_template = str(target / TRACK_TMPL)

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

        cooling = youtube_limiter.cooldown_remaining
        if cooling > 0:
            Enhanced_Menu.print_status(
                f"A throttling cooldown from an earlier run is still active - the first "
                f"link will wait about {cooling:.0f}s.", "warning")

        Enhanced_Menu.print_status(f"Starting batch download of {total} links...", "info")
        print()

        try:
            for index, entry in enumerate(valid, 1):
                url, title = entry["url"], entry.get("title", "")
                label = title or url
                print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(label)[:65]}")

                ok, error, throttled = self._download_with_retry(
                    url, output_template, item_type="track", show_progress=False)

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
                    self.retry_queue.add_failure(url, title, error, str(path),
                                                 throttled=throttled, item_type="track")
                    if throttled:
                        rate_limit_streak += 1
                        print(f"      {Fore.RED}throttled (403/429) -> retry queue{Style.RESET_ALL}")
                    else:
                        rate_limit_streak = 0
                        print(f"      {Fore.RED}failed -> retry queue{Style.RESET_ALL}")

                # Written per link rather than in batches: the file should say
                # what the screen just said, and an interrupted run shouldn't
                # lose the last few results.
                if self.batch_file.mark_statuses(path, pending):
                    pending = {}

                # Once YouTube starts refusing, every remaining link fails the
                # same way and burns max_retries doing it. Stop instead: the
                # markers already written make the re-run pick up here.
                if rate_limit_streak >= 3:
                    throttled_out = True
                    Enhanced_Menu.print_status(
                        "Three throttled links in a row - stopping here. Wait a while, "
                        "then re-run this file; finished links will be skipped.", "warning")
                    break

                if index < total:
                    if rate_limit_streak:
                        # Hand the backoff to the limiter rather than sleeping
                        # here: a local sleep only pauses this loop, while a
                        # penalty is honoured by every caller of acquire(),
                        # including playlist workers on another thread.
                        penalty = min(self.rate_limit_backoff * (2 ** (rate_limit_streak - 1)),
                                      self.rate_limit_max_wait)
                        youtube_limiter.penalize(penalty)
                        Enhanced_Menu.print_status(
                            f"Throttled - holding off {penalty:.0f}s before the next link",
                            "warning")
                        # The next acquire() inside run_download serves the
                        # cooldown, so there is nothing to sleep on here.
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
        self.retry_queue.clear(cleared)

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
            print(f"  {Fore.YELLOW}Queued for retry in:{Style.RESET_ALL} {self.retry_queue.path}")
        if stopped:
            print(f"  {Fore.YELLOW}Not attempted:{Style.RESET_ALL} {total - succeeded - failed}")
        print(f"  {Fore.CYAN}Statuses written to:{Style.RESET_ALL} {path}")
        cooling = youtube_limiter.cooldown_remaining
        if cooling > 0:
            print(f"  {Fore.YELLOW}Throttle cooldown left:{Style.RESET_ALL} {cooling / 60:.1f} min")
        print(f"  {Fore.CYAN}Elapsed:{Style.RESET_ALL} {elapsed / 60:.1f} min")

        if succeeded:
            Helpers.cleanup_directory(self.__output_directory, self.log_manager)

        return failed == 0 and not stopped

    def download_from_retry_queue(self) -> bool:
        """Re-attempt every link sitting in history/retry_queue.json."""
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Retry Queue", "Re-attempt previously failed links")

        queue = self.retry_queue.read()
        if not queue:
            Enhanced_Menu.print_status("The retry queue is empty.", "info")
            return True

        # Throttled links first: they are the ones most likely to work now.
        items = sorted(queue.values(),
                       key=lambda e: (not e.get("throttled"), int(e.get("attempts", 0))))
        throttled_total = sum(1 for e in items if e.get("throttled"))

        print(f"  {Fore.CYAN}Queued links:{Style.RESET_ALL} {len(items)}")
        if throttled_total:
            print(f"  {Fore.YELLOW}Last failed to throttling:{Style.RESET_ALL} {throttled_total} "
                  f"{Style.DIM}(likely to work now){Style.RESET_ALL}")
        for entry in items[:10]:
            tag = f"{Fore.YELLOW} [throttled]{Style.RESET_ALL}" if entry.get("throttled") else ""
            print(f"      - {str(entry.get('title') or entry['url'])[:55]} "
                  f"{Style.DIM}({entry.get('attempts', 0)} attempts){Style.RESET_ALL}{tag}")
        if len(items) > 10:
            print(f"      ...and {len(items) - 10} more")
        print()

        cooling = youtube_limiter.cooldown_remaining
        if cooling > 0:
            Enhanced_Menu.print_status(
                f"A throttling cooldown is still active - the first link will wait "
                f"about {cooling:.0f}s.", "warning")

        if not Enhanced_Menu.get_input(f"Retry these {len(items)} links? (y/n)",
                                       "yn", default=True):
            Enhanced_Menu.print_status("Cancelled", "info")
            return False

        target = self.__output_directory / "Retries"
        target.mkdir(parents=True, exist_ok=True)
        # An album queued as a whole still wants its artist/album folders.
        templates = {
            "album": str(target / ALBUM_TMPL),
            "playlist": str(target / PLAYLIST_TMPL),
        }
        default_template = str(target / TRACK_TMPL)

        succeeded = failed = 0
        per_source: Dict[str, Dict[str, str]] = {}
        cleared: List[str] = []
        rate_limit_streak = 0
        total = len(items)

        try:
            for index, entry in enumerate(items, 1):
                url = entry["url"]
                label = entry.get("title") or url
                print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(label)[:65]}")

                item_type = entry.get("item_type", "track")
                ok, error, throttled = self._download_with_retry(
                    url, templates.get(item_type, default_template),
                    item_type=item_type, show_progress=False)

                source = entry.get("source")
                if ok:
                    succeeded += 1
                    cleared.append(url)
                    rate_limit_streak = 0
                    if source:
                        per_source.setdefault(source, {})[url] = "success"
                    print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
                else:
                    failed += 1
                    self.retry_queue.add_failure(url, entry.get("title", ""), error,
                                                 source or "", throttled=throttled,
                                                 item_type=item_type)
                    if throttled:
                        rate_limit_streak += 1
                        print(f"      {Fore.RED}still throttled{Style.RESET_ALL}")
                    else:
                        rate_limit_streak = 0
                        print(f"      {Fore.RED}still failing{Style.RESET_ALL}")

                # Draining the queue into a still-throttled host just re-queues
                # everything with a higher attempt count. Stop and come back.
                if rate_limit_streak >= 3:
                    Enhanced_Menu.print_status(
                        "Still being throttled - stopping. The rest stay queued.", "warning")
                    break

                if index < total:
                    if rate_limit_streak:
                        penalty = min(self.rate_limit_backoff * (2 ** (rate_limit_streak - 1)),
                                      self.rate_limit_max_wait)
                        youtube_limiter.penalize(penalty)
                        Enhanced_Menu.print_status(
                            f"Throttled - holding off {penalty:.0f}s before the next link",
                            "warning")
                    else:
                        time.sleep(random.uniform(self.yt_dlp_sleep_min, self.yt_dlp_sleep_max))
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Interrupted - remaining links stay queued.", "warning")

        self.retry_queue.clear(cleared)
        # Mirror the successes back into whichever file each link came from.
        for source, statuses in per_source.items():
            source_path = Path(source)
            if source_path.is_file():
                self.batch_file.mark_statuses(source_path, statuses)

        print()
        Enhanced_Menu.print_header("Retry Complete")
        print(f"  {Fore.GREEN}Recovered:{Style.RESET_ALL} {succeeded}")
        if failed:
            print(f"  {Fore.RED}Still queued:{Style.RESET_ALL} {failed}")

        if succeeded:
            Helpers.cleanup_directory(self.__output_directory, self.log_manager)
        return failed == 0

    # ==================== Public download methods ====================
    def download_track(self):
        """Download a single track."""
        return self._download_item(
            item_type="track",
            url_prompt="track URL",
            relative_template=TRACK_TMPL,
            confirm_large=False,
        )

    def download_album(self):
        """Download an album."""
        return self._download_item(
            item_type="album",
            url_prompt="album URL",
            relative_template=ALBUM_TMPL,
            confirm_large=True,
            use_archive=True,
        )

    def download_playlist(self):
        """Download a playlist with concurrent downloads."""
        return self._download_item(
            item_type="playlist",
            url_prompt="playlist URL",
            relative_template=None,      # computed per-playlist inside
            confirm_large=True,
            concurrent=True,
            max_workers=self.max_concurrent or 3,
        )

    def search_and_download(self):
        """Search for a song and download it."""
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("SEARCH & DOWNLOAD")
        song_query = Enhanced_Menu.get_input(
            "What is the name of the song you're looking for: ", "str")
        song_query = (song_query or "").strip()
        if not song_query:
            Enhanced_Menu.print_status("No search query provided", "error")
            return False

        self.history.add_input(song_query, "search")
        if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
            self.get_user_preferences()

        Enhanced_Menu.print_status("Searching for the song. Browsing through YouTube...", "info")
        output_template = str(self.__output_directory / "Searches" / TRACK_TMPL)

        # Shares the retry loop, so permanent failures stop early here too.
        success, _, _ = self._download_with_retry(
            f"ytsearch1:{song_query}", output_template, item_type="search")
        return success

    # ==================== Utilities ====================
    def manage_cookies(self):
        """Open the Cookie Manager, then offer to use whatever file is active."""
        self.cookie_manager.interactive_menu()
        self._cookie_notices.clear()

        path, report = self.cookie_manager.prepare_for_ytdlp()
        if path is None:
            detail = f" ({report.summary()})" if report else ""
            Enhanced_Menu.print_status(f"No usable cookie file selected{detail}.", "warning")
            if self.use_cookies:
                Enhanced_Menu.print_status(
                    "Cookies are still switched on; downloads will run without them until a file is added.",
                    "info")
            return

        Enhanced_Menu.print_status(f"Active cookie file: {path.name} - {report.summary()}",
                                   "success" if report.signed_in else "warning")
        use_cookies = Enhanced_Menu.get_input(
            "Use these cookies for downloads? (y/n)", "yn", default=report.signed_in)
        self.use_cookies = bool(use_cookies)
        self.save_config()

    def check_ytdlp(self):
        return self.utils.check_ytdlp()

    def check_ffmpeg(self):
        return self.utils.check_ffmpeg()

    def show_ytdlp_help(self):
        return self.utils.show_ytdlp_help()

    def check_dependencies(self):
        return self.utils.check_dependencies()

    def setup_dependencies(self):
        self.utils.setup_dependencies()

    def program_info(self):
        return self.utils.program_info()

    def troubleshooting(self):
        print("\n" + "=" * 50)
        Enhanced_Menu.print_header("TROUBLESHOOTING", "")
        print("=" * 50)
        print("Hello, this troubleshooter is to help if you're experiencing a problem in the program")
        print("Running a simple diagnostic. This might take a while.....")

        Enhanced_Menu.print_status("1. Checking yt-dlp installation...", "info")
        if not self.check_ytdlp():
            Enhanced_Menu.print_status("yt-dlp not found or not working", "error")
            if Enhanced_Menu.get_input("Install yt-dlp now? (y/n)", "yn", default=True):
                self.setup_dependencies()

        Enhanced_Menu.print_status("\n2. Checking FFmpeg installation...", "info")
        if not self.check_ffmpeg():
            Enhanced_Menu.print_status("FFmpeg not found (audio conversion might fail)", "error")

        Enhanced_Menu.print_status("\n3. Testing YouTube access...", "info")
        test_url = "https://music.youtube.com/watch?v=215T8NF93kw"
        youtube_limiter.acquire()
        try:
            result = subprocess.run(
                ["yt-dlp", "--skip-download", "--print-json", test_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                Enhanced_Menu.print_status("Can access YouTube", "success")
            else:
                Enhanced_Menu.print_status(
                    f"Cannot access YouTube: {result.stderr[:100]}", "error")
        except Exception as e:
            Enhanced_Menu.print_status(f"Test failed: {e}", "error")

        Enhanced_Menu.print_status("\n4. Checking cookies...", "info")
        if not self.use_cookies:
            Enhanced_Menu.print_status(
                "Cookies are off (only needed for age-restricted, members-only or bot-check errors)", "info")
        else:
            verdict, detail = self.cookie_manager.verify_with_ytdlp()
            Enhanced_Menu.print_status(detail, "success" if verdict == "valid" else "error")
            if verdict in ("rotated", "signed_out", "bad_file", "no_file"):
                Enhanced_Menu.print_status(
                    "Fix: Cookie Manager -> Manual export (private-window method)", "info")

    def reset_to_defaults(self):
        """Reset all settings to default values."""
        self.__output_directory = Path.home() / "Music" / "Collection" / "YouTube"
        self.__output_directory.mkdir(parents=True, exist_ok=True)
        self.__audio_quality = "320k"
        self.__audio_format = "mp3"
        self.use_cookies = False
        self.max_retries = 3
        self.retry_delay = 10
        self.download_timeout = 120
        self.postprocess_timeout = 900
        self.max_concurrent = 2
        self.yt_dlp_sleep_min = 3
        self.yt_dlp_sleep_max = 7
        self.save_config()
        Enhanced_Menu.print_status("Settings reset to defaults", "success")