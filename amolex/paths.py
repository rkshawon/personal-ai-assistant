"""Where Amolex keeps things.

Code lives in the project folder (E:\\Amolex). Everything installed or downloaded
(Python, packages, models) lives under AMOLEX_HOME (default E:\\ProgramData\\Amolex),
so nothing is written to C: or the user profile.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
HOME_DIR = Path(os.environ.get("AMOLEX_HOME", r"E:\ProgramData\Amolex"))

MODELS_DIR = HOME_DIR / "models"
VOSK_DIR = MODELS_DIR / "vosk"
WHISPER_DIR = MODELS_DIR / "whisper"
TEMP_DIR = HOME_DIR / "temp"
LOG_DIR = PROJECT_DIR / "logs"

CONFIG_FILE = PROJECT_DIR / "config.json"
COMMANDS_FILE = PROJECT_DIR / "commands.json"


def expand(path: str) -> Path:
    """Expand %VARS% and ~ in a configured path."""
    return Path(os.path.expandvars(os.path.expanduser(path)))


def ensure_dirs() -> None:
    for d in (MODELS_DIR, VOSK_DIR, WHISPER_DIR, TEMP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
