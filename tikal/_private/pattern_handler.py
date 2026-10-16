"""Private module: pattern playback state and timing, used by the async core."""

import math
from bisect import bisect_right
from itertools import accumulate
from time import time


class PatternHandler:
    """
    Pattern playback state of one toy: the pattern, the pause, and the pattern time. Sends nothing.

    - A pattern is a list of ``(duration_ms, intensity1, intensity2)`` segments.
    - The pattern time is the playback position in ms. It stands still while paused.
    """

    def __init__(self) -> None:
        self._pattern: list[tuple[int, int, int]] = []
        # End time of each segment in ms since the pattern start, for a binary search.
        self._segment_ends: list[int] = []
        self._pattern_wraparound = True
        self._is_paused = False
        # Pattern time (ms) reached before the current running stretch.
        self._pattern_elapsed_time: float = 0.0
        # Wall-clock time (ms) at which the current running stretch began. None while not running.
        self._segment_start_time: float | None = None
        self._pattern_version = 0

    @property
    def is_paused(self) -> bool:
        """Whether playback is paused."""
        return self._is_paused

    @property
    def pattern_version(self) -> int:
        """Counter that increases whenever the pattern, the pause, or the position (:meth:`seek`) changes."""
        return self._pattern_version

    @property
    def has_active_pattern(self) -> bool:
        """Whether a non-empty pattern is set."""
        return bool(self._pattern)

    def set_pattern(
        self,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> None:
        """
        Set a new pattern. An empty list clears the pattern.

        Args:
            pattern: Segments as ``(duration_ms, intensity1, intensity2)``. A negative duration counts as 0.
            wraparound: Loop the pattern. If False, it plays once, then the intensities are 0.
            reset_time: Start at the beginning. If False, keep the current pattern time.
        """
        self._pattern = pattern
        self._segment_ends = list(
            accumulate(max(0, duration) for duration, _, _ in pattern)
        )
        self._pattern_wraparound = wraparound
        self._pattern_version += 1

        if reset_time:
            self._pattern_elapsed_time = 0.0
            if not self._is_paused and self.has_active_pattern:
                self._segment_start_time = time() * 1000
            else:
                self._segment_start_time = None
        else:
            if (
                not self._is_paused
                and self.has_active_pattern
                and self._segment_start_time is None
            ):
                # There was no pattern before: start running from the kept pattern time.
                self._segment_start_time = time() * 1000

    def set_paused(self, paused: bool) -> None:
        """
        Pause or resume playback. The pattern time stands still while paused.

        Args:
            paused: True to pause, False to resume. Repeating the current state changes nothing.
        """
        if paused == self._is_paused:
            return

        self._is_paused = paused
        self._pattern_version += 1

        if paused:
            if self.has_active_pattern and self._segment_start_time is not None:
                current_time = time() * 1000
                segment_elapsed = current_time - self._segment_start_time
                self._pattern_elapsed_time += segment_elapsed
                self._segment_start_time = None
        else:
            if self.has_active_pattern:
                self._segment_start_time = time() * 1000
            else:
                self._segment_start_time = None

    def seek(self, pattern_time: float) -> None:
        """
        Move playback to a position, e.g., to follow a video that jumped.

        - A paused pattern stays paused at the new position.
        - Without a pattern, the position is kept for a pattern set later with ``reset_time=False``.

        Args:
            pattern_time: The new pattern time in ms. Negative values count as 0.
                Past the end of a wraparound pattern, playback wraps around.

        Raises:
            ValueError: *pattern_time* is not a finite number. Nothing was changed.
        """
        if not math.isfinite(pattern_time):
            raise ValueError(
                f"pattern_time must be a finite number, got {pattern_time}"
            )
        self._pattern_elapsed_time = max(0.0, float(pattern_time))
        if not self._is_paused and self.has_active_pattern:
            self._segment_start_time = time() * 1000
        else:
            self._segment_start_time = None
        self._pattern_version += 1

    def get_pattern_time(self) -> float:
        """
        Get elapsed time in the current pattern (Time spent paused does not count toward elapsed time).

        Returns:
            elapsed_time: Time elapsed in the pattern in milliseconds.
        """
        if self._is_paused:
            return self._pattern_elapsed_time

        if self._segment_start_time is None:
            return 0.0

        current_time = time() * 1000
        segment_elapsed = current_time - self._segment_start_time
        return self._pattern_elapsed_time + segment_elapsed

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """
        The intensities the pattern plays at *pattern time*.

        Args:
            pattern_time: Pattern time in ms (see :meth:`get_pattern_time`).

        Returns:
            ``(intensity1, intensity2)``.
            ``(0, 0)`` without a pattern, for a pattern lasting 0 ms, and past the end of a pattern without wraparound.
        """
        if not self._pattern:
            return 0, 0

        total_duration = self._segment_ends[-1]
        if total_duration == 0:
            return 0, 0

        if self._pattern_wraparound:
            pattern_time = pattern_time % total_duration
        elif pattern_time >= total_duration:
            return 0, 0

        # The segment we're in is the first one that ends after pattern_time (a boundary belongs to the next segment)
        _, intensity1, intensity2 = self._pattern[
            bisect_right(self._segment_ends, pattern_time)
        ]
        return intensity1, intensity2

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """
        Gets the complete pattern state for visualization.

        Pattern state consists of:

        - pattern: List of tuples (duration_ms, intensity1, intensity2) defining the pattern segments.
        - wraparound: Whether the pattern repeats from the beginning after completing the last segment.
        - is_paused: Whether the pattern is currently paused.
        - elapsed_time: Time elapsed since the start of the pattern or last wraparound in ms

        Returns:
            tuple: (pattern, wraparound, is_paused, elapsed_time).
        """
        return (
            self._pattern.copy(),
            self._pattern_wraparound,
            self._is_paused,
            self.get_pattern_time(),
        )
