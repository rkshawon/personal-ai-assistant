"""Everything the local brain is allowed to do.

Two kinds of tools:
  * Windows / PC tasks it does itself (apps, media, volume, screenshots, timers, power...)
  * hand-off tools that give big tasks to another AI and report back when finished

Each tool is a plain function with type hints and a docstring; the model reads those to
decide when and how to call it. Anything destructive asks for a spoken "yes" first.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from .. import paths
from ..config import ToolsCfg
from .jobs import JobManager

log = logging.getLogger(__name__)

Confirm = Callable[[str], bool]
Notify = Callable[[str], None]

SETTINGS_PAGES = {
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "updates": "ms-settings:windowsupdate",
    "apps": "ms-settings:appsfeatures",
    "power": "ms-settings:powersleep",
    "storage": "ms-settings:storagesense",
    "notifications": "ms-settings:notifications",
    "personalization": "ms-settings:personalization",
    "gaming": "ms-settings:gaming-gamebar",
}

MEDIA_KEYS = {
    "play": "playpause",
    "pause": "playpause",
    "play_pause": "playpause",
    "next": "nexttrack",
    "previous": "prevtrack",
    "mute": "volumemute",
    "volume_up": "volumeup",
    "volume_down": "volumedown",
}


ORDINALS = {"first": 1, "one": 1, "second": 2, "two": 2, "third": 3, "three": 3, "fourth": 4, "four": 4, "fifth": 5, "five": 5}


def chrome_profiles() -> list[tuple[str, str]]:
    """Chrome's profiles as (folder, display name), in Chrome's own order (Default, Profile 1, ...).

    Read from Chrome's "Local State" file, so it always matches what the profile picker shows.
    """
    import json

    local_state = Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State"))
    try:
        cache = json.loads(local_state.read_text(encoding="utf-8"))["profile"]["info_cache"]
    except (OSError, KeyError, ValueError):
        return []

    def order(folder: str) -> tuple[int, int]:
        return (0, 0) if folder == "Default" else (1, int(folder.split()[-1]) if folder.split()[-1].isdigit() else 999)

    profiles = []
    for folder in sorted(cache, key=order):
        info = cache[folder]
        label = info.get("name") or folder
        email = info.get("user_name") or ""
        profiles.append((folder, f"{label} ({email})" if email else label))
    return profiles


def match_chrome_profile(spoken: str, profiles: list[tuple[str, str]]) -> tuple[str, str] | None:
    """'second' / '2' -> 2nd profile; 'vivasoft' or Whisper's 'Vibhasaftlimited.com' -> closest name."""
    import difflib
    import re

    if not profiles:
        return None
    words = re.findall(r"[a-z0-9]+", spoken.lower())
    for w in words:
        n = ORDINALS.get(w) or (int(w) if w.isdigit() else 0)
        if 1 <= n <= len(profiles):
            return profiles[n - 1]
    filler = {"chrome", "profile", "the", "my", "open", "with", "in", "com", "account", "browser", "google"}
    spoken_words = [w for w in words if w not in filler]
    wanted = "".join(spoken_words)
    if not wanted:
        return None
    best, best_score = None, 0.0
    for folder, label in profiles:
        display = label.split(" (")[0].lower()  # "Vivasoft Limited"
        email_user = label.split("(")[-1].split("@")[0].lower() if "@" in label else ""
        name_words = re.findall(r"[a-z0-9]+", display)
        if any(w in spoken_words for w in name_words if len(w) >= 2):  # "rk", "vivasoft"
            score = 1.0
        else:  # misheard spellings: "vibhasaftlimited" vs "vivasoftlimited"
            joined = "".join(name_words)
            score = max(
                difflib.SequenceMatcher(None, wanted, joined).ratio(),
                difflib.SequenceMatcher(None, wanted, email_user).ratio() if email_user else 0.0,
                0.9 if joined and (joined in wanted or wanted in joined) else 0.0,
            )
        if score > best_score:
            best, best_score = (folder, label), score
    return best if best_score >= 0.6 else None


