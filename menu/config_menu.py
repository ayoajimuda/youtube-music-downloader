"""Config menu: view, change, validate and reset the downloader's settings.

Everything is read and written through managers.config_manager, so this menu
always shows the same settings the downloader uses.

Usage:
    from menu import config_menu
    config_menu.run(downloader)            # from main_menu; changes apply to the downloader
    config = config_menu.config_menu(config)   # older style: pass a dict, get the updated one
"""

from typing import Any, Dict, Optional

import questionary

from managers import config_manager
from config import (
    CONFIG_SCHEMA, PROFILE_DESCRIPTIONS, apply_config_profile, get_config_profile,
    list_profiles, load_config, reset_to_defaults, settings_by_group, update_config,
    validate_config,
)
from managers import log_manager
from menu.colorful_menu import Enhanced_Menu

BACK = "Back"


# ==================== Helpers ====================
def _ok(message: str) -> None:
    Enhanced_Menu.print_status(message, "success")
    log_manager.log_info(f"Config: {message}", console=False)    # audit trail in info.log


def _fail(message: str) -> None:
    Enhanced_Menu.print_status(message, "error")


def _pause() -> None:
    Enhanced_Menu.pause()


def _show(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "✓ Enabled" if value else "✗ Disabled"
    if value == "" and CONFIG_SCHEMA.get(key, {}).get("must_exist"):
        return "(use the one on PATH)"
    return str(value)


def _label(key: str) -> str:
    return CONFIG_SCHEMA[key].get("label", key)


# ==================== Main loop ====================
def config_menu(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Display the configuration menu and handle user selections.
    Returns the updated config dict.
    """
    config = config if config is not None else load_config()
    while True:
        profile = get_config_profile(config)
        choice = questionary.select(
            f"⚙️ Config Menu (profile: {profile}) — What would you like to do?",
            choices=[
                "View current config",
                "Update a setting",
                "Switch config profile",
                "Toggle features on/off",
                "Reset to defaults",
                "Validate configuration",
                BACK,
            ],
        ).ask()

        # ask() returns None on Ctrl-C; treat it like Back.
        if choice in (None, BACK):
            break
        if choice == "View current config":
            view_config(config)
        elif choice == "Update a setting":
            config = update_setting_menu(config)
        elif choice == "Switch config profile":
            config = switch_profile_menu(config)
        elif choice == "Toggle features on/off":
            config = toggle_features_menu(config)
        elif choice == "Reset to defaults":
            config = reset_config_menu(config)
        elif choice == "Validate configuration":
            validate_config_menu()
    return config


def run(downloader=None) -> Dict[str, Any]:
    """Entry point for main_menu: show the menu, then apply the result to the downloader."""
    config = config_menu(load_config())
    if downloader is not None:
        from tools import ask_user_preference as preferences
        if preferences.is_configured():
            preferences.apply(downloader, config)
    return config


# ==================== Screens ====================
def view_config(config: Dict[str, Any]) -> None:
    """Display the current configuration, grouped, with defaults beside changed values."""
    defaults = config_manager.defaults()
    print("\n" + "=" * 60)
    print(f"📋 Current Configuration  (profile: {get_config_profile(config)})")
    print("=" * 60)

    width = max(len(_label(k)) for k in CONFIG_SCHEMA)
    for group, keys in settings_by_group().items():
        print(f"\n{group}:")
        for key in keys:
            value = config.get(key)
            note = "" if value == defaults.get(key) else f"   (default: {_show(key, defaults[key])})"
            print(f"  {_label(key):<{width}}  {_show(key, value)}{note}")

    print(f"\nConfig file: {config_manager.config_path()}")
    print("=" * 60)
    _pause()


def _ask_value(key: str, current: Any):
    """Prompt for a new value in the form that suits the setting. None = cancelled."""
    rules = CONFIG_SCHEMA[key]
    if rules.get("choices"):
        return questionary.select(
            f"New value for {_label(key)}:",
            choices=list(rules["choices"]),
            default=current if current in rules["choices"] else None,
        ).ask()
    if rules["type"] is bool:
        return questionary.confirm(f"Enable {_label(key)}?", default=bool(current)).ask()
    if rules["type"] is int:
        low, high = rules["min"], rules["max"]

        def valid(text: str):
            _, error = config_manager.check_value(key, text)
            return True if error is None else f"Must be a whole number from {low} to {high}"

        return questionary.text(f"New value for {_label(key)} ({low}-{high}):",
                                default=str(current), validate=valid).ask()
    if rules.get("path"):
        hint = " (blank = find it on PATH)" if rules.get("must_exist") else ""
        return questionary.path(f"New value for {_label(key)}{hint}:",
                                default=str(current or ""),
                                only_directories=key == "output_directory").ask()
    return questionary.text(f"New value for {_label(key)}:", default=str(current or "")).ask()


def update_setting_menu(config: Dict[str, Any]) -> Dict[str, Any]:
    """Menu to update individual settings."""
    choices = [questionary.Choice(f"{_label(k)}: {_show(k, config.get(k))}", value=k)
               for k in CONFIG_SCHEMA]
    choices.append(questionary.Choice(BACK, value=BACK))
    key = questionary.select("Select setting to update:", choices=choices).ask()
    if key in (None, BACK):
        return config

    current = config.get(key)
    print(f"\nCurrent value: {_show(key, current)}")
    new_value = _ask_value(key, current)
    if new_value is None:
        return config                       # cancelled

    if key == "output_directory":
        # Create it now, so a typo shows up here rather than on the next download.
        clean, error = config_manager.check_value(key, new_value)
        if error is None:
            try:
                config_manager.resolve_path(clean).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                _fail(f"Can't create {clean}: {error}")
                return config

    success, message = update_config(key, new_value)
    if success:
        _ok(message)
        config = load_config()
    else:
        _fail(message)
    return config


def switch_profile_menu(config: Dict[str, Any]) -> Dict[str, Any]:
    """Menu to switch between configuration profiles."""
    profiles = list_profiles()
    current = get_config_profile(config)

    print("\n📋 Available Profiles:\n")
    for name, settings in profiles.items():
        marker = " (current)" if name == current else ""
        print(f"  {name}{marker}: {PROFILE_DESCRIPTIONS.get(name, '')}")
        for key, value in settings.items():
            print(f"    - {_label(key)}: {value}")
        print()
    if current == "custom":
        print("  Your settings don't match any profile right now (custom).\n")

    choice = questionary.select("Select profile to apply:",
                                choices=list(profiles) + [BACK]).ask()
    if choice in (None, BACK):
        return config

    if questionary.confirm(f"Apply '{choice}' profile? This will update several settings.",
                           default=True).ask():
        success, message = apply_config_profile(choice)
        if success:
            _ok(message)
            config = load_config()
        else:
            _fail(message)
    return config


def toggle_features_menu(config: Dict[str, Any]) -> Dict[str, Any]:
    """Menu to toggle on/off settings."""
    switches = [k for k, rules in CONFIG_SCHEMA.items() if rules["type"] is bool]
    while True:
        choices = [questionary.Choice(f"{'✓' if config.get(k) else '✗'} {_label(k)}", value=k)
                   for k in switches]
        choices.append(questionary.Choice(BACK, value=BACK))
        key = questionary.select("Toggle features:", choices=choices).ask()
        if key in (None, BACK):
            break
        new_value = not config.get(key, False)
        success, message = update_config(key, new_value)
        if success:
            config = load_config()
            _ok(f"{_label(key)} {'enabled' if new_value else 'disabled'}")
        else:
            _fail(message)
    return config


def reset_config_menu(config: Dict[str, Any]) -> Dict[str, Any]:
    """Menu to reset configuration to defaults."""
    if questionary.confirm("⚠️ Reset all settings to defaults? This cannot be undone.",
                           default=False).ask():
        success, message = reset_to_defaults()
        if success:
            _ok(message)
            config = load_config()
        else:
            _fail(message)
    return config


def validate_config_menu() -> None:
    """Check the config file as it is on disk and list any problems."""
    is_valid, errors = validate_config()

    print("\n" + "=" * 60)
    print("🔍 Configuration Validation")
    print("=" * 60)
    if is_valid:
        Enhanced_Menu.print_status("Configuration is valid!", "success")
    else:
        Enhanced_Menu.print_status("Configuration has problems:", "error")
        for error in errors:
            print(f"  ✗ {error}")
        print("\nInvalid values are replaced with their defaults while the program runs.")
        print("Fix them with 'Update a setting', or 'Reset to defaults'.")
    print("=" * 60)
    _pause()