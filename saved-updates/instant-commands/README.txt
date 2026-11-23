Saved update: instant commands, Chrome profiles, chatter filter, model-loading fixes (23 Nov 2026)

Undone at RK's request. To switch back to it, copy everything in this folder over E:\Amolex
(keep the same sub-folders), or ask Claude to re-apply it.

What it adds:
- Instant commands without the AI: time, open app, Chrome profile, volume/media, timers, screenshot, lock
- open_chrome_profile tool ("open Chrome Vivasoft profile", "second profile")
- Chatter filter: no key presses from normal talking; outside games commands wait for a confident result
- Speaks tool results directly (no second AI round), ignores duplicate tool calls
- Pre-loads light + normal models, 15 min keep-alive, longer load timeout, falls back to the light model on timeout
