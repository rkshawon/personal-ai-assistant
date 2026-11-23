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
import re
import shutil
import subprocess
import threading
import time
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
# Ask a question with several spoken answers: choose(question, {"key": ["phrase", ...]}) -> key or None
Choose = Callable[[str, dict[str, list[str]]], "str | None"]

SAVE_CHOICES = {
    "dont_save": ["don't save", "dont save", "do not save", "discard", "don't", "no", "throw it away"],
    "cancel": ["cancel", "keep it open", "leave it", "never mind", "stop", "wait"],
    "save": ["save", "yes", "save it", "keep the changes"],
}

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


def _user32():
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    for fn in ("IsWindowVisible", "IsIconic", "GetWindowTextLengthW", "BringWindowToTop", "SetForegroundWindow"):
        getattr(user32, fn).argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    return user32


def find_window(process_names: list[str]) -> int | None:
    """The front-most visible, titled window of any of these processes (e.g. KMPlayer64.exe), or None."""
    if os.name != "nt":
        return None
    import psutil
    from ctypes import wintypes

    wanted = {n.lower() for n in process_names}
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() in wanted}
    if not pids:
        return None
    user32 = _user32()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)  # top-to-bottom, so the first match is the one used last
    return found[0] if found else None


def foreground_window() -> int | None:
    return _user32().GetForegroundWindow() if os.name == "nt" else None


def focus_window(hwnd: int) -> bool:
    """Bring a window to the front so key presses reach it.

    Windows blocks background programs from stealing focus; attaching to the current
    foreground window's input thread is the documented way around that, without the
    Alt-tap trick (which would open menus in the app you're leaving).
    """
    import time

    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = ctypes.windll.kernel32.GetCurrentThreadId()  # type: ignore[attr-defined]
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(fg_thread, me, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, me, False)
    time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


# What common Windows apps are really called: "task manager" runs as Taskmgr.exe
WINDOWS_APPS = {
    "taskmanager": "taskmgr", "calculator": "calculatorapp", "paint": "mspaint", "commandprompt": "cmd",
    "cmd": "cmd", "powershell": "powershell", "terminal": "windowsterminal", "windowsterminal": "windowsterminal",
    "controlpanel": "control", "settings": "systemsettings", "edge": "msedge", "microsoftedge": "msedge",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "visualstudiocode": "code", "vscode": "code",
    "fileexplorer": "explorer", "photos": "photos", "snippingtool": "snippingtool", "spotify": "spotify",
}
# How to open them directly (Settings, for one, isn't an .exe in the Start menu)
WINDOWS_APP_COMMANDS = {
    "taskmgr": "taskmgr.exe", "calculatorapp": "calc.exe", "mspaint": "mspaint.exe", "cmd": "cmd.exe",
    "powershell": "powershell.exe", "windowsterminal": "wt.exe", "control": "control.exe",
    "systemsettings": "ms-settings:", "msedge": "msedge.exe", "snippingtool": "snippingtool.exe",
}


_start_apps: list[tuple[str, str]] = []  # (name, AppID) from the Start menu
_start_apps_at = 0.0
_start_apps_lock = threading.Lock()


