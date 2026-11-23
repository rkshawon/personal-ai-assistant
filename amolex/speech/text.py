"""Pure text helpers (no audio deps), so they can be unit tested."""

from __future__ import annotations

import difflib
import re

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def normalise(text: str) -> str:
    return " ".join(words(text))


def say_duration(seconds: float) -> str:
    """100 -> 'about a minute and a half', 45 -> 'about 45 seconds', 200 -> 'about 3 minutes'."""
    if seconds < 60:
        return f"about {max(5, int(round(seconds / 5) * 5))} seconds"
    if seconds < 75:
        return "about a minute"
    if seconds < 110:
        return "about a minute and a half"
    return f"about {round(seconds / 60)} minutes"


def pick_option(reply: str, options: dict[str, list[str]]) -> str | None:
    """Which option a spoken answer chose. Options are checked in order, so put the longer phrase
    first: "don't save" must win over "save"."""
    said = " " + " ".join(words(reply.replace("’", "'"))) + " "
    for key, phrases in options.items():
        if any(f" {p} " in said for p in phrases):
            return key
    return None


_NOT_DONE = re.compile(
    r"^(sorry|i can't|i cannot|i couldn't|i didn't|i don't|i won't|i'm not sure|cancelled|there's nothing|"
    r"there are no|no \w+ (is|are) running|the user said|windows couldn't|it failed|i found no|okay, i left|"
    r"i didn't catch)"
    r"|isn't running|is still open|is asking|\?$",
    re.IGNORECASE,
)


def task_succeeded(reply: str) -> bool:
    """False for failures, refusals, cancellations and questions: those don't get "The task is done."."""
    return bool(reply.strip()) and not _NOT_DONE.search(reply.strip().replace("’", "'"))


_FRAGMENTS = {"open", "launch", "start", "close", "write", "type", "search", "play", "go", "hey", "ok", "okay", "um", "uh"}


def is_fragment(text: str) -> bool:
    """A lone word like "Open." that can't be a request on its own: noise, or speech cut off early."""
    w = words(text)
    return not w or (len(w) == 1 and w[0] in _FRAGMENTS)


def contains_phrase(text: str, phrases: set[str]) -> bool:
    """True if any phrase appears as a whole-word run inside text."""
    padded = f" {normalise(text)} "
    return any(f" {p} " in padded for p in phrases)


# How Whisper has actually written "Amolex" (spaces and apostrophes removed), e.g. "Hey, I'm Alex."
NAME_MISHEARINGS = {
    "amolex", "alex", "imalex", "amalex", "ammolex", "amolax", "amulex", "emolex", "imolex",
    "hamolex", "amolecks", "amolexs", "amolexx", "amorlex", "amelex", "omolex",
    "ammo", "amo", "ammu", "amu",  # the nickname: "Hey ammo, open Chrome"
}


def strip_wake(text: str, name: str = "amolex") -> str:
    """Remove a leading "hey amolex"-style address from a Whisper transcript.

    Whisper spells an invented name in many ways ("Amalex", "a molex", "Ammo Lex",
    even "I'm Alex"), so the first few words are checked against known mishearings
    and fuzzy-matched against the name.
    """
    tokens = text.strip().split()
    if not tokens:
        return ""
    joined_lower = [normalise(t).replace("'", "") for t in tokens]
    best_end, best_ratio = 0, 0.72  # minimum similarity to count as the name
    # windows over the first tokens, e.g. "amolex", "ammo lex", "i'm alex"; the closest match wins,
    # so "amolex open" never beats "amolex" and swallows the request
    for start in range(0, min(2, len(tokens))):
        if start == 1 and joined_lower[0] not in {"hey", "hi", "hello", "hiya", "ok", "okay", "yo"}:
            break
        for end in range(start + 1, min(start + 3, len(tokens)) + 1):
            candidate = "".join(joined_lower[start:end])
            ratio = 1.0 if candidate in NAME_MISHEARINGS else difflib.SequenceMatcher(None, candidate, name).ratio()
            # two exact matches ("ammo" and "ammo lex"): the longer one is the name
            if ratio > best_ratio or (ratio == best_ratio == 1.0 and end > best_end):
                best_end, best_ratio = end, ratio
    rest = " ".join(tokens[best_end:])
    return rest.lstrip(" ,.!?-").strip()


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def speakable(text: str, max_chars: int = 300) -> str:
    """Make a reply fit for speaking: no markdown symbols, and cut at a sentence end
    once it passes max_chars, so a runaway answer can't talk for minutes."""
    text = re.sub(r"[*_#`>|]+", "", text)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    out = ""
    for sentence in _SENTENCE_END.split(text):
        if out and len(out) + len(sentence) > max_chars:
            break
        out = f"{out} {sentence}".strip()
    if len(out) > max_chars:  # a single huge sentence
        out = out[:max_chars].rsplit(" ", 1)[0] + "..."
    return out


_YES = {"yes", "yeah", "yep", "sure", "confirm", "confirmed", "do it", "go ahead", "affirmative", "ok", "okay"}
_NO = {"no", "nope", "cancel", "stop", "don't", "dont", "negative", "never mind"}


def yes_no(text: str) -> bool | None:
    t = normalise(text)
    if not t:
        return None
    if contains_phrase(t, _NO):
        return False
    if contains_phrase(t, _YES):
        return True
    return None


_SWITCH = re.compile(
    r"\b(?:use|switch to|change to)\s+(?:the\s+)?(gemini|google|local|offline|ollama)"
    r"(?:\s+(?:model\s+)?(flash|pro|[a-z0-9.:\-]+))?",
)


_SIZE = re.compile(
    r"\b(?:use|switch to|change to)\s+(?:the\s+)?(auto|automatic|light|small|normal|medium|heavy|big)\s+model\b"
)
_GAMING = re.compile(r"\bgaming mode\s+(on|off|auto|automatic)\b|\b(start|stop)\s+gaming mode\b")


def parse_gaming(text: str) -> bool | None | str:
    """'gaming mode on' -> True, 'off' -> False, 'auto' -> None; no match -> 'no'."""
    m = _GAMING.search(normalise(text))
    if not m:
        return "no"
    word = m.group(1) or m.group(2)
    return {"on": True, "start": True, "off": False, "stop": False}.get(word)


def parse_switch(text: str) -> tuple[str, str | None] | None:
    """'use gemini pro' -> ('gemini', 'pro'); 'switch to local model' -> ('local', None);
    'use the big model' -> ('local', 'big'); 'use automatic model' -> ('local', 'auto')."""
    size = _SIZE.search(normalise(text))
    if size:
        return "local", size.group(1)
    m = _SWITCH.search(normalise(text))
    if not m:
        return None
    provider = "gemini" if m.group(1) in {"gemini", "google"} else "local"
    variant = m.group(2)
    if variant == "model":
        variant = None
    return provider, variant
