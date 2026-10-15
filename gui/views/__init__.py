"""The pages of the main window."""

from gui.views.about_view import LINKS, AboutView
from gui.views.batch_view import BatchView
from gui.views.debug_view import DebugView
from gui.views.history_view import HistoryView
from gui.views.home_view import HomeView
from gui.views.settings_view import SettingsView
from gui.views.widgets import Card, DropZone, NavButton, StatCard

__all__ = ["AboutView", "BatchView", "DebugView", "HistoryView", "HomeView", "SettingsView",
           "Card", "DropZone", "NavButton", "StatCard", "LINKS"]
