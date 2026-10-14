"""Private Module: Implements the necessary logic for pattern handling functionality of the High-Level API"""

from bisect import bisect_right
from itertools import accumulate
from time import time


class PatternHandler:
    """
    Helper class responsible for toy pattern playback logic.

    Manages:

    - Pattern list and wraparound behavior
    - Pause state and timing (elapsed time freezes during pause)
    - Pattern version tracking
    - Current intensity values at any pattern time
    - Pattern data for visualization
    """

    def __init__(self) -> None:
        # Pattern state
        self._pattern: list[tuple[int, int, int]] = []
        # End time of each segment (ms since the pattern start). Lookup is done via binary search.
        self._segment_ends: list[int] = []
        self._pattern_wraparound = True
        self._is_paused = False
        self._pattern_elapsed_time: float = 0.0
        self._segment_start_time: float | None = None
        self._pattern_version = 0

    @property
    def is_paused(self) -> bool:
        """Returns True if pattern playback is currently paused, else False"""
        return self._is_paused

    @property
    def pattern_version(self) -> int:
        """Each time the pattern state changes, the version number is incremented."""
        return self._pattern_version

    @property
    def has_active_pattern(self) -> bool:
        """Returns True if a non-empty pattern is currently set, else False"""
        return bool(self._pattern)

    def set_pattern(
        self,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> None:
        """
        Set a new pattern for playback.

        Args:
            pattern: List of pattern segments as tuples (duration_ms, intensity1, intensity2).
                A negative duration counts as zero.
            wraparound: If True, the pattern loops indefinitely, else it stops after one playthrough.
            reset_time: If True, restart the pattern from the beginning, if False, start from the current elapsed time.
        """
        self._pattern = pattern
        self._segment_ends = list(
            accumulate(max(0, duration) for duration, _, _ in pattern)
        )
        self._pattern_wraparound = wraparound
        self._pattern_version += 1

        if reset_time:
            # Reset elapsed time to zero, but don't start the segment timer if paused
            self._pattern_elapsed_time = 0.0
            if not self._is_paused and self.has_active_pattern:
                self._segment_start_time = time() * 1000
            else:
                self._segment_start_time = None
        else:
            # Keep current elapsed time, but ensure segment timer is consistent
            if (
                not self._is_paused
                and self.has_active_pattern
                and self._segment_start_time is None
            ):
                # Should only happen when resuming after having no pattern
                self._segment_start_time = time() * 1000

    def set_paused(self, paused: bool) -> None:
        """
        Set the paused state. While paused patterns do not progress.

        Args:
            paused: True to pause, False to resume
        """
        if paused == self._is_paused:
            return

        self._is_paused = paused
        self._pattern_version += 1

        if paused:
            # Entering pause: freeze elapsed time if a pattern is playing
            if self.has_active_pattern and self._segment_start_time is not None:
                current_time = time() * 1000
                segment_elapsed = current_time - self._segment_start_time
                self._pattern_elapsed_time += segment_elapsed
                self._segment_start_time = None
            # If no pattern, there is nothing to freeze, just store the paused state
        else:
            # Exiting pause: resume timing if a pattern exists
            if self.has_active_pattern:
                self._segment_start_time = time() * 1000
            else:
                self._segment_start_time = None

    def seek(self, pattern_time: float) -> None:
        """
        Move playback to a position in the pattern, e.g., to follow a video that was skipped forward or back.

        Works while playing and while paused: a paused pattern stays at the new position until it is resumed. Without
        a pattern, the position is kept for a pattern set later with ``reset_time=False``.

        Args:
            pattern_time: The new position in milliseconds. Negative values count as 0. A position past the end of a
                wraparound pattern wraps around as usual.
        """
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
            # When paused, return the frozen elapsed time
            return self._pattern_elapsed_time

        if self._segment_start_time is None:
            return 0.0

        # Calculate time elapsed in current segment
        current_time = time() * 1000
        segment_elapsed = current_time - self._segment_start_time

        # Total elapsed = pattern elapsed at segment start + current segment elapsed
        return self._pattern_elapsed_time + segment_elapsed

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """
        Get intensity values at a specific time in the pattern.

        Args:
            pattern_time: Time position in the pattern (milliseconds).

        Returns:
            tuple: (intensity1, intensity2) at the specified time.
        """
        if not self._pattern:
            return 0, 0

        total_duration = self._segment_ends[-1]
        if total_duration == 0:
            return 0, 0

        # Handle wraparound
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
