"""One sentence = one wake.

Vosk reports the wake phrase twice per sentence: first as a partial result, then
as the final result a moment later. The end-of-speech detector (VAD) usually closes
the sentence in between, so without this the late final looked like a second
"Hey Amolex" and Amolex answered an empty request with "Yes, RK?".

Rule: once a sentence with the wake phrase has been handled, ignore further wake
hits from the same Vosk segment, i.e. until Vosk finalises it.
"""

from __future__ import annotations


class WakeTracker:
    def __init__(self) -> None:
        self.pending = False  # wake heard in the sentence currently being spoken
        self._segment_open = False  # Vosk has partial results it hasn't finalised yet
        self._consumed = False  # the open segment's wake was already handled

    def on_hit(self, final: bool) -> None:
        """Call for every Vosk result that has text (wake or not), before on_wake."""
        if not final:
            self._segment_open = True

    def on_wake(self) -> bool:
        """A wake phrase was heard. Returns True if it counts (not a repeat of a handled one)."""
        if self._consumed:
            return False
        self.pending = True
        return True

    def on_final(self) -> None:
        """Vosk finalised its segment: later wake hits belong to a new sentence."""
        self._segment_open = False
        self._consumed = False

    def take(self) -> bool:
        """The sentence ended. Returns True if it contained a (new) wake phrase."""
        had = self.pending
        self.pending = False
        if had and self._segment_open:
            self._consumed = True  # Vosk's coming final for this segment repeats the wake
        return had
