"""
Funscript support: load ``.funscript`` files and turn them into patterns.

A funscript is a JSON file of timed positions, originally written for linear strokers::

    {"version": "1.0", "inverted": false, "actions": [{"at": 0, "pos": 10}, {"at": 400, "pos": 90}]}

``at`` is the time in milliseconds, ``pos`` the position from 0 (bottom) to 100 (top). The toys tikal supports take an
intensity instead of a position, so :func:`funscript_to_pattern` maps a script onto the pattern format that pattern
playback already understands: a list of ``(duration_ms, intensity1, intensity2)`` segments. Every rule that applies to
a pattern (intensity limits, pause, block, the safety hold) therefore applies to a funscript as well.

- :class:`Funscript`: a validated script (load it with :meth:`Funscript.load` or :meth:`Funscript.from_json`)
- :func:`funscript_to_pattern`: converts a script (and optionally a second one for the secondary capability)

Example::

    from tikal.funscript import Funscript, funscript_to_pattern

    script = Funscript.load("video.funscript")
    pattern = funscript_to_pattern(script, max_intensity=toy.max_intensity, min_segment_ms=200)
    toy.set_pattern(pattern, wraparound=False)
"""

import math
import os
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator

#: Highest position a funscript action can have (the lowest is 0).
MAX_POSITION = 100


class Funscript(BaseModel):
    """
    A validated funscript.

    Actions are sorted by time. Of several actions at the same time, the last one wins. Fractional times and positions
    are rounded to whole numbers. Other fields of the file (``version``, ``range``, ``metadata``, ...) are ignored.

    Raises:
        pydantic.ValidationError: (subclass of ValueError) The script is malformed, e.g., an action without ``pos``,
            a negative time, or a position outside 0-100.

    Example::

        script = Funscript.load("video.funscript")
        script = Funscript.from_json('{"actions": [{"at": 0, "pos": 0}, {"at": 300, "pos": 100}]}')
        script = Funscript(actions=[(0, 0), (300, 100)])
    """

    #: The script's actions as ``(at_ms, pos)`` pairs, sorted by time. ``pos`` runs from 0 to 100.
    actions: tuple[tuple[int, int], ...]
    #: Whether the positions are upside down (``pos`` becomes ``100 - pos``). :func:`funscript_to_pattern` handles it.
    inverted: bool = False
    model_config = ConfigDict(extra="ignore", frozen=True)

    @field_validator("actions", mode="before")
    @classmethod
    def parse_actions(cls, value: Any) -> list[tuple[int, int]]:
        """Accept ``{"at", "pos"}`` objects (the file format) or ``[at, pos]`` pairs; sort them and drop duplicates."""
        if not isinstance(value, (list, tuple)):
            raise ValueError("'actions' must be a list")
        positions: dict[int, int] = {}
        for index, action in enumerate(value):
            if isinstance(action, dict):
                if "at" not in action or "pos" not in action:
                    raise ValueError(f"Action {index} needs both 'at' and 'pos'")
                at, pos = action["at"], action["pos"]
            elif isinstance(action, (list, tuple)) and len(action) == 2:
                at, pos = action
            else:
                raise ValueError(
                    f"Action {index} must be an object with 'at' and 'pos' or an [at, pos] pair"
                )
            at_ms = _whole_number(at, f"Action {index}: 'at'")
            position = _whole_number(pos, f"Action {index}: 'pos'")
            if at_ms < 0:
                raise ValueError(f"Action {index}: 'at' must not be negative, got {at}")
            if not 0 <= position <= MAX_POSITION:
                raise ValueError(
                    f"Action {index}: 'pos' must be between 0 and {MAX_POSITION}, got {pos}"
                )
            positions[at_ms] = (
                position  # A later action at the same time replaces an earlier one
            )
        return sorted(positions.items())

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Funscript":
        """
        Read a ``.funscript`` file.

        Args:
            path: Path of the file. It must be UTF-8 encoded JSON (a byte order mark is fine).

        Raises:
            OSError: The file could not be read.
            pydantic.ValidationError: (subclass of ValueError) The file is not valid JSON or not a valid funscript.

        Returns:
            The script.
        """
        return cls.from_json(Path(path).read_text(encoding="utf-8-sig"))

    @classmethod
    def from_json(cls, data: str | bytes) -> "Funscript":
        """
        Parse a funscript from its JSON text.

        Args:
            data: The JSON document, e.g., the content of a ``.funscript`` file.

        Raises:
            pydantic.ValidationError: (subclass of ValueError) The text is not valid JSON or not a valid funscript.

        Returns:
            The script.
        """
        return cls.model_validate_json(data)

    @property
    def duration_ms(self) -> int:
        """Time of the last action in milliseconds (0 for a script without actions)."""
        return self.actions[-1][0] if self.actions else 0


