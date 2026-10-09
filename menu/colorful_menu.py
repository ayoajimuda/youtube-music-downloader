"""Coloured console helpers shared by every menu.

Enhanced_Menu is a namespace of static methods, so callers use it without an
instance: Enhanced_Menu.print_status("Saved", "success").

This module imports nothing from the rest of the project. managers.cookie_manager
imports it, so importing a manager here would create a circular import.
"""

import os
import sys
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Union

from colorama import Fore, Style, init

# NO_COLOR (https://no-color.org) turns colour off; otherwise colorama decides
# (it strips colour automatically when output isn't a terminal).
init(autoreset=True, strip=True if os.environ.get("NO_COLOR") else None)


def _can_print(sample: str) -> bool:
    """False on consoles whose encoding can't show box-drawing characters and icons."""
    try:
        sample.encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE = _can_print("╔═╗║╚╝─✓✗⚠ℹ")

# An item is (label, action) or (label, action, pause_after).
MenuItem = Union[Tuple[str, Callable[[], object]], Tuple[str, Callable[[], object], bool]]

BACK_WORDS = ("0", "b", "back", "q", "quit", "exit")


class Enhanced_Menu:
    """An enhanced menu system for better program interaction"""

    WIDTH = 60

    # Predefined color combinations
    COLORS = {
        'header': f"{Fore.CYAN}{Style.BRIGHT}",
        'title': f"{Fore.MAGENTA}{Style.BRIGHT}",
        'section': f"{Fore.BLUE}{Style.BRIGHT}",
        'menu_item': f"{Fore.YELLOW}",
        'menu_desc': f"{Fore.WHITE}",
        'success': f"{Fore.GREEN}{Style.BRIGHT}",
        'failure': f"{Fore.RED}{Style.BRIGHT}",
        'warning': f"{Fore.LIGHTYELLOW_EX}{Style.BRIGHT}",
        'error': f"{Fore.RED}{Style.BRIGHT}",
        'info': f"{Fore.CYAN}",
        'input': f"{Fore.GREEN}{Style.BRIGHT}",
        'highlight': f"{Fore.YELLOW}{Style.BRIGHT}",
        'dim': f"{Style.DIM}",
    }

    ICONS = {
        "success": "✓" if _UNICODE else "+",
        "failure": "✗" if _UNICODE else "x",
        "error": "✗" if _UNICODE else "x",
        "warning": "⚠" if _UNICODE else "!",
        "info": "ℹ" if _UNICODE else "i",
    }

    # ==================== Output ====================
    @staticmethod
    def clear_screen():
        """Clear the terminal screen. Skipped when output is piped or redirected."""
        try:
            if os.isatty(1):
                os.system('cls' if os.name == 'nt' else 'clear')
        except OSError:
            pass

    @staticmethod
    def print_color(text, color_type='info', bold=False, end='\n'):
        """Print colored text"""
        color_code = Enhanced_Menu.COLORS.get(color_type, Enhanced_Menu.COLORS['info'])
        if bold and Style.BRIGHT not in color_code:
            text = f"{Style.BRIGHT}{text}"
        print(f"{color_code}{text}{Style.RESET_ALL}", end=end)

    @staticmethod
    def print_boxed_title(title, width=WIDTH):
        """Print a title in a decorative box"""
        title = str(title).strip()
        width = max(width, len(title) + 4)        # a long title widens the box
        h, tl, tr, v, bl, br = ("═", "╔", "╗", "║", "╚", "╝") if _UNICODE else \
                               ("=", "+", "+", "|", "+", "+")
        colour = Enhanced_Menu.COLORS['title']
        print(f"{colour}{tl}{h * (width - 2)}{tr}")
        print(f"{colour}{v}{title.center(width - 2)}{v}")
        print(f"{colour}{bl}{h * (width - 2)}{br}{Style.RESET_ALL}")

    @staticmethod
    def print_header(title, subtitle=""):
        """Print a formatted header"""
        print()
        Enhanced_Menu.print_boxed_title(title)
        if subtitle:
            print(f"{Enhanced_Menu.COLORS['info']}{subtitle}{Style.RESET_ALL}")
        print()

    @staticmethod
    def print_section(title, symbol=None):
        """Print a section header"""
        symbol = symbol or ("─" if _UNICODE else "-")
        title = str(title).strip()     # some callers pass "\nTitle"
        colour = Enhanced_Menu.COLORS['section']
        print(f"\n{colour}{symbol * Enhanced_Menu.WIDTH}")
        print(f"{colour}  {title}")
        print(f"{colour}{symbol * Enhanced_Menu.WIDTH}{Style.RESET_ALL}")

    @staticmethod
    def print_menu_item(number, title, description="", indent=2):
        """Print a menu item with number and description"""
        indent_str = " " * indent
        print(f"{indent_str}{Enhanced_Menu.COLORS['menu_item']}[{str(number):>2}]{Style.RESET_ALL} "
              f"{Enhanced_Menu.COLORS['menu_item']}{Style.BRIGHT}{title}{Style.RESET_ALL}")
        if description:
            desc_indent = " " * (indent + 5)
            for line in Enhanced_Menu.wrap_text(str(description), width=50):
                print(f"{desc_indent}{Enhanced_Menu.COLORS['menu_desc']}{line}{Style.RESET_ALL}")

    @staticmethod
    def wrap_text(text, width=50):
        """Wrap text to specified width"""
        words = text.split()
        lines = []
        current_line = []
        current_length = 0
        for word in words:
            if current_line and current_length + len(word) + 1 > width:
                lines.append(" ".join(current_line))
                current_line, current_length = [], 0
            current_line.append(word)
            current_length += len(word) + 1
        if current_line:
            lines.append(" ".join(current_line))
        return lines

    @staticmethod
    def print_status(message: str, status_type: str = "info", icon="",
                     pause_on_error: bool = False):
        """
        Print a status message with appropriate color and icon.

        pause_on_error is off by default: errors inside loops (one per browser,
        one per link) would otherwise stop the program on every line.
        """
        if status_type not in Enhanced_Menu.ICONS:
            status_type = "info"
        colour = Enhanced_Menu.COLORS[status_type]
        icon_to_use = icon or Enhanced_Menu.ICONS[status_type]

        message = str(message)
        leading = ""
        while message.startswith("\n"):          # keep callers' leading blank lines
            leading += "\n"
            message = message[1:]
        lines = message.split("\n")
        print(f"{leading}{colour}{icon_to_use} {lines[0]}{Style.RESET_ALL}")
        for extra in lines[1:]:
            print(f"{colour}  {extra}{Style.RESET_ALL}")

        if status_type in ("error", "failure") and pause_on_error:
            Enhanced_Menu.pause()

    @staticmethod
    def print_key_value(key, value, width: int = 22, note: str = ""):
        """An aligned 'label   value' line, with an optional dim note after it."""
        extra = f"  {Style.DIM}{note}{Style.RESET_ALL}" if note else ""
        print(f"  {Fore.CYAN}{str(key):<{width}}{Style.RESET_ALL} {value}{extra}")

    @staticmethod
    def print_table(headers: Sequence[str], rows: Iterable[Sequence], max_width: int = 40):
        """A plain column table. Long cells are cut to max_width."""
        rows = [[_cut(cell, max_width) for cell in row] for row in rows]
        if not rows:
            return
        widths = [max(len(str(h)), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
        print("  " + Fore.CYAN + "  ".join(f"{h:<{w}}" for h, w in zip(headers, widths)))
        print("  " + Style.DIM + "  ".join("-" * w for w in widths))
        for row in rows:
            print("  " + "  ".join(f"{c:<{w}}" for c, w in zip(row, widths)))

    # ==================== Input ====================
    @staticmethod
    def _prompt_text(prompt, input_type, default) -> str:
        prompt = str(prompt)
        leading = ""
        while prompt.startswith("\n"):
            leading += "\n"
            prompt = prompt[1:]
        # Callers write prompts both with and without a trailing ':'; add exactly one.
        prompt = prompt.rstrip().rstrip(":").rstrip()
        colour, reset = Enhanced_Menu.COLORS['input'], Style.RESET_ALL
        hint = ""
        if input_type == "yn" and default is not None:
            hint = f" [{Fore.YELLOW}{'Y/n' if default else 'y/N'}{reset}]"
        elif default not in (None, ""):
            hint = f" [{Fore.YELLOW}{default}{reset}]"
        return f"{leading}{colour}{prompt}{reset}{hint}{colour}:{reset} "

    @staticmethod
    def get_input(prompt, input_type="int", min_val=None, max_val=None, default=None):
        """
        Get validated user input with colored prompt.

        input_type  "int" / "float"  a number within min_val..max_val
                    "str"            the stripped text ("" if empty and no default; never None)
                    "yn"             True/False

        Enter returns `default` when one is given. End of input (Ctrl-Z / Ctrl-D,
        or a closed pipe) also returns the default. Ctrl-C is passed on to the
        caller as KeyboardInterrupt, so a menu can treat it as "cancel".
        """
        text = Enhanced_Menu._prompt_text(prompt, input_type, default)
        while True:
            try:
                user_input = input(text).strip()
            except EOFError:
                print()
                if input_type == "yn":
                    return bool(default)
                if default is not None:
                    return default
                return "" if input_type == "str" else None

            if not user_input and default is not None:
                return default

            if input_type == "str":
                return user_input

            if input_type == "yn":
                low = user_input.lower()
                if low in ('y', 'yes'):
                    return True
                if low in ('n', 'no'):
                    return False
                Enhanced_Menu.print_status("Please enter 'y' or 'n'", "error")
                continue

            if input_type in ("int", "float"):
                try:
                    value = int(user_input) if input_type == "int" else float(user_input)
                except ValueError:
                    kind = "a whole number" if input_type == "int" else "a number"
                    Enhanced_Menu.print_status(f"Please enter {kind}", "error")
                    continue
                if min_val is not None and value < min_val:
                    Enhanced_Menu.print_status(f"Value must be at least {min_val}", "error")
                    continue
                if max_val is not None and value > max_val:
                    Enhanced_Menu.print_status(f"Value must be at most {max_val}", "error")
                    continue
                return value

            return user_input

    @staticmethod
    def confirm(prompt, default: bool = False) -> bool:
        """Yes/no question; Enter gives `default`."""
        return bool(Enhanced_Menu.get_input(prompt, "yn", default=default))

    @staticmethod
    def pause(message="Press Enter to continue..."):
        try:
            input(f"\n{Fore.YELLOW}  {message}{Style.RESET_ALL}")
        except (EOFError, KeyboardInterrupt):
            print()

    # ==================== Menu loop ====================
    @staticmethod
    def run_menu(title: str, items: List[MenuItem], subtitle: str = "",
                 status: Optional[Callable[[], None]] = None,
                 back_label: str = "Back") -> None:
        """
        Show a numbered menu until the user picks 0 / 'back' (or presses Ctrl-C).

        items   [(label, action), ...] or (label, action, pause_after); actions take
                no arguments. pause_after=False skips the "Press Enter" afterwards,
                for actions that already end with their own prompt.
        status  optional callable that prints a status block above the options

        An action interrupted with Ctrl-C returns to the menu; an action that
        raises is reported rather than crashing the program.
        """
        while True:
            Enhanced_Menu.clear_screen()
            Enhanced_Menu.print_header(title, subtitle)
            if status:
                try:
                    status()
                except Exception as error:      # a broken status line shouldn't block the menu
                    Enhanced_Menu.print_status(f"Couldn't show status: {error}", "error")
            Enhanced_Menu.print_section("Options")
            for number, item in enumerate(items, 1):
                Enhanced_Menu.print_menu_item(number, item[0])
            Enhanced_Menu.print_menu_item(0, back_label)
            print()

            try:
                choice = Enhanced_Menu.get_input(f"Select an option (0-{len(items)})", "str")
            except KeyboardInterrupt:
                print()
                return
            choice = choice.lower()
            if choice in BACK_WORDS:
                return
            if not (choice.isdigit() and 1 <= int(choice) <= len(items)):
                Enhanced_Menu.print_status("Invalid choice", "error")
                Enhanced_Menu.pause()
                continue

            item = items[int(choice) - 1]
            label, action = item[0], item[1]
            pause_after = item[2] if len(item) > 2 else True
            try:
                action()
            except KeyboardInterrupt:
                print()
                Enhanced_Menu.print_status("Cancelled", "warning")
                pause_after = True
            except Exception as error:
                Enhanced_Menu.print_status(f"{label} failed: {type(error).__name__}: {error}",
                                           "error")
                pause_after = True
            if pause_after:
                Enhanced_Menu.pause()


def _cut(value, limit: int) -> str:
    text = str(value if value is not None else "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 3] + "..."