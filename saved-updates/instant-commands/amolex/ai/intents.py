"""Instant commands: common requests handled directly, without asking the AI.

On RK's PC every request went through the model twice (pick a tool, then phrase the answer),
which cost 4-15 s even for "open Chrome". These patterns call the same tools the AI would,
so behaviour is identical, just near-instant. Anything not matched here goes to the AI.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Callable

from ..speech.text import normalise

log = logging.getLogger(__name__)

Tools = dict[str, Callable[..., str]]

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10,
              "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45, "sixty": 60}

_MEDIA = [
    (r"\b(volume up|turn (it|the volume) up|louder)\b", "volume_up", 5),
    (r"\b(volume down|turn (it|the volume) down|quieter|lower the volume)\b", "volume_down", 5),
    (r"\b(unmute|mute)\b", "mute", 1),
    (r"\b(next (song|track|video)|skip (this|song|track))\b", "next", 1),
    (r"\b(previous (song|track|video)|go back a (song|track))\b", "previous", 1),
    (r"^(pause|play|resume|stop the music|pause (the )?(music|video|song)|play (the )?(music|video))$", "play_pause", 1),
]


def _number(text: str) -> float | None:
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if m:
        return float(m.group(1))
    for word, value in sorted(_NUM_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b", text):
            return float(value)
    return None


def handle(text: str, tools: Tools, apps: set[str]) -> str | None:
    """Return the spoken reply if this request was handled directly, else None (ask the AI)."""
    t = normalise(text).replace("'", "")
    if not t:
        return None

    # time and date
    if re.search(r"\b(what time is it|whats the time|tell me the time|current time|the time)\b", t) and len(t.split()) <= 7:
        return f"It's {dt.datetime.now():%I:%M %p}".replace(" 0", " ") + "."
    if re.search(r"\b(whats (the date|todays date)|what day is it|todays date|what date is it)\b", t):
        return f"Today is {dt.datetime.now():%A, %d %B}."

    # Chrome with a profile: "open chrome second profile", "open the vivasoft profile", "chrome with my work profile"
    if "profile" in t and ("chrome" in t or t.startswith(("open", "launch", "start"))):
        rest = re.sub(r"\b(open|launch|start|chrome|google|in|with|using|the|my|please|profile|browser)\b", " ", t)
        return tools["open_chrome_profile"](rest.strip() or t)

    # media and volume
    for pattern, action, presses in _MEDIA:
        if re.search(pattern, t) and len(t.split()) <= 6:
            times = int(_number(t) or presses) if action.startswith("volume") else 1
            tools["media_control"](action, times)
            return {"volume_up": "Volume up.", "volume_down": "Volume down.", "mute": "Done.",
                    "next": "Next.", "previous": "Previous.", "play_pause": "Done."}[action]

    # timers: "set a timer for 5 minutes", "remind me in ten minutes"
    m = re.search(r"\b(timer|remind me)\b.*\b(minute|minutes|min|mins|hour|hours)\b", t)
    if m:
        amount = _number(t)
        if amount:
            minutes = amount * (60 if "hour" in m.group(2) else 1)
            label = re.sub(r".*\bto\b\s+", "", t) if " to " in t else "timer"
            return tools["set_timer"](minutes, label)

    # quick system actions
    if re.search(r"\b(take a screenshot|screenshot|screen shot)\b", t) and len(t.split()) <= 5:
        return tools["take_screenshot"]()
    if re.search(r"^(lock|lock (the|my) (pc|computer|screen|laptop))$", t):
        return tools["lock_pc"]()
    if re.search(r"\b(system status|how is my (pc|computer) doing|pc status)\b", t):
        return tools["system_status"]()
    if re.search(r"^(cancel (the )?shutdown|abort shutdown)$", t):
        return tools["cancel_shutdown"]()

    # "open X" / "launch X" for apps in the allowlist; websites like "open github.com"
    m = re.match(r"^(?:now |please |can you |could you |lets )*(?:open|launch|start)\s+(?:up\s+)?(?:the\s+|my\s+)?(.+?)(?:\s+please)?$", t)
    if m:
        target = m.group(1).strip()
        if target in apps or any(target == a or target.replace(" ", "") == a.replace(" ", "") for a in apps):
            return tools["open_app"](target)
        if re.search(r"\b\w+ (com|org|net|io|dev)\b", target) or re.search(r"\.\w{2,}$", text.strip().rstrip(".")):
            return tools["open_website"](text.strip().split()[-1].rstrip("."))
    return None
