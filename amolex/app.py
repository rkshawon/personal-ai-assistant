"""The coordinator: mic -> (fast path | AI path) -> action / spoken reply.

Threads
  audio loop (main)   reads mic frames, runs Vosk + end-of-speech detection
  stt worker          Whisper transcription of finished utterances
  ai worker           one AI request at a time, plus everything that speaks
  hotkey / tray       callbacks only flip state or queue work
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import paths
from .ai import intents
from .ai.brain import Brain
from .ai.jobs import JobManager
from .ai.providers import unload, warm_up
from .ai.tools import build_tools, start_menu_apps
from .audio.capture import MicStream
from .audio.vad import Endpointer
from .config import CommandSet, Config
from .fast import actions
from .fast.rules import new_commands
from .log import timed
from .speech.text import (
    contains_phrase, is_fragment, normalise, parse_gaming, parse_switch, pick_option, say_duration, speakable, strip_wake,
    task_succeeded, yes_no,
)
from .speech.transcribe import Transcriber
from .speech.vosk_engine import GrammarRecognizer, Hit
from .speech.wake import WakeTracker
from .state import State, StateMachine
from .voice.speaker import Speaker

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: Config, commands: CommandSet) -> None:
        self.cfg = cfg
        self.sm = StateMachine(State.STANDBY)
        self.cmds = commands.by_phrase()
        self.standby_phrases = commands.control_phrases("standby")
        self.stop_phrases = commands.control_phrases("stop")

        wake = {normalise(p) for p in cfg.wake.phrases}
        grammar = [*wake, *self.cmds, *self.standby_phrases, *self.stop_phrases]
        self.recognizer = GrammarRecognizer(paths.VOSK_DIR / cfg.speech.vosk_model, cfg.audio.sample_rate, grammar)
        self.wake_phrases = wake & self.recognizer.phrases
        if not self.wake_phrases:
            log.warning("no wake phrase is usable with this Vosk model; use %s to wake", cfg.wake.hotkey.upper())

        self.mic = MicStream(cfg.audio)
        self.endpointer = Endpointer(
            cfg.audio.sample_rate, cfg.audio.block_ms, cfg.speech.end_of_speech_ms, cfg.speech.max_utterance_seconds
        )
        self.transcriber = Transcriber(cfg.speech.whisper_model, cfg.speech.language, cfg.speech.whisper_prompt)
        self.speaker = Speaker(cfg.voice)
        self.ai = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai")
        self.jobs = JobManager(cfg.delegates, cfg.jobs, notify=self.notify)
        tools = build_tools(cfg.tools, self.confirm, self.notify, self.jobs, extra=[self._switch_tool()],
                            choose=self.choose)
        self.tools = {t.__name__: t for t in tools}  # also used by instant commands
        self.brain = Brain(cfg, tools, has_delegates=bool(self.jobs.available()))

        self.utterances: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self.confirm_replies: queue.Queue[str] = queue.Queue()
        self.running = threading.Event()

        self.wake = WakeTracker()
        self._fired = 0  # fast commands already fired in the current Vosk segment
        self._awaiting_confirm = threading.Event()
        self._reset_endpointer = threading.Event()
        self._transcribing = threading.Event()
        self._asking = threading.Event()  # an AI answer is in progress: don't unload the model under it
        self._voice = threading.Lock()  # the slow-task notice and yes/no questions never talk over each other
        self._loading_since: float | None = None  # when the main model started loading, while it loads
        self._attentive_since = 0.0
        self._attentive_limit = cfg.speech.attentive_timeout_seconds  # longer during a follow-up window
        self._fallback_announced = False
        self.tray = None
        self.sm.on_change(self._on_state)

    # ---------------------------------------------------------------- lifecycle
    def run(self) -> None:
        self.running.set()
        threading.Thread(target=self._load_whisper, name="whisper-load", daemon=True).start()
        threading.Thread(target=self._stt_loop, name="stt", daemon=True).start()
        threading.Thread(target=start_menu_apps, name="app-list", daemon=True).start()  # read installed apps now
        if self.cfg.brain.provider == "local":  # load models now, not in the middle of a question
            threading.Thread(target=self._warm_models, name="warm-up", daemon=True).start()
        self._start_hotkey()
        self._start_tray()
        self.mic.start()
        greeting = f"Online, {self.cfg.user_name}. Press {self.cfg.wake.hotkey.upper()} or say the wake word."
        if self.cfg.brain.provider == "local" and self._model_cold():
            how_long = say_duration(self.cfg.brain.expected_load_seconds)
            greeting = (f"Online, {self.cfg.user_name}. The AI is still loading, {how_long}. Simple commands like "
                        "time, volume and opening apps work now. I'll tell you when it's ready.")
        self.ai.submit(self.say, greeting, State.STANDBY)
        log.info("%s running. Wake phrases: %s", self.cfg.assistant_name, sorted(self.wake_phrases) or "none")
        try:
            self._audio_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if not self.running.is_set():
            return
        self.running.clear()
        self.speaker.stop()
        self.mic.stop()
        try:
            import keyboard

            keyboard.unhook_all()
        except Exception:
            pass
        if self.tray:
            self.tray.stop()
        self.ai.shutdown(wait=False, cancel_futures=True)
        log.info("stopped")

    def _warm_models(self) -> None:
        """Load the main model at startup and keep it loaded, except while a game runs: then its GPU
        memory goes back to the game, and it's loaded again when the game closes. A separate heavy
        model is only loaded after you say yes to it."""
        router = self.brain.router
        models = list(dict.fromkeys([router.model_for("light"), router.model_for("normal")]))
        warmed = False
        while self.running.is_set():
            gaming = self.cfg.brain.free_gpu_while_gaming and router.gaming.check()[0]
            if gaming and not self._asking.is_set():
                for model in models:
                    if self.brain.local.is_loaded(model):  # also after an AI request mid-game
                        unload(self.cfg.brain, model)
                warmed = False
            elif not warmed:
                cold = not all(self.brain.local.is_loaded(m) for m in models)
                if cold:
                    self._loading_since = time.monotonic()
                for model in models:
                    warm_up(self.cfg.brain, model)
                if cold and self._loading_since is not None:
                    took = time.monotonic() - self._loading_since
                    self.cfg.brain.expected_load_seconds = took  # better guess next time
                    if took > 5:
                        self.notify(f"The AI is ready, {self.cfg.user_name}.")
                self._loading_since = None
                warmed = True
            time.sleep(10)

    def _model_cold(self) -> bool:
        """True if the main model isn't in GPU memory yet (after a PC restart it loads from disk)."""
        if self._loading_since is not None:
            return True
        router = self.brain.router
        try:
            return not all(self.brain.local.is_loaded(m) for m in {router.model_for("light"), router.model_for("normal")})
        except Exception:
            return True

    def _loading_notice(self) -> str:
        """What to say when a request arrives before the model has finished loading."""
        if self._loading_since is None:
            return "One moment, loading the model."
        left = self.cfg.brain.expected_load_seconds - (time.monotonic() - self._loading_since)
        if left <= 10:
            return "The AI is almost loaded. I'll answer in a moment."
        return f"The AI is still loading, about {int(round(left, -1))} more seconds. I'll answer as soon as it's ready."

    def _load_whisper(self) -> None:
        try:
            self.transcriber.load()
        except Exception:
            log.exception("Whisper failed to load; AI requests won't work until it does")

    def _start_hotkey(self) -> None:
        try:
            import keyboard

            keyboard.add_hotkey(self.cfg.wake.hotkey, self.toggle)
        except Exception as e:  # e.g. blocked by an elevated game window
            log.warning("hotkey %s unavailable: %s (try running as admin)", self.cfg.wake.hotkey, e)

    def _start_tray(self) -> None:
        try:
            from .tray import Tray

            self.tray = Tray(
                self.cfg.assistant_name,
                toggle=self.toggle,
                use_gemini=lambda: self.ai.submit(self._switch_and_say, "gemini", None),
                use_local=lambda: self.ai.submit(self._switch_and_say, "local", "auto"),
                toggle_gaming=lambda: self.ai.submit(
                    lambda: self.say(self.brain.set_gaming(self.brain.router.gaming.manual is not True))
                ),
                quit_=self.shutdown,
            )
            self.tray.start()
        except Exception as e:
            log.warning("tray icon unavailable: %s", e)

    def _on_state(self, state: State) -> None:
        if state is State.ATTENTIVE:
            self._attentive_since = time.monotonic()
        if self.tray:
            self.tray.update(state, self.brain.active)

    # ---------------------------------------------------------------- audio path
    def _audio_loop(self) -> None:
        while self.running.is_set():
            try:
                frame = self.mic.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process(frame)
            self._check_attentive_timeout()

    def _process(self, frame: bytes) -> None:
        if self._reset_endpointer.is_set():
            self._reset_endpointer.clear()
            self.endpointer.reset()
        hit = self.recognizer.accept(frame)
        if hit is not None:
            if hit.text:
                self.wake.on_hit(hit.final)
                self._on_hit(hit)
            if hit.final:
                self._fired = 0  # new Vosk segment: count fired commands from zero
                self.wake.on_final()
        audio = self.endpointer.feed(frame)
        if audio:
            self._on_utterance(audio)

    def _on_hit(self, hit: Hit) -> None:
        text = normalise(hit.text)
        state = self.sm.state

        if self.wake_phrases and contains_phrase(text, self.wake_phrases):
            if state is State.SPEAKING and not self.cfg.wake.barge_in:
                return
            if state in (State.STANDBY, State.ACTIVE, State.SPEAKING) and self.wake.on_wake():
                self.speaker.stop()
                self._attentive_limit = self.cfg.speech.attentive_timeout_seconds
                self.sm.set(State.ATTENTIVE)
            return

        # words after "Hey Amolex" are an AI request ("open Chrome" can sound like "crouch")
        if state is not State.ACTIVE or self.wake.pending:
            return
        good = hit.confidence >= self.cfg.fast.min_confidence
        if hit.final and good and text in self.standby_phrases:
            self.standby()
            return
        gaming = self.brain.router.gaming.check()[0]  # cached; cheap enough per result
        for phrase in new_commands(text, hit.final, hit.confidence, self.cfg.fast, set(self.cmds), self._fired, gaming):
            self._fired += 1
            try:
                actions.execute(self.cmds[phrase])
            except Exception:
                log.exception("fast command %r failed", phrase)

    def _on_utterance(self, audio: bytes) -> None:
        woke = self.wake.take()
        if self._awaiting_confirm.is_set() and not self.speaker.speaking:
            self.utterances.put(("confirm", audio))
        elif woke:
            self.utterances.put(("wake", audio))
        elif self.sm.state is State.ATTENTIVE and not self.speaker.speaking:
            self.utterances.put(("request", audio))
        # anything else (chatter, game audio, our own voice) is ignored

    def _check_attentive_timeout(self) -> None:
        if (
            self.sm.state is State.ATTENTIVE
            and not self.endpointer.in_speech
            and self.utterances.empty()
            and not self._transcribing.is_set()  # don't give up while your request is being transcribed
            and time.monotonic() - self._attentive_since > self._attentive_limit
        ):
            self.sm.set(State.ACTIVE)

    # ---------------------------------------------------------------- AI path
    def _stt_loop(self) -> None:
        while self.running.is_set():
            try:
                kind, audio = self.utterances.get(timeout=0.5)
            except queue.Empty:
                continue
            self._transcribing.set()
            try:
                text = self.transcriber.transcribe(audio)
            except Exception:
                log.exception("transcription failed")
                continue
            finally:
                self._transcribing.clear()
            if kind == "confirm":
                self.confirm_replies.put(text)
                continue
            if kind in ("wake", "request"):  # "request": the name may still be said during a follow-up
                text = strip_wake(text)
            if kind == "wake":
                if is_fragment(text):  # just the name (maybe plus a stray "open"): ask what's wanted
                    self.ai.submit(self.say, f"Yes, {self.cfg.user_name}?", State.ATTENTIVE)
                    continue
            if is_fragment(text):  # "Open." alone: ignore it and keep listening
                if text.strip():
                    log.info("ignored fragment %r", text)
                continue
            self.sm.set(State.THINKING)
            self.ai.submit(self._handle_request, text)

    def _handle_request(self, text: str) -> None:
        if normalise(text) in self.stop_phrases:
            self.sm.set(State.ACTIVE)
            return
        gaming = parse_gaming(text)
        if gaming != "no":
            self.say(self.brain.set_gaming(gaming))
            return
        switch = parse_switch(text)
        if switch:
            self._switch_and_say(*switch)
            return
        # common commands (time, open app, Chrome profile, volume...) run directly: no AI, near-instant
        with timed("instant command", log):
            apps = set(self.cfg.tools.apps) | {name.lower() for name, _ in start_menu_apps()}  # every installed app
            reply = intents.handle(text, self.tools, apps)
        if reply:
            log.info("instant: %r -> %s", text, reply)
            self.say(reply, self._after_answer())
            return
        route = self.brain.plan(text)
        one_model = self.brain.router.model_for("heavy") == self.brain.router.model_for("normal")
        if route and route.tier == "heavy" and not one_model:  # nothing to ask when there's only one model
            route = self._approve_heavy(route)
        loading = bool(route and not self.brain.local.is_loaded(route.model))
        if loading and not (route.tier == "heavy" and not one_model):
            self.say(self._loading_notice(), State.THINKING)
        if self.tray and route:
            self.tray.update(State.THINKING, f"{route.model}, {route.tier}")
        self._asking.set()
        try:
            answer, announced = self._with_slow_notice(lambda: self.brain.ask(text, route), already=loading)
        finally:
            self._asking.clear()
        reply = answer.text
        if announced and task_succeeded(reply):
            reply = reply.rstrip() + " The task is done."
        if answer.fell_back and not self._fallback_announced:
            reply = "Gemini isn't available, so I'm using the local model. " + reply
            self._fallback_announced = True
        elif answer.provider == "gemini":
            self._fallback_announced = False
        self.say(reply, self._after_answer())

    def _with_slow_notice(self, work, already: bool = False):
        """Run work(); if it's still busy after `slow_notice_seconds` (not counting time spent asking
        you yes/no or talking), say it will take a moment. Returns (result, whether you were told)."""
        limit = self.cfg.speech.slow_notice_seconds
        if limit <= 0:
            return work(), already
        done = threading.Event()
        told = threading.Event()
        if already:
            told.set()

        def watch() -> None:
            busy, last = 0.0, time.monotonic()
            while not done.wait(0.2):
                now = time.monotonic()
                if not self._awaiting_confirm.is_set() and not self.speaker.speaking:
                    busy += now - last
                last = now
                if busy >= limit and not told.is_set():
                    with self._voice:
                        if done.is_set() or self._awaiting_confirm.is_set():
                            continue
                        told.set()
                        log.info("slow task: telling the user it will take a moment")
                        self.speaker.say("This will take a moment. I'll tell you when it's done.")
                        self._reset_endpointer.set()

        watcher = threading.Thread(target=watch, name="slow-notice", daemon=True)
        watcher.start()
        try:
            result = work()
        finally:
            done.set()
            watcher.join(timeout=15)  # let the notice finish before the answer is spoken
        return result, told.is_set()

    def _after_answer(self) -> State:
        """Open a follow-up window (no wake word needed) unless it's off or a game is running,
        where waiting for a request would pause fast commands."""
        seconds = self.cfg.wake.follow_up_seconds
        if seconds <= 0 or self.brain.router.gaming.check()[0]:
            return State.ACTIVE
        self._attentive_limit = seconds
        log.info("follow-up: listening %.0f s for another request without the wake word", seconds)
        return State.ATTENTIVE

    def _approve_heavy(self, route):
        """Ask before loading the big model; anything but a clear yes keeps the normal model."""
        routing = self.cfg.brain.routing
        if routing.pinned or not routing.ask_before_heavy:
            self.say("Loading the big model, give me a moment.", State.THINKING)
            return route
        if self.confirm("This looks like a heavy task. Use the big model?"):
            self.say("Okay, loading the big model.", State.THINKING)
            return route
        normal = self.brain.router.model_for("normal")
        log.info("big model declined; using %s", normal)
        return type(route)(normal, "normal", route.reason + "; big model declined")

    def _switch_and_say(self, provider: str, variant: str | None) -> None:
        self.say(self.brain.switch(provider, variant))

    def _switch_tool(self):
        brain_ref = self

        def switch_ai_model(provider: str, variant: str = "") -> str:
            """Switch which AI answers from now on.

            Args:
                provider: "gemini" for Google Gemini, or "local" for the offline model.
                variant: Optional model variant, for example "pro" or "flash".
            """
            return brain_ref.brain.switch(provider, variant or None)

        return switch_ai_model

    # ---------------------------------------------------------------- speaking + confirmation
    def say(self, text: str, then: State = State.ACTIVE) -> None:
        """Speak (on the ai worker) and move to `then`, unless interrupted by the wake word."""
        interrupted = not self.speaker.say(speakable(text), on_start=lambda: self.sm.set(State.SPEAKING))
        self._reset_endpointer.set()  # drop any of our own voice the endpointer picked up
        # only move on if nothing else (F8, wake word) changed the state meanwhile
        if not interrupted and self.sm.state in (State.SPEAKING, State.THINKING):
            self.sm.set(then)

    def notify(self, text: str) -> None:
        """Announce something unprompted (finished task, timer). Queued behind any current reply."""
        if self.running.is_set():
            self.ai.submit(self._announce, text)

    def _announce(self, text: str) -> None:
        before = self.sm.state
        self.say(text, before if before in (State.STANDBY, State.ACTIVE) else State.ACTIVE)

    def _ask_aloud(self, prompt: str, wait_seconds: float = 12) -> str:
        """Speak a question and return what you answered ('' if nothing). Called from tools."""
        while not self.confirm_replies.empty():
            self.confirm_replies.get_nowait()
        with self._voice:
            self._awaiting_confirm.set()  # the slow-task timer pauses while you're being asked
            self.speaker.say(prompt, on_start=lambda: self.sm.set(State.SPEAKING))
        self._reset_endpointer.set()
        while not self.confirm_replies.empty():  # anything heard while it was asking is its own voice
            self.confirm_replies.get_nowait()
        self.sm.set(State.CONFIRM)
        deadline = time.monotonic() + wait_seconds
        try:
            # Empty transcripts are the tail of its own voice or noise, not your answer: keep waiting.
            # (On RK's PC every "Did you mean Taskmgr?" got '' instantly and was taken as "no".)
            while (left := deadline - time.monotonic()) > 0:
                try:
                    reply = self.confirm_replies.get(timeout=left)
                except queue.Empty:
                    break
                if reply.strip():
                    return reply
            return ""
        finally:
            self._awaiting_confirm.clear()
            self.sm.set(State.THINKING)

    def confirm(self, question: str) -> bool:
        """Ask a yes/no question out loud and wait for the answer (called from tools)."""
        reply = self._ask_aloud(f"{question} Yes or no?")
        decision = yes_no(reply)
        log.info("confirm %r -> %r -> %s", question, reply, decision)
        return decision is True

    def choose(self, question: str, options: dict[str, list[str]]) -> str | None:
        """Ask a question with several answers ("save, don't save, or cancel?"); None if unclear."""
        reply = self._ask_aloud(question, wait_seconds=14)
        choice = pick_option(reply, options)
        log.info("choose %r -> %r -> %s", question, reply, choice)
        return choice

    # ---------------------------------------------------------------- controls
    def toggle(self) -> None:
        """F8 / tray: same as saying the wake word, so what you say next is a request. While it's
        talking, F8 just stops it; pressing F8 again while it waits for a request puts it on standby.

        (F8 used to switch to fast-command mode and say "Listening.", where a normal request was
        ignored: RK pressed it, talked, and got nothing.)"""
        state = self.sm.state
        if state is State.SPEAKING:
            self.speaker.stop()
            self.sm.set(State.ACTIVE)
        elif state in (State.STANDBY, State.ACTIVE):
            self._attentive_limit = self.cfg.speech.attentive_timeout_seconds
            self.ai.submit(self.say, f"Yes, {self.cfg.user_name}?", State.ATTENTIVE)
        else:
            self.standby()

    def standby(self) -> None:
        self.speaker.stop()
        self.sm.set(State.STANDBY)
        self.ai.submit(self.say, "Standing by.", State.STANDBY)
