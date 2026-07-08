"""
Private Module: the execution-model-agnostic core shared by the two high-level toy controllers.

Both the synchronous :class:`tikal.high_level.toy_controller.ToyController` (queued, callback-based) and the async
:class:`tikal.websocket._toy_controller._ToyController` wrap a low-level :class:`~tikal.low_level.Toy` and add the same
things: read-only passthroughs to the toy, the pattern/pause/block bookkeeping, and the same pattern-playback algorithm.
This module contains the shared code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .pattern_handler import PatternHandler

if TYPE_CHECKING:
    from ..low_level import Toy


class BaseToyController(ABC):
    """
    Shared base for the High-Level and WebSocket toy controllers.

    Holds the underlying toy, the :class:`PatternHandler`, the block flag, and the last-sent-values bookkeeping, and
    implements every part of a controller that does not depend on the concrete execution model. Subclasses add their
    own control surface (the synchronous command queue, or the async intensity limits / battery cache) and implement
    the three ``_send_*`` primitives.

    Args:
        toy: The low-level toy this controller wraps.
    """

    def __init__(self, toy: "Toy") -> None:
        self._toy = toy
        self._pattern_handler = PatternHandler()
        self._last_values: dict[str, int | None] = {
            "intensity1": None,
            "intensity2": None,
        }
        self._accepted_pause = False
        self._is_blocked = False

    # ------------------------------------------------------------------
    # Read-only passthroughs to the underlying toy
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        """Model name of the toy (e.g., "Nora", "Lush")."""
        return self._toy.model_name

    @property
    def toy_id(self) -> str:
        """Unique identifier for the toy (typically its Bluetooth address)."""
        return self._toy.toy_id

    @property
    def name(self) -> str:
        """Human-readable identifier of the toy (e.g., its Bluetooth name)."""
        return self._toy.name

    @property
    def brand(self) -> str:
        """Human-readable identifier of the toy brand (e.g., 'Lovense')."""
        return self._toy.brand

    @property
    def max_intensity(self) -> int:
        """Maximum intensity value for this toy (e.g., 20 for Lovense toys)."""
        return self._toy.max_intensity

    @property
    def current_intensities(self) -> tuple[int, int]:
        """Current (primary, secondary) intensity values. Secondary is always 0 for single-capability toys."""
        return self._toy.current_intensities

    # ------------------------------------------------------------------
    # Pattern / pause / block state (read)
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Whether pattern playback is currently paused (the pattern timer stops advancing)."""
        return self._pattern_handler.is_paused

    @property
    def is_blocked(self) -> bool:
        """Whether the toy is currently blocked (all intensities forced to zero)."""
        return self._is_blocked

    @property
    def pattern_version(self) -> int:
        """Incremented each time the pattern state changes."""
        return self._pattern_handler.pattern_version

    def get_pattern_time(self) -> float:
        """Elapsed time in the current pattern in milliseconds (paused time does not count). 0.0 if no pattern."""
        return self._pattern_handler.get_pattern_time()

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """The ``(intensity1, intensity2)`` values at ``pattern_time`` (milliseconds) in the current pattern."""
        return self._pattern_handler.get_pattern_values(pattern_time)

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """The full pattern state: ``(pattern, wraparound, is_paused, elapsed_ms)``."""
        return self._pattern_handler.get_pattern_data()

    # ------------------------------------------------------------------
    # Shared pattern-playback algorithm (template method)
    # ------------------------------------------------------------------

    async def _run_pattern_playback(self) -> None:
        """
        Advance pattern playback by one tick, driving the toy through the ``_send_*`` primitives.

        On the first tick after entering a paused or blocked state, sends a single stop and latches it (no repeated
        stops). While active, sends an intensity only when its target value changed since the last successful send.
        Tracking state is updated only after each send's ``await`` returns, so a strict send that raises leaves the
        state unchanged and the command is retried next tick.
        """
        if not self._pattern_handler.has_active_pattern:
            return

        # Handle a paused or blocked state
        if self._pattern_handler.is_paused or self._is_blocked:
            if not self._accepted_pause:
                # First time entering paused/blocked state - send stop command
                await self._send_stop()
                self._last_values["intensity1"] = None
                self._last_values["intensity2"] = None
                self._accepted_pause = True
            # If already paused/blocked, do nothing (no repeated stop commands)

        # Handle active state
        else:
            self._accepted_pause = False

            # Get current values and send commands if values have changed
            pattern_time = self._pattern_handler.get_pattern_time()
            intensity1_value, intensity2_value = (
                self._pattern_handler.get_pattern_values(pattern_time)
            )

            if intensity1_value != self._last_values["intensity1"]:
                await self._send_intensity1(intensity1_value)
                self._last_values["intensity1"] = intensity1_value

            if intensity2_value != self._last_values["intensity2"]:
                await self._send_intensity2(intensity2_value)
                self._last_values["intensity2"] = intensity2_value

    @abstractmethod
    async def _send_stop(self) -> None:
        """Send a stop to the toy. High-Level uses the non-strict path; WebSocket uses the strict path."""
        raise NotImplementedError

    @abstractmethod
    async def _send_intensity1(self, level: int) -> None:
        """Send a primary-capability intensity to the toy (execution-model specific)."""
        raise NotImplementedError

    @abstractmethod
    async def _send_intensity2(self, level: int) -> None:
        """Send a secondary-capability intensity to the toy (execution-model specific)."""
        raise NotImplementedError
