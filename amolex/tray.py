"""System tray icon that shows the current state and offers quick controls."""

from __future__ import annotations

import logging
from typing import Callable

from PIL import Image, ImageDraw

from .state import State

log = logging.getLogger(__name__)

COLOURS = {
    State.STANDBY: (120, 120, 120),
    State.ACTIVE: (46, 204, 113),
    State.ATTENTIVE: (52, 152, 219),
    State.CONFIRM: (241, 196, 15),
    State.THINKING: (155, 89, 182),
    State.SPEAKING: (230, 126, 34),
}


def _icon(colour: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(245, 245, 245, 255))  # white ring, for the white build
    d.ellipse((14, 14, 50, 50), fill=(*colour, 255))
    return img


class Tray:
    def __init__(
        self,
        name: str,
        toggle: Callable[[], None],
        use_gemini: Callable[[], None],
        use_local: Callable[[], None],
        quit_: Callable[[], None],
        toggle_gaming: Callable[[], None] | None = None,
    ) -> None:
        import pystray

        self.name = name
        items = [
            pystray.MenuItem("Listen / standby (F8)", lambda: toggle(), default=True),
            pystray.MenuItem("Use automatic local models", lambda: use_local()),
            pystray.MenuItem("Use Gemini", lambda: use_gemini()),
        ]
        if toggle_gaming:
            items.append(pystray.MenuItem("Gaming mode on / off", lambda: toggle_gaming()))
        items.append(pystray.MenuItem("Quit", lambda: quit_()))
        menu = pystray.Menu(*items)
        self.icon = pystray.Icon("amolex", _icon(COLOURS[State.STANDBY]), f"{name}: standby", menu)

    def start(self) -> None:
        self.icon.run_detached()

    def update(self, state: State, detail: str = "") -> None:
        self.icon.icon = _icon(COLOURS[state])
        self.icon.title = f"{self.name}: {state.value}" + (f" ({detail})" if detail else "")

    def stop(self) -> None:
        self.icon.stop()
