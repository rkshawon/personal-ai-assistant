# Amolex-9026

A voice assistant for RK's Windows PC. Simple commands ("shoot", "next reel") act instantly
through a local recognizer. Anything else ("Hey Amolex, open VS Code") goes to the **local brain**
(Qwen3 8B on Ollama, running on the RX 9060 XT), which does PC and Windows tasks itself and hands
big tasks (writing, coding, research) to a bigger AI, then tells you when they're finished.

- Project code: `E:\Amolex`
- Everything installed (uv, Python, packages, models, Ollama): `E:\ProgramData\Amolex`
  (change with the `AMOLEX_HOME` environment variable)

## First run

1. Double-click **start.bat**. The first run installs everything into `E:\ProgramData\Amolex`
   (about 7–8 GB, mostly Ollama and the Qwen3 8B model). Answer **y** when it asks about Ollama.
2. Run `start.bat --doctor` and fix anything marked `!!`. It also checks the model runs on the GPU.
3. Run `start.bat --test-voice` and tune the voice commands (next section).
4. Run `start.bat`. A tray icon appears (grey = standby).

## Tuning voice commands (start here)

`start.bat --test-voice` listens and prints what it hears. It **executes nothing**: no keys,
no AI, no speech. For each phrase you see one of:

- `WAKE`: the wake phrase was recognised.
- `FIRE 'shoot' -> click left`: this command would fire in the real assistant.
- `heard 'shoot' but would NOT fire`: recognised, but below `fast.min_confidence`.
- `'...' (no match)`: speech that isn't a command (should be most normal talk).
- After a wake phrase: `request: 'open chrome' -> light model`, showing how it would be routed.

A good session: say each command 5 times, then talk normally for a minute.
- Commands **don't fire** → lower `fast.min_confidence` (e.g. 0.6) or pick a clearer phrase.
- Normal talk **fires commands** → set `fast.fire_on_partial` to `false`, raise `min_confidence`,
  or use two-word commands ("fire now" instead of "shoot").
- **Wake word not recognised** → check the start of the output for which wake spellings are
  usable, and try saying it the way one of those is spelled.
- `--no-whisper` skips transcription for a faster start when you only test commands.

Live input level is shown as `level [#####.....]`; if it never moves, the wrong mic is selected
(`start.bat --list-devices`, then set `audio.input_device`).

No Gemini key is needed: the brain is local. (Gemini via API key is still optional, see
"Optional: Gemini as the brain".)

## The brain and its models

| Situation | Model | GPU memory |
|---|---|---|
| Instant commands: time, date, volume, media, KMPlayer, timers, screenshot, lock, "open X", "go to YouTube", Chrome profiles | none (no AI) | 0 |
| Everything else | `gpt-oss:20b` | ~14 GB |

- **One model.** `gpt-oss:20b` loads at startup (1.5-2 min from a hard drive) and stays loaded.
  It's released while a game runs, so the game gets the GPU (`brain.free_gpu_while_gaming`), and
  loaded again when the game closes. An AI question mid-game reloads it, which is slow.
- **What it can do:** search the whole PC for files ("play Black Clover in KMPlayer"), open files
  in any app, open and switch apps, type into them ("open Notepad and write Hello World"), press
  shortcuts ("press ctrl+s in Notepad"), create and edit text files, make and list folders, move or
  rename files, send files to the Recycle Bin, and run PowerShell for anything else.
- **Any installed app opens by name:** "open Antigravity", "open Steam", "open Calculator". It uses the
  Windows Start menu's own app list (refreshed every 10 minutes), so anything you can find in Start
  search works. A close-but-unsure name is checked first ("Did you mean Antigravity?").
  `tools.apps` in `config.json` is now only for nicknames or exact paths.
- **Reading the screen:** "what's on my screen?", "close that", "read this to me". It reads the window
  in front as text, like Windows' Narrator does (title, buttons, menus, labels, text in documents and
  web pages), and lists open windows, so "this" and "that" make sense to it. It can't see pictures,
  video or games. Apps running as administrator (Task Manager) can only be read or closed if Amolex
  also runs as administrator.
- **Closing apps with unsaved work:** "close Notepad" closes it like clicking X. If it asks to save,
  Amolex asks you "save, don't save, or cancel?" and presses that button (with the `uiautomation`
  package, installed automatically on the next start). A new, never-saved file opens Save As: pick
  the name on screen. "Force close Notepad" ends it without asking the app, after your yes.
