"""Microphone capture: 16 kHz mono int16 frames pushed onto a queue.

Works with any Windows input device, including a phone used as a mic through
WO Mic (it shows up as "Microphone (WO Mic Device)").
"""

from __future__ import annotations

import logging
import queue

import numpy as np
import sounddevice as sd

from ..config import AudioCfg

log = logging.getLogger(__name__)


def list_input_devices() -> list[tuple[int, str]]:
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def resolve_device(wanted: str | int | None) -> int | None:
    """Map a configured device (index, name fragment or None) to a device index."""
    if wanted is None or isinstance(wanted, int):
        return wanted
    for idx, name in list_input_devices():
        if wanted.lower() in name.lower():
            return idx
    log.warning("input device %r not found; using the Windows default", wanted)
    return None


class MicStream:
    """Opens the mic and delivers fixed-size frames (bytes) to `frames`."""

    def __init__(self, cfg: AudioCfg) -> None:
        self.cfg = cfg
        self.frame_samples = cfg.sample_rate * cfg.block_ms // 1000
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self._stream: sd.RawInputStream | None = None

    def _callback(self, indata, _frames, _time, status) -> None:
        if status:
            log.debug("audio status: %s", status)
        data = bytes(indata)
        if self.cfg.gain != 1.0:  # boost a quiet mic, clipping instead of wrapping around
            boosted = np.frombuffer(data, dtype=np.int16).astype(np.float32) * self.cfg.gain
            data = np.clip(boosted, -32768, 32767).astype(np.int16).tobytes()
        try:
            self.frames.put_nowait(data)
        except queue.Full:  # consumer stalled; drop audio rather than lag behind
            pass

    def start(self) -> None:
        device = resolve_device(self.cfg.input_device)
        self._stream = sd.RawInputStream(
            samplerate=self.cfg.sample_rate,
            blocksize=self.frame_samples,
            channels=1,
            dtype="int16",
            device=device,
            callback=self._callback,
        )
        self._stream.start()
        name = sd.query_devices(device if device is not None else sd.default.device[0])["name"]
        log.info("mic open: %s", name)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
