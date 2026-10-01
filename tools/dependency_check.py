import subprocess
import importlib
import shutil
import sys
import os
from utils.logger import log_info, log_success, log_error
from constants import PYTHON_DEPENDENCIES, SYSTEM_DEPENDENCIES

def dependency_check():
    """
    Checks for missing Python and system dependencies.
    Reads from constants + requirements.txt.
    """
    missing_python = []
    missing_system = []

    # --- Check Python dependencies ---
    for dep in PYTHON_DEPENDENCIES:
        try:
            importlib.import_module(dep)
        except ImportError:
            missing_python.append(dep)

    # --- Check system dependencies ---
    for binary in SYSTEM_DEPENDENCIES:
        if not shutil.which(binary):
            missing_system.append(binary)

    # --- Read requirements.txt and double-check ---
    if os.path.exists("requirements.txt"):
        with open("requirements.txt", "r", encoding="utf-8") as f:
            for line in f:
                package = line.strip().split("==")[0]
                if package and package not in PYTHON_DEPENDENCIES:
                    try:
                        importlib.import_module(package)
                    except ImportError:
                        if package not in missing_python:
                            missing_python.append(package)

    # --- Output results ---
    if missing_python:
        log_info("Missing Python dependencies:")
        for dep in missing_python:
            log_error(f" - {dep}")
    else:
        log_success("All Python dependencies are installed.")

    if missing_system:
        log_info("Missing system dependencies:")
        for dep in missing_system:
            log_error(f" - {dep}")
    else:
        log_success("All required system dependencies are installed.")

    return missing_python, missing_system