def _whole_number(value: Any, name: str) -> int:
    """Round a JSON number to an int. Rejects booleans, strings, NaN and infinity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value}")
    return round(value)


class _Piece(NamedTuple):
    """Stretch between two actions over which the level changes linearly from *start_level* to *end_level*."""

    start: int
    end: int
    start_level: float
    end_level: float


def funscript_to_pattern(
    script: Funscript,
    *,
    max_intensity: int,
    min_segment_ms: int,
    mode: Literal["speed", "position"] = "speed",
    max_speed: float = 400.0,
    secondary: Funscript | None = None,
) -> list[tuple[int, int, int]]:
    """
    Convert a funscript into a pattern of ``(duration_ms, intensity1, intensity2)`` segments.

    Two mappings are available:

    - ``"speed"`` (default): the intensity follows how fast the script moves. Between two actions, the speed is
      ``|pos change| / time``; *max_speed* (position units per second) and anything faster map to *max_intensity*.
      With the default of 400, a full stroke (0 to 100) in 250 ms gives the maximum, the same stroke over one second a
      quarter of it, and holding a position gives 0.
    - ``"position"``: the intensity follows the position (0 to 100 maps to 0 to *max_intensity*).

    The result is laid on a grid of *min_segment_ms* (pass the toy's recommended minimum interval): each grid slot gets
    the average level over its time and neighboring slots with the same levels are merged (= Every segment except the
    last is at least *min_segment_ms* long).

    Args:
        script: The script for the primary capability (``intensity1``).
        max_intensity: The toy's maximum intensity (e.g., 20 for Lovense toys).
        min_segment_ms: Shortest segment to produce, in milliseconds.
        mode: ``"speed"`` or ``"position"``, see above.
        max_speed: Speed (position units per second) that maps to *max_intensity* in ``"speed"`` mode.
        secondary: Optional script for the secondary capability. Converted with the same settings.
            Without it, ``intensity2`` is always 0.

    Raises:
        ValueError: *max_intensity* or *min_segment_ms* is below 1, *max_speed* is not positive, or *mode* is unknown.

    Returns:
        The pattern, to be played without wraparound. Empty if no script has an action after 0 ms.
        Setting an empty pattern clears the toy's pattern!

    Example::

        script = Funscript.load("video.funscript")
        twist = Funscript.load("video.twist.funscript")
        pattern = funscript_to_pattern(script, max_intensity=20, min_segment_ms=200, secondary=twist)
    """
    if max_intensity < 1:
        raise ValueError(f"max_intensity must be at least 1, got {max_intensity}")
    if min_segment_ms < 1:
        raise ValueError(f"min_segment_ms must be at least 1, got {min_segment_ms}")
    if not max_speed > 0:
        raise ValueError(f"max_speed must be positive, got {max_speed}")
    if mode not in ("speed", "position"):
        raise ValueError(f"mode must be 'speed' or 'position', got {mode!r}")

    end = max(script.duration_ms, secondary.duration_ms if secondary else 0)
    channels = [
        _slot_levels(_pieces(s, mode, max_speed, max_intensity), end, min_segment_ms)
        for s in (script, secondary)
        if s is not None
    ]
    if len(channels) == 1:
        channels.append([0] * len(channels[0]))

    pattern: list[tuple[int, int, int]] = []
    for slot, (level1, level2) in enumerate(zip(*channels)):
        duration = min(min_segment_ms, end - slot * min_segment_ms)
        if pattern and pattern[-1][1:] == (level1, level2):
            pattern[-1] = (pattern[-1][0] + duration, level1, level2)
        else:
            pattern.append((duration, level1, level2))
    return pattern


def _pieces(
    script: Funscript, mode: str, max_speed: float, max_intensity: int
) -> list[_Piece]:
    """Describe the level between each two consecutive actions, scaled to 0 - max_intensity."""
    actions = script.actions
    if script.inverted:
        actions = tuple((at, MAX_POSITION - pos) for at, pos in actions)
    pieces = []
    for (start, start_pos), (end, end_pos) in zip(actions, actions[1:]):
        if mode == "speed":
            speed = abs(end_pos - start_pos) / (end - start) * 1000
            level = min(speed / max_speed, 1.0) * max_intensity
            pieces.append(_Piece(start, end, level, level))
        else:
            scale = max_intensity / MAX_POSITION
            pieces.append(_Piece(start, end, start_pos * scale, end_pos * scale))
    return pieces


def _slot_levels(pieces: list[_Piece], end: int, width: int) -> list[int]:
    """
    Average the level over each slot of a grid of *width* ms that covers 0 to *end*.

    Outside the pieces, the level is 0. The last slot ends at *end* and may be shorter than *width*.
    """
    levels = []
    first = 0  # First piece that does not end before the current slot
    for slot_start in range(0, end, width):
        slot_end = min(slot_start + width, end)
        while first < len(pieces) and pieces[first].end <= slot_start:
            first += 1
        area = 0.0
        index = first
        while index < len(pieces) and pieces[index].start < slot_end:
            piece = pieces[index]
            area += _area(piece, max(piece.start, slot_start), min(piece.end, slot_end))
            index += 1
        levels.append(int(area / (slot_end - slot_start) + 0.5))
    return levels


def _area(piece: _Piece, start: int, end: int) -> float:
    """Integral of the piece's (linear) level from *start* to *end*, which lie within the piece."""

    def level_at(t: int) -> float:
        fraction = (t - piece.start) / (piece.end - piece.start)
        return piece.start_level + (piece.end_level - piece.start_level) * fraction

    return (level_at(start) + level_at(end)) / 2 * (end - start)
