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

    def __init__(self):
        # Pattern state
        self._pattern: list[tuple[int, int, int]] = []
        self._pattern_wraparound = True
        self._is_paused = False
        self._pattern_elapsed_time: float = 0.0
        self._segment_start_time: float | None = None
        self._pause_segment_elapsed: float = 0.0
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
        Internal: Set pattern data and update state.
        (Logging, stop() call, and block clearing happen in ToyController.)
        """
        self._pattern = pattern
        self._pattern_wraparound = wraparound
        self._pattern_version += 1
        if reset_time:
            self._restart_pattern()

    def set_paused(self, paused: bool) -> None:
        """Update pause state and adjust timing."""
        if paused == self._is_paused:
            return

        self._is_paused = paused
        self._pattern_version += 1
        if paused:
            # Entering pause: freeze elapsed time
            if self._segment_start_time is not None:
                current_time = time() * 1000
                segment_elapsed = current_time - self._segment_start_time
                self._pattern_elapsed_time += segment_elapsed
                self._pause_segment_elapsed = segment_elapsed
                self._segment_start_time = None
        else:
            # Exiting pause: resume timing from now
            self._segment_start_time = time() * 1000

    def get_pattern_time(self) -> float:
        """Get elapsed time in the current pattern (Time spent paused does not count toward elapsed time)."""
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
        """Get intensity values at a specific time in the pattern."""
        pattern = self._pattern
        if not pattern:
            return 0, 0

        # Calculate total pattern duration
        total_duration = sum(duration for duration, _, _ in pattern)

        if total_duration == 0:
            return 0, 0

        # Handle wraparound
        if self._pattern_wraparound:
            pattern_time = pattern_time % total_duration
        elif pattern_time >= total_duration:
            return 0, 0

        # Find the segment we're in
        elapsed = 0.0
        for duration, intensity1, intensity2 in pattern:
            if pattern_time < elapsed + duration:
                return intensity1, intensity2
            elapsed += duration

        # Should not reach here, but return last segment values
        return pattern[-1][1], pattern[-1][2]

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """Gets the complete pattern state for visualization."""
        return (
            self._pattern.copy(),
            self._pattern_wraparound,
            self._is_paused,
            self.get_pattern_time(),
        )

    def _restart_pattern(self) -> None:
        """Restart pattern playback from the beginning."""
        self._pattern_elapsed_time = 0.0
        self._segment_start_time = time() * 1000
        self._pause_segment_elapsed = 0.0
