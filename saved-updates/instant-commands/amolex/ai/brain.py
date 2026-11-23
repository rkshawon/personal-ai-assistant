"""The brain: a local model (default) that runs PC tasks and hands big tasks to bigger AIs.

Gemini via API key stays available as an optional provider; switchable by voice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..config import Config, save_config
from .providers import GeminiProvider, OllamaProvider, ProviderUnavailable
from .router import Route, Router

log = logging.getLogger(__name__)


@dataclass
class Answer:
    text: str
    provider: str
    fell_back: bool = False


def system_prompt(cfg: Config) -> str:
    delegates = {n: d for n, d in cfg.delegates.items() if d.enabled and d.command}
    if delegates:
        handoff = (
            "You do PC and Windows tasks yourself with your tools. You do NOT do big work yourself: for writing, "
            "coding, research, analysis or long answers, call delegate_task with a complete, self-contained task "
            "and tell the user it was sent. Available AIs: "
            + "; ".join(f"{n} ({d.description or 'general'})" for n, d in delegates.items())
            + ". If the user names one, use it. "
        )
    else:
        handoff = (
            "You do PC and Windows tasks yourself with your tools. No bigger AI is connected yet, so for big "
            "work (long writing, coding, research) say briefly that no task model is set up. "
        )
    return (
        f"You are {cfg.assistant_name}, a voice assistant on {cfg.user_name}'s Windows PC. "
        f"Address the user as {cfg.user_name}. {handoff}"
        "Your replies are spoken aloud: one or two short sentences, no markdown, no lists, no emoji. "
        "Never claim you did something unless a tool confirmed it. If a request needs an app, repo or "
        "folder that isn't available, say so briefly."
    )


class Brain:
    def __init__(self, cfg: Config, tools: list[Callable], has_delegates: bool = False) -> None:
        self.cfg = cfg
        prompt = system_prompt(cfg)
        self.gemini = GeminiProvider(cfg.brain, tools, prompt)
        self.local = OllamaProvider(cfg.brain, tools, prompt)
        self.router = Router(cfg.brain.routing, has_delegates)
        self.last_route: Route | None = None

    @property
    def active(self) -> str:
        b = self.cfg.brain
        if b.provider == "gemini":
            return f"Gemini, {b.gemini_model}"
        if b.routing.pinned:
            return f"the local model, {b.routing.pinned}"
        return "automatic local models"

    def plan(self, text: str) -> Route | None:
        """Which local model a request will use (None when Gemini is the brain)."""
        return None if self.cfg.brain.provider == "gemini" else self.router.choose(text)

    def _ask_local(self, text: str, route: Route | None = None) -> str:
        route = route or self.router.choose(text)
        self.last_route = route
        try:
            return self.local.ask(text, model=route.model)
        except ProviderUnavailable as e:
            r = self.cfg.brain.routing
            reason = str(e).lower()
            # the 20B model was never pulled: answer with the normal model instead of failing
            if route.model != r.normal and "not found" in reason:
                fallback, why = r.normal, f"{route.model} not pulled"
            # a bigger model took too long (usually still loading from disk): the light one answers
            elif route.model != r.light and "timed out" in reason:
                fallback, why = r.light, f"{route.model} timed out"
            else:
                raise
            log.warning("%s (%s); using %s", why, e, fallback)
            self.local.history = self.local.history[:-1]  # the retry re-adds this turn
            self.last_route = Route(fallback, "light" if fallback == r.light else "normal", why)
            return self.local.ask(text, model=fallback)

    def ask(self, text: str, route: Route | None = None) -> Answer:
        b = self.cfg.brain
        if b.provider == "gemini":
            try:
                return Answer(self.gemini.ask(text), "gemini")
            except ProviderUnavailable as e:
                log.warning("%s", e)
                if not b.fallback_to_local:
                    return Answer("I can't reach Gemini right now.", "none")
                try:
                    return Answer(self._ask_local(text), "local", fell_back=True)
                except ProviderUnavailable as e2:
                    log.error("%s", e2)
                    return Answer("I can't reach Gemini or the local model right now.", "none")
        try:
            return Answer(self._ask_local(text, route), "local")
        except ProviderUnavailable as e:
            log.error("%s", e)
            if "timed out" in str(e).lower():
                return Answer("Sorry, the model is taking too long. Try again in a moment.", "none")
            return Answer("I can't reach the local AI. Check that Ollama is running.", "none")

    def set_gaming(self, on: bool | None) -> str:
        """True / False forces gaming mode; None returns to automatic detection."""
        self.router.gaming.manual = on
        return {True: "Gaming mode on. I'll keep to the light model.", False: "Gaming mode off.", None: "Gaming mode is automatic again."}[on]

    def switch(self, provider: str, variant: str | None = None) -> str:
        """provider 'gemini' | 'local'; variant e.g. 'pro', 'gpt-oss', 'qwen', or 'auto' for automatic sizing."""
        b = self.cfg.brain
        b.provider = "gemini" if provider == "gemini" else "local"
        if b.provider == "local":
            if not variant or variant.lower() in {"auto", "automatic"}:
                b.routing.pinned = None
            else:
                tiers = {"light": b.routing.light, "small": b.routing.light, "normal": b.routing.normal,
                         "medium": b.routing.normal, "heavy": b.routing.heavy, "big": b.routing.heavy}
                pool = list(dict.fromkeys([*tiers.values(), *b.local_models]))
                match = tiers.get(variant.lower()) or next((m for m in pool if variant.lower() in m.lower()), None)
                if not match:
                    return f"I don't have a {variant} model. Options: {', '.join(pool)}, or automatic."
                b.routing.pinned = match
        elif variant:
            match = next((m for m in b.gemini_models if variant.lower() in m.lower()), None)
            if not match:
                return f"I don't have a {variant} model configured. Options: {', '.join(b.gemini_models)}."
            b.gemini_model = match
        self.gemini.reset()
        self.local.reset()
        save_config(self.cfg)
        log.info("brain switched to %s", self.active)
        return f"Switched to {self.active}."
