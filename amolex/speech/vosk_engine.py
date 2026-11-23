"""Vosk recognizer restricted to a fixed grammar: wake phrases, fast commands, control words.

Restricting the grammar is what makes the fast path fast and accurate: Vosk only
has to choose between a few dozen phrases (or "[unk]") instead of all of English.
Words that are not in the model's dictionary are dropped from the grammar with a
warning, because Vosk cannot recognise them at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from vosk import KaldiRecognizer, Model, SetLogLevel

from .text import normalise

log = logging.getLogger(__name__)
SetLogLevel(-1)


@dataclass
class Hit:
    text: str
    confidence: float  # mean word confidence, 0..1 (partials report 1.0)
    final: bool


class GrammarRecognizer:
    def __init__(self, model_dir: Path, sample_rate: int, phrases: list[str]) -> None:
        if not model_dir.exists():
            raise FileNotFoundError(f"Vosk model missing: {model_dir} (run start.bat once to download it)")
        self.model = Model(str(model_dir))
        self.sample_rate = sample_rate
        self.phrases, self.rejected = self._usable(phrases)
        for p, missing in self.rejected.items():
            log.warning("phrase %r skipped: word(s) %s not in the Vosk dictionary", p, missing)
        grammar = json.dumps(sorted(self.phrases) + ["[unk]"])
        self.rec = KaldiRecognizer(self.model, sample_rate, grammar)
        self.rec.SetWords(True)
        self._last_partial = ""

    def _usable(self, phrases: list[str]) -> tuple[set[str], dict[str, list[str]]]:
        ok, bad = set(), {}
        for phrase in {normalise(p) for p in phrases if p.strip()}:
            missing = [w for w in phrase.split() if self.model.vosk_model_find_word(w) < 0]
            if missing:
                bad[phrase] = missing
            else:
                ok.add(phrase)
        return ok, bad

    def accept(self, frame: bytes) -> Hit | None:
        """Feed a frame; returns a new partial or a final hit, or None."""
        if self.rec.AcceptWaveform(frame):
            self._last_partial = ""
            res = json.loads(self.rec.Result())
            text = res.get("text", "").strip()
            if not text or text == "[unk]":
                return Hit("", 0.0, True)
            confs = [w.get("conf", 0.0) for w in res.get("result", [])]
            return Hit(text, sum(confs) / len(confs) if confs else 0.0, True)
        partial = json.loads(self.rec.PartialResult()).get("partial", "").strip()
        if partial and partial != self._last_partial:
            self._last_partial = partial
            return Hit(partial, 1.0, False)
        return None

    def reset(self) -> None:
        self.rec.Reset()
        self._last_partial = ""
