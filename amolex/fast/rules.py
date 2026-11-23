"""The single rule for when fast commands fire. Used by the assistant and by --test-voice,
so what the test shows is exactly what the assistant would do.

Commands said in one breath ("shoot jump reload", "jump jump jump") arrive as one growing
Vosk result, so the text is split into a sequence of commands and every command not fired
yet in this segment fires, in order.
"""

from __future__ import annotations

from ..config import FastCfg


def command_sequence(text: str, commands: set[str]) -> list[str]:
    """Greedy longest-match split: 'shoot press w unk reload' -> ['shoot', 'press w', 'reload']."""
    words = text.split()
    longest = max((len(c.split()) for c in commands), default=1)
    seq: list[str] = []
    i = 0
    while i < len(words):
        for n in range(min(longest, len(words) - i), 0, -1):
            candidate = " ".join(words[i : i + n])
            if candidate in commands:
                seq.append(candidate)
                i += n
                break
        else:  # not a command word ([unk], wake words, noise): skip it
            i += 1
    return seq


def new_commands(
    text: str,
    final: bool,
    confidence: float,
    cfg: FastCfg,
    commands: set[str],
    already_fired: int,
    gaming: bool = True,
) -> list[str]:
    """Commands in `text` that haven't fired yet in this Vosk segment (already_fired = how many did).

    Firing on Vosk's first guess (partial) is fastest but can't be confidence-checked, so outside
    games (partial_only_when_gaming) commands wait for the final result and its confidence.
    """
    if not cfg.enabled:
        return []
    partial_ok = cfg.fire_on_partial and (gaming or not cfg.partial_only_when_gaming)
    if not final and not partial_ok:
        return []
    if final and confidence < cfg.min_confidence:
        return []
    if cfg.ignore_chatter and is_chatter(text, commands):
        return []
    return command_sequence(text, commands)[already_fired:]


def is_chatter(text: str, commands: set[str]) -> bool:
    """Normal talking: Vosk marks words outside the command list as [unk] ("unk" after normalising).

    On RK's PC, sentences like "did you hear me" came out as "[unk] crouch [unk]" and pressed keys.
    A real command phrase ("shoot", "shoot jump reload") has no unknown words around it.
    Commands said before the chatter starts ("reload ... unk") have already fired on the partial.
    """
    return "unk" in text.split()
