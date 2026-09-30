"""Retry manager: A manager handles the retry function that retries failing downloads and failed links in the retry queue """

import datetime
import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from colorama import Fore, Style

import assist_methods

THROTTLE_STREAK_LIMIT = 3

def _on_error(self, message: str) -> None:
    try:
        self. 