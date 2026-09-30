"""Cookie management for authentication.

Module-level: import it and call the functions directly, or assign the module
to an attribute (self.cookies = cookie_manager) so existing call sites keep
working.

  - get_status()        check which browsers have YouTube cookies
  - extract_cookies()   pull them out of a browser into a cookies.txt
  - load_cookies()      point at an existing file and make it active
  - save_cookies()      keep a timestamped copy
  - list_cookies()      what's in the cookie folder
  - clear_cookies()     delete them
  - test_cookies()      check the active file still works
  - interactive_menu()  all of the above, from the menu
"""

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from colorama import Fore, Style, init

from EnhancedMenu import Enhanced_Menu  # assumed existing

try:
    import browser_cookie3
except ImportError:                     # optional: manual export still works
    browser_cookie3 = None

init(autoreset=True)

COOKIE_DIRECTORY = Path("cookies")
COOKIE_DIRECTORY.mkdir(parents=True, exist_ok=True)

# yt-dlp only accepts a cookie file whose first line is exactly this,
# capitalisation included.
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"

BROWSERS = ("chrome", "firefox", "edge", "opera", "opera_gx", "brave", "safari", "chromium")

# For Linux fallback (not used yet, but kept for future expansion)
LINUX_COOKIE_PATHS = {
    "chrome": "~/.config/google-chrome/Default/Cookies",
    "chromium": "~/.config/chromium/Default/Cookies",
    "firefox": "~/.mozilla/firefox/*.default-release/cookies.sqlite",
}

TEST_URL = "https://music.youtube.com/watch?v=215T8NF93kw"

# The one piece of state: which cookie file downloads should use.
current_cookie_file: Optional[Path] = None


# ==================== Setup helpers ====================
def cookie_sources() -> Dict[str, Any]:
    """The browsers browser_cookie3 can read on this machine."""
    if browser_cookie3 is None:
        return {}
    return {name: getattr(browser_cookie3, name)
            for name in BROWSERS if getattr(browser_cookie3, name, None)}


def check_admin() -> bool:
    """True if running with admin privileges on Windows (always True elsewhere)."""
    if platform.system() == "Windows":
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return True


def get_active_cookie_file() -> Optional[Path]:
    """The cookie file to hand yt-dlp, or None."""
    return current_cookie_file if current_cookie_file and current_cookie_file.is_file() else None


def set_active(path: Optional[Path]) -> None:
    global current_cookie_file
    current_cookie_file = Path(path) if path else None


# ==================== Status ====================
def get_status() -> bool:
    """Check available browser cookies and report status."""
    Enhanced_Menu.print_header("Checking available browser cookies....")

    sources = cookie_sources()
    if not sources:
        Enhanced_Menu.print_status(
            "browser_cookie3 isn't installed. Install it with: pip install browser-cookie3",
            "error")
        return False

    if platform.system() == "Windows" and not check_admin():
        Enhanced_Menu.print_status("Running without admin privileges on Windows", "warning")
        Enhanced_Menu.print_status("Some browsers may not be accessible", "info")

    available = []
    for browser, cookie_func in sources.items():
        try:
            # Convert generator to list to avoid exhaustion
            cookies = list(cookie_func(domain_name="music.youtube.com"))
            if cookies:
                available.append(browser)
                Enhanced_Menu.print_status(f"{browser}: Found {len(cookies)} cookies", "success")
            else:
                Enhanced_Menu.print_status(f"{browser}: No cookies found", "info")
        except PermissionError as error:
            if "admin" in str(error).lower():
                Enhanced_Menu.print_status(f"{browser}: Need admin rights", "warning")
            else:
                Enhanced_Menu.print_status(f"{browser}: Permission denied", "warning")
        except Exception as error:
            Enhanced_Menu.print_status(f"{browser}: {str(error)[:50]}", "error")

    if available:
        Enhanced_Menu.print_status(f"Available cookies from: {', '.join(available)}", "success")
        return True

    Enhanced_Menu.print_status("No browser cookies found for YouTube Music", "error")
    Enhanced_Menu.print_status(
        "Try:\n1. Run as Administrator\n2. Manual export\n3. A different browser", "info")
    return False


