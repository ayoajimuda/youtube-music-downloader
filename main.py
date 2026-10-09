"""
Everything that happens before the main menu appears lives in this file:
logging, config, cookies, the downloader, the retry manager and the
startup checks. The menus themselves only show choices.
"""
 
import sys
from pathlib import Path
 
from managers import config_manager, cookie_manager, log_manager, retry_manager
from managers.log_manager import log_error, log_info, log_warning
from downloader import batch_downloader, rate_limiter
from menu.colorful_menu import Enhanced_Menu
from menu.main_menu import main_menu
from menu import config_menu, cookies_menu, downloader_menu, management_menu, tools_menu
from tools import ask_user_preference as preferences
 
# The project root when run as a script; the user data folder in a packaged app
# (see config_manager.get_app_dir), so settings never end up inside the app itself.
PROJECT_ROOT = config_manager.APP_DIR
CONFIG_FILE = config_manager.CONFIG_PATH
 
 
def build_downloader():
    """Set up logging, config and cookies, then create the downloader (which wires itself)."""
    from downloader.base_downloader import YoutubeMusicDownloader
 
    log_manager.setup()
    log_manager.reset_session()
    config_manager.configure_youtube(str(CONFIG_FILE), on_error=log_manager.log_warning)
    preferences.configure(config_manager, Enhanced_Menu, cookie_manager)
    cookies_menu.restore()                    # last active cookie file
 
    downloader = YoutubeMusicDownloader()     # reads its settings from the config
    retry_manager.configure(downloader, log_manager, batch_downloader, rate_limiter)
    return downloader
 
 
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
            if choice == "Downloads Menu":
                downloader_menu.run(downloader)
 
            # Management Menu
            elif choice == "Management Menu":
                management_menu.run(downloader)
 
            # Tools Menu
            elif choice == "Tools Menu":
                tools_menu.run(downloader)
 
            # Config Menu (changes apply to the downloader straight away)
            elif choice == "Config Menu":
                config_menu.run(downloader)
 
            # Cookies Menu
            elif choice == "Cookies Menu":
                cookies_menu.run(downloader)
 
            # Exit (None = Ctrl-C at the main menu)
            elif choice in ("Exit", None):
                log_info("Exiting program...")
                return 0
 
            else:
                log_error("Invalid choice.")
 
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Cancelled", "warning")
        except Exception as error:      # report it, keep the program running
            log_error(f"{choice} failed: {type(error).__name__}: {error}", exc_info=True)
 
 
def main(downloader=None) -> int:
    """
    Start the program. Pass a downloader to reuse one that's already built
    (the startup checks are skipped then).
    """
    if downloader is None:
        # The config never fails to load (missing -> created, broken -> replaced
        # with defaults and the bad file kept as .bad); what can fail is the
        # download folder.
        try:
            downloader = build_downloader()
        except OSError as error:
            log_error(f"Couldn't create the download folder: {error}")
            log_error(f"Change output_directory in {config_manager.config_path()} and try again.")
            return 1
        except Exception as error:
            log_error(f"Error starting up: {type(error).__name__}: {error}", exc_info=True)
            return 1
        startup_checks()
    return run(downloader)
 
 
if __name__ == "__main__":
    sys.exit(main())
 