"""
The terminal program (python main.py) and this window are two front ends to
the same project: they share the downloader, settings, cookies, logs, history
and retry queue. This file sets the project up exactly as main.py does, then
opens the window instead of the menus.
"""

import sys


def _missing_pyside() -> int:
    print("The window needs PySide6, which isn't installed.\n"
          "Install it with:\n"
          f"    {sys.executable} -m pip install PySide6\n"
          "or use the terminal version:  python main.py")
    return 1


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return _missing_pyside()

    from main import build_downloader            # same setup as the terminal program
    from managers import config_manager, log_manager

    app = QApplication(sys.argv)
    app.setApplicationName("YouTube Music Downloader")

    try:
        downloader = build_downloader()
    except OSError as error:
        QMessageBox.critical(None, "YouTube Music Downloader",
                             f"Couldn't create the download folder:\n{error}\n\n"
                             f"Change output_directory in {config_manager.config_path()} "
                             "and start again.")
        return 1
    except Exception as error:
        log_manager.log_error(f"GUI start-up failed: {type(error).__name__}: {error}",
                              exc_info=True, console=False)
        QMessageBox.critical(None, "YouTube Music Downloader",
                             f"Error starting up:\n{type(error).__name__}: {error}")
        return 1

    from gui.main_window import MainWindow
    window = MainWindow(downloader)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
