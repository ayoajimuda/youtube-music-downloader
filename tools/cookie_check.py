"""Check whether a cookie file will actually sign yt-dlp in to YouTube.

Offline checks (instant):
  - it's a Netscape cookies.txt, not a JSON export
  - it has YouTube cookies, including the sign-in ones yt-dlp needs
  - none of the sign-in cookies have expired, and the file isn't stale
Online check (optional, needs yt-dlp):
  - YouTube accepts them: no "cookies are no longer valid" (rotated) warning
    and no "confirm you're not a bot" check

Usage:
    from tools.cookie_check import check_cookies, cookie_checker
    report = check_cookies()               # the active cookie file
    report = check_cookies("cookies/firefox_cookies.txt", online=False)
    cookie_checker()                       # interactive: pick a file, print the report
"""

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import find_program, run_program

# yt-dlp treats an account as signed in when one of these is present...
API_SID_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")
# ...and these carry the session itself.
SESSION_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID")
STALE_AFTER_DAYS = 14
EXPIRY_WARNING_DAYS = 7
TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"     # "Me at the zoo": public, stable

ROTATED_MARKERS = ("cookies are no longer valid", "have likely been rotated")
BOT_MARKERS = ("sign in to confirm", "not a bot")


@dataclass
class Cookie:
    domain: str
    name: str
    expires: int          # 0 = session cookie


@dataclass
class CookieReport:
    path: Optional[Path]
    problems: List[str] = field(default_factory=list)      # it won't work until these are fixed
    warnings: List[str] = field(default_factory=list)      # it may work, but probably not for long
    info: List[str] = field(default_factory=list)
    youtube_cookies: int = 0
    signed_in: bool = False
    online_checked: bool = False
    online_ok: Optional[bool] = None

    @property
    def ok(self) -> bool:
        return not self.problems


# ==================== Reading ====================
def parse_cookie_file(text: str) -> List[Cookie]:
    cookies = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):            # how exporters mark HttpOnly cookies
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) < 7:
            continue
        try:
            expires = int(float(parts[4] or 0))
        except ValueError:
            expires = 0
        cookies.append(Cookie(parts[0].lower(), parts[5], expires))
    return cookies


def _days(seconds: float) -> str:
    days = seconds / 86400
    return "less than a day" if days < 1 else f"{days:.0f} day{'s' if round(days) != 1 else ''}"


# ==================== Checks ====================
def _offline(path: Path, report: CookieReport) -> None:
    try:
        raw = path.read_bytes()
    except OSError as error:
        report.problems.append(f"Can't read the file: {error}")
        return
    text = raw.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if not stripped:
        report.problems.append("The file is empty.")
        return
    if stripped.startswith(("[", "{")):
        report.problems.append("This is a JSON export. yt-dlp needs the Netscape "
                               "(cookies.txt) format: export again and pick that format.")
        return
    if not stripped.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        report.warnings.append("The first line isn't '# Netscape HTTP Cookie File'. yt-dlp "
                               "may refuse the file.")

    cookies = parse_cookie_file(text)
    youtube = [c for c in cookies if c.domain.lstrip(".").endswith("youtube.com")]
    report.youtube_cookies = len(youtube)
    report.info.append(f"{len(cookies)} cookies, {len(youtube)} for youtube.com")
    if not youtube:
        report.problems.append("No youtube.com cookies. Export while on music.youtube.com "
                               "(or youtube.com) and signed in.")
        return

    by_name: Dict[str, Cookie] = {c.name: c for c in youtube}
    has_api = [n for n in API_SID_COOKIES if n in by_name]
    has_session = [n for n in SESSION_COOKIES if n in by_name]
    report.signed_in = bool(has_api and has_session)
    if not report.signed_in:
        missing = "SAPISID" if not has_api else "SID"
        report.problems.append(f"No sign-in cookies ({missing} is missing): these are "
                               "signed-out cookies. Sign in to YouTube, then export again.")

    now = time.time()
    auth = [by_name[n] for n in has_api + has_session]
    expired = [c.name for c in auth if 0 < c.expires < now]
    if expired:
        report.problems.append(f"Sign-in cookies have expired ({', '.join(expired)}). "
                               "Export fresh cookies.")
    future = [c.expires for c in auth if c.expires > now]
    if future and not expired:
        soonest = min(future) - now
        if soonest < EXPIRY_WARNING_DAYS * 86400:
            report.warnings.append(f"Sign-in cookies expire in {_days(soonest)}.")
        else:
            report.info.append(f"Sign-in cookies expire in {_days(soonest)}")
    if auth and all(c.expires == 0 for c in auth):
        report.warnings.append("The sign-in cookies are session-only (no expiry date); some "
                               "exporters do this and YouTube may not accept them.")

    try:
        age = now - path.stat().st_mtime
    except OSError:
        age = 0
    report.info.append(f"File saved {_days(age)} ago")
    if age > STALE_AFTER_DAYS * 86400:
        report.warnings.append(f"The file is {_days(age)} old. YouTube rotates cookies, so "
                               "older exports often stop working; export fresh ones if "
                               "downloads hit sign-in or bot checks.")


