"""Choose the audio format, with the active one listed first."""

import questionary

from managers import config_manager, log_manager
from menu.colorful_menu import Enhanced_Menu


def choose_audio_format(downloader=None):
    """Let the user pick an audio format. Saves it, and applies it to the downloader if given."""
    current = config_manager.load()["audio_format"]
    formats = [current] + [f for f in config_manager.VALID_FORMATS if f != current]
    choices = [questionary.Choice(f"{fmt} (active)" if fmt == current else fmt, value=fmt)
               for fmt in formats]

    new_format = questionary.select("Select audio format:", choices=choices).ask()
    if not new_format or new_format == current:        # None = Ctrl-C
        return None

    ok, message = config_manager.update_config("audio_format", new_format)
    if not ok:
        Enhanced_Menu.print_status(f"Failed to update audio format: {message}", "error")
        return None
    if downloader is not None:
        downloader.audio_format = new_format
    log_manager.log_info(f"Audio format changed: {current} -> {new_format}", console=False)
    Enhanced_Menu.print_status(f"Audio format updated to: {new_format}", "success")
    return new_format