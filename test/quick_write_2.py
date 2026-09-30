    def _download_with_retry(self, url: str, output_template: str, additional_args: list = None,
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