def find_chrome() -> str | None:
    for candidate in (
        shutil.which("chrome"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _inside(path: Path, roots: list[Path]) -> bool:
    path = path.resolve()
    return any(path == r or r in path.parents for r in roots)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def build_tools(
    cfg: ToolsCfg,
    confirm: Confirm,
    notify: Notify,
    jobs: JobManager,
    extra: list[Callable] | None = None,
) -> list[Callable]:
    readable = [paths.expand(p).resolve() for p in cfg.readable_folders]
    screenshots = paths.PROJECT_DIR / "screenshots"

    # ------------------------------------------------------------------ apps and places
    def open_app(name: str) -> str:
        """Open a desktop application by its friendly name, for example "VS Code", "Chrome" or "Claude".

        Args:
            name: The application's name as the user said it.
        """
        key = name.lower().strip()
        target = cfg.apps.get(key) or next((v for k, v in cfg.apps.items() if k in key or key in k), None)
        if not target:
            return f"'{name}' is not in my app list. Known apps: {', '.join(cfg.apps) or 'none'}."
        expanded = os.path.expandvars(target)
        try:
            if Path(expanded).exists():
                os.startfile(expanded)  # type: ignore[attr-defined]
            elif exe := shutil.which(expanded):
                subprocess.Popen([exe], close_fds=True)
            else:
                os.startfile(expanded)  # type: ignore[attr-defined]  (lets Windows resolve app aliases)
        except OSError as e:
            return f"Could not open {name}: {e}"
        return f"Opened {name}."

    def open_chrome_profile(profile: str) -> str:
        """Open Google Chrome with a specific profile, for example "second", "work", "Vivasoft" or a person's name.

        Args:
            profile: The profile as the user said it: its name, email, or position like "second".
        """
        profiles = chrome_profiles()
        if not profiles:
            return "I couldn't read Chrome's profiles. Is Chrome installed?"
        match = match_chrome_profile(profile, profiles)
        if not match:
            names = "; ".join(f"{i}. {label}" for i, (_, label) in enumerate(profiles, 1))
            return f"I couldn't tell which profile you meant. Your profiles: {names}."
        chrome = find_chrome()
        if not chrome:
            return "I couldn't find chrome.exe."
        folder, label = match
        subprocess.Popen([chrome, f"--profile-directory={folder}"], close_fds=True)
        return f"Opened Chrome as {label.split(' (')[0]}."

    def close_app(process_name: str) -> str:
        """Close a running application by its process name, for example "chrome" or "notepad". Asks to confirm first.

        Args:
            process_name: Executable name with or without ".exe".
        """
        import psutil

        target = process_name.lower().removesuffix(".exe") + ".exe"
        procs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == target]
        if not procs:
            return f"{process_name} isn't running."
        if not confirm(f"Close {process_name}? Unsaved work in it may be lost."):
            return "Cancelled."
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        return f"Closed {process_name}."

    def open_folder(path: str) -> str:
        """Open a folder in File Explorer, for example "Downloads" or "E:\\Movie".

        Args:
            path: A full path, or a known folder name: downloads, documents, desktop, pictures, videos, music.
        """
        known = {n: Path.home() / n.capitalize() for n in ("downloads", "documents", "desktop", "pictures", "videos", "music")}
        p = known.get(path.lower().strip(), paths.expand(path))
        if not p.is_dir():
            return f"No folder at {p}."
        os.startfile(str(p))  # type: ignore[attr-defined]
        return f"Opened {p.name or p}."

    def open_website(url_or_search: str) -> str:
        """Open a website, or run a Google search if it isn't a web address.

        Args:
            url_or_search: A URL like "github.com" or words to search for.
        """
        text = url_or_search.strip()
        if " " not in text and "." in text:
            url = text if text.startswith("http") else "https://" + text
        else:
            url = "https://www.google.com/search?q=" + quote_plus(text)
        webbrowser.open(url)
        return f"Opened {url}."

    def search_youtube(query: str) -> str:
        """Search YouTube in the default browser.

        Args:
            query: What to search for.
        """
        webbrowser.open("https://www.youtube.com/results?search_query=" + quote_plus(query))
        return f"Searching YouTube for {query}."

    def open_settings(page: str) -> str:
        """Open a Windows Settings page.

        Args:
            page: One of display, sound, bluetooth, wifi, network, updates, apps, power, storage, notifications, personalization, gaming.
        """
        uri = SETTINGS_PAGES.get(page.lower().strip())
        if not uri:
            return f"Unknown settings page. Options: {', '.join(SETTINGS_PAGES)}."
        os.startfile(uri)  # type: ignore[attr-defined]
        return f"Opened {page} settings."

    # ------------------------------------------------------------------ media and system
    def media_control(action: str, times: int = 1) -> str:
        """Control media playback and volume.

        Args:
            action: One of play_pause, next, previous, mute, volume_up, volume_down.
            times: How many presses (each volume press is about 2 percent).
        """
        import pyautogui

        key = MEDIA_KEYS.get(action.lower().strip())
        if not key:
            return f"Unknown media action. Options: {', '.join(MEDIA_KEYS)}."
        pyautogui.press(key, presses=max(1, min(times, 50)))
        return "Done."

    def take_screenshot() -> str:
        """Take a screenshot of the whole screen and save it in the screenshots folder."""
        import pyautogui

        screenshots.mkdir(parents=True, exist_ok=True)
        out = screenshots / f"{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
        pyautogui.screenshot(str(out))
        return f"Saved screenshot {out.name}."

    def system_status() -> str:
        """Report CPU and memory use, free space on C and E, and battery if there is one."""
        import psutil

        parts = [f"CPU {psutil.cpu_percent(interval=0.3):.0f} percent", f"memory {psutil.virtual_memory().percent:.0f} percent used"]
        for drive in ("C:\\", "E:\\"):
            if Path(drive).exists():
                parts.append(f"{drive[0]} drive {psutil.disk_usage(drive).free / 1e9:.0f} gigabytes free")
        bat = psutil.sensors_battery()
        if bat:
            parts.append(f"battery {bat.percent:.0f} percent")
        return ", ".join(parts) + "."

    def lock_pc() -> str:
        """Lock the PC (Windows + L)."""
        ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
        return "Locked."

    def power(action: str) -> str:
        """Sleep, restart or shut down the PC. Always asks to confirm first.

        Args:
            action: One of sleep, restart, shutdown.
        """
        action = action.lower().strip()
        commands = {
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            "restart": ["shutdown", "/r", "/t", "30"],
            "shutdown": ["shutdown", "/s", "/t", "30"],
        }
        if action not in commands:
            return "Use sleep, restart or shutdown."
        if not confirm(f"{action.capitalize()} the PC now?"):
            return "Cancelled."
        _run(commands[action])
        return "Going to sleep." if action == "sleep" else f"{action.capitalize()} in 30 seconds. Say cancel shutdown to stop it."

    def cancel_shutdown() -> str:
        """Cancel a pending restart or shutdown."""
        _run(["shutdown", "/a"])
        return "Shutdown cancelled."

    def set_timer(minutes: float, label: str = "timer") -> str:
        """Set a spoken timer or reminder.

        Args:
            minutes: How many minutes from now.
            label: What it's for, said aloud when it ends.
        """
        if minutes <= 0 or minutes > 24 * 60:
            return "Pick between 1 minute and 24 hours."
        t = threading.Timer(minutes * 60, lambda: notify(f"Time's up: {label}."))
        t.daemon = True
        t.start()
        return f"Timer set for {minutes:g} minutes."

    def get_current_time() -> str:
        """Get the current local date and time."""
        return dt.datetime.now().strftime("%A %d %B %Y, %I:%M %p")

    # ------------------------------------------------------------------ files
    def find_files(name: str) -> str:
        """Find files whose name contains some text, inside the allowed folders.

        Args:
            name: Part of the file name.
        """
        hits: list[str] = []
        needle = name.lower()
        for root in readable:
            for p in root.rglob("*"):
                if needle in p.name.lower():
                    hits.append(str(p))
                    if len(hits) >= 10:
                        return "Found: " + "; ".join(hits)
        return "Found: " + "; ".join(hits) if hits else "No matching files in the allowed folders."

    def read_text_file(path: str, max_chars: int = 4000) -> str:
        """Read a text file so its contents can be summarised or read aloud. Only allowed folders can be read.

        Args:
            path: Full path to the file.
            max_chars: Maximum characters to return.
        """
        p = paths.expand(path)
        if not _inside(p, readable):
            return "That file is outside the folders I'm allowed to read."
        if not p.is_file():
            return f"No file at {p}."
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:max_chars] + ("\n[truncated]" if len(text) > max_chars else "")

    def undo_last_git_commit(repo: str) -> str:
        """Undo the most recent git commit in a known repository. Keeps the changes (soft reset). Asks to confirm first.

        Args:
            repo: The repository's short name, for example "novara".
        """
        folder = cfg.git_repos.get(repo.lower().strip())
        if not folder:
            return f"'{repo}' is not in my repository list. Known: {', '.join(cfg.git_repos) or 'none'}."
        cwd = paths.expand(folder)
        head = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=cwd, capture_output=True, text=True)
        if head.returncode != 0:
            return f"Couldn't read the git history: {head.stderr.strip()}"
        if subprocess.run(["git", "rev-parse", "--verify", "HEAD~1"], cwd=cwd, capture_output=True).returncode != 0:
            return "That repository has only one commit; I won't undo it."
        if not confirm(f"Undo the last commit in {repo}, '{head.stdout.strip()}'? Your changes stay in the folder."):
            return "Cancelled. Nothing changed."
        res = subprocess.run(["git", "reset", "--soft", "HEAD~1"], cwd=cwd, capture_output=True, text=True)
        return f"git refused: {res.stderr.strip()}" if res.returncode else f"Undid the last commit in {repo}."

    # ------------------------------------------------------------------ hand-off to bigger AIs
    def delegate_task(delegate: str, task: str) -> str:
        """Hand a big task (writing, coding, research, analysis) to a more capable AI. It runs in the background and the user is told when it finishes. Use this for anything that isn't a quick PC action.

        Args:
            delegate: Which AI to use, for example "gemini", "claude" or "gpt". Use the user's choice; if none, pick the first available.
            task: The complete task, written so the other AI can do it without this conversation.
        """
        if not delegate.strip() and jobs.available():
            delegate = next(iter(jobs.available()))
        return jobs.start(delegate, task)

    def task_status() -> str:
        """Report the status of delegated tasks."""
        return jobs.status()

    def read_last_task_result() -> str:
        """Get the full result of the most recently finished delegated task, to summarise it."""
        return jobs.last_result()

    tools: list[Callable] = [
        open_app, open_chrome_profile, close_app, open_folder, open_website, search_youtube, open_settings,
        media_control, take_screenshot, system_status, lock_pc, power, cancel_shutdown,
        set_timer, get_current_time, find_files, read_text_file, undo_last_git_commit,
        task_status, read_last_task_result,
    ]
    if jobs.available():  # only offer hand-off when at least one delegate is set up
        tools.append(delegate_task)
    return [*tools, *(extra or [])]
