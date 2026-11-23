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
from .ai.providers import warm_up
from .ai.tools import build_tools
from .audio.capture import MicStream
from .audio.vad import Endpointer
from .config import CommandSet, Config
from .fast import actions
from .fast.rules import new_commands
from .log import timed
from .speech.text import contains_phrase, normalise, parse_gaming, parse_switch, speakable, strip_wake, yes_no
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
        tools = build_tools(cfg.tools, self.confirm, self.notify, self.jobs, extra=[self._switch_tool()])
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
        self._attentive_since = 0.0
        self._fallback_announced = False
        self.tray = None
        self.sm.on_change(self._on_state)

    # ---------------------------------------------------------------- lifecycle
    def run(self) -> None:
        self.running.set()
        threading.Thread(target=self._load_whisper, name="whisper-load", daemon=True).start()
        threading.Thread(target=self._stt_loop, name="stt", daemon=True).start()
        if self.cfg.brain.provider == "local":  # load models now, not in the middle of a question
            threading.Thread(target=self._warm_models, name="warm-up", daemon=True).start()
        self._start_hotkey()
        self._start_tray()
        self.mic.start()
        self.ai.submit(self.say, f"Online, {self.cfg.user_name}. Press {self.cfg.wake.hotkey.upper()} or say the wake word.", State.STANDBY)
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
        """Light model first (most requests); then the normal one unless a game needs the VRAM.
        The heavy model is only loaded after you say yes to it."""
        router = self.brain.router
        warm_up(self.cfg.brain, router.model_for("light"))
        if not router.gaming.check()[0]:
            warm_up(self.cfg.brain, router.model_for("normal"))

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
            and time.monotonic() - self._attentive_since > self.cfg.speech.attentive_timeout_seconds
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
            if kind == "wake":
                text = strip_wake(text)
                if not text:
                    self.ai.submit(self.say, f"Yes, {self.cfg.user_name}?", State.ATTENTIVE)
                    continue
            if text.strip():
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
            reply = intents.handle(text, self.tools, set(self.cfg.tools.apps))
        if reply:
            log.info("instant: %r -> %s", text, reply)
            self.say(reply)
            return
        route = self.brain.plan(text)
        if route and route.tier == "heavy":
            route = self._approve_heavy(route)
        if route and route.tier != "heavy" and not self.brain.local.is_loaded(route.model):
            self.say("One moment, loading the model.", State.THINKING)
        if self.tray and route:
            self.tray.update(State.THINKING, f"{route.model}, {route.tier}")
        answer = self.brain.ask(text, route)
        reply = answer.text
        if answer.fell_back and not self._fallback_announced:
            reply = "Gemini isn't available, so I'm using the local model. " + reply
            self._fallback_announced = True
        elif answer.provider == "gemini":
            self._fallback_announced = False
        self.say(reply)

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

    def confirm(self, question: str) -> bool:
        """Ask a yes/no question out loud and wait for the answer (called from tools)."""
        while not self.confirm_replies.empty():
            self.confirm_replies.get_nowait()
        self.speaker.say(f"{question} Yes or no?", on_start=lambda: self.sm.set(State.SPEAKING))
        self._reset_endpointer.set()
        self._awaiting_confirm.set()
        self.sm.set(State.CONFIRM)
        try:
            reply = self.confirm_replies.get(timeout=8)
        except queue.Empty:
            reply = ""
        finally:
            self._awaiting_confirm.clear()
            self.sm.set(State.THINKING)
        decision = yes_no(reply)
        log.info("confirm %r -> %r -> %s", question, reply, decision)
        return decision is True

    # ---------------------------------------------------------------- controls
    def toggle(self) -> None:
        """F8 / tray: standby <-> listening; while speaking, just stop talking."""
        state = self.sm.state
        if state is State.SPEAKING:
            self.speaker.stop()
            self.sm.set(State.ACTIVE)
        elif state is State.STANDBY:
            self.sm.set(State.ACTIVE)
            self.ai.submit(self.say, "Listening.")
        else:
            self.standby()

    def standby(self) -> None:
        self.speaker.stop()
        self.sm.set(State.STANDBY)
        self.ai.submit(self.say, "Standing by.", State.STANDBY)
