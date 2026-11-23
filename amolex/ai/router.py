"""Chooses the local model size for each request.

  light   quick PC commands, and everything while gaming   (qwen3:4b,   ~3 GB VRAM)
  normal  everyday questions and multi-step PC tasks       (qwen3:8b,   ~6 GB)
  heavy   coding, writing, analysis                        (gpt-oss:20b, ~14 GB)

Gaming caps the size (default: light) so a big model never steals VRAM from a game.
When a delegate AI is set up, heavy tasks use the normal model, which hands them off
instead of loading the 20B model locally.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..config import TIERS, RoutingCfg, Tier
from ..speech.text import words

log = logging.getLogger(__name__)


@dataclass
class Route:
    model: str
    tier: Tier
    reason: str


def classify(text: str, cfg: RoutingCfg) -> tuple[Tier, str]:
    """Pure text rule: which tier a request needs."""
    w = words(text)
    heavy_hits = sorted(set(w) & {h.lower() for h in cfg.heavy_words})
    if heavy_hits:
        return "heavy", f"heavy words: {', '.join(heavy_hits)}"
    if len(w) >= cfg.heavy_min_words:
        return "heavy", f"long request ({len(w)} words)"
    if len(w) <= cfg.light_max_words and set(w) & {x.lower() for x in cfg.light_words}:
        return "light", "quick command"
    return "normal", "everyday request"


def cap(tier: Tier, maximum: Tier) -> Tier:
    return tier if TIERS.index(tier) <= TIERS.index(maximum) else maximum


class GamingDetector:
    """Gaming = manual 'gaming mode', a listed game running, or (optionally) any full-screen app."""

    def __init__(self, cfg: RoutingCfg) -> None:
        self.cfg = cfg
        self.manual: bool | None = None  # None = automatic
        self._cached = (0.0, False, "")
        self._lock = threading.Lock()

    def check(self) -> tuple[bool, str]:
        if self.manual is not None:
            return self.manual, "gaming mode " + ("on" if self.manual else "off")
        with self._lock:
            stamp, value, why = self._cached
            if time.monotonic() - stamp < 5:  # scanning processes costs a few ms; cache it
                return value, why
            value, why = self._detect()
            self._cached = (time.monotonic(), value, why)
            return value, why

    def _detect(self) -> tuple[bool, str]:
        try:
            import psutil

            games = {g.lower() for g in self.cfg.games}
            for p in psutil.process_iter(["name"]):
                name = (p.info["name"] or "").lower()
                if name in games:
                    return True, f"{name} running"
        except Exception as e:
            log.debug("process scan failed: %s", e)
        if self.cfg.fullscreen_is_gaming and _foreground_is_fullscreen():
            return True, "full-screen app"
        return False, ""


def _foreground_is_fullscreen() -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
            return False
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return (rect.right - rect.left) >= user32.GetSystemMetrics(0) and (rect.bottom - rect.top) >= user32.GetSystemMetrics(1)
    except Exception:
        return False


class Router:
    def __init__(self, cfg: RoutingCfg, has_delegates: bool) -> None:
        self.cfg = cfg
        self.has_delegates = has_delegates
        self.gaming = GamingDetector(cfg)

    def model_for(self, tier: Tier) -> str:
        return {"light": self.cfg.light, "normal": self.cfg.normal, "heavy": self.cfg.heavy}[tier]

    def choose(self, text: str) -> Route:
        if self.cfg.pinned:
            return Route(self.cfg.pinned, "normal", "pinned by you")
        tier, reason = classify(text, self.cfg) if self.cfg.enabled else ("normal", "routing off")
        if tier == "heavy" and self.has_delegates:
            tier, reason = "normal", reason + "; handing off to a delegate"
        gaming, why = self.gaming.check()
        if gaming and cap(tier, self.cfg.gaming_max) != tier:
            tier, reason = cap(tier, self.cfg.gaming_max), f"{reason}; capped while gaming ({why})"
        route = Route(self.model_for(tier), tier, reason)
        log.info("route: %s (%s) - %s", route.model, route.tier, route.reason)
        return route
