"""Retry manager: A manager handles the retry function that retries failing downloads and failed links in the retry queue """

import datetime
import json
import os
import random
import threading
import time
from pathlib import Path
import batch_downloader
from managers import log_manager
from assist_methods import cleanup_directory
from typing import Dict, Iterable, List, Tuple
from colorama import Fore, Style

THROTTLE_STREAK_LIMIT = 3 # A link that has failed this 
GIVE_UP_AFTER = 5 # A link that has failed this often without ever being throttled is probably dead (private, deleted, region-locked) rather than unlucky.


_lock = threading.Lock()
_downloader = None
_cookies = None

_state = {
    "path": Path("history/retry_queue.json"),
    "backoff_base": 300,
    "backoff_max": 1800,
}
