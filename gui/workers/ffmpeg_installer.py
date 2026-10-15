"""Download FFmpeg (Windows) into the program's bin/ folder.

It saves ffmpeg.exe and ffprobe.exe to <app folder>/bin and points the
ffmpeg_path setting there, so the downloader, the dependency check and the
terminal program all find it. A packaged build looks in that folder anyway.

On macOS and Linux the system package manager is the better way, so the
worker explains that instead of downloading anything.
"""

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Signal

from managers import config_manager

FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")
INSTALL_DIR = config_manager.APP_DIR / "bin"
MANUAL = {
    "darwin": "Install it with Homebrew:\n    brew install ffmpeg",
    "linux": "Install it with your package manager, for example:\n    sudo apt install ffmpeg",
}


class FFmpegInstallWorker(QThread):
    progress = Signal(int, str)          # percent, message
    finished_install = Signal(bool, str)

    def __init__(self, install_dir: Path = INSTALL_DIR):
        super().__init__()
        self.install_dir = Path(install_dir)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if sys.platform != "win32":
            key = "darwin" if sys.platform == "darwin" else "linux"
            self.finished_install.emit(False, "Automatic installation is only available on "
                                       "Windows.\n" + MANUAL[key])
            return

        temp = Path(tempfile.mkdtemp())
        archive = temp / "ffmpeg.zip"
        try:
            self.progress.emit(2, "Connecting…")
            response = urlopen(Request(FFMPEG_URL, headers={"User-Agent": "Mozilla/5.0"}),
                               timeout=30)
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with open(archive, "wb") as handle:
                while True:
                    if self._cancelled:
                        self.finished_install.emit(False, "Cancelled.")
                        return
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    handle.write(block)
                    done += len(block)
                    if total:
                        self.progress.emit(5 + int(done * 75 / total),
                                           f"Downloading… {done / 1048576:.0f} / "
                                           f"{total / 1048576:.0f} MB")

            self.progress.emit(85, "Extracting…")
            self.install_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as zf:
                wanted = [n for n in zf.namelist()
                          if n.lower().endswith(("/ffmpeg.exe", "/ffprobe.exe"))]
                if not wanted:
                    self.finished_install.emit(False, "ffmpeg.exe wasn't in the download.")
                    return
                for name in wanted:
                    target = self.install_dir / Path(name).name
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            if not (self.install_dir / "ffmpeg.exe").is_file():
                self.finished_install.emit(False, "Installation couldn't be verified.")
                return
            config_manager.update(ffmpeg_path=str(self.install_dir))
            self.progress.emit(100, "Installed.")
            self.finished_install.emit(True, f"FFmpeg installed to {self.install_dir}")
        except URLError as error:
            self.finished_install.emit(False, f"Download failed: {error.reason}")
        except zipfile.BadZipFile:
            self.finished_install.emit(False, "The download was damaged. Try again.")
        except PermissionError:
            self.finished_install.emit(False, f"No permission to write to {self.install_dir}")
        except Exception as error:
            self.finished_install.emit(False, f"Installation failed: {error}")
        finally:
            shutil.rmtree(temp, ignore_errors=True)
