"""Amolex-9026 entry point.

  start.bat                 run the assistant
  start.bat --doctor        check models, mic, credentials, Ollama, command words
  start.bat --test-voice    show what is heard (wake word, commands, requests); executes nothing
  start.bat --list-devices  show microphones (use a name fragment in config.json)
  start.bat --set-key       store a Gemini API key in Windows Credential Manager
  start.bat --login-google  sign in with your Google account instead of a key
  start.bat --forget        remove stored Gemini credentials
"""

from __future__ import annotations

import argparse
import getpass
import sys

from amolex import log as amolex_log
from amolex import paths, secrets
from amolex.config import load_commands, load_config


def doctor() -> int:
    cfg, cmds = load_config(), load_commands()
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {label}" + (f"  ->  {hint}" if hint and not good else ""))

    print(f"{cfg.assistant_name} doctor\n  project: {paths.PROJECT_DIR}\n  installs: {paths.HOME_DIR}\n")
    vosk_dir = paths.VOSK_DIR / cfg.speech.vosk_model
    check("Vosk model", vosk_dir.exists(), f"missing {vosk_dir}; run setup.ps1")
    if vosk_dir.exists():
        from amolex.speech.vosk_engine import GrammarRecognizer

        phrases = [*cfg.wake.phrases, *(c.phrase for c in cmds.commands)]
        rec = GrammarRecognizer(vosk_dir, cfg.audio.sample_rate, phrases)
        wake_norm = {" ".join(p.lower().split()) for p in cfg.wake.phrases}
        usable_wake = sorted(wake_norm & rec.phrases)
        check(f"wake phrases usable: {usable_wake}", bool(usable_wake), "add spellings made of dictionary words")
        for p, missing in rec.rejected.items():
            if p in wake_norm and usable_wake:  # an extra spelling that doesn't work is fine if another does
                print(f"  [--] wake spelling '{p}' ignored: unknown word(s) {missing}")
            else:
                check(f"phrase '{p}'", False, f"unknown word(s) {missing}")

    try:
        from amolex.audio.capture import list_input_devices, resolve_device

        devices = list_input_devices()
        check(f"microphones found: {len(devices)}", bool(devices))
        for i, name in devices:
            print(f"        {i:>3}: {name}")
        if cfg.audio.input_device is not None:
            check(f"configured mic '{cfg.audio.input_device}'", resolve_device(cfg.audio.input_device) is not None)
    except Exception as e:
        check("audio system", False, str(e))

    b = cfg.brain
    print(f"\n  brain: {b.provider} ({b.local_model if b.provider == 'local' else b.gemini_model})")
    if b.provider == "gemini":
        if b.gemini_auth == "api_key":
            check("Gemini API key stored", bool(secrets.get_api_key()), "run start.bat --set-key")
        else:
            try:
                check("Google sign-in", secrets.load_oauth_creds(b) is not None, "run start.bat --login-google")
            except Exception as e:
                check("Google sign-in", False, str(e))
    try:
        import ollama

        client = ollama.Client(host=b.ollama_host)
        names = [m.model for m in client.list().models]
        check("Ollama running", True)
        r = b.routing
        tiers = {}
        for tier, model in (("light", r.light), ("normal", r.normal), ("heavy", r.heavy)):
            tiers.setdefault(model, []).append(tier)  # the 8B serves light and normal: check it once
        for model, tier_names in tiers.items():
            tier = "/".join(tier_names)
            pulled = any(n.startswith(model) for n in names)
            if tier == "heavy" and not pulled:
                print(f"  [--] heavy model '{model}' not pulled (optional, ~14 GB): heavy tasks use the normal model")
                continue
            check(f"{tier} model '{model}' pulled", pulled, f"ollama pull {model}")
        if any(n.startswith(r.light) for n in names):
            client.generate(model=r.light, prompt="hi", keep_alive="1m", options={"num_predict": 1})
            for m in client.ps().models:
                if m.model.startswith(r.light) and m.size:
                    share = m.size_vram / m.size
                    check(f"model on GPU: {share:.0%}", share > 0.9, "running on CPU; check that Ollama supports your AMD GPU")
    except Exception as e:
        check("Ollama running", False, f"{e} (the brain can't answer without it)")

    from amolex.ai.jobs import JobManager

    enabled = JobManager(cfg.delegates, cfg.jobs, notify=print).available()
    print(f"\n  delegates enabled: {', '.join(enabled) or 'none (add them later in config.json)'}")
    for name, d in enabled.items():
        import shutil

        check(f"delegate '{name}' command '{d.command[0]}' found", shutil.which(d.command[0]) is not None, "install its CLI and sign in")
    print("\nAll good." if ok else "\nFix the items marked !!, then run start.bat again.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="amolex")
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--test-voice", action="store_true", help="show what is heard; executes nothing")
    ap.add_argument("--no-whisper", action="store_true", help="with --test-voice: skip transcription (faster start)")
    ap.add_argument("--gaming", action="store_true", help="with --test-voice: behave as while gaming (instant firing)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--set-key", action="store_true")
    ap.add_argument("--login-google", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    paths.ensure_dirs()
    amolex_log.setup(args.verbose)

    if args.test_voice:
        from amolex.voicetest import run as test_voice

        return test_voice(load_config(), load_commands(), transcribe=not args.no_whisper, gaming_mode=args.gaming)
    if args.doctor:
        return doctor()
    if args.list_devices:
        from amolex.audio.capture import list_input_devices

        for i, name in list_input_devices():
            print(f"{i:>3}: {name}")
        return 0
    if args.set_key:
        key = getpass.getpass("Paste your Gemini API key (input hidden): ")
        if not key.strip():
            print("No key entered.")
            return 1
        secrets.set_api_key(key)
        print("Saved to Windows Credential Manager. Set \"gemini_auth\": \"api_key\" in config.json.")
        return 0
    if args.login_google:
        secrets.login_google(load_config().brain)
        print("Signed in. Set \"gemini_auth\": \"oauth\" in config.json.")
        return 0
    if args.forget:
        secrets.clear_all()
        print("Stored Gemini credentials removed.")
        return 0

    from amolex.app import App

    App(load_config(), load_commands()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