def _online(path: Path, report: CookieReport) -> None:
    ytdlp, where = find_program("yt-dlp")
    if not ytdlp:
        report.info.append(f"Online check skipped: yt-dlp {where}")
        return
    # yt-dlp writes cookies back to the file it's given, so test a copy.
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / "cookies.txt"
        shutil.copyfile(path, copy)
        code, output = run_program(
            [ytdlp, "--cookies", str(copy), "--simulate", "--no-playlist",
             "--print", "id", TEST_VIDEO], timeout=60)
    report.online_checked = True
    low = output.lower()
    if code is None:
        report.warnings.append(f"Online check couldn't run: {output}")
        report.online_checked = False
    elif any(m in low for m in ROTATED_MARKERS):
        report.online_ok = False
        report.problems.append("YouTube says these cookies are no longer valid (rotated). "
                               "Export again from a private/incognito window and close it "
                               "straight after.")
    elif any(m in low for m in BOT_MARKERS):
        report.online_ok = False
        report.problems.append("YouTube still asks for a bot check with these cookies. "
                               "Export fresh ones, or wait a while if you've downloaded a lot.")
    elif code == 0:
        report.online_ok = True
        report.info.append("YouTube accepted the cookies (online check)")
    else:
        report.online_ok = False
        errors = [l for l in output.splitlines() if l.strip().upper().startswith("ERROR")]
        report.warnings.append("Online check failed for another reason: "
                               + (errors[-1] if errors else output.strip()[-200:]))


def check_cookies(path=None, online: bool = True) -> CookieReport:
    """Check a cookie file (the active one if no path is given)."""
    if path is None:
        from managers import cookie_manager
        path = cookie_manager.get_active_cookie_file()
        if path is None:
            report = CookieReport(None)
            report.problems.append("No cookie file is active. Extract or load one in the "
                                   "Cookies menu.")
            return report
    path = Path(path)
    report = CookieReport(path)
    if not path.is_file():
        report.problems.append(f"File not found: {path}")
        return report
    _offline(path, report)
    if online and not any("JSON export" in p or "empty" in p for p in report.problems):
        _online(path, report)
    return report


def print_report(report: CookieReport) -> None:
    if report.path:
        Enhanced_Menu.print_key_value("File", report.path, width=8)
    for line in report.info:
        Enhanced_Menu.print_status(line, "info")
    for line in report.warnings:
        Enhanced_Menu.print_status(line, "warning")
    for line in report.problems:
        Enhanced_Menu.print_status(line, "error")
    print()
    if report.ok and report.online_ok:
        Enhanced_Menu.print_status("These cookies look good.", "success")
    elif report.ok:
        Enhanced_Menu.print_status("No problems found in the file"
                                   + ("" if report.online_checked
                                      else " (YouTube itself wasn't asked)") + ".", "success")
    else:
        Enhanced_Menu.print_status("These cookies won't sign yt-dlp in.", "error")


# ==================== Interactive ====================
def cookie_checker() -> Optional[CookieReport]:
    """Pick a cookie file (the active one first), check it, print the report."""
    import questionary
    from managers import cookie_manager

    active = cookie_manager.get_active_cookie_file()
    folder = Path(cookie_manager.COOKIE_DIRECTORY)
    files = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if folder.is_dir() else []
    if active and all(p.resolve() != active.resolve() for p in files):
        files.insert(0, active)
    if not files:
        Enhanced_Menu.print_status("No cookie files found. Extract or load one in the "
                                   "Cookies menu.", "info")
        return None

    if len(files) == 1:
        path = files[0]
    else:
        choices = [questionary.Choice(f"{p.name}{'  (active)' if active and p.resolve() == active.resolve() else ''}",
                                      value=p) for p in files]
        path = questionary.select("Check which cookie file?", choices=choices + ["Back"]).ask()
        if path in (None, "Back"):
            return None

    online = questionary.confirm("Also ask YouTube whether it accepts them? (needs yt-dlp, "
                                 "takes a few seconds)", default=True).ask()
    if online is None:
        return None
    report = check_cookies(path, online=online)
    print_report(report)
    return report