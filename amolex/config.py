"""Typed configuration loaded from config.json and commands.json.

Secrets (Gemini API key, Google sign-in token) are never stored here; see secrets.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import paths


class AudioCfg(BaseModel):
    input_device: str | int | None = None  # None = Windows default; or a name fragment like "WO Mic"
    sample_rate: int = 16000
    block_ms: Literal[10, 20, 30] = 30  # webrtcvad accepts only these frame sizes
    gain: float = Field(1.0, gt=0, le=20)  # software boost for a quiet mic; --test-voice shows the level


class WakeCfg(BaseModel):
    phrases: list[str] = Field(default_factory=lambda: ["hey amolex", "amolex"])
    hotkey: str = "f8"
    barge_in: bool = True
    # after an answer, keep listening this long for a follow-up request without the wake word
    # (0 = off; skipped while gaming so fast commands aren't paused)
    follow_up_seconds: float = 8


class FastCfg(BaseModel):
    enabled: bool = True
    fire_on_partial: bool = True
    min_confidence: float = Field(0.75, ge=0, le=1)
    ignore_chatter: bool = True  # don't fire when the phrase also contains non-command words
    partial_only_when_gaming: bool = True  # outside games, wait for Vosk's final result (+confidence)


class SpeechCfg(BaseModel):
    vosk_model: str = "vosk-model-small-en-us-0.15"
    whisper_model: str = "base"
    # Spellings Whisper should expect ("Plot Desktop" for Claude). Only names, no commands: with
    # "Open Claude. Open KMPlayer..." here, Whisper wrote "Open." for silence again and again.
    whisper_prompt: str = "Amolex, KMPlayer, Claude, Gemini, Chrome, Notepad, VS Code, YouTube."
    language: str = "en"
    attentive_timeout_seconds: float = 6
    end_of_speech_ms: int = 700
    max_utterance_seconds: float = 12


Tier = Literal["light", "normal", "heavy"]
TIERS: tuple[Tier, ...] = ("light", "normal", "heavy")


class RoutingCfg(BaseModel):
    """Pick the local model size per request: light for quick commands and gaming,
    normal for everyday use, heavy for coding / writing / analysis."""

    enabled: bool = True
    # RK runs one model for everything (the 8B made things up). With one model there's no
    # "use the big model?" question; set different models here to bring the tiers back.
    light: str = "gpt-oss:20b"
    normal: str = "gpt-oss:20b"
    heavy: str = "gpt-oss:20b"
    gaming_max: Tier = "light"  # never load a bigger model than this while gaming
    ask_before_heavy: bool = True  # ask "use the big model?" and wait for a spoken yes
    pinned: str | None = None  # set when you say "use local model X"; "automatic model" clears it
    games: list[str] = Field(
        default_factory=lambda: ["valorant.exe", "cs2.exe", "gta5.exe", "eldenring.exe", "cyberpunk2077.exe"]
    )
    fullscreen_is_gaming: bool = False  # treat any full-screen app as a game (also catches full-screen video)
    light_words: list[str] = Field(
        default_factory=lambda: [
            "open", "close", "launch", "start", "play", "pause", "resume", "next", "previous", "skip", "mute",
            "unmute", "volume", "louder", "quieter", "lock", "screenshot", "timer", "remind", "time", "date",
            "settings", "folder", "youtube", "search",
        ]
    )
    heavy_words: list[str] = Field(
        default_factory=lambda: [
            "code", "coding", "program", "script", "function", "debug", "bug", "fix", "refactor", "write",
            "essay", "email", "letter", "article", "story", "explain", "analyse", "analyze", "compare",
            "plan", "summarise", "summarize", "translate", "research", "review", "design", "calculate",
        ]
    )
    light_max_words: int = 8  # longer requests are never treated as quick commands
    heavy_min_words: int = 30  # very long requests are treated as heavy


class BrainCfg(BaseModel):
    provider: Literal["gemini", "local"] = "local"
    gemini_auth: Literal["api_key", "oauth"] = "api_key"
    gemini_model: str = "gemini-flash-latest"
    gemini_models: list[str] = Field(default_factory=lambda: ["gemini-flash-latest", "gemini-pro-latest"])
    oauth_client_secret_file: str = "client_secret.json"
    oauth_scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/generative-language.retriever",
        ]
    )
    oauth_quota_project: str | None = None
    local_engine: Literal["ollama"] = "ollama"
    local_model: str = "gpt-oss:20b"
    local_models: list[str] = Field(default_factory=lambda: ["gpt-oss:20b"])
    ollama_host: str = "http://127.0.0.1:11434"
    routing: RoutingCfg = Field(default_factory=RoutingCfg)
    local_think: bool = False  # reasoning off = faster spoken replies (gpt-oss uses "low" instead)
    # How long a model stays in GPU memory after its last use. The main model stays loaded all day:
    # reloading it from a hard drive took ~40 s. The heavy model is let go quickly to free VRAM.
    keep_alive: str = "24h"
    heavy_keep_alive: str = "5m"
    load_timeout_seconds: float = 240  # the 20B (~13 GB) can take 1.5-2 min to load from a hard drive
    # A 20B model fills ~14 GB of the 16 GB GPU, which games need: release it while a game runs,
    # load it again when the game closes
    free_gpu_while_gaming: bool = True
    fallback_to_local: bool = True
    timeout_seconds: float = 8


class DelegateCfg(BaseModel):
    """A bigger AI the brain can hand a task to, run through its official CLI.

    `command` is the program and its arguments; "{task}" is replaced by the task text.
    With `stdin: true` the task is sent on standard input instead.
    """

    enabled: bool = False
    description: str = ""
    command: list[str] = Field(default_factory=list)
    stdin: bool = False
    workdir: str = r"E:\Amolex\workspace"
    timeout_minutes: float = 30


class JobsCfg(BaseModel):
    results_dir: str = r"E:\Amolex\jobs"
    max_parallel: int = 2


class VoiceCfg(BaseModel):
    online_voice: str = "en-GB-RyanNeural"
    rate: str = "+0%"
    prefer_online: bool = True


class PlayerCfg(BaseModel):
    """Video player that "pause", "forward 30 seconds" and "go back" control (KMPlayer by default)."""

    process_names: list[str] = Field(default_factory=lambda: ["kmplayer64.exe", "kmplayer.exe"])
    seek_step_seconds: int = 5  # what one Right/Left arrow press skips in the player
    default_seek_seconds: int = 10  # "forward" with no amount


class ToolsCfg(BaseModel):
    player: PlayerCfg = Field(default_factory=PlayerCfg)
    apps: dict[str, str] = Field(default_factory=dict)
    git_repos: dict[str, str] = Field(default_factory=dict)
    readable_folders: list[str] = Field(default_factory=list)
    # Where it may create and change files. "*" = anywhere except Windows' own folders and Amolex's.
    # Creating something new is free; editing, moving or recycling existing files asks you first.
    writable_folders: list[str] = Field(default_factory=lambda: ["*"])
    # Catch-all for anything without its own tool. Commands that only look (IP address, disk space,
    # running programs) run straight away; anything that changes something is read back and needs
    # your "yes"; permanent deletion, formatting and downloads are always refused.
    allow_powershell: bool = True
    search_seconds: float = 12  # how long a file search may take (E: is a hard drive)

    @field_validator("apps", "git_repos")
    @classmethod
    def _lower_keys(cls, v: dict[str, str]) -> dict[str, str]:
        return {k.lower().strip(): val for k, val in v.items()}


class Config(BaseModel):
    assistant_name: str = "Amolex-9026"
    user_name: str = "RK"
    audio: AudioCfg = Field(default_factory=AudioCfg)
    wake: WakeCfg = Field(default_factory=WakeCfg)
    fast: FastCfg = Field(default_factory=FastCfg)
    speech: SpeechCfg = Field(default_factory=SpeechCfg)
    brain: BrainCfg = Field(default_factory=BrainCfg)
    voice: VoiceCfg = Field(default_factory=VoiceCfg)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    delegates: dict[str, DelegateCfg] = Field(default_factory=dict)
    jobs: JobsCfg = Field(default_factory=JobsCfg)

    @field_validator("delegates")
    @classmethod
    def _lower_delegate_keys(cls, v: dict[str, DelegateCfg]) -> dict[str, DelegateCfg]:
        return {k.lower().strip(): val for k, val in v.items()}


class FastCommand(BaseModel):
    phrase: str
    action: Literal["click", "key", "hotkey", "scroll"]
    button: Literal["left", "right", "middle"] = "left"
    key: str | None = None
    keys: list[str] = Field(default_factory=list)
    hold_ms: int = 0
    amount: int = 0

    @field_validator("phrase")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return " ".join(v.lower().split())


class CommandSet(BaseModel):
    commands: list[FastCommand] = Field(default_factory=list)
    control: dict[str, list[str]] = Field(default_factory=dict)

    def by_phrase(self) -> dict[str, FastCommand]:
        return {c.phrase: c for c in self.commands}

    def control_phrases(self, name: str) -> set[str]:
        return {" ".join(p.lower().split()) for p in self.control.get(name, [])}


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_config(path: Path = paths.CONFIG_FILE) -> Config:
    return Config.model_validate(_read_json(path)) if path.exists() else Config()


def save_config(cfg: Config, path: Path = paths.CONFIG_FILE) -> None:
    path.write_text(json.dumps(cfg.model_dump(), indent=2) + "\n", encoding="utf-8")


def load_commands(path: Path = paths.COMMANDS_FILE) -> CommandSet:
    return CommandSet.model_validate(_read_json(path)) if path.exists() else CommandSet()
