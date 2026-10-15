"""Small message boxes: info, warning, error, and yes/no questions."""

from PySide6.QtWidgets import QMessageBox


def info(parent, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def warning(parent, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def error(parent, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def confirm(parent, title: str, text: str, default_yes: bool = True) -> bool:
    default = QMessageBox.Yes if default_yes else QMessageBox.No
    answer = QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No, default)
    return answer == QMessageBox.Yes