def start_menu_apps(max_age: float = 600) -> list[tuple[str, str]]:
    """Every app in the Start menu, desktop programs and Store apps alike, as (name, AppID).

    Windows' own list (Get-StartApps), so anything you can find in Start search can be opened.
    Cached for 10 minutes; newly installed apps show up after that.
    """
    global _start_apps, _start_apps_at
    if os.name != "nt":
        return _start_apps
    with _start_apps_lock:
        if _start_apps and time.monotonic() - _start_apps_at < max_age:
            return _start_apps
        import json

        try:
            res = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"])
            data = json.loads(res.stdout or "[]")
            data = [data] if isinstance(data, dict) else data
            _start_apps = [(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]
            _start_apps_at = time.monotonic()
            log.info("found %d apps in the Start menu", len(_start_apps))
        except (OSError, ValueError, KeyError) as e:
            log.warning("couldn't read the Start menu apps: %s", e)
        return _start_apps


_KNOWN_FOLDER_IDS = {  # how Start menu AppIDs write common folders
    "{6D809377-6AF0-444B-8957-A3773F02200E}": "%ProgramW6432%",
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": "%ProgramFiles(x86)%",
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": "%SystemRoot%",
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": "%SystemRoot%\\System32",
}


def app_exe_from_id(app_id: str) -> str | None:
    """The .exe behind a Start menu AppID, when it is one ('{6D80...}\\KMPlayer 64X\\KMPlayer64.exe'),
    so a file can be opened with it. Store apps have no .exe path: None."""
    if not app_id.lower().endswith(".exe"):
        return None
    for guid, folder in _KNOWN_FOLDER_IDS.items():
        if app_id.upper().startswith(guid):
            app_id = os.path.expandvars(folder) + app_id[len(guid):]
            break
    return app_id if Path(app_id).exists() else None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_start_app(spoken: str, apps: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
    """Apps whose name fits what was said. Returns (matches, sure): sure means one clear match
    ('antigravity' -> Antigravity); otherwise the caller asks "Did you mean ...?"."""
    wanted = _squash(spoken.removesuffix(" app").removesuffix(" application"))
    if not wanted:
        return [], False
    exact = [a for a in apps if _squash(a[0]) == wanted]
    if exact:
        return exact[:1], True
    starts = sorted((a for a in apps if _squash(a[0]).startswith(wanted)), key=lambda a: len(a[0]))
    if len(starts) == 1:
        return starts, True
    if starts:
        return starts, False  # "word" -> Word, WordPad...: ask about the shortest
    inside = sorted((a for a in apps if wanted in _squash(a[0]) and len(wanted) >= 4), key=lambda a: len(a[0]))
    if inside:
        return inside, len(inside) == 1
    guess = closest_name(spoken, [a[0] for a in apps])
    return ([a for a in apps if a[0] == guess][:1], False) if guess else ([], False)


def app_matches(spoken: str, exe_name: str) -> bool:
    """'kmplayer' matches KMPlayer64.exe, 'vs code' matches Code.exe, 'task manager' Taskmgr.exe."""
    needle = re.sub(r"[^a-z0-9]", "", spoken.lower().removesuffix(".exe"))
    needle = WINDOWS_APPS.get(needle, needle)
    exe = re.sub(r"[^a-z0-9]", "", exe_name.lower().removesuffix(".exe"))
    return len(needle) >= 3 and len(exe) >= 3 and (needle in exe or exe in needle)


# The desktop and taskbar belong to explorer.exe too; sending them "close" opens the shut-down dialog
_SHELL_CLASSES = {"progman", "shell_traywnd", "shell_secondarytraywnd", "workerw"}


def find_windows_by_name(name: str) -> list[tuple[int, int]]:
    """Every visible window of an app by its spoken name, front-most first, as (window, process id).

    Matches the program's file name first (Notepad.exe, Code.exe, KMPlayer64.exe), then the window title.
    """
    if os.name != "nt":
        return []
    import psutil
    from ctypes import wintypes

    needle = re.sub(r"[^a-z0-9]", "", name.lower())
    if not needle:
        return []
    user32 = _user32()
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    names: dict[int, str] = {}
    by_process: list[tuple[int, int]] = []
    by_title: list[tuple[int, int]] = []
    me = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if not length or not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value.lower() in _SHELL_CLASSES:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            return True
        if pid.value not in names:
            try:
                names[pid.value] = psutil.Process(pid.value).name()
            except psutil.Error:
                names[pid.value] = ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = re.sub(r"[^a-z0-9]", "", buf.value.lower())
        if app_matches(name, names[pid.value]):
            by_process.append((hwnd, pid.value))
        elif needle in title:
            by_title.append((hwnd, pid.value))
        return True

    user32.EnumWindows(each, 0)
    return by_process or by_title


def find_window_by_name(name: str) -> int | None:
    """The front-most window of an app by its spoken name: "notepad", "vs code", "kmplayer", "chrome"."""
    found = find_windows_by_name(name)
    return found[0][0] if found else None


def closest_name(spoken: str, choices: list[str]) -> str | None:
    """The choice that sounds closest to what was said: 'game player' -> 'kmplayer', 'note pad' -> 'notepad'."""
    import difflib

    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower().removesuffix(".exe"))  # noqa: E731
    wanted = squash(spoken)
    if not wanted:
        return None
    scored = [(difflib.SequenceMatcher(None, wanted, squash(c)).ratio(), c) for c in choices if squash(c)]
    best = max(scored, default=(0.0, None))
    return best[1] if best[0] >= 0.6 else None


def running_app_names() -> list[str]:
    """Programs that have a window open right now, e.g. ['KMPlayer64', 'chrome', 'Code']."""
    if os.name != "nt":
        return []
    import psutil
    from ctypes import wintypes

    user32 = _user32()
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True

    user32.EnumWindows(each, 0)
    names = set()
    for pid in pids - {os.getpid()}:
        try:
            names.add(psutil.Process(pid).name().removesuffix(".exe"))
        except psutil.Error:
            pass
    return sorted(names - {"explorer", "ApplicationFrameHost", "TextInputHost", "SystemSettings"})


def _amolex_console() -> int:
    """The console window Amolex runs in (it's in front whenever you just used it; skip it)."""
    try:
        return ctypes.windll.kernel32.GetConsoleWindow() or 0  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0


def list_windows() -> list[tuple[int, str, str]]:
    """Visible app windows, front-most first, as (handle, title, program) - not Amolex's own console."""
    if os.name != "nt":
        return []
    import psutil
    from ctypes import wintypes

    user32 = _user32()
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    console, me = _amolex_console(), os.getpid()
    found: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if not length or not user32.IsWindowVisible(hwnd) or hwnd == console:
            return True  # untitled, hidden, or Amolex's own console
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value.lower() in _SHELL_CLASSES:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        try:
            program = psutil.Process(pid.value).name().removesuffix(".exe")
        except psutil.Error:
            program = ""
        if "start.bat" in title.value.lower() or "\\amolex" in title.value.lower():
            return True  # the terminal running Amolex
        found.append((hwnd, title.value, program))
        return True

    user32.EnumWindows(each, 0)
    return found


_UIA_LABELS = {
    "ButtonControl": "button", "TextControl": "", "EditControl": "text box", "DocumentControl": "document",
    "MenuItemControl": "menu", "TabItemControl": "tab", "ListItemControl": "item", "HyperlinkControl": "link",
    "CheckBoxControl": "checkbox", "ComboBoxControl": "dropdown", "TreeItemControl": "item",
    "HeaderItemControl": "column", "DataItemControl": "cell", "TitleBarControl": "",
}


def read_window_text(hwnd: int, max_chars: int = 3000, seconds: float = 4) -> str:
    """The text a screen reader would read in a window: labels, buttons, tabs, and what's typed in
    documents and boxes. Pictures, video and games have no text to read."""
    try:
        import uiautomation as auto
    except ImportError:
        return "I can't read windows: the uiautomation package isn't installed yet. Restart Amolex once."
    lines: list[str] = []
    seen: set[str] = set()
    size = 0
    deadline = time.monotonic() + seconds
    with auto.UIAutomationInitializerInThread():
        top = auto.ControlFromHandle(hwnd)
        if top is None:
            return ""
        for control, _depth in auto.WalkControl(top, maxDepth=30):
            if time.monotonic() > deadline or size > max_chars:
                break
            kind = control.ControlTypeName
            if kind not in _UIA_LABELS:
                continue
            try:
                if control.IsOffscreen:
                    continue
                text = ""
                if kind in ("EditControl", "DocumentControl"):
                    try:
                        text = control.GetValuePattern().Value
                    except Exception:
                        try:
                            text = control.GetTextPattern().DocumentRange.GetText(max_chars)
                        except Exception:
                            text = ""
                text = re.sub(r"\s+", " ", (text or control.Name or "")).strip()
            except Exception:  # controls can vanish while we read them
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            label = _UIA_LABELS[kind]
            line = f"{label}: {text}" if label else text
            lines.append(line)
            size += len(line)
    out = "\n".join(lines)
    return out[:max_chars] + (" [more not read]" if len(out) > max_chars else "")


def save_prompt_role(button_name: str) -> str | None:
    """What a button in a "save your changes?" prompt does: Notepad's "Save" / "Don't save" /
    "Cancel", classic "&Save" / "Do&n't Save", or a Yes / No / Cancel box."""
    n = re.sub(r"\s+", " ", button_name.replace("&", "").replace("’", "'").strip().lower())
    if n in {"don't save", "dont save", "do not save", "no", "discard", "discard changes", "close without saving",
             "don't save changes"}:
        return "dont_save"
    if n in {"save", "save changes", "yes", "save all"}:
        return "save"
    if n == "cancel":
        return "cancel"
    return None


def answer_save_prompt(hwnds: list[int], decide: Callable[[], str | None]) -> str | None:
    """If one of these windows shows a "save your changes?" prompt, ask decide() what to do and press
    that button. Returns the choice ('save' / 'dont_save' / 'cancel'), 'unanswered' if decide() gave
    nothing (no button is pressed then), or None if there's no such prompt or it can't be read."""
    if os.name != "nt":
        return None
    try:
        import uiautomation as auto
    except ImportError:
        log.warning("uiautomation isn't installed, so I can't read other apps' buttons")
        return None
    with auto.UIAutomationInitializerInThread():
        buttons: dict[str, object] = {}
        deadline = time.monotonic() + 4  # big windows (Chrome) have huge trees: don't walk them forever
        for hwnd in hwnds:
            top = auto.ControlFromHandle(hwnd)
            if top is None:
                continue
            found: dict[str, object] = {}
            for control, _depth in auto.WalkControl(top, maxDepth=15):
                if control.ControlTypeName == "ButtonControl":
                    role = save_prompt_role(control.Name or "")
                    if role and role not in found:
                        found[role] = control
                if time.monotonic() > deadline:
                    break
            if "save" in found and "dont_save" in found:  # both, or it isn't a save prompt
                buttons = found
                break
        if not buttons:
            return None
        choice = decide()
        if choice not in buttons:
            return "unanswered" if choice is None else choice
        buttons[choice].GetInvokePattern().Invoke()  # type: ignore[attr-defined]
        return choice


def _process_matches(pid: int, name: str) -> bool:
    """Closing only targets the app's own process, never a window that just has the name in its title."""
    import psutil

    try:
        return app_matches(name, psutil.Process(pid).name())
    except psutil.Error:
        return False


def get_clipboard() -> str | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32  # type: ignore[attr-defined]
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not u32.OpenClipboard(None):
        return None
    try:
        handle = u32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        ptr = k32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


def set_clipboard(text: str) -> None:
    from ctypes import wintypes

    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32  # type: ignore[attr-defined]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    data = (text + "\0").encode("utf-16-le")
    for _ in range(20):  # another app may hold the clipboard for a moment
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("the clipboard is busy")
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE; owned by Windows after SetClipboardData
        ptr = k32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(handle)
        u32.SetClipboardData(13, handle)
    finally:
        u32.CloseClipboard()


_CSIDL = {"documents": 0x05, "document": 0x05, "desktop": 0x10, "pictures": 0x27, "music": 0x0D, "videos": 0x0E}


def known_folder(name: str) -> Path | None:
    """Your real Documents / Desktop / Downloads... even when OneDrive has moved them."""
    key = name.lower().strip().removeprefix("my ").strip()
    if key in ("downloads", "download"):
        return Path.home() / "Downloads"
    if key not in _CSIDL:
        return None
    if os.name == "nt":
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, _CSIDL[key], None, 0, buf) == 0:  # type: ignore[attr-defined]
            return Path(buf.value)
    return Path.home() / {"document": "Documents"}.get(key, key.capitalize())


