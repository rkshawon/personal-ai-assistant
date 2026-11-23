"""Logging to console and logs/amolex.log, plus a small latency timer."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from . import paths


def setup(verbose: bool = False) -> None:
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file = RotatingFileHandler(paths.LOG_DIR / "amolex.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]
    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def timed(label: str, logger: logging.Logger):
    """Log how long a block took, e.g. `with timed("whisper", log): ...`."""
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.info("timing %s %.0f ms", label, (time.perf_counter() - start) * 1000)
