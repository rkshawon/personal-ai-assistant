"""`start.bat --test-voice`: see exactly what Amolex hears, without doing anything.

No keys are pressed, no AI is called, nothing is spoken. For every phrase it shows
whether it was heard as the wake word, a fast command (and whether that command
would fire), a control word, or ignored. After a wake phrase it also transcribes
what you said with Whisper and shows which model size the request would get.

Use it to tune config.json (wake phrases, fast.min_confidence, fast.fire_on_partial)
and commands.json until the right things fire and nothing else does.
"""

from __future__ import annotations

import collections
import queue
import time

from . import paths
from .ai import intents
from .ai.router import classify

INSTANT_TOOLS = ["open_app", "open_chrome_profile", "open_website", "media_control", "set_timer",
                 "take_screenshot", "lock_pc", "system_status", "cancel_shutdown"]
from .audio.capture import MicStream, resolve_device
from .audio.vad import Endpointer
from .config import CommandSet, Config
from .fast.rules import command_sequence, new_commands
from .speech.text import contains_phrase, normalise, strip_wake
from .speech.vosk_engine import GrammarRecognizer
from .speech.wake import WakeTracker

RESET, DIM, GREEN, YELLOW, CYAN, RED = "\033[0m", "\033[2m", "\033[92m", "\033[93m", "\033[96m", "\033[91m"


def _db(frame: bytes) -> float:
    """Loudness of a frame in dBFS: 0 = maximum, about -20 to -10 is clear speech, below -40 is too quiet."""
    import numpy as np

    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    return 20 * np.log10(max(rms, 1.0) / 32768.0)


def _bar(db: float) -> str:
    n = max(0, min(20, int((db + 60) / 3)))  # -60 dB empty .. 0 dB full
    return "#" * n + "." * (20 - n)