- **Long tasks:** if a task is still running after 3 s (`speech.slow_notice_seconds`; time spent
  waiting for your yes/no doesn't count), it says "This will take a moment. I'll tell you when it's
  done." and ends its reply with "The task is done." (not when it failed or you cancelled).
- **Names don't have to be exact.** Files and apps are matched loosely ("black cover" finds Black
  Clover, "game player" finds KMPlayer). A single clear match is used straight away; a close-but-not-exact
  match or several matches are checked with you first ("I found 4 matches. Do you mean Black Clover
  S2 175?"). Say no and it lists the others instead of guessing.
- **Permission rule:** opening, searching, reading, typing, creating new files and folders, and
  look-only commands (IP address, disk space, running programs) just happen. Editing, adding to,
  replacing, moving or renaming existing files, sending them to the Recycle Bin, and any command that
  changes something ask for a spoken yes first. Windows' own folders, Amolex's folders and whole
  drives are never changed. PowerShell never deletes permanently, downloads, or touches disks and boot
  settings. Limit where it may change files with `tools.writable_folders` (default `["*"]`); turn
  PowerShell off with `tools.allow_powershell: false`. File search walks your folders, then every
  drive, for up to `tools.search_seconds` (12). Typing goes through the clipboard, which is put back.
- **No made-up actions.** If the model says it did something without a tool actually running,
  Amolex says "I didn't do that" instead.
- **Speech:** Whisper `small.en` (more accurate than `base`, about a second per request on CPU).
  Downloaded once, about 500 MB.
- To bring back sizes per request, set different models in `brain.routing` (light / normal / heavy);
  the "use the big model?" question returns with them.

- **KMPlayer:** "open KMPlayer", "pause", "resume", "forward", "go back", "forward 30 seconds",
  "go back 1 minute". Amolex switches to KMPlayer for the key press, then back to what you were
  using. One arrow press skips `tools.player.seek_step_seconds` (5); change it if your KMPlayer
  skips a different amount. With KMPlayer closed, "pause" sends the normal media key.
- Wake with "Hey Amolex", "Amolex" or "Hey ammo". After an answer you have 8 s to say a follow-up
  without the name (`wake.follow_up_seconds`; off while gaming).
- Add your games' `.exe` names to `routing.games` (Task Manager → Details shows them), or say
  "gaming mode on / off / auto".

What the brain does itself: open/close apps, folders, websites, YouTube, Windows Settings pages,
media and volume, screenshots, system status, timers, lock/sleep/restart/shutdown (asks first),
find and read files in allowed folders, undo a git commit (asks first).

## Delegates: handing big tasks to bigger AIs

The brain doesn't do heavy work. When a delegate is set up, it sends big tasks to it through
that AI's official command-line tool, keeps listening meanwhile, saves the full result in
`E:\Amolex\jobs\`, and says "Task finished" when it's done. Ask "what's the status of my tasks?"
or "summarise the last result".

All delegates ship **switched off** in `config.json` → `delegates`. To add one:
1. Install its CLI and sign in with your subscription (for example Claude Code: `claude`,
   OpenAI Codex: `codex`, Google Antigravity: `agy`).
2. Check the non-interactive command in its `--help` and set `command` (`{task}` is replaced by
   the task; or set `"stdin": true` to send it on standard input).
3. Set `"enabled": true`, run `start.bat --doctor`, and restart Amolex.

The shipped commands (`claude -p`, `codex exec`, `agy`) are starting points: verify each against
the tool's current help before enabling. Delegates run in `E:\Amolex\workspace`.

## Using it

| Do | Result |
|---|---|
| Press **F8** | Same as the wake word: it says "Yes, RK?" and your next sentence is a request. Stops it mid-sentence; press again while it waits to put it on standby |
| Say "**Hey Amolex**" | Wakes it and opens an AI request; you can say the request in the same breath |
| Say a fast command while listening | Acts at once, e.g. "shoot", "press w", "next reel" (see `commands.json`) |
| "Standby" | Back to sleep |
| "Use Gemini Pro" / "Use local model" | Switches the AI and remembers it in `config.json` |

Tray colours: grey standby · green listening · blue waiting for your request · purple thinking ·
orange speaking · yellow waiting for yes/no.

## Configuration

- `config.json`: mic (`audio.input_device`: a name fragment such as `"WO Mic"`), wake phrases,
  AI provider and models, voice, and the allowlists for the AI tools (`apps`, `git_repos`,
  `readable_folders`).
- `commands.json`: fast commands. Every word must exist in the Vosk dictionary;
  `start.bat --doctor` lists any that don't.
- Model names such as `gemini-flash-latest` are placeholders: check the current names in
  Google AI Studio.

### Optional: Gemini as the brain

A Google AI Pro/Ultra subscription does not cover API use, so this needs a (free-tier) API key
from aistudio.google.com: `start.bat --set-key`, then set `brain.provider` to `"gemini"`.
If Gemini fails, the local brain answers instead.

### Google sign-in (instead of an API key)

1. Google Cloud Console: create or pick a project and enable the **Generative Language API**.
2. OAuth consent screen: user type **External**, add yourself as a **test user**.
3. Credentials → Create credentials → OAuth client ID → **Desktop app**; download the JSON and
   save it as `E:\Amolex\client_secret.json`.
4. `start.bat --login-google`. If calls fail with a quota-project error, set
   `brain.oauth_quota_project` to your project ID.

## Safety notes

- **Anti-cheat:** fast commands send synthetic input. Use them in single-player or offline games
  only; online games with anti-cheat may flag it.
- **Destructive tools** (git undo) always ask for a spoken yes first and use a soft reset, so your
  changes stay in the folder.
- The AI can only open apps, repos and folders listed in `config.json`.
- Secrets live in Windows Credential Manager, never in `config.json`. `start.bat --forget`
  removes them.
- Headphones stop it hearing its own voice. F8 always works, even while it talks.

## Known limits (first version)

- "Amolex" is a made-up word, so Vosk may not know it. The config tries several spellings built
  from dictionary words; `--doctor` shows which are usable. If none are, use F8, or train a
  custom wake word later.
- Repeating a command fast ("shoot shoot shoot") fires once; say it with short pauses.
- Commands fire on Vosk's first guess for speed (`fast.fire_on_partial`). If normal talking
  triggers commands, set it to `false`: slightly slower, but every command must pass
  `fast.min_confidence`.
- Whisper runs on the CPU (its engine has no AMD GPU support on Windows); the brain runs on the
  GPU through Ollama. If `--doctor` says the model is on the CPU, Ollama doesn't support the
  card yet on your driver: update the AMD driver and Ollama first.

## Development

```
E:\ProgramData\Amolex\uv\uv.exe run --project E:\Amolex pytest
```
Logs: `E:\Amolex\logs\amolex.log` (includes per-stage timings).
