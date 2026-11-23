"""Gemini (cloud) and Ollama (local) behind one small interface.

Both raise ProviderUnavailable when they can't answer (no credentials, offline,
quota, server error, timeout), which is the brain's cue to fall back.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

from .. import secrets
from ..config import BrainCfg
from ..log import timed

log = logging.getLogger(__name__)


class ProviderUnavailable(Exception):
    pass


# Tools whose result is a complete spoken reply on its own
SELF_REPORTING_TOOLS = {
    "open_app", "open_chrome_profile", "close_app", "open_folder", "open_website", "search_youtube",
    "open_settings", "media_control", "take_screenshot", "lock_pc", "power", "cancel_shutdown",
    "set_timer", "undo_last_git_commit", "delegate_task", "switch_ai_model", "player_control",
    "type_text", "press_keys", "switch_to_app", "write_file", "create_folder", "move_item", "recycle_item",
    "open_file",
}

_MULTI_STEP = re.compile(r"\b(and|then|after that|also|plus)\b|,", re.IGNORECASE)


def is_multi_step(request: str) -> bool:
    """'open Notepad and write Hello World' -> True; 'Okay, can you open Notepad?' -> False."""
    request = re.sub(r"^\s*(?:(?:ok|okay|so|now|well|hey|please)\b[,\s]*)+", "", request, flags=re.IGNORECASE)
    return bool(_MULTI_STEP.search(request.strip().rstrip("?.!")))


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove a model's visible reasoning, keeping only the answer.

    Handles a full <think>...</think> block and the case seen on RK's PC where only
    the closing </think> came back, with the whole reasoning before it.
    """
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.replace("<think>", "").strip()


_ACTION_CLAIM = re.compile(
    r"\b(?:i|i've|i have|i just|it's|it is|it has been|it was|file (?:has been|was)|has been|have been)\s+"
    r"(?:successfully\s+|now\s+|already\s+|just\s+)?"
    r"(?:created|made|saved|written|wrote|deleted|removed|moved|renamed|copied|opened|closed|launched|"
    r"started|sent|installed|downloaded|set up|changed|updated|turned (?:on|off)|played|paused|typed)\b",
    re.IGNORECASE,
)
NO_TOOL_REPLY = "I didn't do that. I don't have a tool for it yet, so nothing changed on your PC."


def claims_action(answer: str) -> bool:
    """True if a reply says something was done ("I've created notes.txt", "Done.").

    Only trusted when a tool actually ran: on RK's PC the 8B said it created a .txt file
    in Documents with no tool call at all, so nothing was there.
    """
    return answer.strip().rstrip(".!").lower() in {"done", "all done", "ok done"} or bool(_ACTION_CLAIM.search(answer))


def keep_alive_for(cfg: BrainCfg, model: str) -> str:
    """The main model stays loaded (reloading from disk is slow); a separate heavy one is released soon."""
    r = cfg.routing
    separate_heavy = r.heavy not in (r.light, r.normal)
    return cfg.heavy_keep_alive if separate_heavy and model == r.heavy else cfg.keep_alive


def unload(cfg: BrainCfg, model: str) -> None:
    """Release a model's GPU memory now (keep_alive 0)."""
    import ollama

    try:
        ollama.Client(host=cfg.ollama_host, timeout=30).generate(model=model, prompt="", keep_alive=0)
        log.info("unloaded %s to free the GPU", model)
    except Exception as e:
        log.warning("couldn't unload %s: %s", model, e)


def warm_up(cfg: BrainCfg, model: str) -> None:
    """Load a model into GPU memory ahead of time so the first request isn't slow."""
    import ollama

    try:
        with timed(f"warm up {model}", log):
            ollama.Client(host=cfg.ollama_host, timeout=cfg.load_timeout_seconds).generate(
                model=model, prompt="", keep_alive=keep_alive_for(cfg, model)
            )
    except Exception as e:
        log.warning("couldn't pre-load %s: %s", model, e)


class GeminiProvider:
    name = "gemini"

    def __init__(self, cfg: BrainCfg, tools: list[Callable], system_prompt: str) -> None:
        self.cfg = cfg
        self.tools = tools
        self.system_prompt = system_prompt
        self._chat = None
        self._chat_model = None

    def _client(self):
        from google import genai
        from google.genai import types

        http = types.HttpOptions(timeout=int(self.cfg.timeout_seconds * 1000))
        if self.cfg.gemini_auth == "api_key":
            key = secrets.get_api_key()
            if not key:
                raise ProviderUnavailable("no Gemini API key (run start.bat --set-key)")
            return genai.Client(api_key=key, http_options=http)
        try:
            creds = secrets.load_oauth_creds(self.cfg)
        except Exception as e:  # refresh failed, e.g. offline or token revoked
            raise ProviderUnavailable(f"Google sign-in failed: {e}") from e
        if creds is None:
            raise ProviderUnavailable("not signed in to Google (run start.bat --login-google)")
        return genai.Client(credentials=creds, http_options=http)

    def reset(self) -> None:
        self._chat = None

    def ask(self, text: str) -> str:
        from google.genai import errors, types

        try:
            if self._chat is None or self._chat_model != self.cfg.gemini_model:
                self._chat = self._client().chats.create(
                    model=self.cfg.gemini_model,
                    config=types.GenerateContentConfig(
                        system_instruction=self.system_prompt,
                        tools=self.tools,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=5),
                    ),
                )
                self._chat_model = self.cfg.gemini_model
            with timed(f"gemini {self.cfg.gemini_model}", log):
                reply = self._chat.send_message(text)
            return (reply.text or "").strip() or "Done."
        except ProviderUnavailable:
            raise
        except errors.APIError as e:
            # 429 quota, 5xx server, 401/403 bad key, 404 unknown model: all mean "use the fallback"
            self._chat = None
            raise ProviderUnavailable(f"Gemini error {e.code}: {e.message}") from e
        except Exception as e:  # network down, DNS, timeout
            self._chat = None
            raise ProviderUnavailable(f"Gemini unreachable: {e}") from e


