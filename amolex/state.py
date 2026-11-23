"""Assistant states and a thread-safe state holder.

STANDBY    only the wake phrase / F8 is acted on
ACTIVE     fast commands fire; the wake phrase opens an AI request
ATTENTIVE  waiting for the AI request after "Hey Amolex"
CONFIRM    waiting for a spoken yes/no before a risky tool runs
THINKING   the AI is working
SPEAKING   a reply is playing; the wake phrase or F8 interrupts it
"""

from __future__ import annotations

import enum
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class State(enum.Enum):
    STANDBY = "standby"
    ACTIVE = "active"
    ATTENTIVE = "attentive"
    CONFIRM = "confirm"
    THINKING = "thinking"
    SPEAKING = "speaking"


class StateMachine:
    def __init__(self, initial: State = State.STANDBY) -> None:
        self._state = initial
        self._lock = threading.Lock()
        self._listeners: list[Callable[[State], None]] = []

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def on_change(self, fn: Callable[[State], None]) -> None:
        self._listeners.append(fn)

    def set(self, new: State) -> None:
        with self._lock:
            if new is self._state:
                return
            old, self._state = self._state, new
        log.info("state %s -> %s", old.value, new.value)
        for fn in self._listeners:
            try:
                fn(new)
            except Exception:  # a broken listener must never stop the pipeline
                log.exception("state listener failed")

    def is_(self, *states: State) -> bool:
        return self.state in states
