"""Cookie handling for YoutubeMusicDownloader.

The downloader needs one thing from a cookie file: YouTube account cookies in
the Netscape format yt-dlp reads. There is a single active cookie file, and
the choice is remembered between runs in cookies/active_cookie.json.

Details that come from yt-dlp's own source and matter here:

- yt-dlp refuses a cookie file unless its first line matches
  "# Netscape HTTP Cookie File" exactly, capitalisation included.
- It treats an account as signed in only when LOGIN_INFO is present together
  with one of the SAPISID cookies. YouTube clears LOGIN_INFO when it rotates
  cookies, so a file can look complete and still be signed out.
- It rewrites its --cookies file in place when it exits. Two concurrent yt-dlp
  processes sharing one file can therefore truncate it under each other, so
  every download gets its own copy (begin_run / end_run).
- It cannot sign in to YouTube with a username and password; cookies are the
  only way to authenticate.
"""

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from colorama import init, Fore, Style

try:
    import browser_cookie3
except ImportError:          # extraction is optional; file-based cookies still work
    browser_cookie3 = None

from .EnhancedMenu import Enhanced_Menu

try:
    from .RateLimiter import youtube_limiter
except Exception:
    youtube_limiter = None

init(autoreset=True)

COOKIE_DIRECTORY = "cookies"
ACTIVE_STATE_FILE = "active_cookie.json"
RUNS_DIRECTORY = ".runs"            # per-download copies, inside the cookie folder
STALE_RUN_SECONDS = 24 * 3600

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
NETSCAPE_MAGIC = re.compile(r"#( Netscape)? HTTP Cookie File")
HTTPONLY_PREFIX = "#HttpOnly_"
LOGIN_COOKIE = "LOGIN_INFO"
SAPISID_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")
LOGIN_COOKIES = (LOGIN_COOKIE,) + SAPISID_COOKIES

COOKIE_HOWTO_URL = "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
LOCALLY_EXTENSION_URL = ("https://chromewebstore.google.com/detail/get-cookiestxt-locally/"
                         "cclelndahbckbenkjhflpdbgdldlbecc")

# The liked-videos feed. yt-dlp won't open it without a signed-in session, so
# it shows whether YouTube still accepts the cookies without downloading anything.
VERIFY_TARGET = ":ytfav"

BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "opera_gx", "chromium", "safari")
CHROMIUM_BROWSERS = {"chrome", "edge", "brave", "opera", "opera_gx", "chromium"}