class OllamaProvider:
    name = "local"

    def __init__(self, cfg: BrainCfg, tools: list[Callable], system_prompt: str) -> None:
        self.cfg = cfg
        self.tools = {t.__name__: t for t in tools}
        self.system_prompt = system_prompt
        self.history: list[dict] = []

    def reset(self) -> None:
        self.history = []

    def is_loaded(self, model: str) -> bool:
        """True if the model is already in (GPU) memory, so a request won't wait for a load."""
        import ollama

        try:
            running = ollama.Client(host=self.cfg.ollama_host, timeout=3).ps().models
            return any(m.model == model or m.model.startswith(model) for m in running)
        except Exception:
            return False

    def ask(self, text: str, model: str | None = None) -> str:
        import ollama

        model = model or self.cfg.local_model
        # Loading a model from a hard drive took 18+ s for the 4B on RK's PC, so the first call to a
        # model that isn't in memory yet gets the long load timeout
        timeout = max(30, self.cfg.timeout_seconds * 4)
        if not self.is_loaded(model):
            timeout = max(timeout, self.cfg.load_timeout_seconds)
        client = ollama.Client(host=self.cfg.ollama_host, timeout=timeout)
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-20:]
        # Reasoning makes spoken replies slow and long. Qwen3 obeys its "/no_think" switch;
        # gpt-oss can't turn reasoning off, only down to "low".
        think: bool | str = self.cfg.local_think
        system = self.system_prompt
        if not think and model.startswith("qwen3"):
            system += " /no_think"
        if not think and model.startswith("gpt-oss"):
            think = "low"
        messages = [{"role": "system", "content": system}, *self.history]
        options = dict(model=model, tools=list(self.tools.values()), keep_alive=keep_alive_for(self.cfg, model), think=think)
        used_tools = False
        try:
            for _ in range(6):  # at most 6 tool rounds
                with timed(f"ollama {model}", log):
                    try:
                        resp = client.chat(messages=messages, **options)
                    except TypeError:  # older ollama client without `think`
                        log.warning("this ollama client has no 'think' option; relying on /no_think")
                        options.pop("think", None)
                        resp = client.chat(messages=messages, **options)
                msg = resp.message
                messages.append(msg)
                if not msg.tool_calls:
                    answer = strip_reasoning(msg.content or "") or "Done."
                    if not used_tools and claims_action(answer):
                        log.warning("model claimed an action without calling a tool: %r", answer)
                        answer = NO_TOOL_REPLY
                    self.history.append({"role": "assistant", "content": answer})
                    return answer
                used_tools = True
                results, seen = [], set()
                for call in msg.tool_calls:
                    args = call.function.arguments or {}
                    key = (call.function.name, json.dumps(args, sort_keys=True))
                    if key in seen:  # the model asked for the exact same action twice (Chrome opened twice)
                        continue
                    seen.add(key)
                    fn = self.tools.get(call.function.name)
                    result = fn(**args) if fn else f"Unknown tool {call.function.name}"
                    log.info("tool %s -> %s", call.function.name, str(result)[:120])
                    results.append((call.function.name, str(result)))
                    messages.append({"role": "tool", "content": str(result), "tool_name": call.function.name})
                # Actions whose result already says what happened ("Opened Chrome.") are spoken as-is,
                # saving a model round. Not for multi-step requests ("open Notepad and write Hello
                # World"): the model needs the next round to do the rest.
                if results and all(name in SELF_REPORTING_TOOLS for name, _ in results) and not is_multi_step(text):
                    answer = " ".join(dict.fromkeys(r for _, r in results))
                    self.history.append({"role": "assistant", "content": answer})
                    return answer
            return "I couldn't finish that in a few steps."
        except ollama.ResponseError as e:
            raise ProviderUnavailable(f"Ollama error: {e.error}") from e
        except Exception as e:  # Ollama not running
            raise ProviderUnavailable(f"Ollama unreachable at {self.cfg.ollama_host}: {e}") from e
