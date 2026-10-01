import subprocess
import platform
import os
from constants import LOG_FILE

def open_log():
    """
    Opens the app.log file
    """
    if not os.path.exists(LOG_FILE):
        print(f"Log file '{LOG_FILE}' does not exist.")
        return

    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(LOG_FILE)
        elif system == "Darwin":  # macOS
            subprocess.run(["open", LOG_FILE])
        else:  # Linux and others
            subprocess.run(["xdg-open", LOG_FILE])
    except Exception as e:
        print(f"Failed to open log file: {e}")