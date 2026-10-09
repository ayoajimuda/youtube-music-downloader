"""HARMONI Music Downloader - start here:

    python main.py
"""

import sys

from managers import config_manager, log_manager
from managers.log_manager import log_error, log_info, log_warning
from menu.colorful_menu import Enhanced_Menu
from menu.main_menu import (CONFIG, COOKIES, DOWNLOADS, EXIT, LOGS, MANAGEMENT, TOOLS,
                            build_downloader, main_menu)
from menu import (config_menu, cookies_menu, downloader_menu, logs_menu, management_menu,
                  tools_menu)


def startup_checks() -> None:
    """Non-fatal checks: problems are reported, and the program still starts."""
    # Config: invalid values are replaced with defaults while running, but say so.
    is_valid, problems = config_manager.validate_config()
    if not is_valid:
        log_warning(f"The config file has {len(problems)} problem(s); defaults are used "
                    "for those settings. Fix them in the Config Menu.")
        for problem in problems[:5]:
            log_warning(f"  - {problem}")

    # Programs the downloader can't work without.
    from tools.dependency_check import find_program
    for name in ("yt-dlp", "ffmpeg"):
        path, reason = find_program(name)
        if not path:
            log_warning(f"{name} {reason}. Downloads won't work until it's installed "
                        "(Tools Menu -> Check dependencies).")

    # Check for yt-dlp updates (at most once a day; silent unless there's one)
    try:
        from tools.ytdlp_update_checker import check_on_startup
        check_on_startup()
    except Exception:
        pass  # Silently ignore update check failures

    # A reminder of anything left to retry from last time.
    waiting = len(log_manager.read_failures())
    if waiting:
        log_info(f"{waiting} failed download(s) are waiting in the retry queue "
                 "(Downloads Menu -> Retry failed downloads).")


def run(downloader) -> int:
    """The main-menu loop."""
    while True:
        downloader_menu.status_block(downloader)
        choice = main_menu()

        try:
            # Downloads Menu
            if choice == DOWNLOADS:
                downloader_menu.run(downloader)

            # Management Menu
            elif choice == MANAGEMENT:
                management_menu.run(downloader)

            # Tools Menu
            elif choice == TOOLS:
                tools_menu.run(downloader)

            # Config Menu (changes apply to the downloader straight away)
            elif choice == CONFIG:
                config_menu.run(downloader)

            # Cookies Menu
            elif choice == COOKIES:
                cookies_menu.run(downloader)

            # Logs Menu
            elif choice == LOGS:
                logs_menu.run()

            # Exit (None = Ctrl-C at the main menu)
            elif choice in (EXIT, None):
                log_info("Exiting program...")
                return 0

            else:
                log_error("Invalid choice.")

        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Cancelled", "warning")
        except Exception as error:      # report it, keep the program running
            log_error(f"{choice} failed: {type(error).__name__}: {error}", exc_info=True)


if __name__ == "__main__":
    # Logging, config, cookies and the downloader itself. The config never
    # fails to load (missing -> created, broken -> replaced with defaults and
    # the bad file kept as .bad); what can fail is the download folder.
    try:
        downloader = build_downloader()
    except OSError as error:
        log_error(f"Couldn't create the download folder: {error}")
        log_error(f"Change output_directory in {config_manager.config_path()} and try again.")
        sys.exit(1)
    except Exception as error:
        log_error(f"Error starting up: {type(error).__name__}: {error}", exc_info=True)
        sys.exit(1)

    startup_checks()
    sys.exit(run(downloader))