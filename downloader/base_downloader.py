""" Base downloader for the youtube music downloader. Contains only the download functions"""


import re
import os
import random
import subprocess
import time
import hashlib
import threading

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from colorama import init, Fore, Style
from assist_methods import cleanup_directory, sanitize_filename, parse_size

init(autoreset=True)

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

def looks_throttled(text: str) -> bool:
    """True if yt-dlp output suggests YouTube is throttling or refusing us."""
    low = (text or "").lower()
    return any(marker in low for marker in _THROTTLE_MARKERS)

class YoutubeMusicDownloader:
    """
    Downloader class that contains all the download functions
    """
    
    def __init__(self):
        self.__output_directory = Path.home() / "Music" / "Collection" / "YouTube"
        self.__audio_quality = "320k"
        self.__audio_format = "mp3"
        self.__configuration_file = r"config/YoutubeMusicDownloader.json"
        self.use_cookies = False
        self.max_retries = 2 # No of retries for a struggling download
        self.retry_delay = 20 # The amount of time between each retry
        self.download_timeout = 120 # seconds of silence before a download is killed

        
        self.debug = False
        self.max_concurrency = 3 # Number of thread / downloads running at once
        self.yt_dlp_sleep_min = 3          # min seconds yt-dlp waits between downloads
        self.yt_dlp_sleep_max = 7          # max seconds (random delay in this range)
        
        self.rate_limit_backoff = 300
        self.rate_limit_max_wait = 1800
        self._last_run_throttled = False
        
        self.archives_dir = Path("history/archives")
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        
    # ==================== Public properties ====================
    @property
    def audio_format(self) -> str:
        """Current audio format (mp3, flac, etc.)."""
        return self.__audio_format

    @audio_format.setter
    def audio_format(self, value: str):
        if value in ["mp3", "flac", "ogg", "opus", "m4a", "wav"]:
            self.__audio_format = value
        else:
            raise ValueError(f"Unsupported audio format: {value}")

    @property
    def audio_quality(self) -> str:
        """Current audio bitrate (320k, 192k, auto, etc.)."""
        return self.__audio_quality

    @audio_quality.setter
    def audio_quality(self, value: str):
        valid_qualities = ["auto", "disable", "8k", "16k", "24k", "32k", "40k", "48k", "64k",
                           "80k", "96k", "112k", "128k", "160k", "192k", "224k", "256k", "320k"]
        if value in valid_qualities:
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

    # ========================= Download Functions ================================
    def run_download(self, url: str, output_template: str, additional_args=None, show_progress: bool = True):
        """
        Runs a yt-dlp download with a tqdm progress bar and a stall watchdog

        Args:
            url (str): The input url
            output_template (str): _description_
            additional_args (_type_, optional): _description_. Defaults to None.
            show_progress (bool, optional): _description_. Defaults to True.
        """
        
        if not output_template:
            raise ValueError("run_download function requires an output template") # If no output template is given, program fails
        
        output_directory = os.path.dirname(output_template)
        if output_directory:
            os.makedirs(output_directory, exist_ok=True)
        
        # The list is passed as a subprocess 
        command = [
            "yt-dlp",
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
            "--quiet", "--no-warnings"          
        ]
        
        cookie_file = self._get_cookie_file()
        if cookie_file:
            command.extend(["--cookies", cookie_file])
            
        if additional_args:
            if isinstance(additional_args, list):
                command.extend(additional_args)
            else:
                command.append(additional_args)
        command.append(url)
        
        # The progress bar for the downloader  
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
        self._last_run_throttled = False
        
        # Run the command as 
        try:
            process = subprocess.Popen(
                command,
                stdout = subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace'
            )
            
            # download_timeout is treated as "no output for N seconds" so that
            # long but healthy downloads aren't killed mid-transfer.
            def _kill(proc):
                try:
                    proc.kill()
                except Exception:
                    pass
                
            def _arm():
                t = threading.Timer(self.download_timeout, _kill, args=(process,))
                t.daemon = True
                t.start()
                return t
            
            # The watchdog makes sure that there isn't a stall in the download
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
                    if not throttled and looks_throttled(line):
                        throttled = True
                        self.log_manager.log_error("YouTube throttling detected")

                    if "[download]" in line:
                        try:
                            percent_match = re.search(r'(\d+\.?\d*)%', line)
                            if percent_match:
                                percent = float(percent_match.group(1))
                                progress_bar.set_description(
                                    f"{Fore.CYAN}Downloading: {percent:.1f}%{Style.RESET_ALL}")

                            size_match = re.search(r'of\s+([\d.]+\s*[KMGT]?i?B)', line)
                            if size_match and progress_bar.total is None:
                                total_bytes = parse_size(size_match.group(1))
                                if total_bytes:
                                    progress_bar.total = total_bytes

                            downloaded_match = (re.search(r'([\d.]+\s*[KMGT]?i?B)\s+at', line) or
                                                re.search(r'([\d.]+\s*[KMGT]?i?B)\s+ETA', line) or
                                                re.search(r'([\d.]+\s*[KMGT]?i?B)\s*/', line))
                            if downloaded_match:
                                downloaded_bytes = parse_size(downloaded_match.group(1))
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
                            continue

                    if "100%" in line or "already been downloaded" in line or "[Merger]" in line:
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

            # Checked against the whole output, since a marker that never
            # appeared on a streamed line would otherwise be missed here.
            if not throttled and looks_throttled(full_output):
                throttled = True
            self._last_run_throttled = throttled

            # yt-dlp can report an error and still exit 0 (that is what

            had_error_line = any(line.lstrip().upper().startswith("ERROR:")
                                 for line in output_lines)
            already_have = ("has already been recorded in the archive" in low
                            or "already been downloaded" in low
                            or "nothing to download" in low)

            # Success
            if process.returncode == 0 and not had_error_line:
                done = subprocess.CompletedProcess(
                    args=command, returncode=0, stdout=full_output, stderr="")
                done.throttled = throttled
                return done

            # Archive skip / already-have-it is NOT a failure
            if already_have and not had_error_line:
                self.log_manager.log_success(f"Already downloaded (skipped): {url}")
                done = subprocess.CompletedProcess(
                    args=command, returncode=0, stdout=full_output, stderr="")
                done.throttled = throttled
                return done

            # Genuine failure - classify (specific first, catch-all keeps raw tail)
            error_msg = f"Download failed for {url} with code {process.returncode}"
            if process.returncode == 0 and had_error_line:
                error_msg = (f"Download failed for {url} - yt-dlp reported an error "
                             f"but exited 0")
            if "403" in low or "forbidden" in low:
                error_msg += (" - HTTP 403 (YouTube refused the stream: update yt-dlp, "
                              "then check cookies / JS runtime)")
            elif "429" in low or "too many requests" in low:
                error_msg += " - HTTP 429 (rate limited, slow down or wait)"
            elif "only images are available" in low:
                error_msg += " - No audio stream (SABR/format restriction)"
            elif "requested format is not available" in low:
                error_msg += " - Requested format not available"
            elif "javascript runtime" in low or "no supported js" in low:
                error_msg += " - Missing JS runtime (install Deno or Node)"
            elif "sign in" in low or "not a bot" in low or "confirm your age" in low:
                error_msg += " - YouTube requires authentication (check cookies)"
            elif "private video" in low:
                error_msg += " - Video is private"
            elif "age" in low and "restrict" in low:
                error_msg += " - Age restricted"
            elif "members-only" in low or "members only" in low:
                error_msg += " - Members-only content"
            elif "unavailable" in low or "not available in your" in low:
                error_msg += " - Video unavailable / region-locked"
            elif "copyright" in low:
                error_msg += " - Copyright restriction"
            elif "ffmpeg" in low:
                error_msg += " - FFmpeg conversion error"
            elif not full_output:
                error_msg += f" - No output (likely killed after {self.download_timeout}s of inactivity)"
            else:
                error_msg += f" - Error: {full_output[-400:]}"

            self.log_manager.log_failure(error_msg)
            failure = subprocess.CalledProcessError(
                process.returncode or 1, command, output=full_output, stderr="")
            failure.throttled = throttled
            raise failure

        except FileNotFoundError:
            progress_bar.close()
            error_msg = "yt-dlp not found. Please install it with: pip install yt-dlp"
            self.log_manager.log_error(error_msg)
            raise RuntimeError(error_msg)
        except subprocess.CalledProcessError:
            # Already classified and logged above - don't let the generic
            # handler below relabel it as an "unexpected error".
            progress_bar.close()
            raise
        except Exception as e:
            progress_bar.close()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            self.log_manager.log_error(f"Unexpected error in run_download: {e}")
            raise

    def parallel_run_download(self, tasks, archive_path: Optional[Path], max_workers: int = 3, desc: str = "Downloading", source: str = "") -> Dict[str, bool]:
        """
        Function that download 

        Args:
            tasks (_type_): _description_
            archive_path (Optional[Path]): _description_
            max_workers (int, optional): _description_. Defaults to 3.
            desc (str, optional): _description_. Defaults to "Downloading".
            source (str, optional): _description_. Defaults to "".

        Returns:
            Dict[str, bool]: _description_
        """
        pass
    
    def _retry_logic(self, url: str, output_template: str, additional_args: list = None,
                             item_type: str = "item", show_progress: bool = True) -> Tuple[bool, str, bool]:
        """
        Unified retry logic. Returns (success, last_error, throttled) so callers
        that batch many links can report why each one failed without re-parsing
        the logs, and can tell a bad link apart from a refusing host.
        """
        last_error = ""
        last_throttled = False
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
                                           show_progress=show_progress)
                # run_download only ever returns code 0 or raises, so this is the success path.
                if result and result.returncode == 0:
                    self.log_manager.log_success(f"Successfully downloaded {item_type}: {url}")
                    if item_type in ('album', 'playlist'):
                        Helpers.cleanup_directory(self.__output_directory, self.log_manager)
                    return True, "", False
            except subprocess.CalledProcessError as e:
                last_error = str(e)[:300]
                last_throttled = getattr(e, "throttled", False)
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

    def download_item(self, item_type: str, url_prompt: str, output_template: str = None, additional_args: list = None, confirm_large: bool = False, use_archive: bool = False, concurrent: bool = False, max_workers: int = 3) -> bool:
        """
        Unified download for tracks, albums and playlists.
        
        Args:
            item_type (str): _description_
            url_prompt (str): _description_
            output_template (str, optional): _description_. Defaults to None.
            additional_args (list, optional): _description_. Defaults to None.
            confirm_large (bool, optional): _description_. Defaults to False.
            use_archive (bool, optional): _description_. Defaults to False.
            concurrent (bool, optional): _description_. Defaults to False.
            max_workers (int, optional): _description_. Defaults to 3.

        Returns:
            bool: _description_
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
    
    def download_track(self):
        """Download a single track."""
        return self._download_item(
            item_type="track",
            url_prompt="track URL",
            output_template=str(self.__output_directory / "%(artist)s - %(title)s.%(ext)s"),
            confirm_large=False,
        )

    def download_album(self):
        """Download an album."""
        return self._download_item(
            item_type="album",
            url_prompt="album URL",
            output_template=str(self.__output_directory /
                                "%(artist)s/%(album)s/%(artist)s - %(title)s.%(ext)s"),
            confirm_large=True,
            use_archive=True,
        )

    def download_playlist(self):
        """Download a playlist with concurrent downloads."""
        return self._download_item(
            item_type="playlist",
            url_prompt="playlist URL",
            output_template=None,        # computed per-playlist inside
            confirm_large=True,
            concurrent=True,
            max_workers=self.max_concurrent or 3,
        )

    def download_from_file(self, file_path: str = None) -> bool:
        """
        Download every link in a .txt or .csv file, strictly one at a time.

        Sequential by design: a batch file is usually long, and firing several
        requests at once at the same host is what gets you throttled. Every
        success is written back into the source file, so a re-run picks up where
        the last one stopped; every failure lands in the retry queue with its
        error and attempt count.
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
        print(f"  {Fore.CYAN}Elapsed:{Style.RESET_ALL} {elapsed / 60:.1f} min")

        if succeeded:
            Helpers.cleanup_directory(self.__output_directory, self.log_manager)

        return failed == 0 and not stopped

    def download_from_retry_queue(self) -> bool:
        """Re-attempt every link sitting in the retry queue."""
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

        if not Enhanced_Menu.get_input(f"Retry these {len(items)} links? (y/n)",
                                       "yn", default=True):
            Enhanced_Menu.print_status("Cancelled", "info")
            return False

        target = self.__output_directory / "Retries"
        target.mkdir(parents=True, exist_ok=True)
        # An album queued as a whole still wants its artist/album folders.
        templates = {
            "album": str(target / "%(artist)s/%(album)s/%(artist)s - %(title)s.%(ext)s"),
            "playlist": str(target / "%(playlist)s/%(artist)s - %(title)s.%(ext)s"),
        }
        default_template = str(target / "%(artist)s - %(title)s.%(ext)s")

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
                        wait = self._backoff_seconds(rate_limit_streak)
                        Enhanced_Menu.print_status(
                            f"Throttled - waiting {wait:.0f}s before the next link", "warning")
                        time.sleep(wait)
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
