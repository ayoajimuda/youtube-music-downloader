import questionary

from managers.retry_manager import retry_failed
from managers.file_manager import detect_duplicates, organize_files
from menu.colorful_menu import Enhanced_Menu


def management_menu(downloader):
    """
    Displays the Management Menu and routes to the selected function.
    """
    while True:
        choice = questionary.select(
            "🛠 Management Menu — What would you like to do?",
            choices=[
                "Retry failed downloads",
                "Detect duplicates",
                "Organize files by artist/album",
                "Back"
            ]
        ).ask()

        if choice in (None, "Back"):          # None = Ctrl-C
            return

        music_folder = str(downloader.output_directory)
        try:
            if choice == "Retry failed downloads":
                retry_failed()

            elif choice == "Detect duplicates":
                detect_duplicates(music_folder)

            elif choice == "Organize files by artist/album":
                organize_files(music_folder)

        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Cancelled", "warning")
        except Exception as error:            # report it; stay in the menu
            Enhanced_Menu.print_status(f"{choice} failed: {type(error).__name__}: {error}", "error")


def run(downloader):
    """Entry point for main.py."""
    management_menu(downloader)