# ==================== Extract ====================
def extract_cookies(browser_name: str = "brave") -> Optional[Path]:
    """Extract cookies from the named browser and save them to a file."""
    Enhanced_Menu.print_header(f"Extracting cookies from {browser_name}....")

    sources = cookie_sources()
    if not sources:
        Enhanced_Menu.print_status(
            "browser_cookie3 isn't installed. Install it with: pip install browser-cookie3",
            "error")
        return None

    browser_name = (browser_name or "").strip().lower()
    if browser_name not in sources:
        Enhanced_Menu.print_status("Browser not supported", "error")
        Enhanced_Menu.print_status(f"Available browsers: {', '.join(sources)}", "info")
        return None

    if platform.system() == "Windows" and not check_admin():
        Enhanced_Menu.print_status("Warning: running without admin privileges", "warning")
        Enhanced_Menu.print_status("Cookie extraction may fail. Consider:", "info")
        Enhanced_Menu.print_status("1. Run as Administrator", "info")
        Enhanced_Menu.print_status("2. Use manual export (option in menu)", "info")
        if not Enhanced_Menu.get_input("Continue anyway? (y/n): ", "yn", default=False):
            return None

    domains = ["music.youtube.com", "youtube.com", "open.spotify.com"]
    all_cookies = []
    seen = set()
    for domain in domains:
        try:
            # Get cookies as a list to avoid generator exhaustion
            cookies = list(sources[browser_name](domain_name=domain))
            for cookie in cookies:
                # Keyed on domain + path + name, so two different cookies
                # can't collide the way a name+value-prefix key could.
                key = (cookie.domain, cookie.path or "/", cookie.name)
                if key not in seen:
                    seen.add(key)
                    all_cookies.append(cookie)
            Enhanced_Menu.print_status(f"Found {len(cookies)} cookies for {domain}",
                                       "success" if cookies else "info")
        except PermissionError:
            Enhanced_Menu.print_status(
                f"Permission denied for {domain}: need admin rights", "error")
            return handle_permission_error(browser_name)
        except Exception as error:
            Enhanced_Menu.print_status(
                f"Couldn't get cookies for {domain}: {str(error)[:50]}", "error")

    if not all_cookies:
        Enhanced_Menu.print_status(
            f"No cookies found for YouTube Music in {browser_name}", "info")
        return None

    # Save cookies to file in Netscape format
    cookie_file = COOKIE_DIRECTORY / f"{browser_name}_cookies.txt"
    skipped = 0
    try:
        with open(cookie_file, "w", encoding="utf-8") as handle:
            handle.write(NETSCAPE_HEADER + "\n")
            handle.write("# This file was generated by Music Downloader\n")
            for cookie in all_cookies:
                value = str(cookie.value or "")
                if any(ch in value or ch in cookie.name for ch in "\t\r\n"):
                    skipped += 1        # would break the tab-separated format
                    continue
                # Use the domain exactly as provided by browser_cookie3
                # (may already have a leading dot for domain-wide cookies)
                domain = cookie.domain
                handle.write("\t".join([
                    domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    cookie.path or "/",
                    "TRUE" if cookie.secure else "FALSE",
                    str(int(cookie.expires)) if cookie.expires else "0",
                    cookie.name,
                    value,
                ]) + "\n")
        try:
            os.chmod(cookie_file, 0o600)   # cookies sign in to your account
        except OSError:
            pass
    except OSError as error:
        Enhanced_Menu.print_status(f"Failed to write {cookie_file}: {error}", "error")
        return None

    note = f" ({skipped} unusable skipped)" if skipped else ""
    Enhanced_Menu.print_status(
        f"Successfully extracted {len(all_cookies) - skipped} cookies to {cookie_file}{note}",
        "success")
    set_active(cookie_file)
    return cookie_file


def handle_permission_error(browser_name: str) -> Optional[Path]:
    """Handle permission errors by offering alternatives."""
    Enhanced_Menu.print_section("\nCookie Extraction Failed")
    Enhanced_Menu.print_status("This usually happens because:", "info")
    Enhanced_Menu.print_status("- The browser is running in protected mode", "info")
    Enhanced_Menu.print_status("- Administrator privileges are needed", "info")
    Enhanced_Menu.print_status("- The browser encrypts its cookies", "info")

    print(f"\n{Fore.CYAN}Alternative solutions:{Style.RESET_ALL}")
    print("1. Run this program as Administrator")
    print("2. Use manual cookie export:")
    print("   - Install a 'Get cookies.txt LOCALLY' extension for Chrome/Edge")
    print("   - Export cookies from music.youtube.com")
    print("   - Load the exported file from the menu")
    print(f"3. Try a different browser (not {browser_name})")

    if Enhanced_Menu.get_input("\nTry manual export now? (y/n): ", "yn", default=True):
        return manual_cookie_instructions()
    return None


