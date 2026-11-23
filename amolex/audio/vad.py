"""End-of-speech detection with WebRTC VAD.

Collects the audio of one utterance (speech plus a little padding) and hands it
over once the speaker has been quiet for `end_of_speech_ms`.
"""

from __future__ import annotations

import collections

import webrtcvad


class Endpointer:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        end_of_speech_ms: int = 700,
        max_seconds: float = 12,
        aggressiveness: int = 2,
        start_frames: int = 3,
    ) -> None:
        self.vad = webrtcvad.Vad(aggressiveness)
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_frames_needed = max(1, end_of_speech_ms // frame_ms)
        self.max_frames = int(max_seconds * 1000 / frame_ms)
        self.start_frames = start_frames
        self._pre = collections.deque(maxlen=10)  # ~300 ms kept before speech starts
        self.reset()

    def reset(self) -> None:
        self._buf: list[bytes] = []
        self._in_speech = False
        self._voiced_run = 0
        self._silent_run = 0
        self._pre.clear()

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def feed(self, frame: bytes) -> bytes | None:
        """Feed one frame; returns the whole utterance when it ends, else None."""
        voiced = self.vad.is_speech(frame, self.sample_rate)
        if not self._in_speech:
            self._pre.append(frame)
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            if self._voiced_run >= self.start_frames:
                self._in_speech = True
                self._buf = list(self._pre)
                self._silent_run = 0
            return None

        self._buf.append(frame)
        self._silent_run = 0 if voiced else self._silent_run + 1
        if self._silent_run >= self.silence_frames_needed or len(self._buf) >= self.max_frames:
            audio = b"".join(self._buf)
            self.reset()
            return audio
        return None
