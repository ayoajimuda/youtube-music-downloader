import json
import os
from utils.logger import log_info, log_success, log_error

def playlist_to_tracklist(config):
    """
    Converts playlists.json into a flat track list JSON.
    """
    try:
        playlists_file = config.get("playlists_file", "data/playlists.json")

        if not os.path.exists(playlists_file):
            log_error(f"Playlists file not found: {playlists_file}")
            return

        with open(playlists_file, "r", encoding="utf-8") as f:
            playlists_data = json.load(f)

        tracks = []
        for playlist in playlists_data.get("playlists", []):
            for item in playlist.get("items", []):
                track_info = item.get("track")
                if track_info:
                    tracks.append({
                        "artist": track_info.get("artistName"),
                        "album": track_info.get("albumName"),
                        "track": track_info.get("trackName"),
                        "uri": track_info.get("trackUri")
                    })

        export_dir = "export"
        os.makedirs(export_dir, exist_ok=True)

        export_path = os.path.join(export_dir, "playlist_tracklist.json")
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump({"tracks": tracks}, f, indent=2, ensure_ascii=False)

        log_success(f"Playlist track list saved to {export_path}")

    except Exception as e:
        log_error(f"Failed to convert playlist to tracklist: {e}")