def manual_cookie_instructions() -> Optional[Path]:
    """Guide the user through a manual cookie export and load the file."""
    Enhanced_Menu.print_section("\nManual Cookie Export Instructions")

    print(f"\n{Fore.YELLOW}For Chrome/Edge/Brave:{Style.RESET_ALL}")
    print("1. Install the 'Get cookies.txt LOCALLY' extension from the Chrome Web Store")
    print("2. Go to https://music.youtube.com")
    print("3. Make sure you're logged in")
    print("4. Click the extension icon, then Export")
    print("5. Save the file into the 'cookies' folder")

    print(f"\n{Fore.YELLOW}For Firefox:{Style.RESET_ALL}")
    print("1. Install the 'cookies.txt' extension")
    print("2. Go to https://music.youtube.com")
    print("3. Click the extension, then Export Cookies")

    print(f"\n{Fore.CYAN}Export in Netscape (cookies.txt) format, not JSON.{Style.RESET_ALL}")

    cookie_path = Enhanced_Menu.get_input(
        "\nEnter path to exported cookie file (or press Enter to skip): ", "str")
    return load_cookies(cookie_path) if cookie_path else None


# ==================== Load & save ====================
def load_cookies(cookie_file: str) -> Optional[Path]:
    """Load cookies from an existing file and make it active."""
    # Drag-and-drop on Windows wraps the path in quotes.
    name = str(cookie_file).strip().strip('"').strip("'")
    cookie_path = Path(name).expanduser()
    if not cookie_path.is_file():
        cookie_path = COOKIE_DIRECTORY / name
    if not cookie_path.is_file():
        Enhanced_Menu.print_status(f"Cookie file not found: {name}", "error")
        return None

    try:
        # Validate file format (simple check)
        with open(cookie_path, "r", encoding="utf-8-sig", errors="replace") as handle:
            content = handle.read(200)
        if "Netscape" not in content and ".youtube.com" not in content:
            Enhanced_Menu.print_status(
                "Warning: this may not be a Netscape-format cookie file", "warning")
            if not Enhanced_Menu.get_input("Continue anyway? (y/n): ", "yn", default=False):
                return None
    except OSError as error:
        Enhanced_Menu.print_status(f"Failed to load cookies: {error}", "error")
        return None

    set_active(cookie_path)
    Enhanced_Menu.print_status(f"Cookies loaded from: {cookie_path}", "info")
    return cookie_path


def save_cookies(name: str = "cookies") -> Optional[Path]:
    """Save the active cookie file to persistent storage, with a warning."""
    active = get_active_cookie_file()
    if not active:
        Enhanced_Menu.print_status("No active cookie file to save", "error")
        return None

    # Security warning
    Enhanced_Menu.print_status(
        "WARNING: cookies are stored in plain text and sign in to your account. "
        "Protect this file.", "warning")
    if not Enhanced_Menu.get_input("Proceed with saving? (y/n): ", "yn", default=True):
        return None

    try:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        save_path = COOKIE_DIRECTORY / f"{name}_{timestamp}.txt"
        shutil.copy2(active, save_path)
        Enhanced_Menu.print_status(f"Cookies saved to: {save_path}", "success")
        return save_path
    except OSError as error:
        Enhanced_Menu.print_status(f"Failed to save cookies: {error}", "error")
        return None


# ==================== List & clear ====================
def list_cookies() -> List[Path]:
    """List all saved cookie files, newest first."""
    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    cookie_files = sorted(COOKIE_DIRECTORY.glob("*.txt"), key=modified, reverse=True)
    if not cookie_files:
        Enhanced_Menu.print_status("No saved cookie files found.", "info")
        return []

    active = get_active_cookie_file()
    Enhanced_Menu.print_status("Saved cookie files:", "info")
    for index, cookie_file in enumerate(cookie_files, 1):
        size = cookie_file.stat().st_size
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(modified(cookie_file)))
        marker = (f" {Fore.GREEN}(active){Style.RESET_ALL}"
                  if active and cookie_file.resolve() == active.resolve() else "")
        print(f"{Fore.YELLOW}[{index}]{Style.RESET_ALL} "
              f"{Fore.CYAN}{cookie_file.name:30}{Style.RESET_ALL}{marker}")
        print(f"     Size: {size} bytes | Modified: {stamp}")
    return cookie_files


def clear_cookies() -> int:
    """Delete all cookie files from the cookie directory. Returns how many went."""
    cookie_files = list(COOKIE_DIRECTORY.glob("*.txt"))
    if not cookie_files:
        Enhanced_Menu.print_status(f"No cookie files found in {COOKIE_DIRECTORY}", "info")
        return 0

    Enhanced_Menu.print_status(f"Found {len(cookie_files)} cookie file(s) to delete:", "info")
    for cookie_file in cookie_files:
        print(f"  - {cookie_file.name}")

    if not Enhanced_Menu.get_input(
            f"\nDelete ALL {len(cookie_files)} cookie files? (y/n): ", "yn", default=False):
        Enhanced_Menu.print_status("Cookie deletion cancelled.", "info")
        return 0

    deleted = 0
    for cookie_file in cookie_files:
        try:
            cookie_file.unlink()
            deleted += 1
            Enhanced_Menu.print_status(f"Deleted: {cookie_file.name}", "success")
        except OSError as error:
            Enhanced_Menu.print_status(f"Failed to delete {cookie_file.name}: {error}", "error")

    if not get_active_cookie_file():
        set_active(None)

    Enhanced_Menu.print_status(
        f"\nDeleted {deleted} cookie file(s) from {COOKIE_DIRECTORY}", "success")
    return deleted