def resolve_user_path(path: str) -> Path:
    """'notes.txt' -> Documents\\notes.txt, 'desktop/todo.txt' -> the real Desktop, full paths as given."""
    raw = path.strip().strip('"')
    full = paths.expand(raw)
    if full.is_absolute() or re.match(r"^[a-zA-Z]:", raw):
        return full
    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    base = known_folder(parts[0]) if parts else None
    if base is not None:
        return base.joinpath(*parts[1:])
    return (known_folder("documents") or Path.home() / "Documents").joinpath(*parts)


# Never run through PowerShell, whatever the model asks: permanent deletion (use recycle_item, which
# goes to the Recycle Bin), disk/boot/partition tools, security policy changes, shutdown (use power).
_PS_BLOCKED = re.compile(
    r"\b(remove-item|rm|del|erase|rmdir|rd|ri|format(-volume)?|diskpart|bcdedit|clear-disk|initialize-disk|"
    r"remove-partition|set-executionpolicy|vssadmin|cipher|takeown|icacls|stop-computer|restart-computer|"
    r"shutdown|reg\s+delete|remove-itemproperty|clear-content|set-mppreference|disable-\w+|invoke-expression|iex|"
    r"invoke-webrequest|iwr|curl|wget|start-bitstransfer|downloadstring|downloadfile)\b",
    re.IGNORECASE,
)


