"""Colours, the Qt stylesheet, and the GUI's own appearance settings.

Appearance (theme mode, accent, font) is GUI-only, so it lives in its own file,
config/gui_settings.json, next to the downloader's config. Download settings
are NOT kept here: they come from managers.config_manager, so the GUI and the
terminal program always agree.
"""

import json
from pathlib import Path

from managers import config_manager

GUI_SETTINGS_FILE = config_manager.APP_DIR / "config" / "gui_settings.json"

PALETTES = {
    "Dark": dict(bg="#0a0a0a", sidebar="#0d0d0d", surface="#161616", surface_alt="#1e1e1e",
                 hover="#262626", border="#2a2a2a", text="#ffffff", text_dim="#b3b3b3",
                 text_faint="#707070"),
    "Light": dict(bg="#f4f4f5", sidebar="#ffffff", surface="#ffffff", surface_alt="#f0f0f0",
                  hover="#e8e8e8", border="#e2e2e2", text="#18181b", text_dim="#52525b",
                  text_faint="#a1a1aa"),
}

ACCENTS = {
    "Red": "#ff0033", "Rose": "#f43f5e", "Amber": "#f59e0b",
    "Emerald": "#1DB954", "Blue": "#3b82f6", "Purple": "#8b5cf6", "Cyan": "#06b6d4",
}

# Status colours used in tables (downloaded / skipped / failed).
STATUS_COLOURS = {"downloaded": "#22c55e", "skipped": "#a1a1aa", "failed": "#ef4444",
                  "downloading": None}        # None = the accent colour


# ==================== Colour helpers ====================
def _hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def adjust(h: str, f: float) -> str:
    """Lighten (f > 0) or darken (f < 0) a colour."""
    r, g, b = _hex_rgb(h)
    if f >= 0:
        r, g, b = (int(r + (255 - r) * f), int(g + (255 - g) * f), int(b + (255 - b) * f))
    else:
        f = -f
        r, g, b = (int(r * (1 - f)), int(g * (1 - f)), int(b * (1 - f)))
    return f"#{r:02x}{g:02x}{b:02x}"


def rgba(h: str, a: float) -> str:
    r, g, b = _hex_rgb(h)
    return f"rgba({r},{g},{b},{a})"


def on_colour(h: str) -> str:
    """Black or white, whichever reads better on top of `h`."""
    r, g, b = _hex_rgb(h)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if lum > 0.6 else "#ffffff"