# ==================== File helpers ====================
def _is_youtube_domain(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return d == "youtube.com" or d.endswith(".youtube.com")


def _write_private(path: Path, data) -> None:
    """
    Atomic write with owner-only permissions on POSIX.

    mkstemp creates the temp file as 0600, and os.replace keeps that mode, so
    the cookie file is never briefly readable by other users.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class CookieReport:
    """What a cookie file holds, judged the way yt-dlp will judge it."""
    path: Path
    readable: bool = False
    header_ok: bool = False
    json_format: bool = False
    entries: int = 0
    youtube_entries: int = 0
    bad_lines: int = 0
    signed_in: bool = False
    login_expired: bool = False
    login_expires: Optional[float] = None    # earliest expiry of the login cookies; None = session
    error: str = ""

    @property
    def repairable(self) -> bool:
        """Valid entries, but a header yt-dlp won't accept."""
        return self.readable and not self.json_format and not self.header_ok and self.entries > 0

    @property
    def usable(self) -> bool:
        return self.readable and self.header_ok and self.signed_in

    def summary(self) -> str:
        if not self.readable:
            return f"unreadable ({self.error})"
        if self.json_format:
            return "JSON export - yt-dlp needs the Netscape (cookies.txt) format"
        if not self.entries:
            return "no cookies in file"
        parts = [f"{self.youtube_entries} YouTube cookie(s)"]
        if not self.header_ok:
            parts.append("header yt-dlp rejects (repairable)")
        if self.signed_in:
            when = (time.strftime("%Y-%m-%d", time.localtime(self.login_expires))
                    if self.login_expires else "end of session")
            parts.append(f"signed in (login cookies last until {when})")
        elif self.login_expired:
            parts.append("login cookies have expired")
        else:
            parts.append("not signed in")
        return ", ".join(parts)


def inspect_cookie_file(path) -> CookieReport:
    """Read a cookie file with the same rules yt-dlp's loader applies."""
    report = CookieReport(Path(path))
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        report.error = str(e)
        return report
    report.readable = True

    stripped = text.lstrip()
    if stripped and stripped[0] in "[{":
        report.json_format = True
        return report

    lines = text.splitlines()
    report.header_ok = bool(lines) and bool(NETSCAPE_MAGIC.search(lines[0]))

    now = time.time()
    live_names = set()
    login_expiries: List[float] = []
    saw_expired_login = False

    for line in lines:
        if line.startswith(HTTPONLY_PREFIX):
            line = line[len(HTTPONLY_PREFIX):]
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7 or (fields[4] and not re.fullmatch(r"\d+(?:\.\d+)?", fields[4])):
            report.bad_lines += 1
            continue
        domain, _, _, _, expires, name, _ = fields
        report.entries += 1
        if not _is_youtube_domain(domain):
            continue
        report.youtube_entries += 1

        expiry = float(expires) if expires else 0.0
        if expiry and expiry < now:
            # yt-dlp won't send an expired cookie, so it doesn't count.
            saw_expired_login = saw_expired_login or name in LOGIN_COOKIES
            continue
        live_names.add(name)
        if name in LOGIN_COOKIES and expiry:
            login_expiries.append(expiry)

    report.signed_in = LOGIN_COOKIE in live_names and any(n in live_names for n in SAPISID_COOKIES)
    report.login_expired = saw_expired_login and not report.signed_in
    if report.signed_in and login_expiries:
        report.login_expires = min(login_expiries)
    return report


class CookieManager:
    """Manages cookies for authentication"""

    def __init__(self):
        self.cookie_directory = Path(COOKIE_DIRECTORY)
        self.cookie_directory.mkdir(exist_ok=True)
        self.current_cookie_file: Optional[Path] = None

        # Guards the active file: the downloader reads and writes it from
        # several worker threads at once.
        self._lock = threading.RLock()
        self._report_cache: Dict[str, Tuple[Tuple[int, int], CookieReport]] = {}

        self.cookie_sources: Dict[str, Callable] = {}
        if browser_cookie3 is not None:
            for name in BROWSERS:
                func = getattr(browser_cookie3, name, None)
                if func is not None:
                    self.cookie_sources[name] = func

        self.is_admin = self._check_admin()
        self._restore_active()
        self._clean_stale_runs()

    # ==================== Active cookie file ====================
    def _state_path(self) -> Path:
        return self.cookie_directory / ACTIVE_STATE_FILE

    def _restore_active(self) -> None:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            path = Path(data.get("path", ""))
            if data.get("path") and path.is_file():
                self.current_cookie_file = path
        except (OSError, ValueError):
            pass

    def set_active(self, path: Optional[Path]) -> None:
        """Make `path` the cookie file every download uses, and remember it."""
        with self._lock:
            self.current_cookie_file = Path(path).resolve() if path else None
            try:
                if self.current_cookie_file:
                    _write_private(self._state_path(),
                                   json.dumps({"path": str(self.current_cookie_file)}, indent=2))
                elif self._state_path().exists():
                    self._state_path().unlink()
            except OSError as e:
                Enhanced_Menu.print_status(f"Could not remember the active cookie file: {e}", "warning")

    def get_active_cookie_file(self) -> Optional[Path]:
        path = self.current_cookie_file
        return path if path and path.is_file() else None

    def report(self, path: Path) -> CookieReport:
        """inspect_cookie_file, cached until the file changes."""
        path = Path(path)
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return inspect_cookie_file(path)
        cached = self._report_cache.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
        result = inspect_cookie_file(path)
        self._report_cache[str(path)] = (key, result)
        return result

    def _in_cookie_directory(self, path: Path) -> bool:
        try:
            return Path(path).resolve().parent == self.cookie_directory.resolve()
        except OSError:
            return False

    def repair_cookie_file(self, path: Path, quiet: bool = False) -> Optional[Path]:
        """
        Give a file the header yt-dlp insists on.

        A file inside the cookie folder is fixed in place. A file elsewhere
        (say, in Downloads) is left alone and a fixed copy goes into the
        cookie folder. Files written by the earlier version of this class had
        "# Netscape HTTP cookie file" in lowercase, which yt-dlp rejects.
        """
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            target = (path if self._in_cookie_directory(path)
                      else self.cookie_directory / f"{path.stem}_fixed.txt")
            _write_private(target, NETSCAPE_HEADER + "\n" + text)
        except OSError as e:
            if not quiet:
                Enhanced_Menu.print_status(f"Could not repair {path.name}: {e}", "error")
            return None
        if not quiet:
            where = "in place" if target == path else f"as {target}"
            Enhanced_Menu.print_status(f"Repaired the cookie file header {where}", "success")
        return target

    def auto_select(self) -> Optional[Path]:
        """With nothing active, adopt the newest signed-in file in the cookie folder."""
        candidates = sorted(self.cookie_directory.glob("*.txt"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for candidate in candidates:
            rep = self.report(candidate)
            if rep.repairable:
                fixed = self.repair_cookie_file(candidate, quiet=True)
                rep = self.report(fixed) if fixed else rep
                candidate = fixed or candidate
            if rep.usable:
                self.set_active(candidate)
                return self.current_cookie_file
        return None

    def prepare_for_ytdlp(self) -> Tuple[Optional[Path], Optional[CookieReport]]:
        """
        The cookie file to hand yt-dlp, and its report.

        Returns (None, report) when the active file can't be used at all (JSON
        export, unreadable), and (None, None) when there is no file.
        """
        with self._lock:
            path = self.get_active_cookie_file() or self.auto_select()
            if path is None:
                return None, None
            rep = self.report(path)
            if rep.repairable:
                fixed = self.repair_cookie_file(path, quiet=True)
                if fixed:
                    if fixed != path:
                        self.set_active(fixed)
                    path, rep = fixed, self.report(fixed)
            if not rep.readable or rep.json_format or not rep.header_ok:
                return None, rep
            return path, rep

    # ==================== Per-download copies ====================
    def _runs_directory(self) -> Path:
        runs = self.cookie_directory / RUNS_DIRECTORY
        runs.mkdir(exist_ok=True)
        return runs

    def _clean_stale_runs(self) -> None:
        """Remove copies left behind by a crash (they contain live session cookies)."""
        runs = self.cookie_directory / RUNS_DIRECTORY
        if not runs.is_dir():
            return
        cutoff = time.time() - STALE_RUN_SECONDS
        for leftover in runs.glob("*"):
            try:
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
            except OSError:
                pass

    def begin_run(self, cookie_file) -> Optional[Path]:
        """
        A private copy of the cookie file for one yt-dlp process.

        yt-dlp rewrites its --cookies file in place on exit. With several
        downloads running, one process can empty the shared file while another
        is still reading it, and that download then fails with "does not look
        like a Netscape format cookies file". Returns None if no copy could be
        made; the caller should then pass the original file.
        """
        try:
            fd, tmp = tempfile.mkstemp(prefix="run_", suffix=".txt", dir=self._runs_directory())
            with self._lock:
                data = Path(cookie_file).read_bytes()
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return Path(tmp)
        except OSError:
            return None

    def end_run(self, run_copy, cookie_file) -> None:
        """
        Fold what yt-dlp saved back into the real file, then drop the copy.

        yt-dlp stores refreshed cookies when it exits, which is how a cookie
        file stays alive in normal use. The copy is only written back when it
        is still a valid, signed-in file, so a run that YouTube signed out
        can't overwrite good cookies.
        """
        run_copy, target = Path(run_copy), Path(cookie_file)
        try:
            with self._lock:
                data = run_copy.read_bytes()
                try:
                    unchanged = data == target.read_bytes()
                except OSError:
                    unchanged = False
                if not unchanged:
                    rep = inspect_cookie_file(run_copy)
                    if rep.header_ok and rep.signed_in:
                        _write_private(target, data)
        except OSError:
            pass
        finally:
            try:
                run_copy.unlink()
            except OSError:
                pass

    # ==================== yt-dlp ====================
    def verify_with_ytdlp(self, cookie_file=None, timeout: int = 90) -> Tuple[str, str]:
        """
        Ask yt-dlp whether YouTube still accepts the cookies.

        Returns (verdict, detail). verdict is one of:
        "valid", "rotated", "signed_out", "bad_file", "no_file", "no_ytdlp", "error".
        """
        path = Path(cookie_file) if cookie_file else self.get_active_cookie_file()
        if not path or not path.is_file():
            return "no_file", "No active cookie file"

        rep = self.report(path)
        if rep.repairable:
            fixed = self.repair_cookie_file(path, quiet=True)
            if fixed:
                path, rep = fixed, self.report(fixed)
        if not rep.readable or rep.json_format or not rep.header_ok:
            return "bad_file", rep.summary()
        if not rep.signed_in:
            # No point asking YouTube: yt-dlp won't even try to sign in.
            return "signed_out", rep.summary()

        if not shutil.which("yt-dlp"):
            return "no_ytdlp", "yt-dlp not found in PATH"

        if youtube_limiter is not None:
            youtube_limiter.acquire()

        run_copy = self.begin_run(path)
        command = ["yt-dlp", "--cookies", str(run_copy or path), "--simulate",
                   "--flat-playlist", "--playlist-items", "1", VERIFY_TARGET]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return "error", f"yt-dlp did not answer within {timeout}s"
        except OSError as e:
            return "error", f"Could not run yt-dlp: {e}"
        finally:
            if run_copy:
                self.end_run(run_copy, path)

        output = f"{result.stdout}\n{result.stderr}"
        low = output.lower()
        if "cookies are no longer valid" in low:
            return "rotated", ("YouTube has rotated these cookies, so they no longer sign you in. "
                               "Export a fresh copy with the private-window method.")
        if "does not look like a netscape" in low or "must be netscape formatted" in low:
            return "bad_file", "yt-dlp could not read the file as Netscape cookies"
        if "login details are needed" in low:
            return "signed_out", "YouTube did not accept the login cookies"
        if "not a bot" in low:
            return "error", "YouTube wants bot verification even with these cookies - wait and retry"
        if result.returncode == 0:
            return "valid", f"YouTube accepted the cookies ({rep.summary()})"
        errors = [l for l in output.splitlines() if l.strip().upper().startswith("ERROR:")]
        return "error", (errors[-1][:200] if errors else f"yt-dlp exited with code {result.returncode}")

    def get_arguments_ytdlp(self) -> List[str]:
        """Get yt-dlp cookie arguments if cookies are available."""
        path, _ = self.prepare_for_ytdlp()
        return ["--cookies", str(path)] if path else []

    # ==================== Browsers ====================
    def _check_admin(self) -> bool:
        """Check if script is running with admin privileges on Windows"""
        if platform.system() == "Windows":
            try:
                return ctypes.windll.shell32.IsUserAnAdmin() != 0
            except Exception:
                return False
        return True

    def _require_browser_support(self) -> bool:
        if self.cookie_sources:
            return True
        Enhanced_Menu.print_status(
            "Reading cookies straight from a browser needs browser_cookie3: pip install browser_cookie3",
            "error")
        Enhanced_Menu.print_status("The manual export works without it.", "info")
        return False

    def get_status(self):
        """Check which browsers hold YouTube cookies, and whether they're signed in."""
        Enhanced_Menu.print_header("Checking available browser cookies....")
        if not self._require_browser_support():
            return False

        available_browsers = []
        for browser, cookie_func in self.cookie_sources.items():
            try:
                # "youtube.com" also matches music.youtube.com. Filtering on
                # "music.youtube.com" alone misses the login cookies, which
                # sit on .youtube.com.
                cookies = list(cookie_func(domain_name="youtube.com"))
                names = {c.name for c in cookies if _is_youtube_domain(c.domain)}
                if not names:
                    Enhanced_Menu.print_status(f"• {browser}: No YouTube cookies", "info")
                    continue
                available_browsers.append(browser)
                signed_in = LOGIN_COOKIE in names and any(n in names for n in SAPISID_COOKIES)
                state = "signed in" if signed_in else "not signed in"
                Enhanced_Menu.print_status(f"✓ {browser}: {len(cookies)} cookies, {state}",
                                           "success" if signed_in else "warning")
            except PermissionError:
                Enhanced_Menu.print_status(f"⚠️ {browser}: Permission denied (close the browser and retry)", "warning")
            except Exception as e:
                Enhanced_Menu.print_status(f"• {browser}: {str(e)[:60]}", "info")

        if available_browsers:
            Enhanced_Menu.print_status(f"✅ YouTube cookies found in: {', '.join(available_browsers)}", "success")
            return True
        Enhanced_Menu.print_status("❌ No browser cookies found for YouTube", "error")
        Enhanced_Menu.print_status("Use the manual export (private-window method) instead.", "info")
        return False

    def extract_cookies(self, browser_name: str = 'firefox') -> Optional[Path]:
        """Extract YouTube cookies from a browser into a yt-dlp-readable file."""
        Enhanced_Menu.print_header(f"Extracting cookies from {browser_name}....")
        if not self._require_browser_support():
            return None

        browser_name = (browser_name or "").strip().lower()
        if browser_name not in self.cookie_sources:
            Enhanced_Menu.print_status("Browser not supported", "error")
            Enhanced_Menu.print_status(f"Available browsers: {', '.join(self.cookie_sources)}", "info")
            return None

        if platform.system() == "Windows" and browser_name in CHROMIUM_BROWSERS:
            Enhanced_Menu.print_status(
                "Recent Chrome-based browsers on Windows encrypt cookies in a way outside tools "
                "often can't read. If this fails, use Firefox or the manual export.", "warning")

        try:
            cookies = list(self.cookie_sources[browser_name](domain_name="youtube.com"))
        except PermissionError:
            Enhanced_Menu.print_status("Permission denied reading the browser's cookie store", "error")
            return self._handle_permission_error(browser_name)
        except Exception as e:
            Enhanced_Menu.print_status(f"Failed to read cookies: {str(e)[:120]}", "error")
            return self._handle_permission_error(browser_name)

        now = time.time()
        unique = {}
        skipped = 0
        for cookie in cookies:
            if not _is_youtube_domain(cookie.domain):
                continue
            if cookie.expires and cookie.expires < now:
                continue
            value = str(cookie.value or "")
            if any(ch in value or ch in cookie.name for ch in "\t\r\n"):
                skipped += 1            # would break the tab-separated format
                continue
            # Keyed on domain + path + name. The old name+value-prefix key could
            # drop a real cookie that happened to share a value prefix.
            unique[(cookie.domain, cookie.path or "/", cookie.name)] = cookie

        if not unique:
            Enhanced_Menu.print_status(
                f"No YouTube cookies in {browser_name}. Sign in to music.youtube.com there first.", "info")
            return None

        lines = [NETSCAPE_HEADER,
                 "# Written by Music Downloader. Treat this file like a password:",
                 "# it signs in to your Google account.",
                 ""]
        for cookie in unique.values():
            lines.append("\t".join([
                cookie.domain,
                "TRUE" if cookie.domain.startswith(".") else "FALSE",
                cookie.path or "/",
                "TRUE" if cookie.secure else "FALSE",
                str(int(cookie.expires)) if cookie.expires else "0",
                cookie.name,
                str(cookie.value or ""),
            ]))

        cookie_file = self.cookie_directory / f"{browser_name}_cookies.txt"
        try:
            _write_private(cookie_file, "\n".join(lines) + "\n")
        except OSError as e:
            Enhanced_Menu.print_status(f"Could not write {cookie_file}: {e}", "error")
            return None

        rep = inspect_cookie_file(cookie_file)
        note = f" ({skipped} unusable skipped)" if skipped else ""
        Enhanced_Menu.print_status(f"Saved {len(unique)} cookies to {cookie_file}{note}", "success")
        if rep.signed_in:
            Enhanced_Menu.print_status(
                "Signed-in session found. YouTube rotates cookies in open browser tabs, which can "
                "sign this copy out later - if that happens, use the private-window export.", "info")
        else:
            Enhanced_Menu.print_status(
                f"{browser_name} has no signed-in YouTube session, so age-restricted and "
                "members-only content still won't download.", "warning")

        self.set_active(cookie_file)
        return cookie_file

    def _handle_permission_error(self, browser_name: str) -> Optional[Path]:
        """Explain why reading the browser failed and offer the manual route."""
        Enhanced_Menu.print_section("\n🔧 Cookie Extraction Failed")
        Enhanced_Menu.print_status("This usually happens because:", "info")
        Enhanced_Menu.print_status("• The browser is open and has its cookie database locked", "info")
        Enhanced_Menu.print_status("• The browser encrypts cookies in a way outside tools can't read", "info")
        Enhanced_Menu.print_status("• The program lacks permission to read the browser profile", "info")

        print(f"\n{Fore.CYAN}Alternative solutions:{Style.RESET_ALL}")
        print("1. Close the browser completely and try again")
        print("2. Try Firefox, which outside tools can usually read")
        print("3. Use the manual export (recommended - it also avoids cookie rotation)")
        print("4. Run this program as Administrator")

        if Enhanced_Menu.get_input("\nShow the manual export steps now? (y/n): ", "yn", default=True):
            return self.manual_cookie_instructions()
        return None

    def manual_cookie_instructions(self) -> Optional[Path]:
        """Guide the user through a safe manual export, then load the file."""
        Enhanced_Menu.print_section("\n📋 Manual Cookie Export (private-window method)")
        print("This follows yt-dlp's guide for YouTube cookies:")
        print(f"  {COOKIE_HOWTO_URL}")

        print(f"\n{Fore.YELLOW}1.{Style.RESET_ALL} Open a private/incognito window and sign in to YouTube there.")
        print(f"{Fore.YELLOW}2.{Style.RESET_ALL} In that window, keep just one tab on https://music.youtube.com")
        print(f"{Fore.YELLOW}3.{Style.RESET_ALL} Export the youtube.com cookies in Netscape (cookies.txt) format, not JSON:")
        print(f"     • Chrome / Edge / Brave: \"Get cookies.txt LOCALLY\"")
        print(f"       {LOCALLY_EXTENSION_URL}")
        print(f"     • Firefox: \"cookies.txt\"")
        print("     The extension has to be allowed in private windows.")
        print(f"{Fore.YELLOW}4.{Style.RESET_ALL} Close the private window straight away. Don't sign out first.")
        print("     YouTube rotates cookies in open sessions; closing the window keeps the export valid.")

        print(f"\n{Fore.RED}⚠ Avoid the older \"Get cookies.txt\" extension (without LOCALLY).{Style.RESET_ALL}")
        print("  It was reported as malware and removed from the Chrome Web Store.")
        print("  If you have it installed, remove it.")

        cookie_path = Enhanced_Menu.get_input(
            "\nEnter path to exported cookie file (or press Enter to skip): ", "str")
        if cookie_path:
            return self.load_cookies(cookie_path)
        return None

    # ==================== Files ====================
    def load_cookies(self, cookie_file: str) -> Optional[Path]:
        """Load cookies from a file, check them, and make them active."""
        # Drag-and-drop on Windows wraps the path in quotes.
        name = str(cookie_file).strip().strip('"').strip("'")
        cookie_path = Path(name).expanduser()
        if not cookie_path.is_file():
            cookie_path = self.cookie_directory / name
        if not cookie_path.is_file():
            Enhanced_Menu.print_status(f"Cookie file not found: {name}", "failure")
            return None

        rep = inspect_cookie_file(cookie_path)
        if not rep.readable:
            Enhanced_Menu.print_status(f"Failed to load cookies: {rep.error}", "failure")
            return None
        if rep.json_format:
            Enhanced_Menu.print_status(
                "That is a JSON export. Export again choosing the Netscape / cookies.txt format.", "failure")
            return None
        if not rep.entries:
            Enhanced_Menu.print_status("No cookies found in that file.", "failure")
            return None
        if rep.repairable:
            fixed = self.repair_cookie_file(cookie_path)
            if not fixed:
                return None
            cookie_path, rep = fixed, inspect_cookie_file(fixed)

        Enhanced_Menu.print_status(f"{cookie_path.name}: {rep.summary()}",
                                   "success" if rep.signed_in else "warning")
        if not rep.signed_in:
            Enhanced_Menu.print_status(
                "Without a signed-in session these cookies won't help with age-restricted, "
                "members-only or bot-check errors.", "warning")
            if not Enhanced_Menu.get_input("Use this file anyway? (y/n): ", "yn", default=False):
                return None

        self.set_active(cookie_path)
        Enhanced_Menu.print_status(f"Active cookie file: {self.current_cookie_file}", "info")
        return self.current_cookie_file

    def save_cookies(self, name: str = "cookies") -> Optional[Path]:
        """Save a timestamped copy of the active cookie file."""
        active = self.get_active_cookie_file()
        if not active:
            Enhanced_Menu.print_status("No active cookie file to save", "error")
            return None

        Enhanced_Menu.print_status(
            "⚠️  WARNING: Cookies are stored in plain text and sign in to your Google account. "
            "Protect this file.", "warning")
        if not Enhanced_Menu.get_input("Proceed with saving? (y/n): ", "yn", default=True):
            return None

        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            save_path = self.cookie_directory / f"{name}_{timestamp}.txt"
            _write_private(save_path, active.read_bytes())
            Enhanced_Menu.print_status(f"Cookies saved to: {save_path}", "success")
            return save_path
        except Exception as e:
            Enhanced_Menu.print_status(f"Failed to save cookies: {e}", "error")
            return None

    def list_cookies(self) -> List[Path]:
        """List saved cookie files with what each one holds."""
        cookie_files = sorted(self.cookie_directory.glob("*.txt"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
        if not cookie_files:
            Enhanced_Menu.print_status("No saved cookie files found.", "error")
            return []

        active = self.get_active_cookie_file()
        Enhanced_Menu.print_status("Saved cookie files:", "info")
        for i, cookie_file in enumerate(cookie_files, 1):
            mod_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(cookie_file.stat().st_mtime))
            marker = f" {Fore.GREEN}(active){Style.RESET_ALL}" if active and cookie_file.resolve() == active else ""
            print(f"{Fore.YELLOW}[{i}]{Style.RESET_ALL} {Fore.CYAN}{cookie_file.name:30}{Style.RESET_ALL}{marker}")
            print(f"     {self.report(cookie_file).summary()} | Modified: {mod_time}")
        return cookie_files

    def clear_cookies(self):
        """Delete all cookie files from the main cookie directory."""
        try:
            cookie_files = list(self.cookie_directory.glob("*.txt"))
            if not cookie_files:
                Enhanced_Menu.print_status(f"No cookie files found in {self.cookie_directory}", "info")
                return

            Enhanced_Menu.print_status(f"Found {len(cookie_files)} cookie file(s) to delete:", "info")
            for cookie_file in cookie_files:
                print(f"  - {cookie_file.name}")

            if not Enhanced_Menu.get_input(
                    f"\nAre you sure you want to delete ALL {len(cookie_files)} cookie files? (y/n): ",
                    "yn", default=False):
                Enhanced_Menu.print_status("Cookie deletion cancelled.", "failure")
                return

            deleted_count = 0
            for cookie_file in cookie_files:
                try:
                    cookie_file.unlink()
                    deleted_count += 1
                    Enhanced_Menu.print_status(f"Deleted: {cookie_file.name}", "success")
                except Exception as e:
                    Enhanced_Menu.print_status(f"Failed to delete {cookie_file.name}: {e}", "failure")

            if not self.get_active_cookie_file():
                self.set_active(None)
            self._report_cache.clear()

            Enhanced_Menu.print_status(
                f"\nSuccessfully deleted {deleted_count} cookie file(s) from {self.cookie_directory}", "success")
        except Exception as e:
            Enhanced_Menu.print_status(f"Error clearing cookies: {e}", "error")

    def test_cookies(self, url=None) -> bool:
        """Check the active cookies with yt-dlp. `url` is accepted for compatibility and unused."""
        verdict, detail = self.verify_with_ytdlp()
        if verdict == "valid":
            Enhanced_Menu.print_status(f"✅ {detail}", "success")
            return True
        Enhanced_Menu.print_status(f"❌ {detail}", "error")
        if verdict in ("rotated", "signed_out"):
            Enhanced_Menu.print_status("Use the manual export (private-window method) to get cookies that last.", "info")
        return False

    def ytdlp_auth(self) -> bool:
        """
        yt-dlp can't sign in to YouTube with a username and password - it
        prints "Login with password is not supported for YouTube" - so this
        verifies the cookies instead of asking for a password.
        """
        Enhanced_Menu.print_header("\n🎵 Checking YouTube sign-in...")
        Enhanced_Menu.print_status(
            "YouTube sign-in works through cookies only; no password is needed or used.", "info")
        return self.test_cookies()

    # ==================== Menu ====================
    def _menu_extract(self):
        if not self._require_browser_support():
            return
        print(f"\n====={Fore.CYAN}Available Browsers:{Style.RESET_ALL}======")
        browsers = list(self.cookie_sources)
        for i, browser in enumerate(browsers, 1):
            print(f"{i}. {browser}")
        choice = (Enhanced_Menu.get_input("\nSelect browser (name or number): ", "str") or "").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(browsers):
            choice = browsers[int(choice) - 1]
        if choice:
            self.extract_cookies(choice)

    def _menu_list(self):
        cookie_files = self.list_cookies()
        if cookie_files:
            load_choice = (Enhanced_Menu.get_input(
                "\nEnter number to make that file active (or press Enter to skip): ", "str") or "").strip()
            if load_choice.isdigit() and 0 < int(load_choice) <= len(cookie_files):
                self.load_cookies(str(cookie_files[int(load_choice) - 1]))

    def _menu_load(self):
        cookie_file = (Enhanced_Menu.get_input("Enter cookie filename or path: ", "str") or "").strip()
        if cookie_file:
            self.load_cookies(cookie_file)

    def _menu_save(self):
        if not self.get_active_cookie_file():
            Enhanced_Menu.print_status("No active cookies to save", "info")
            return
        name = (Enhanced_Menu.get_input("Enter name for cookie file (optional): ", "str") or "").strip()
        self.save_cookies(name or "cookies")

    def interactive_menu(self):
        """Interactive cookie setup menu."""
        items = [
            ("Check browsers for YouTube cookies", self.get_status),
            ("Extract cookies from a browser", self._menu_extract),
            ("Manual export (recommended: private-window method)", self.manual_cookie_instructions),
            ("List saved cookie files / choose active", self._menu_list),
            ("Load cookies from a file", self._menu_load),
            ("Verify active cookies with yt-dlp", self.test_cookies),
            ("Save a timestamped copy of the active cookies", self._menu_save),
            ("Delete all cookie files", self.clear_cookies),
        ]
        while True:
            Enhanced_Menu.clear_screen()
            Enhanced_Menu.print_header("🍪 Cookie Manager Menu", "A simple program to help manage cookies")

            Enhanced_Menu.print_section("Options:")
            for number, (label, _) in enumerate(items, 1):
                Enhanced_Menu.print_menu_item(number, label)
            back = len(items) + 1
            Enhanced_Menu.print_menu_item(back, "Return to main menu")

            Enhanced_Menu.print_section("STATUS")
            active = self.get_active_cookie_file()
            if active:
                rep = self.report(active)
                Enhanced_Menu.print_status(f"Active cookie file: {active}", "success" if rep.usable else "warning")
                Enhanced_Menu.print_status(rep.summary(), "info")
            else:
                Enhanced_Menu.print_status("No active cookie file", "error")

            choice = input(f"Select option (1-{back}): ").strip()
            if choice == str(back):
                break
            if choice.isdigit() and 1 <= int(choice) <= len(items):
                items[int(choice) - 1][1]()
            else:
                Enhanced_Menu.print_status("Invalid choice", "info")
            input("\nPress Enter to continue...")