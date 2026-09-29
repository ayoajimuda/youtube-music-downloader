import os
import subprocess
import time
import hashlib
import threading

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from colorama import init, Fore, Style

from assist_methods import cleanup_directory
from tools.EnhancedMenu import Enhanced_Menu
from tools.Progress import DownloadProgress
from tools.YtDlpErrors import (looks_throttled, has_error_line,
                               already_downloaded, describe_failure)
from utils.validators import Helpers

init(autoreset=True)


class YoutubeMusicDownloader:
    """
    Downloader class that contains the download functions.

    Everything else lives elsewhere and is handed in:
      log_manager, history, file_helpers, batch_file, cookies  - constructor
      retry (RetryManager) and menu (Menu)                     - set by build_app,
                                                                  since both need this object
    """

    def __init__(self, log_manager, history, file_helpers, batch_file, cookies):
        self.log_manager = log_manager
        self.history = history
        self.file_helpers = file_helpers
        self.batch_file = batch_file
        self.cookies = cookies
        self.retry = None                  # RetryManager, attached by build_app
        self.menu = None                   # Menu, attached by build_app

        self.__output_directory = Path.home() / "Music" / "Collection" / "YouTube"
        self.__audio_quality = "320k"
        self.__audio_format = "mp3"

        self.max_retries = 2               # attempts per link
        self.retry_delay = 20              # seconds between attempts
        self.download_timeout = 120        # seconds of *silence* before a download is killed
        self.max_concurrency = 3           # downloads running at once in a playlist
        self.yt_dlp_sleep_min = 3          # min seconds yt-dlp waits between downloads
        self.yt_dlp_sleep_max = 7          # max seconds (random delay in this range)
        self.debug = False

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

    @property
    def use_cookies(self) -> bool:
        """Whether cookies are sent with downloads (the flag lives on the cookie service)."""
        return self.cookies.enabled

    @use_cookies.setter
    def use_cookies(self, value: bool):
        self.cookies.enabled = bool(value)

    # ========================= Download functions ================================
    @staticmethod
    def _result(command, output: str, throttled: bool) -> subprocess.CompletedProcess:
        done = subprocess.CompletedProcess(args=command, returncode=0, stdout=output, stderr="")
        done.throttled = throttled
        return done

    def run_download(self, url: str, output_template: str,
                     additional_args=None, show_progress: bool = True):
        """
        Run one yt-dlp download with a progress bar and a stall watchdog.

        Returns a CompletedProcess (with a .throttled flag) on success; raises
        CalledProcessError (also with .throttled) on failure, and RuntimeError
        if yt-dlp isn't installed.
        """
        if not output_template:
            raise ValueError("run_download requires an output template")

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
        ]

        if not self.debug:
            command += ["--quiet", "--no-warnings"]

        # yt-dlp rewrites its --cookies file when it exits, so concurrent
        # workers must not share one. Each download gets a private copy, and
        # end_run() (in the finally below) merges refreshed cookies back.
        cookie_file = self.cookies.file_for_run()
        run_copy = self.cookies.begin_run(cookie_file) if cookie_file else None
        if cookie_file:
            command.extend(["--cookies", str(run_copy or cookie_file)])

        if additional_args:
            if isinstance(additional_args, list):
                command.extend(additional_args)
            else:
                command.append(additional_args)
        command.append(url)

        progress = DownloadProgress(show_progress)
        process = None
        watchdog = None
        throttled = False

        try:
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

                    progress.update(line)
            finally:
                if watchdog is not None:
                    watchdog.cancel()

            process.wait()
            progress.close()
            full_output = "\n".join(output_lines)

            # Checked against the whole output, since a marker that never
            # appeared on a streamed line would otherwise be missed here.
            if not throttled and looks_throttled(full_output):
                throttled = True

            had_error = has_error_line(output_lines)

            if process.returncode == 0 and not had_error:
                return self._result(command, full_output, throttled)

            # Archive skip / already-have-it is NOT a failure
            if already_downloaded(full_output) and not had_error:
                self.log_manager.log_success(f"Already downloaded (skipped): {url}")
                return self._result(command, full_output, throttled)

            error_msg = describe_failure(url, process.returncode, full_output,
                                         had_error, self.download_timeout)
            self.log_manager.log_failure(error_msg)
            failure = subprocess.CalledProcessError(
                process.returncode or 1, command, output=full_output, stderr="")
            # Ride along on the exception so a caller in another thread reads
            # its own verdict rather than whatever a sibling thread just set.
            failure.throttled = throttled
            raise failure

        except FileNotFoundError:
            progress.close()
            error_msg = "yt-dlp not found. Please install it with: pip install yt-dlp"
            self.log_manager.log_error(error_msg)
            raise RuntimeError(error_msg)
        except subprocess.CalledProcessError:
            # Already classified and logged above - don't let the generic
            # handler below relabel it as an "unexpected error".
            progress.close()
            raise
        except Exception as e:
            progress.close()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            self.log_manager.log_error(f"Unexpected error in run_download: {e}")
            raise
        finally:
            if run_copy is not None:
                self.cookies.end_run(run_copy, cookie_file)

    def parallel_run_download(self, tasks, archive_path: Optional[Path],
                              max_workers: int = 3, desc: str = "Downloading",
                              source: str = "") -> Dict[str, bool]:
        """
        Download many items at once.

        The archive is pre-filtered by the caller and appended to under a narrow
        lock, so workers run in parallel rather than queueing on one mutex.

        tasks:   list of (url, output_template, additional_args, video_id, title)
        returns: {video_id: success_bool}

        Items that fail because the host was throttling go to the retry queue:
        the link is probably fine and only the timing was wrong.
        """
        results: Dict[str, bool] = {}
        result_lock = threading.Lock()
        archive_lock = threading.Lock()
        pbar_lock = threading.Lock()

        with tqdm(total=len(tasks), desc=desc, unit="item", dynamic_ncols=True) as pbar:
            def worker(url, tmpl, args, video_id, title):
                success, error, throttled = self.retry.attempt(
                    url, tmpl, args, "item", show_progress=False)
                if success and archive_path is not None:
                    self.file_helpers.append_archive(archive_path, video_id, archive_lock)
                elif throttled:
                    self.retry.add_failure(url, title, error, source,
                                           throttled=True, item_type="track")
                with result_lock:
                    results[video_id] = success
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

        return results

    def _tidy_output(self) -> None:
        """Remove leftovers (thumbnails, partial files) from the output folder."""
        cleanup_directory(self.__output_directory, self.log_manager)

    def download_item(self, item_type: str, url_prompt: str, output_template=None,
                      additional_args: list = None, confirm_large: bool = False,
                      use_archive: bool = False, concurrent: bool = False,
                      max_workers: int = 3) -> bool:
        """
        Unified download for tracks, albums and playlists.

        item_type:        "track", "album" or "playlist" (used for prompts and folders)
        url_prompt:       what to call the URL in the prompt
        output_template:  yt-dlp -o template, or a callable returning one. A callable
                          is evaluated after the settings prompt, so a changed output
                          folder is respected.
        additional_args:  extra yt-dlp arguments for the single-call path
        confirm_large:    ask before starting a collection of more than 50 items
        use_archive:      keep a yt-dlp download archive for this album
        concurrent:       expand into items and download them in parallel (playlists)
        max_workers:      parallel downloads when concurrent=True
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
            self.cookies.preflight()

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
            count = metadata.get('playlist_count') or 0
            if confirm_large and count > 50:
                Enhanced_Menu.print_status(
                    f"This {item_type} contains {count} items. This may take a while.", "warning")
                if not Enhanced_Menu.get_input("Continue with download? (y/n)", "yn", default=False):
                    Enhanced_Menu.print_status("Download cancelled", "info")
                    continue

            if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
                self.menu.get_user_preferences()

            # ---------------- Concurrent (playlist) path ----------------
            if concurrent:
                success = self._run_playlist(url, metadata, max_workers)

            # ---------------- Single-call (track / album) path ----------------
            else:
                template = output_template() if callable(output_template) else output_template
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
                success, error, throttled = self.retry.attempt(
                    url, template, item_args, item_type)
                if throttled:
                    self.retry.add_failure(url, metadata.get('title', ''), error,
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
        collection_template = str(playlist_folder / "%(artist)s - %(title)s.%(ext)s")

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
        results = self.parallel_run_download(
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
            queued = self.retry.throttled_count()
            if queued:
                print(f"  {Fore.YELLOW}Throttled tracks queued for retry: {queued}{Style.RESET_ALL}")
            print(f"{Fore.RED}  Re-run later - finished tracks will be skipped.{Style.RESET_ALL}")
        print("=" * 55)

        self._tidy_output()
        return failed_count == 0

    # ==================== Public download methods ====================
    def download_track(self):
        """Download a single track."""
        return self.download_item(
            item_type="track",
            url_prompt="track URL",
            output_template=lambda: str(self.__output_directory / "%(artist)s - %(title)s.%(ext)s"),
            confirm_large=False,
        )

    def download_album(self):
        """Download an album."""
        return self.download_item(
            item_type="album",
            url_prompt="album URL",
            output_template=lambda: str(self.__output_directory /
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
            max_workers=self.max_concurrency or 3,
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
        self.cookies.preflight()
        if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
            self.menu.get_user_preferences()

        Enhanced_Menu.print_status("Searching for the song. Browsing through YouTube...", "info")
        output_template = str(self.__output_directory / "Searches" /
                              "%(artist)s - %(title)s.%(ext)s")

        ok, _error, _throttled = self.retry.attempt(
            f"ytsearch1:{song_query}", output_template, item_type="search")
        if ok:
            self.log_manager.log_success(f"Successfully downloaded: '{song_query}'")
        return ok

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

        # Settings first, so a changed output folder applies to the folder chosen below.
        if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
            self.menu.get_user_preferences()

        # Output folder: named after the file by default, so a batch stays together.
        folder_name = self.file_helpers.safe_name(path.stem, "Batch")
        if Enhanced_Menu.get_input(f"Save into a subfolder named '{folder_name}'? (y/n)",
                                   "yn", default=True):
            target = self.__output_directory / folder_name
        else:
            target = self.__output_directory
        target.mkdir(parents=True, exist_ok=True)
        output_template = str(target / "%(artist)s - %(title)s.%(ext)s")

        total = len(valid)
        succeeded = failed = 0
        pending: Dict[str, str] = {}          # url -> status, not yet flushed to file
        cleared: List[str] = []               # urls to remove from the retry queue
        failures: List[Tuple[str, str, str]] = []
        interrupted = throttled_out = False
        streak = 0
        started = time.monotonic()

        self.cookies.preflight()
        Enhanced_Menu.print_status(f"Starting batch download of {total} links...", "info")
        print()

        try:
            for index, entry in enumerate(valid, 1):
                url, title = entry["url"], entry.get("title", "")
                label = title or url
                print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(label)[:65]}")

                ok, error, throttled = self.retry.attempt(
                    url, output_template, item_type="track", show_progress=False)

                if ok:
                    succeeded += 1
                    pending[url] = "success"
                    cleared.append(url)
                    streak = 0
                    print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
                else:
                    failed += 1
                    pending[url] = "failed"
                    failures.append((url, title, error))
                    self.retry.add_failure(url, title, error, str(path),
                                           throttled=throttled, item_type="track")
                    if throttled:
                        streak += 1
                        print(f"      {Fore.RED}throttled (403/429) -> retry queue{Style.RESET_ALL}")
                    else:
                        streak = 0
                        print(f"      {Fore.RED}failed -> retry queue{Style.RESET_ALL}")

                # Written per link rather than in batches: the file should say
                # what the screen just said, and an interrupted run shouldn't
                # lose the last few results.
                if self.batch_file.mark_statuses(path, pending):
                    pending = {}

                # Once YouTube starts refusing, every remaining link fails the
                # same way and burns max_retries doing it. Stop instead: the
                # markers already written make the re-run pick up here.
                if self.retry.should_stop(streak):
                    throttled_out = True
                    Enhanced_Menu.print_status(
                        "Three throttled links in a row - stopping here. Wait a while, "
                        "then re-run this file; finished links will be skipped.", "warning")
                    break

                if index < total:
                    self.retry.pause_between(streak)

        except KeyboardInterrupt:
            interrupted = True
            print()
            Enhanced_Menu.print_status(
                "Interrupted. Re-run this file later and finished links will be skipped.",
                "warning")

        # Final flush of statuses, and drop anything that succeeded from the queue.
        self.batch_file.mark_statuses(path, pending)
        self.retry.clear(cleared)

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
            print(f"  {Fore.YELLOW}Queued for retry in:{Style.RESET_ALL} {self.retry.path}")
        if stopped:
            print(f"  {Fore.YELLOW}Not attempted:{Style.RESET_ALL} {total - succeeded - failed}")
        print(f"  {Fore.CYAN}Statuses written to:{Style.RESET_ALL} {path}")
        print(f"  {Fore.CYAN}Elapsed:{Style.RESET_ALL} {elapsed / 60:.1f} min")

        if succeeded:
            self._tidy_output()

        return failed == 0 and not stopped