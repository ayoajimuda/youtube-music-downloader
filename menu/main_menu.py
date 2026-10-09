import questionary

def main_menu():
    """
    Displays the main menu and returns the user's choice.
    """
    return questionary.select(
        "🎵 Welcome to HARMONI Music Downloader — Select an option:",
        choices=[
            "Downloads Menu",
            "Management Menu",
            "Tools Menu",
            "Config Menu",
            "Cookies Menu",
            "Exit"
        ]
    ).ask()