def powershell_blocked(command: str) -> str | None:
    """Why a PowerShell command is refused, or None if it may run (after your spoken yes)."""
    m = _PS_BLOCKED.search(command)
    if not m:
        return None
    word = m.group(1).lower()
    if word in ("remove-item", "rm", "del", "erase", "rmdir", "rd", "ri", "clear-content"):
        return "I don't delete through PowerShell. I can send things to the Recycle Bin instead."
    if word in ("invoke-webrequest", "iwr", "curl", "wget", "start-bitstransfer", "downloadstring", "downloadfile",
                "invoke-expression", "iex"):
        return "I don't download or run code from the internet."
    return f"I won't run '{m.group(1)}': it can damage Windows or your disks."


# Commands that only look at things run without asking (RK: "edit and delete need my permission,
# other tasks should just happen"). Anything not clearly look-only asks first.
_PS_SAFE_START = re.compile(
    r"^(get|test|select|where|sort|measure|format|out-string|out-host|convertto|convertfrom|resolve|find|compare|"
    r"group|join-path|split-path)\b|^(ipconfig|systeminfo|whoami|hostname|tasklist|ping|nslookup|tracert|where|"
    r"dir|ls|gci|gc|cat|type|echo|write-output|pwd|date|ver|nvidia-smi|driverquery|wmic|netstat|getmac)\b",
    re.IGNORECASE,
)
_PS_CHANGES = re.compile(
    r"\b(?:set|new|rename|move|copy|add|clear|remove|stop|start|restart|disable|enable|install|uninstall|update|"
    r"export|import|invoke|register|unregister|mount|dismount|suspend|resume|reset|grant|revoke|block|unblock|"
    r"lock|unlock|send|connect|disconnect|initialize|publish|save|expand|compress|merge)-\w+"
    r"|\bwrite-(?!output\b|host\b)\w+|\b(?:out-file|tee-object)\b"
    r"|\b(?:netsh|sc|reg|taskkill|attrib|robocopy|xcopy|copy|move|ren|rename|mkdir|md|winget|choco|pip|npm|"
    r"msiexec|powercfg|schtasks)\b|\bwmic\s+\w+\s+(?:call|delete|set)\b|>",
    re.IGNORECASE,
)


def powershell_needs_permission(command: str) -> bool:
    """False only for commands that clearly just look: every step starts with a look-only command
    (Get-..., ipconfig, dir...) and nothing anywhere changes, writes or starts something."""
    if _PS_CHANGES.search(command):
        return True
    steps = [s.strip().lstrip("(").strip() for s in re.split(r"\||;|&&|\n", command) if s.strip()]
    steps = [re.sub(r"^\$\w+\s*=\s*", "", s) for s in steps]  # "$ip = Get-NetIPAddress" -> "Get-NetIPAddress"
    return not steps or not all(_PS_SAFE_START.match(s) for s in steps)


# Folder names a file search never walks into: system, caches, and other programs' internals
_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
    "system volume information", "recovery", "node_modules", ".git", "__pycache__", ".venv", "venv",
    "site-packages", "msocache", "perflogs", "$windows.~bt", "$windows.~ws", "windowsapps",
}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".opus"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff"}
DOC_EXT = {".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".md", ".csv", ".rtf", ".odt"}
KINDS = {"video": VIDEO_EXT, "audio": AUDIO_EXT, "music": AUDIO_EXT, "image": IMAGE_EXT, "photo": IMAGE_EXT,
         "document": DOC_EXT}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def name_matches(file_name: str, query: str) -> bool:
    """'black clover' matches 'Black.Clover.S01E05.1080p.mkv' and 'black_clover-ep5.mp4'."""
    hay = " " + _norm(file_name) + " "
    joined = hay.replace(" ", "")
    return all(w in hay or w in joined for w in _norm(query).split())


FUZZY_MIN = 0.75  # every spoken word must be at least this close to a word in the name


def match_score(file_name: str, query: str) -> float:
    """1.0 for a real match; 0.75-0.99 when the name is close ('black cover' vs Black.Clover, as
    Whisper heard it on RK's PC); 0 otherwise. Close matches are confirmed with the user first."""
    if name_matches(file_name, query):
        return 1.0
    import difflib

    tokens = _norm(os.path.splitext(file_name)[0]).split()
    words = _norm(query).split()
    if not tokens or not words:
        return 0.0
    sims = [max(difflib.SequenceMatcher(None, w, t).ratio() for t in tokens) for w in words]
    return min(0.99, sum(sims) / len(sims)) if min(sims) >= FUZZY_MIN else 0.0


def pretty_name(path: Path | str) -> str:
    """How to say a file's name: '[SubsPlease] Black Clover S2 - 175 (1080p) [19BCC639].mkv.mp4'
    -> 'Black Clover S2 175'."""
    name = Path(path).name
    while os.path.splitext(name)[1] and len(os.path.splitext(name)[1]) <= 5:
        name = os.path.splitext(name)[0]
    name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)
    name = re.sub(r"[._\-]+", " ", name)
    return re.sub(r"\s+", " ", name).strip() or Path(path).name