def run(cfg: Config, commands: CommandSet, transcribe: bool = True, gaming_mode: bool = False) -> int:
    import os
    import sounddevice as sd

    os.system("")  # turn on ANSI colours in the Windows console
    cmds = commands.by_phrase()
    standby = commands.control_phrases("standby")
    stop = commands.control_phrases("stop")
    wake = {normalise(p) for p in cfg.wake.phrases}

    print(f"\n{CYAN}{cfg.assistant_name} voice test{RESET}  (Ctrl+C to stop; nothing is executed)\n")
    rec = GrammarRecognizer(paths.VOSK_DIR / cfg.speech.vosk_model, cfg.audio.sample_rate, [*wake, *cmds, *standby, *stop])
    usable_wake = wake & rec.phrases
    print(f"  wake phrases usable: {sorted(usable_wake) or RED + 'NONE (use F8)' + RESET}")
    if rec.rejected:
        for p, missing in sorted(rec.rejected.items()):
            print(f"  {YELLOW}skipped '{p}': unknown word(s) {missing}{RESET}")
    print(f"  fast commands active: {len([c for c in cmds if c in rec.phrases])}/{len(cmds)}")
    print(f"  fire_on_partial={cfg.fast.fire_on_partial}  min_confidence={cfg.fast.min_confidence}")
    print(f"  simulating: {'GAMING (instant firing)' if gaming_mode else 'normal use (waits for confident final result)'}"
          f"  -> use --gaming to test the other")

    transcriber = None
    if transcribe:
        from .speech.transcribe import Transcriber

        print(f"  loading Whisper '{cfg.speech.whisper_model}' (first time downloads it)...")
        transcriber = Transcriber(cfg.speech.whisper_model, cfg.speech.language, cfg.speech.whisper_prompt)
        transcriber.load()

    mic = MicStream(cfg.audio)
    mic.start()
    device = resolve_device(cfg.audio.input_device)
    print(f"  mic: {sd.query_devices(device if device is not None else sd.default.device[0])['name']}")
    print(f"\n{DIM}Speak now. Try the wake word, a few commands, and some normal talking.{RESET}\n")

    endpointer = Endpointer(cfg.audio.sample_rate, cfg.audio.block_ms, cfg.speech.end_of_speech_ms, cfg.speech.max_utterance_seconds)
    fired = 0  # commands already fired in the current Vosk segment
    tracker = WakeTracker()
    tracker_reported = False
    peak_db = -120.0
    stats = collections.Counter()
    start = time.monotonic()
    last_meter = 0.0
    try:
        while True:
            try:
                frame = mic.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            now = time.monotonic()
            db = _db(frame)
            peak_db = max(peak_db, db)
            if now - last_meter > 0.25:  # live input level so you can see the mic works
                print(f"\r  {DIM}level [{_bar(db)}] {db:5.0f} dB{RESET}   ", end="", flush=True)
                last_meter = now
            hit = rec.accept(frame)
            if hit is not None and hit.text:
                text = normalise(hit.text)
                kind = "final  " if hit.final else "partial"
                conf = f"{hit.confidence:.2f}" if hit.final else " -- "
                t = f"{now - start:6.1f}s"
                tracker.on_hit(hit.final)
                if usable_wake and contains_phrase(text, usable_wake):
                    if tracker.on_wake():
                        if not tracker_reported:
                            stats["wake"] += 1
                            tracker_reported = True
                        print(f"\r{t} {kind} conf {conf}  {CYAN}WAKE  '{text}'{RESET}")
                    else:
                        print(f"\r{t} {kind} conf {conf}  {DIM}(same wake again, ignored){RESET}")
                elif text in standby or text in stop:
                    print(f"\r{t} {kind} conf {conf}  {YELLOW}CONTROL '{text}'{RESET}")
                elif tracker.pending:
                    # same rule as the assistant: words after the wake phrase are an AI request
                    if command_sequence(text, set(cmds)):
                        print(f"\r{t} {kind} conf {conf}  {DIM}'{text}' after wake word: AI request, no command fires{RESET}")
                else:
                    new = new_commands(text, hit.final, hit.confidence, cfg.fast, set(cmds), fired, gaming_mode)
                    for phrase in new:
                        fired += 1
                        stats["fired"] += 1
                        c = cmds[phrase]
                        what = (c.key or c.button) if c.action in ("key", "click") else c.action
                        print(f"\r{t} {kind} conf {conf}  {GREEN}FIRE  '{phrase}' -> {c.action} {what}{RESET}")
                    missed = len(command_sequence(text, set(cmds))) - fired
                    if hit.final and not new and missed > 0:
                        stats["low_conf"] += 1
                        print(f"\r{t} {kind} conf {conf}  {YELLOW}heard '{text}' but would NOT fire (low confidence){RESET}")
                    elif hit.final and not new and not command_sequence(text, set(cmds)):
                        print(f"\r{t} {kind} conf {conf}  {DIM}'{text}' (no match){RESET}")
            if hit is not None and hit.final:
                fired = 0
                tracker.on_final()
            audio = endpointer.feed(frame)
            if audio:
                seconds = len(audio) / 2 / cfg.audio.sample_rate
                woke = tracker.take()
                tracker_reported = False
                if woke and transcriber:
                    said = transcriber.transcribe(audio)
                    request = strip_wake(said)
                    if request:
                        used: list[str] = []
                        recorder = {n: (lambda *a, _n=n, **k: (used.append(_n), "ok")[1]) for n in INSTANT_TOOLS}
                        if intents.handle(request, recorder, set(cfg.tools.apps)) is not None:
                            print(f"\r         {GREEN}request: '{request}' -> instant command ({used[0] if used else 'built-in'}, no AI){RESET}")
                        else:
                            tier, why = classify(request, cfg.brain.routing)
                            note = " (would ask before the big model)" if tier == "heavy" and cfg.brain.routing.ask_before_heavy else ""
                            print(f"\r         {CYAN}request: '{request}' -> {tier} model ({why}){note}{RESET}")
                    else:
                        print(f"\r         {CYAN}wake only ('{said}'): Amolex would say 'Yes, {cfg.user_name}?'{RESET}")
                elif transcriber and seconds <= 4:
                    # show what was actually said, to see which commands Vosk missed
                    said = transcriber.transcribe(audio).strip()
                    if said:
                        hint = ""
                        if normalise(said) in cmds:
                            hint = f"  {YELLOW}<- this IS a command but wasn't caught: speak louder or raise audio.gain{RESET}"
                        print(f"\r         {DIM}said '{said}' ({seconds:.1f}s), no command{RESET}{hint}")
                    else:
                        print(f"\r         {DIM}utterance {seconds:.1f}s ended (no words){RESET}")
                else:
                    print(f"\r         {DIM}utterance {seconds:.1f}s ended{RESET}")
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
    print(f"\n\nSummary: {stats['wake']} wake, {stats['fired']} commands fired, {stats['low_conf']} low-confidence commands skipped.")
    advice = (
        "too quiet: raise the mic level in Windows Sound settings, or audio.gain in config.json (try 3)"
        if peak_db < -30
        else "OK" if peak_db < -3 else "clipping: lower audio.gain or the Windows mic level"
    )
    print(f"Loudest input: {peak_db:.0f} dB ({advice}). Current audio.gain = {cfg.audio.gain}")
    return 0
