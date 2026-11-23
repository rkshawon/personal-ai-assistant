"""Free-form transcription for AI requests (faster-whisper, CPU, int8)."""

from __future__ import annotations

import logging
import threading

import numpy as np

from .. import paths
from ..log import timed

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model_size: str = "base", language: str = "en", prompt: str = "") -> None:
        self.model_size = model_size
        self.language = language
        # Words Whisper should expect, e.g. the assistant's made-up name, so it spells them right
        self.prompt = prompt or None
        self._model = None
        self._ready = threading.Event()

    def load(self) -> None:
        """Load (downloading on first run) the Whisper model. Slow: call it in the background."""
        from faster_whisper import WhisperModel

        with timed(f"load whisper {self.model_size}", log):
            self._model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(paths.WHISPER_DIR),
            )
        self._ready.set()

    def transcribe(self, pcm16: bytes) -> str:
        if not self._ready.wait(timeout=120):
            raise RuntimeError("Whisper model is still loading")
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with timed("whisper", log):
            segments, _info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                vad_filter=True,  # Silero VAD: skip fan noise and the tail of Amolex's own voice
                condition_on_previous_text=False,
                initial_prompt=self.prompt,
            )
            kept = []
            for s in segments:
                if is_hallucination(s.no_speech_prob, s.avg_logprob, s.compression_ratio):
                    log.info("dropped likely non-speech: %r (no_speech %.2f, logprob %.2f)",
                             s.text.strip(), s.no_speech_prob, s.avg_logprob)
                    continue
                kept.append(s.text.strip())
            text = " ".join(kept).strip()
        log.info("heard: %r", text)
        return text


def is_hallucination(no_speech_prob: float, avg_logprob: float, compression_ratio: float) -> bool:
    """Whisper's own rules for text it invented from silence or noise (on RK's PC: "Open." over
    and over, copied from the hint prompt) or looped on."""
    return (no_speech_prob > 0.6 and avg_logprob < -0.7) or avg_logprob < -1.2 or compression_ratio > 2.4