def natural_key(path: Path) -> list:
    """Episode 2 before episode 10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def fixed_drives() -> list[Path]:
    """Your hard drives and SSDs (C:, D:, E:...), not DVD or network drives."""
    if os.name != "nt":
        return [Path("/")]
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = k32.GetLogicalDrives()
    drives = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                drives.append(Path(root))
    return drives


def search_files(
    query: str, roots: list[Path], seconds: float, kind: str = "", limit: int = 10
) -> tuple[list[tuple[Path, float]], bool]:
    """Find files and folders named like the query, walking roots in order.

    Returns ([(path, score)], finished): real matches (score 1.0) first, in episode order, then close
    ones, best first. finished is False if it ran out of time.
    """
    exts = KINDS.get(kind.lower().strip().rstrip("s"), set()) if kind else set()
    words = _norm(query).split()
    deadline = time.monotonic() + seconds
    exact: list[Path] = []
    close: list[tuple[Path, float]] = []
    seen: set[str] = set()
    finished = True

    def result():
        ranked_close = sorted(close, key=lambda c: (-c[1], natural_key(c[0])))[:limit]  # equal: episode order
        return [(p, 1.0) for p in sorted(exact, key=natural_key)[:limit]] + ranked_close

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            key = os.path.normcase(dirpath)
            if key in seen:
                dirnames[:] = []
                continue
            seen.add(key)
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames + ([] if exts else dirnames):
                if exts and os.path.splitext(name)[1].lower() not in exts:
                    continue
                if name_matches(name, query):
                    exact.append(Path(dirpath) / name)
                    if len(exact) >= limit:
                        return result(), True
                elif len(words) == 1 or any(w in _norm(name).split() for w in words):  # cheap pre-check
                    score = match_score(name, query)
                    if score:
                        close.append((Path(dirpath) / name, score))
            if time.monotonic() > deadline:
                finished = False
                break
        if not finished:
            break
    return result(), finished


def say_seconds(total: int) -> str:
    """90 -> '1 minute 30 seconds'."""
    minutes, seconds = divmod(int(total), 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if seconds or not minutes:
        parts.append(f"{seconds} second" + ("s" if seconds != 1 else ""))
    return " ".join(parts)


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
    choose: Choose | None = None,
) -> list[Callable]:
    if choose is None:  # no multi-answer questions available: fall back to yes/no
        def choose(question: str, options: dict[str, list[str]]) -> str | None:
            return "save" if confirm(question.split(" Say ")[0] + " Save them first?") else None

    screenshots = paths.PROJECT_DIR / "screenshots"
    anywhere = "*" in cfg.writable_folders
    writable = [paths.expand(p).resolve() for p in cfg.writable_folders if p != "*"]
    writable += [f.resolve() for f in (known_folder("documents"), known_folder("desktop")) if f]
    protected = [paths.PROJECT_DIR.resolve(), paths.HOME_DIR.resolve()]  # Amolex's own code, config and models
    windows_dirs = [paths.expand(p).resolve() for p in (
        "%SystemRoot%", "%ProgramFiles%", "%ProgramFiles(x86)%", "%ProgramData%") if "%" not in os.path.expandvars(p)]

    def _cannot_change(p: Path) -> str | None:
        if _inside(p, protected):
            return "That's inside Amolex's own folders, so I won't change it."
        if _inside(p, windows_dirs):
            return "That's one of Windows' own folders, so I won't change it."
        if p.resolve() == Path(p.anchor).resolve():
            return "I won't change a whole drive."
        if not anywhere and not _inside(p, writable):
            return "I can only change files in " + ", ".join(str(w) for w in dict.fromkeys(writable)) + "."
        return None

    def _search_roots() -> list[Path]:
        """Your own folders first (Videos, Downloads, Desktop, Documents...), then every drive."""
        mine = [known_folder(n) for n in ("videos", "downloads", "desktop", "documents", "music", "pictures")]
        roots = [p for p in mine if p and p.is_dir()]
        return roots + [d for d in fixed_drives() if d not in roots]

    def _focus(app: str) -> str | None:
        """Bring an app's window to the front; returns an error to speak, or None."""
        if not app.strip():
            return None
        hwnd = None
        for _ in range(20):  # an app opened a moment ago may need a few seconds to show its window
            hwnd = find_window_by_name(app)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return f"I can't find an open {app} window. Open it first."
        return None if focus_window(hwnd) else f"Windows didn't let me switch to {app}."

    # ------------------------------------------------------------------ apps and places
    def open_app(name: str) -> str:
        """Open ANY installed app by name, e.g. "VS Code", "Antigravity", "Steam", "Spotify", "Calculator".
        Works for every app in the Windows Start menu, so don't search for it as a file.

        Args:
            name: The application's name as the user said it.
        """
        key = name.lower().strip()
        squashed = key.replace(" ", "")
        # 1. nicknames from config.json, 2. built-in Windows tools, 3. everything in the Start menu
        target = cfg.apps.get(key) or next(
            (v for k, v in cfg.apps.items() if k in key or key in k or k.replace(" ", "") == squashed), None)
        if not target:  # a built-in Windows app: Task Manager, Calculator, Paint, Settings...
            target = WINDOWS_APP_COMMANDS.get(WINDOWS_APPS.get(squashed.replace(" ", ""), ""))
        if not target:
            matches, sure = match_start_app(name, start_menu_apps())
            if matches:
                app_name, app_id = matches[0]
                if not sure and not confirm(f"Did you mean {app_name}?"):
                    others = ", ".join(a[0] for a in matches[1:5])
                    return "Okay, I didn't open anything." + (f" Close matches: {others}." if others else "")
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], close_fds=True)
                return f"Opened {app_name}."
        if not target:
            guess = closest_name(name, list(cfg.apps))
            if guess and confirm(f"I don't have {name}. Did you mean {guess}?"):
                name, target = guess, cfg.apps[guess]
            else:
                return f"I couldn't find an app called {name} on this PC."
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

    def close_app(name: str, force: bool = False) -> str:
        """Close an app the polite way, like clicking its X. If it asks to save changes, this asks the
        user (save, don't save or cancel) and presses the answer, so call it only once per request.

        Args:
            name: The app as the user said it, e.g. "KMPlayer", "Notepad", "Chrome".
            force: True only if it's frozen or ignored a normal close; the user is asked first,
                because unsaved work is lost.
        """
        import psutil

        windows = [(h, pid) for h, pid in find_windows_by_name(name) if _process_matches(pid, name)]
        pids = {pid for _, pid in windows} | {
            p.pid for p in psutil.process_iter(["name"])
            if app_matches(name, p.info["name"] or "") and (p.info["name"] or "").lower() != "explorer.exe"
        }
        pids.discard(os.getpid())  # never close Amolex itself
        if not pids:
            guess = closest_name(name, running_app_names())
            if guess and confirm(f"{name} isn't open. Did you mean {guess}?"):
                return close_app(guess, force)
            return f"{name} isn't running."
        if force:
            if not confirm(f"Force {name} to close? Anything unsaved in it is lost."):
                return "Cancelled."
            for pid in pids:
                try:
                    psutil.Process(pid).kill()
                except psutil.Error:
                    pass
            return f"Force-closed {name}."
        if not windows:
            return f"{name} is running in the background with no window. Say force close {name} to end it."
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        posted = [user32.PostMessageW(hwnd, 0x0010, 0, 0) for hwnd, _ in windows]  # WM_CLOSE: like clicking X
        if not any(posted):  # Windows refuses messages to apps running as administrator (Task Manager)
            return (f"Windows won't let me close {name} because it runs as administrator. "
                    "Run Amolex as administrator to control it.")

        def open_windows() -> list[int]:
            if not any(psutil.pid_exists(pid) for pid in pids):
                return []
            return [h for h, pid in find_windows_by_name(name) if _process_matches(pid, name)]

        def wait_closed(seconds: float) -> list[int]:
            remaining = open_windows()
            for _ in range(int(seconds / 0.25)):
                if not remaining:
                    break
                time.sleep(0.25)
                remaining = open_windows()
            return remaining

        question = f"{name} wants to know whether to save your changes. Say save, don't save, or cancel."
        remaining = wait_closed(2)
        last = None
        for _ in range(6):  # one prompt per unsaved document (Notepad asks for each tab)
            if not remaining:
                return f"Closed {name}" + (" without saving." if last == "dont_save" else ".")
            choice = answer_save_prompt(remaining, lambda: choose(question, SAVE_CHOICES))
            if choice is None:
                break
            if choice == "unanswered":
                return f"I didn't catch an answer, so I left {name} open with its save question."
            if choice == "cancel":
                return f"Okay, I left {name} open."
            last = choice
            remaining = wait_closed(2)
        if last == "save":  # a new, never-saved document opens a Save As window
            return f"{name} is asking where to save it. Pick a name and folder on screen, and it will close."
        return f"{name} is still open; it may be asking you something I can't read."

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

    def player_control(action: str, seconds: float = 0) -> str:
        """Control the video player (KMPlayer): pause or resume, skip forward, or go back.

        Args:
            action: One of play_pause, forward, back.
            seconds: How far to skip for forward or back. 0 means the default (10 seconds).
        """
        import pyautogui

        p = cfg.player
        act = action.lower().strip().replace(" ", "_")
        hwnd = find_window(p.process_names)
        if act in ("play_pause", "pause", "play", "resume"):
            if not hwnd:  # no KMPlayer: the media key still reaches Spotify, YouTube in Chrome, etc.
                pyautogui.press("playpause")
                return "Done."
            key, presses = "space", 1
        elif act in ("forward", "back", "backward", "rewind"):
            if not hwnd:
                return "KMPlayer isn't open."
            wanted = seconds if seconds and seconds > 0 else p.default_seek_seconds
            presses = max(1, min(round(wanted / p.seek_step_seconds), 720))  # cap: 1 hour at 5 s a press
            key = "right" if act == "forward" else "left"
        else:
            return "Unknown player action. Options: play_pause, forward, back."

        previous = foreground_window()
        if not focus_window(hwnd):
            return "I couldn't switch to KMPlayer."
        pyautogui.press(key, presses=presses, interval=0.01)
        if previous and previous != hwnd:
            focus_window(previous)  # hand focus back to whatever you were using
        if key == "space":
            return "Done."
        return f"{'Forward' if key == 'right' else 'Back'} {say_seconds(presses * p.seek_step_seconds)}."

    # ------------------------------------------------------------------ typing and windows
    def type_text(text: str, app: str = "") -> str:
        """Type text into an app, as if the user typed it, for example "Hello World" into Notepad.

        Args:
            text: Exactly what to type. Use \\n for new lines.
            app: The app to type into, for example "notepad". Empty means the window in front.
        """
        import pyautogui

        if error := _focus(app):
            return error
        previous = get_clipboard()
        set_clipboard(text)  # paste instead of key-by-key: fast, and works for any language
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        if previous is not None:
            set_clipboard(previous)  # put your clipboard back
        return f"Typed it into {app}." if app else "Typed it."

    def press_keys(keys: str, app: str = "") -> str:
        """Press a key or shortcut in an app, for example "ctrl+s" to save, "enter", "alt+f4", "ctrl+a".

        Args:
            keys: Key names joined with +, like "ctrl+shift+t" or "enter".
            app: The app to press them in. Empty means the window in front.
        """
        import pyautogui

        parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
        alias = {"control": "ctrl", "windows": "win", "escape": "esc", "return": "enter", "delete": "del"}
        parts = [alias.get(k, k) for k in parts]
        bad = [k for k in parts if k not in pyautogui.KEYBOARD_KEYS]
        if not parts or bad:
            return f"I don't know the key {', '.join(bad) or keys}."
        if error := _focus(app):
            return error
        pyautogui.hotkey(*parts)
        return f"Pressed {keys}."

    def switch_to_app(app: str) -> str:
        """Bring an already-open app's window to the front, for example "Chrome" or "VS Code".

        Args:
            app: The app's name.
        """
        return _focus(app) or f"Switched to {app}."

    # ------------------------------------------------------------------ seeing the screen
    def list_open_windows() -> str:
        """List every open window (title and app), front-most first. Use it to work out which app or
        document the user means, e.g. "close that", "the video", "the other one"."""
        windows = list_windows()
        if not windows:
            return "No app windows are open."
        return "Open windows, front first: " + "; ".join(
            f"{title} ({program})" if program else title for _, title, program in windows[:20])

    def read_screen(app: str = "") -> str:
        """Read the text on screen in the window in front (or a named app's window): its title, buttons,
        menus, labels and the text in documents, pages and boxes, like a screen reader. Call this first
        when the user says "this", "that", "it", "what's on my screen" or refers to something they see.
        Pictures, video and games have no readable text.

        Args:
            app: Optional app to read instead of the one in front, e.g. "chrome" or "notepad".
        """
        if app.strip():
            hwnd = find_window_by_name(app)
            if not hwnd:
                return f"I can't find an open {app} window."
            match = next(((h, t, p) for h, t, p in list_windows() if h == hwnd), (hwnd, app, app))
        else:
            windows = list_windows()
            if not windows:
                return "No app windows are open."
            match = windows[0]
        hwnd, title, program = match
        text = read_window_text(hwnd)
        others = [t for h, t, _ in list_windows()[:6] if h != hwnd]
        return (f"In front: {title} ({program}).\n" + (text or "It has no readable text (a picture, video or game?).")
                + ("\nOther open windows: " + "; ".join(others) if others else ""))

    # ------------------------------------------------------------------ files and folders
    def write_file(path: str, content: str, append: bool = False) -> str:
        """Create a text file, or replace or add to one. A new file is made straight away; changing an
        existing one asks the user first.

        Args:
            path: A file name like "notes.txt" (saved in Documents), "Desktop\\todo.txt", or a full path.
            content: The text to put in the file.
            append: True to add to the end of an existing file instead of replacing it.
        """
        p = resolve_user_path(path)
        if error := _cannot_change(p):
            return error
        if p.is_dir():
            return f"{p} is a folder."
        if p.exists():
            question = f"Add to the end of {p.name}?" if append else f"{p.name} already exists. Replace it?"
            if not confirm(question):
                return "Cancelled. The file is unchanged."
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return f"{'Added to' if append else 'Saved'} {p.name} in {p.parent}."

    def create_folder(path: str) -> str:
        """Create a folder, for example "Projects" (inside Documents), "Desktop\\Screenshots" or "E:\\Movies\\New".

        Args:
            path: Folder name or full path.
        """
        p = resolve_user_path(path)
        if error := _cannot_change(p):
            return error
        if p.is_dir():
            return f"{p.name} already exists in {p.parent}."
        p.mkdir(parents=True)
        return f"Created the folder {p.name} in {p.parent}."

    def list_folder(path: str) -> str:
        """List what's in a folder, for example "Downloads", "Desktop" or "E:\\Movies".

        Args:
            path: Folder name or full path.
        """
        p = resolve_user_path(path)
        if not p.is_dir():
            return f"No folder at {p}."
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        names = [x.name + ("\\" if x.is_dir() else "") for x in items[:40]]
        more = f" and {len(items) - 40} more" if len(items) > 40 else ""
        return f"{p} has {len(items)} items: " + ", ".join(names) + more if items else f"{p} is empty."

    def move_item(source: str, destination: str) -> str:
        """Move or rename a file or folder. Asks to confirm first.

        Args:
            source: The file or folder now, e.g. "Downloads\\report.pdf".
            destination: The new folder or new full name, e.g. "Documents\\Reports" or "Desktop\\final.pdf".
        """
        src, dst = resolve_user_path(source), resolve_user_path(destination)
        for p in (src, dst):
            if error := _cannot_change(p):
                return error
        if not src.exists():
            return f"There's nothing at {src}."
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            return f"{dst.name} already exists there; I won't overwrite it."
        if not confirm(f"Move {src.name} to {dst.parent}" + (f" as {dst.name}" if dst.name != src.name else "") + "?"):
            return "Cancelled. Nothing moved."
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {src.name} to {dst}."

    def recycle_item(path: str) -> str:
        """Send a file or folder to the Recycle Bin (it can be restored from there). Asks to confirm first.

        Args:
            path: The file or folder, e.g. "Desktop\\old notes.txt".
        """
        p = resolve_user_path(path)
        if error := _cannot_change(p):
            return error
        if not p.exists():
            return f"There's nothing at {p}."
        if p in writable or any(p == known_folder(n) for n in ("documents", "desktop", "downloads")):
            return "I won't recycle a whole main folder."
        if not confirm(f"Send {p.name} to the Recycle Bin?"):
            return "Cancelled. Nothing changed."
        kind = "DeleteDirectory" if p.is_dir() else "DeleteFile"
        literal = str(p).replace("'", "''")
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"[Microsoft.VisualBasic.FileIO.FileSystem]::{kind}('{literal}', 'OnlyErrorDialogs', 'SendToRecycleBin')"
        )
        res = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        if res.returncode or p.exists():
            return f"Windows couldn't recycle it: {(res.stderr or 'unknown error').strip()[:200]}"
        return f"Sent {p.name} to the Recycle Bin."

    # ------------------------------------------------------------------ anything else
    def run_powershell(command: str, summary: str) -> str:
        """Run a PowerShell command for a PC task no other tool covers, e.g. checking the IP address,
        disk space or running programs, renaming files in bulk, or changing a setting. Commands that
        only look run straight away; ones that change something ask the user first. Deleting files
        and downloading from the internet are refused.

        Args:
            command: The PowerShell command.
            summary: What it does, in a few plain spoken words, e.g. "show your IP address".
        """
        if not cfg.allow_powershell:
            return "Running PowerShell commands is turned off in my settings."
        if reason := powershell_blocked(command):
            log.warning("refused PowerShell: %s", command)
            return reason
        if powershell_needs_permission(command) and not confirm(f"I'll {summary.rstrip('.')}. Go ahead?"):
            return "Cancelled. Nothing ran."
        log.info("PowerShell: %s", command)
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return "It ran for over a minute, so I stopped it."
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        if res.returncode and err:
            return f"It failed: {err[:500]}"
        return out[:1500] or "It ran with no output."

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
    def find_files(name: str, kind: str = "", folder: str = "", pick_one: bool = False) -> str:
        """Search the PC for files or folders by name, e.g. a video, song, photo or document.
        Names don't have to be exact: word order, dots and dashes don't matter, and close spellings
        are found too ("black cover" finds "Black.Clover.S01E05.mkv"). Use this before open_file,
        move_item etc. when you don't know the path.

        Args:
            name: Words from the name as the user said them, e.g. "black clover" or "invoice march".
            kind: Optional: video, audio, image or document, to find only that type.
            folder: Optional folder to search in, e.g. "E:\\Anime" or "Downloads". Empty searches everywhere.
            pick_one: True when the user wants ONE file to act on (play, open, move...). If the match
                isn't exact, or there are several, the user is asked to confirm, and the one path to
                use is returned.
        """
        roots = [resolve_user_path(folder)] if folder.strip() else _search_roots()
        hits, finished = search_files(name, roots, cfg.search_seconds, kind)
        what = f"{kind} files" if kind else "files"
        if not hits:
            return f"I found no {what} named like '{name}'" + (
                "." if finished else f" in {cfg.search_seconds:.0f} seconds of searching; ask which folder it's in.")
        exact = [p for p, s in hits if s >= 1.0]
        close = [p for p, s in hits if s < 1.0]
        if not pick_one:
            parts = [f"Found {len(exact)}: " + "; ".join(map(str, exact))] if exact else []
            if close:
                parts.append("Close but not exact (check with the user before using): " + "; ".join(map(str, close)))
            return " ".join(parts)

        # one file to act on: verify anything that isn't a single clear match (RK's rule)
        if len(exact) == 1:
            return f"Use this file: {exact[0]}"
        if exact:
            question = f"I found {len(exact)} matches. Do you mean {pretty_name(exact[0])}?"
            candidates = exact
        else:
            question = f"I couldn't find {name} exactly. The closest is {pretty_name(close[0])}. Is that it?"
            candidates = close
        if confirm(question):
            return f"The user confirmed. Use this file: {candidates[0]}"
        options = "; ".join(pretty_name(p) for p in candidates[1:6])
        return ("The user said that's not the one. " + (f"Tell them the other matches and ask which: {options}."
                if options else "There are no other matches; ask them for the exact name or folder."))

    def open_file(path: str, app: str = "") -> str:
        """Open a file or folder, in its usual app or a named one, e.g. a video in KMPlayer.

        Args:
            path: Full path (from find_files), or a name like "Desktop\\notes.txt".
            app: Optional app to open it with, e.g. "kmplayer" or "vs code". Empty uses the usual app.
        """
        p = resolve_user_path(path)
        if not p.exists():
            return f"There's nothing at {p}. Search for it with find_files first."
        if not app.strip():
            os.startfile(str(p))  # type: ignore[attr-defined]
            return f"Opened {p.name}."
        key = app.lower().strip()
        target = cfg.apps.get(key) or next((v for k, v in cfg.apps.items() if k in key or key in k), None)
        exe = os.path.expandvars(target) if target else None
        if exe and not Path(exe).exists():
            exe = shutil.which(exe)
        if not exe:  # any installed app whose Start menu entry points at its .exe (VLC, Photoshop...)
            matches, sure = match_start_app(app, start_menu_apps())
            if matches and (sure or confirm(f"Open it with {matches[0][0]}?")):
                exe = app_exe_from_id(matches[0][1])
        if not exe:
            return f"I can't open files with {app} directly. I can open the file in its usual app instead."
        subprocess.Popen([exe, str(p)], close_fds=True)
        return f"Opened {p.name} in {app}."

    def read_text_file(path: str, max_chars: int = 4000) -> str:
        """Read a text file so its contents can be summarised or read aloud.

        Args:
            path: Full path to the file, or a name like "Documents\\notes.txt".
            max_chars: Maximum characters to return.
        """
        p = resolve_user_path(path)
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
        media_control, player_control, list_open_windows, read_screen, type_text, press_keys, switch_to_app,
        write_file, create_folder, list_folder, move_item, recycle_item,
        take_screenshot, system_status, lock_pc, power, cancel_shutdown,
        set_timer, get_current_time, find_files, open_file, read_text_file, undo_last_git_commit,
        task_status, read_last_task_result,
    ]
    if cfg.allow_powershell:
        tools.append(run_powershell)
    if jobs.available():  # only offer hand-off when at least one delegate is set up
        tools.append(delegate_task)
    return [*tools, *(extra or [])]
