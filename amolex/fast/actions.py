"""Turns a fast command into a key press, click or scroll.

pydirectinput sends DirectInput scan codes, which games read; pyautogui handles
scrolling and keys pydirectinput does not know (media keys).

Warning: synthetic input can be flagged by anti-cheat in online games.
Use fast commands in single-player / offline games only.
"""

from __future__ import annotations

import logging
import threading
import time

import pyautogui
import pydirectinput

from ..config import FastCommand

log = logging.getLogger(__name__)

pydirectinput.PAUSE = 0
pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False  # mouse may sit in a corner during games


def _known(key: str) -> bool:
    return key in getattr(pydirectinput, "KEYBOARD_MAPPING", {})


def _press(key: str, hold_ms: int) -> None:
    if not _known(key):
        pyautogui.press(key)
        return
    if hold_ms > 0:
        pydirectinput.keyDown(key)
        time.sleep(hold_ms / 1000)
        pydirectinput.keyUp(key)
    else:
        pydirectinput.press(key)


def execute(cmd: FastCommand) -> None:
    """Run the action. Holds happen on a thread so the audio loop never waits."""
    if cmd.action == "click":
        pydirectinput.click(button=cmd.button)
    elif cmd.action == "key" and cmd.key:
        if cmd.hold_ms > 0:
            threading.Thread(target=_press, args=(cmd.key, cmd.hold_ms), daemon=True).start()
        else:
            _press(cmd.key, 0)
    elif cmd.action == "hotkey" and cmd.keys:
        pyautogui.hotkey(*cmd.keys)
    elif cmd.action == "scroll":
        pyautogui.scroll(cmd.amount)
    else:
        log.warning("command %r has nothing to do", cmd.phrase)
        return
    log.info("fast: %s -> %s", cmd.phrase, cmd.action)
