"""FFmpeg path detection and configuration utility."""

import os
import sys
import shutil


def get_ffmpeg_path() -> str:
    """
    Get FFmpeg executable path, preferring bundled version.

    Detection order:
    1. PyInstaller bundle (sys._MEIPASS/bin/ffmpeg.exe)
    2. Local bin/ folder (development)
    3. System PATH

    Returns:
        Path to ffmpeg executable

    Raises:
        FileNotFoundError: If ffmpeg is not found anywhere
    """
    # Check for bundled FFmpeg in PyInstaller bundle
    if getattr(sys, 'frozen', False):
        bundle_dir = sys._MEIPASS
        if sys.platform == 'win32':
            bundled = os.path.join(bundle_dir, 'bin', 'ffmpeg.exe')
        else:
            bundled = os.path.join(bundle_dir, 'bin', 'ffmpeg')
        if os.path.exists(bundled):
            return bundled

    # Check local bin folder (development mode)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == 'win32':
        local = os.path.join(project_root, 'bin', 'ffmpeg.exe')
    else:
        local = os.path.join(project_root, 'bin', 'ffmpeg')
    if os.path.exists(local):
        return os.path.abspath(local)

    # Fall back to system PATH
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        return system_ffmpeg

    raise FileNotFoundError(
        "FFmpeg not found. Please install FFmpeg or place ffmpeg.exe in the bin/ folder."
    )


def configure_ffmpeg_path():
    """
    Configure environment for yt-dlp to find FFmpeg.

    Adds the FFmpeg directory to PATH so yt-dlp subprocess calls can find it.

    Returns:
        Path to ffmpeg executable

    Raises:
        FileNotFoundError: If ffmpeg is not found
    """
    ffmpeg_path = get_ffmpeg_path()
    ffmpeg_dir = os.path.dirname(ffmpeg_path)

    # Add FFmpeg directory to PATH for subprocess calls
    current_path = os.environ.get('PATH', '')
    if ffmpeg_dir not in current_path:
        os.environ['PATH'] = ffmpeg_dir + os.pathsep + current_path

    return ffmpeg_path


def check_ffmpeg_available() -> tuple[bool, str]:
    """
    Check if FFmpeg is available.

    Returns:
        Tuple of (is_available, message)
    """
    try:
        path = get_ffmpeg_path()
        return True, f"FFmpeg found at: {path}"
    except FileNotFoundError as e:
        return False, str(e)