# ==================== Appearance settings ====================
class GuiSettings:
    """theme_mode / accent / font, saved to config/gui_settings.json."""

    DEFAULTS = dict(theme_mode="Dark", accent="Red", font="Segoe UI")

    def __init__(self, path: Path = GUI_SETTINGS_FILE):
        self.path = Path(path)
        self.data = dict(self.DEFAULTS)
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self.data.update({k: v for k, v in stored.items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError:
            return False

    def reset(self) -> None:
        self.data = dict(self.DEFAULTS)


# ==================== Theme ====================
class Theme:
    def __init__(self, mode: str = "Dark", accent: str = "Red", font: str = "Segoe UI"):
        self.mode = mode if mode in PALETTES else "Dark"
        self.accent_name = accent if accent in ACCENTS else "Red"
        self.font = font

    @classmethod
    def from_settings(cls, settings: GuiSettings) -> "Theme":
        return cls(settings["theme_mode"], settings["accent"], settings["font"])

    @property
    def c(self) -> dict:
        return PALETTES[self.mode]

    @property
    def accent(self) -> str:
        return ACCENTS[self.accent_name]

    @property
    def on_accent(self) -> str:
        return on_colour(self.accent)

    @property
    def accent_soft(self) -> str:
        return rgba(self.accent, 0.16)

    def status_colour(self, status: str) -> str:
        return STATUS_COLOURS.get(status) or self.accent

    def qss(self) -> str:
        c = self.c
        accent = self.accent
        on_accent = self.on_accent
        accent_hover = adjust(accent, 0.12 if self.mode == "Dark" else -0.10)
        accent_soft = self.accent_soft
        return f"""
        * {{ font-family: "{self.font}"; color: {c['text']}; outline: 0; }}
        QWidget#Root {{ background: {c['bg']}; }}
        QWidget#Sidebar {{ background: {c['sidebar']}; border-right: 1px solid {c['border']}; }}
        QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: 0; }}
        QLabel {{ background: transparent; }}
        QLabel#H1 {{ font-size: 30px; font-weight: 800; }}
        QLabel#H2 {{ font-size: 22px; font-weight: 700; }}
        QLabel#H3 {{ font-size: 15px; font-weight: 700; }}
        QLabel#Sub {{ color: {c['text_dim']}; font-size: 13px; }}
        QLabel#Faint {{ color: {c['text_faint']}; font-size: 12px; }}
        QLabel#Accent {{ color: {accent}; font-weight: 700; }}
        QLabel#Warning {{ color: #f59e0b; font-size: 12px; }}
        QFrame[card="true"] {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 14px; }}
        QFrame#Cover {{ background: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 12px; }}
        QFrame#DropZone {{ background: {c['surface']}; border: 2px dashed {c['border']}; border-radius: 14px; }}
        QFrame#DropZone:hover {{ border: 2px dashed {c['text_faint']}; }}
        QFrame#DropZoneActive {{ background: {accent_soft}; border: 2px dashed {accent}; border-radius: 14px; }}
        QLabel#StatValue {{ font-size: 22px; font-weight: 800; }}
        QLabel#StatLabel {{ color: {c['text_faint']}; font-size: 10px; font-weight: 700; letter-spacing: 1px; }}
        QLabel#SectionTitle {{ font-size: 16px; font-weight: 800; }}
        QLabel#Dot {{ font-size: 16px; }}
        QPlainTextEdit#Paste {{ font-family: "{self.font}"; font-size: 13px; }}
        QPushButton[variant="danger"] {{ background: transparent; color: #ef4444; border: 1px solid {c['border']}; }}
        QPushButton[variant="danger"]:hover {{ background: {rgba('#ef4444', 0.12)}; }}
        QStatusBar {{ background: {c['sidebar']}; border-top: 1px solid {c['border']}; }}
        QStatusBar QLabel {{ color: {c['text_dim']}; font-size: 12px; padding: 0 8px; }}
        QStatusBar::item {{ border: 0; }}
        QFrame#Divider {{ background: {c['border']}; max-height: 1px; min-height: 1px; border: 0; }}
        QLineEdit, QSpinBox {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px;
                     padding: 10px 12px; selection-background-color: {accent}; selection-color: {on_accent}; }}
        QLineEdit:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
        QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: 0; }}
        QComboBox {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px;
                     padding: 8px 12px; min-height: 18px; }}
        QComboBox:hover {{ border: 1px solid {accent}; }}
        QComboBox::drop-down {{ border: 0; width: 22px; }}
        QComboBox QAbstractItemView {{ background: {c['surface_alt']}; border: 1px solid {c['border']};
                     selection-background-color: {accent}; selection-color: {on_accent}; padding: 4px; }}
        QPushButton {{ background: {c['surface_alt']}; border: 1px solid {c['border']}; border-radius: 10px;
                       padding: 9px 14px; font-weight: 600; }}
        QPushButton:hover {{ background: {c['hover']}; }}
        QPushButton:disabled {{ color: {c['text_faint']}; }}
        QPushButton[variant="accent"] {{ background: {accent}; color: {on_accent}; border: 0; }}
        QPushButton[variant="accent"]:hover {{ background: {accent_hover}; }}
        QPushButton[variant="accent"]:disabled {{ background: {c['surface_alt']}; color: {c['text_faint']}; }}
        QPushButton[variant="ghost"] {{ background: transparent; border: 0; }}
        QPushButton[variant="ghost"]:hover {{ background: {c['hover']}; }}
        QPushButton[variant="dlbtn"] {{ background: {accent}; border: 0; border-radius: 8px; padding: 6px; }}
        QPushButton[variant="dlbtn"]:hover {{ background: {accent_hover}; }}
        QPushButton[seg="true"] {{ background: transparent; border: 0; border-bottom: 2px solid transparent;
                       border-radius: 0; color: {c['text_dim']}; padding: 8px 6px; font-weight: 700; }}
        QPushButton[seg="true"]:checked {{ color: {c['text']}; border-bottom: 2px solid {accent}; }}
        QToolButton#Nav {{ background: transparent; border: 0; border-radius: 12px; padding: 9px; }}
        QToolButton#Nav:hover {{ background: {c['hover']}; }}
        QToolButton#Nav:checked {{ background: {accent_soft}; }}
        QTableWidget {{ background: transparent; border: 0; }}
        QHeaderView {{ background: transparent; border: 0; }}
        QTableCornerButton::section {{ background: transparent; border: 0; }}
        QHeaderView::section {{ background: transparent; color: {c['text_faint']}; border: 0;
                       border-bottom: 1px solid {c['border']}; padding: 8px 6px; font-weight: 700; }}
        QTableWidget::item {{ border-bottom: 1px solid {c['border']}; padding: 8px 6px; }}
        QTableWidget::item:selected {{ background: {accent_soft}; color: {c['text']}; }}
        QProgressBar {{ background: {c['surface_alt']}; border: 0; border-radius: 6px; max-height: 8px;
                       min-height: 8px; text-align: center; color: transparent; }}
        QProgressBar::chunk {{ background: {accent}; border-radius: 6px; }}
        QPlainTextEdit {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 12px;
                       font-family: "Consolas","DejaVu Sans Mono","monospace"; font-size: 12px; padding: 8px; }}
        QDialog, QMessageBox {{ background: {c['bg']}; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 30px; }}
        QScrollBar::handle:vertical:hover {{ background: {c['text_faint']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QScrollBar:horizontal {{ height: 0; }}
        """
