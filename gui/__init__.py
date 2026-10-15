"""The desktop window for YouTube Music Downloader (PySide6).

    gui/
      main_window.py   the window: sidebar + pages
      styles.py        colours, stylesheet, appearance settings
      views/           the pages (Home, Batch, History, Settings, Debug, About)
      workers/         background jobs (fetch, download, batch queue, retry, updates)
      dialogs/         pop-ups (messages, cookie file, dependency check)
      resources/       icons, including the app icon (resources/icons/app/)

Start it with:  python gui_main.py
"""

APP_NAME = "YouTube Music Downloader"
APP_VERSION = "1.0.0"
