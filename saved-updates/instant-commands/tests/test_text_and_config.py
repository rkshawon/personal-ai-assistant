"""Unit tests for the parts that don't need a mic, models or Windows."""

import json

from amolex.config import CommandSet, Config
from amolex.speech.text import contains_phrase, normalise, parse_switch, strip_wake, yes_no
from amolex import paths


def test_shipped_config_and_commands_are_valid():
    cfg = Config.model_validate(json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8")))
    assert cfg.assistant_name == "Amolex-9026"
    raw = json.loads(paths.COMMANDS_FILE.read_text(encoding="utf-8"))
    cmds = CommandSet.model_validate({k: v for k, v in raw.items() if not k.startswith("_")})
    phrases = [c.phrase for c in cmds.commands]
    assert len(phrases) == len(set(phrases)), "duplicate fast command phrases"
    assert "next reel" in cmds.by_phrase()


def test_normalise_and_contains():
    assert normalise("  Hey, AMOLEX! ") == "hey amolex"
    assert contains_phrase("hey amolex open chrome", {"hey amolex"})
    assert not contains_phrase("heyamolex", {"amolex"})


def test_strip_wake_variants():
    assert strip_wake("Hey Amolex, open VS Code.") == "open VS Code."
    assert strip_wake("Hey Amalex open chrome") == "open chrome"
    assert strip_wake("Ammo lex, what time is it?") == "what time is it?"
    assert strip_wake("Hey Amolex.") == ""
    assert strip_wake("open chrome please") == "open chrome please"
    # real mishearings from RK's voice test
    assert strip_wake("Hey, I'm Alex.") == ""
    assert strip_wake("Hey, Alex.") == ""
    assert strip_wake("Hey, I'm Alex, open Chrome.") == "open Chrome."
    assert strip_wake("Hey Alex what time is it") == "what time is it"
    assert strip_wake("Hey, open Chrome") == "Hey, open Chrome"  # no name: keep everything
    assert strip_wake("Hello Amolex, open Chrome.") == "open Chrome."


def test_instant_commands_skip_the_ai():
    from amolex.ai.intents import handle

    calls = []

    def fake(name):
        return lambda *a, **k: (calls.append((name, a)), f"{name} done")[1]

    names = ["open_app", "open_chrome_profile", "open_website", "media_control", "set_timer",
             "take_screenshot", "lock_pc", "system_status", "cancel_shutdown"]
    tools = {n: fake(n) for n in names}
    apps = {"chrome", "vs code", "notepad"}

    assert handle("What time is it?", tools, apps).startswith("It's ")
    assert handle("Open Chrome.", tools, apps) == "open_app done" and calls[-1] == ("open_app", ("chrome",))
    assert handle("Now, let's open Chrome.", tools, apps) == "open_app done"  # from RK's log
    handle("Open Vibhasaftlimited.com profile.com.", tools, apps)  # Whisper's version of "Vivasoft Limited profile"
    assert calls[-1][0] == "open_chrome_profile" and "vibhasaftlimited" in calls[-1][1][0]
    handle("open chrome with the second profile", tools, apps)
    assert calls[-1] == ("open_chrome_profile", ("second",))
    handle("volume up", tools, apps)
    assert calls[-1] == ("media_control", ("volume_up", 5))
    handle("set a timer for 5 minutes", tools, apps)
    assert calls[-1] == ("set_timer", (5.0, "timer"))
    # things the AI should handle are left alone
    assert handle("did you hear me?", tools, apps) is None
    assert handle("write a python script", tools, apps) is None
    assert handle("open the thing I was working on yesterday", tools, apps) is None


def test_chrome_profile_matching():
    from amolex.ai.tools import match_chrome_profile

    profiles = [("Default", "RK (rkshawon36@gmail.com)"), ("Profile 1", "Vivasoft Limited (novara.dev@vivasoftltd.com)")]
    assert match_chrome_profile("second", profiles)[0] == "Profile 1"
    assert match_chrome_profile("2", profiles)[0] == "Profile 1"
    assert match_chrome_profile("vibhasaftlimited com", profiles)[0] == "Profile 1"  # Whisper's spelling
    assert match_chrome_profile("vivasoft", profiles)[0] == "Profile 1"
    assert match_chrome_profile("personal rk", profiles)[0] == "Default"
    assert match_chrome_profile("zzz", profiles) is None


def test_chatter_never_presses_keys():
    from amolex.config import FastCfg
    from amolex.fast.rules import new_commands

    cmds = {"crouch", "previous reel", "shoot", "jump", "reload"}
    fast = FastCfg()
    # normal talk that Vosk forced into a command: never fires
    assert new_commands("unk crouch unk", True, 1.0, fast, cmds, 0, gaming=False) == []
    assert new_commands("unk previous reel", False, 1.0, fast, cmds, 0, gaming=True) == []
    # outside games a command waits for the final, confident result
    assert new_commands("crouch", False, 1.0, fast, cmds, 0, gaming=False) == []
    assert new_commands("crouch", True, 0.9, fast, cmds, 0, gaming=False) == ["crouch"]
    # while gaming, instant on the first guess, chains still work
    assert new_commands("shoot jump reload", False, 1.0, fast, cmds, 1, gaming=True) == ["jump", "reload"]


def test_local_model_speaks_action_results_without_second_round(monkeypatch):
    import sys
    import types

    from amolex.ai.providers import OllamaProvider
    from amolex.config import BrainCfg

    opened = []

    def open_app(name: str) -> str:
        """Open an app.

        Args:
            name: app
        """
        opened.append(name)
        return "Opened Chrome."

    call = types.SimpleNamespace(function=types.SimpleNamespace(name="open_app", arguments={"name": "chrome"}))
    rounds = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def ps(self):
            return types.SimpleNamespace(models=[types.SimpleNamespace(model="qwen3:4b")])

        def chat(self, messages, **kw):
            rounds.append(messages[0]["content"])
            # the model asks for the same action twice, as it did on RK's PC
            return types.SimpleNamespace(message=types.SimpleNamespace(content="", tool_calls=[call, call]))

    fake = types.ModuleType("ollama")
    fake.Client = FakeClient
    fake.ResponseError = type("ResponseError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "ollama", fake)
    p = OllamaProvider(BrainCfg(), [open_app], "system")
    assert p.ask("open chrome", model="qwen3:4b") == "Opened Chrome."
    assert opened == ["chrome"]  # duplicate call ignored
    assert len(rounds) == 1  # no second model round
    assert rounds[0].endswith("/no_think")


def test_brain_falls_back_to_light_when_bigger_model_times_out(monkeypatch):
    from amolex.ai import brain as brain_mod
    from amolex.ai.providers import ProviderUnavailable

    cfg = Config()
    b = brain_mod.Brain(cfg, tools=[], has_delegates=False)
    b.router.gaming.manual = False
    asked = []

    def fake_ask(text, model=None):
        asked.append(model)
        b.local.history.append({"role": "user", "content": text})
        if model == cfg.brain.routing.normal:
            raise ProviderUnavailable("Ollama unreachable at http://127.0.0.1:11434: timed out")
        return "answer"

    monkeypatch.setattr(b.local, "ask", fake_ask)
    assert b.ask("did you hear me, can you tell me a joke about cats").text == "answer"
    assert asked == [cfg.brain.routing.normal, cfg.brain.routing.light]


def test_reasoning_is_never_spoken():
    from amolex.ai.providers import strip_reasoning
    from amolex.speech.text import speakable

    # exactly what qwen3 returned on RK's PC: reasoning, a bare </think>, then the answer
    raw = 'Okay, the user asked "What time is it?" and I called get_current_time...\nSo the answer is short.\n</think>\n\nIt\'s 8:56 PM now.'
    assert strip_reasoning(raw) == "It's 8:56 PM now."
    assert strip_reasoning("<think>\nhmm\n</think>\nDone.") == "Done."
    assert strip_reasoning("Opened Chrome.") == "Opened Chrome."

    long = "First sentence here. " * 40
    out = speakable(long)
    assert len(out) <= 300 and out.endswith(".")
    assert speakable("**Bold** and `code`") == "Bold and code"


def test_one_sentence_counts_as_one_wake():
    from amolex.speech.wake import WakeTracker

    w = WakeTracker()
    # RK's test: partial wake, sentence ends, then Vosk's late final repeats the wake
    w.on_hit(final=False)
    assert w.on_wake()
    assert w.take() is True  # sentence handled as a wake
    w.on_hit(final=True)
    assert not w.on_wake()  # the late final is the same wake: ignored
    w.on_final()
    assert w.take() is False  # the trailing bit is not a second wake
    # a genuinely new wake afterwards still counts
    w.on_hit(final=False)
    assert w.on_wake()
    assert w.take() is True
    # wake whose final arrives BEFORE the sentence ends: next sentence's wake must not be blocked
    w.on_final()
    w.on_hit(final=False); w.on_wake(); w.on_hit(final=True); w.on_final()
    assert w.take() is True
    w.on_hit(final=False)
    assert w.on_wake()


def test_yes_no():
    assert yes_no("Yes, do it") is True
    assert yes_no("no thanks") is False
    assert yes_no("hmm maybe") is None


def test_delegate_command_building():
    from amolex.ai.jobs import build_argv, short_title
    from amolex.config import DelegateCfg

    d = DelegateCfg(enabled=True, command=["claude", "-p", "{task}"])
    assert build_argv(d, "write a poem") == ["claude", "-p", "write a poem"]
    d_stdin = DelegateCfg(enabled=True, command=["agy", "{task}"], stdin=True)
    assert build_argv(d_stdin, "x") == ["agy"]
    assert short_title("one two three", 2) == "one two..."


def test_shipped_delegates_are_off_by_default():
    cfg = Config.model_validate(json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8")))
    assert cfg.brain.provider == "local"
    assert cfg.delegates and not any(d.enabled for d in cfg.delegates.values())


def test_routing_tiers():
    from amolex.ai.router import Router, cap, classify
    from amolex.config import RoutingCfg

    r = RoutingCfg()
    assert classify("open chrome", r)[0] == "light"
    assert classify("volume up a bit", r)[0] == "light"
    assert classify("what's a good gift for my brother's birthday", r)[0] == "normal"
    assert classify("write a python script that renames my photos by date", r)[0] == "heavy"
    assert classify("help me debug this error in the novara repo", r)[0] == "heavy"
    assert cap("heavy", "light") == "light" and cap("light", "normal") == "light"

    router = Router(r, has_delegates=False)
    router.gaming.manual = False
    assert router.choose("write me an essay about mars").model == r.heavy
    assert router.choose("open spotify").model == r.light
    router.gaming.manual = True  # gaming: everything capped at light
    route = router.choose("write me an essay about mars")
    assert route.model == r.light and "gaming" in route.reason

    with_delegate = Router(r, has_delegates=True)
    with_delegate.gaming.manual = False
    assert with_delegate.choose("write a python script").model == r.normal  # hands off instead of loading 20B

    r.pinned = "gpt-oss:20b"
    assert Router(r, False).choose("open chrome").model == "gpt-oss:20b"


def test_brain_falls_back_when_heavy_model_missing(monkeypatch):
    from amolex.ai import brain as brain_mod
    from amolex.ai.providers import ProviderUnavailable

    cfg = Config()
    cfg.brain.provider = "local"
    b = brain_mod.Brain(cfg, tools=[], has_delegates=False)
    b.router.gaming.manual = False
    asked = []

    def fake_ask(text, model=None):
        asked.append(model)
        b.local.history.append({"role": "user", "content": text})
        if model == cfg.brain.routing.heavy:
            raise ProviderUnavailable("Ollama error: model 'gpt-oss:20b' not found")
        return f"answered by {model}"

    monkeypatch.setattr(b.local, "ask", fake_ask)
    answer = b.ask("write a python script for me")
    assert asked == [cfg.brain.routing.heavy, cfg.brain.routing.normal]
    assert answer.text == f"answered by {cfg.brain.routing.normal}"
    assert len(b.local.history) == 1  # the failed attempt's turn isn't duplicated

    monkeypatch.setattr(brain_mod, "save_config", lambda cfg: None)  # don't touch the real config.json
    assert "gpt-oss" in b.switch("local", "big")
    assert cfg.brain.routing.pinned == cfg.brain.routing.heavy
    b.switch("local", "auto")
    assert cfg.brain.routing.pinned is None


def test_fast_fire_rule():
    from amolex.config import FastCfg
    from amolex.fast.rules import command_sequence, new_commands

    cmds = {"shoot", "jump", "reload", "press w", "next reel"}
    assert command_sequence("shoot jump reload", cmds) == ["shoot", "jump", "reload"]
    assert command_sequence("reload unk", cmds) == ["reload"]
    assert command_sequence("press w unk next reel", cmds) == ["press w", "next reel"]
    assert command_sequence("press", cmds) == []  # half a command: wait for the rest

    fast = FastCfg(fire_on_partial=True, min_confidence=0.75)
    # RK's test: "shoot, jump, reload" arrives as growing partials; each command fires once
    fired = 0
    for partial in ("shoot", "shoot jump", "shoot jump reload"):
        new = new_commands(partial, False, 1.0, fast, cmds, fired)
        fired += len(new)
    assert fired == 3
    assert new_commands("shoot jump reload", True, 1.0, fast, cmds, fired) == []  # final repeats nothing
    assert new_commands("jump jump jump", False, 1.0, fast, cmds, 1) == ["jump", "jump"]

    slow = FastCfg(fire_on_partial=False, min_confidence=0.75)
    assert new_commands("shoot", False, 1.0, slow, cmds, 0) == []
    assert new_commands("shoot", True, 0.8, slow, cmds, 0) == ["shoot"]
    assert new_commands("shoot", True, 0.5, slow, cmds, 0) == []  # too unsure
    assert new_commands("shoot", True, 0.99, FastCfg(enabled=False), cmds, 0) == []


def test_gaming_and_size_commands():
    from amolex.speech.text import parse_gaming

    assert parse_gaming("gaming mode on") is True
    assert parse_gaming("Stop gaming mode.") is False
    assert parse_gaming("gaming mode auto") is None
    assert parse_gaming("open chrome") == "no"
    assert parse_switch("use the big model") == ("local", "big")
    assert parse_switch("switch to automatic model") == ("local", "automatic")


def test_parse_switch():
    assert parse_switch("use Gemini Pro") == ("gemini", "pro")
    assert parse_switch("switch to local model") == ("local", None)
    assert parse_switch("switch to the offline model") == ("local", None)
    assert parse_switch("open chrome") is None
