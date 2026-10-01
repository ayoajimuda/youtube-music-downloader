import questionary
from downloader.retry_manager import retry_failed
from managers.file_manager import detect_duplicates, organize_files
from downloader.metadata import embed_metadata

def management_menu(config):
    """
    Displays the Management Menu and routes to the selected function.
    """
    choice = questionary.select(
        "🛠 Management Menu — What would you like to do?",
        choices=[
            "Retry failed downloads",
            "Detect duplicates",
            "Organize files by artist/album",
            "Embed metadata in MP3s",
            "Back"
        ]
    ).ask()

    if choice == "Retry failed downloads":
        retry_failed(config)

    elif choice == "Detect duplicates":
        detect_duplicates(config["output_dir"])

    elif choice == "Organize files by artist/album":
        organize_files(config["output_dir"])

    elif choice == "Embed metadata in MP3s":
        embed_metadata(config["output_dir"])