# ==================== Test ====================
def test_cookies(url: str = TEST_URL) -> bool:
    """Test whether the active cookies work by requesting a URL."""
    active = get_active_cookie_file()
    if not active:
        Enhanced_Menu.print_status("No active cookie file to test", "error")
        return False

    try:
        session = requests.Session()
        with open(active, "r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 7:
                    session.cookies.set(name=parts[5], value=parts[6],
                                        domain=parts[0], path=parts[2])

        response = session.get(url, timeout=10)
        if response.status_code == 200:
            Enhanced_Menu.print_status("Cookies work: successfully accessed the URL", "success")
            return True
        Enhanced_Menu.print_status(
            f"Cookies may not work. Status code: {response.status_code}", "error")
        return False
    except (OSError, requests.RequestException) as error:
        Enhanced_Menu.print_status(f"Error testing cookies: {error}", "error")
        return False


# ==================== Menu ====================
def interactive_menu() -> None:
    """Interactive cookie setup menu."""
    while True:
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Cookie Manager Menu",
                                   "A simple program to help manage cookies")

        Enhanced_Menu.print_section("Options:")
        Enhanced_Menu.print_menu_item(1, "Check available browser cookies")
        Enhanced_Menu.print_menu_item(2, "Extract cookies from browser")
        Enhanced_Menu.print_menu_item(3, "List saved cookie files")
        Enhanced_Menu.print_menu_item(4, "Load cookies from file")
        Enhanced_Menu.print_menu_item(5, "Save current cookies")
        Enhanced_Menu.print_menu_item(6, "Clear all cookie files")
        Enhanced_Menu.print_menu_item(7, "Show current cookie status")
        Enhanced_Menu.print_menu_item(8, "Test current cookies")
        Enhanced_Menu.print_menu_item(9, "Return to main menu")

        Enhanced_Menu.print_section("STATUS")
        active = get_active_cookie_file()
        if active:
            Enhanced_Menu.print_status(f"Active cookie file: {active}", "success")
        else:
            Enhanced_Menu.print_status("No active cookie file", "error")

        choice = input("Select option (1-9): ").strip()

        if choice == "1":
            get_status()
        elif choice == "2":
            print(f"\n====={Fore.CYAN}Available Browsers:{Style.RESET_ALL}======")
            browsers = list(cookie_sources())
            for index, browser in enumerate(browsers, 1):
                print(f"{index}. {browser}")
            picked = Enhanced_Menu.get_input(
                "\nSelect browser (name or number): ", "str").strip()
            if picked.isdigit() and 1 <= int(picked) <= len(browsers):
                extract_cookies(browsers[int(picked) - 1])
            elif picked:
                extract_cookies(picked)

            if get_active_cookie_file():
                if Enhanced_Menu.get_input("Save these cookies for future use? (y/n): ",
                                           "yn", default=True):
                    name = Enhanced_Menu.get_input(
                        "Enter name for cookie file (optional): ", "str").strip()
                    save_cookies(name or "cookies")
        elif choice == "3":
            cookie_files = list_cookies()
            if cookie_files:
                picked = Enhanced_Menu.get_input(
                    "\nEnter number to load a cookie file (or press Enter to skip): ", "str")
                if picked.isdigit() and 1 <= int(picked) <= len(cookie_files):
                    load_cookies(str(cookie_files[int(picked) - 1]))
        elif choice == "4":
            path = Enhanced_Menu.get_input("Enter cookie filename or path: ", "str").strip()
            if path:
                load_cookies(path)
        elif choice == "5":
            if get_active_cookie_file():
                name = Enhanced_Menu.get_input("Enter name for cookie file (optional): ",
                                               "str", default="cookies")
                save_cookies(name or "cookies")
            else:
                Enhanced_Menu.print_status("No active cookies to save", "info")
        elif choice == "6":
            clear_cookies()
        elif choice == "7":
            get_status()
            active = get_active_cookie_file()
            if active:
                Enhanced_Menu.print_status(f"Active cookie file: {active.name}", "success")
            else:
                Enhanced_Menu.print_status("No active cookie file", "info")
        elif choice == "8":
            test_cookies()
        elif choice == "9":
            break
        else:
            Enhanced_Menu.print_status("Invalid choice", "info")

        input("\nPress Enter to continue...")