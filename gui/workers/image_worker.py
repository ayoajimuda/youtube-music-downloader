"""Download a cover image in the background."""

import urllib.request

from PySide6.QtCore import QThread, Signal


class ImageWorker(QThread):
    loaded = Signal(bytes)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        if not self.url:
            return
        try:
            request = urllib.request.Request(self.url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                self.loaded.emit(response.read())
        except Exception:
            pass                       # no cover is fine; the placeholder stays
