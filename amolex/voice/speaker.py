"""Spoken replies: Edge-TTS (online, natural) or Windows' built-in voice (offline).

Playback goes through sounddevice so it can be stopped instantly (barge-in).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from .. import paths
from ..config import VoiceCfg

log = logging.getLogger(__name__)


def _edge_tts_pcm(text: str, voice: str, rate: str) -> tuple[np.ndarray, int]:
    import edge_tts
    import miniaudio

    async def collect() -> bytes:
        out = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
            if chunk["type"] == "audio":
                out.extend(chunk["data"])
        return bytes(out)

    mp3 = asyncio.run(collect())
    decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000)
    return np.frombuffer(decoded.samples, dtype=np.int16), 24000


def _windows_voice_pcm(text: str) -> tuple[np.ndarray, int]:
    import pyttsx3

    paths.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav", dir=paths.TEMP_DIR, delete=False) as f:
        out = Path(f.name)
    engine = pyttsx3.init()
    engine.save_to_file(text, str(out))
    engine.runAndWait()
    with wave.open(str(out), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        data = w.readframes(w.getnframes())
    out.unlink(missing_ok=True)
    pcm = np.frombuffer(data, dtype=np.int16 if width == 2 else np.uint8)
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]
    return pcm, rate


class Speaker:
    def __init__(self, cfg: VoiceCfg) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.speaking = False

    def _synth(self, text: str) -> tuple[np.ndarray, int]:
        if self.cfg.prefer_online:
            try:
                return _edge_tts_pcm(text, self.cfg.online_voice, self.cfg.rate)
            except Exception as e:  # offline or service hiccup
                log.warning("edge-tts failed (%s); using the Windows voice", e)
        return _windows_voice_pcm(text)

    def say(self, text: str, on_start: Callable[[], None] | None = None) -> bool:
        """Speak and block until done. Returns False if interrupted."""
        if not text:
            return True
        with self._lock:
            self._stop.clear()
            pcm, rate = self._synth(text)
            if self._stop.is_set():
                return False
            log.info("say: %s", text)
            self.speaking = True
            if on_start:
                on_start()
            try:
                sd.play(pcm, rate)
                duration = len(pcm) / rate
                if self._stop.wait(timeout=duration + 0.3):
                    sd.stop()
                    return False
                return True
            finally:
                self.speaking = False

    def stop(self) -> None:
        """Interrupt the current reply (or one still being synthesised)."""
        self._stop.set()
        if self.speaking:
            sd.stop()
