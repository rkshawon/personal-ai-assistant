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


def find_window_by_name(name: str) -> int | None:
    """The front-most window of an app by its spoken name: "notepad", "vs code", "kmplayer", "chrome".

    Matches the program's file name first (Notepad.exe, Code.exe, KMPlayer64.exe), then the window title.
    """
    if os.name != "nt":
        return None
    import psutil
    from ctypes import wintypes

    needle = re.sub(r"[^a-z0-9]", "", name.lower())
    if not needle:
        return None
    user32 = _user32()
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    names: dict[int, str] = {}
    by_process: list[int] = []
    by_title: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if not length or not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            try:
                names[pid.value] = re.sub(r"[^a-z0-9]", "", psutil.Process(pid.value).name().lower().removesuffix(".exe"))
            except psutil.Error:
                names[pid.value] = ""
        exe = names[pid.value]
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = re.sub(r"[^a-z0-9]", "", buf.value.lower())
        if exe and len(exe) >= 3 and (needle in exe or exe in needle):
            by_process.append(hwnd)
        elif needle in title:
            by_title.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return (by_process or by_title or [None])[0]


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


def search_files(query: str, roots: list[Path], seconds: float, kind: str = "", limit: int = 10) -> tuple[list[Path], bool]:
    """Find files and folders whose names contain all the words, walking roots in order.
    Returns (matches, finished): finished is False if it ran out of time."""
    exts = KINDS.get(kind.lower().strip().rstrip("s"), set()) if kind else set()
    deadline = time.monotonic() + seconds
    hits: list[Path] = []
    seen: set[str] = set()
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
                    hits.append(Path(dirpath) / name)
                    if len(hits) >= limit:
                        return hits, True
            if time.monotonic() > deadline:
                return hits, False
    return hits, True


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
) -> list[Callable]:
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
    def find_files(name: str, kind: str = "", folder: str = "") -> str:
        """Search the PC for files or folders by name, e.g. a video, song, photo or document.
        Words can be in any order and spelling of dots or dashes doesn't matter: "black clover"
        finds "Black.Clover.S01E05.mkv". Use this before open_file when you don't know the path.

        Args:
            name: Words from the name, e.g. "black clover" or "invoice march".
            kind: Optional: video, audio, image or document, to find only that type.
            folder: Optional folder to search in, e.g. "E:\\Anime" or "Downloads". Empty searches everywhere.
        """
        roots = [resolve_user_path(folder)] if folder.strip() else _search_roots()
        hits, finished = search_files(name, roots, cfg.search_seconds, kind)
        if not hits:
            return f"I found no {kind + ' ' if kind else ''}files named like '{name}'" + (
                "." if finished else f" in {cfg.search_seconds:.0f} seconds of searching; tell me which folder it's in.")
        listed = "; ".join(str(h) for h in hits)
        return f"Found {len(hits)}{'+' if len(hits) >= 10 else ''}: {listed}"

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
        if not exe:
            return f"I don't know where {app} is installed. Known apps: {', '.join(cfg.apps) or 'none'}."
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
        media_control, player_control, type_text, press_keys, switch_to_app,
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
