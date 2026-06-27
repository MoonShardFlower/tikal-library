"""Tests for the private :class:`PatternHandler` (pattern playback timing and state)."""

import pytest

from tikal._private import pattern_handler as ph_module
from tikal._private.pattern_handler import PatternHandler


class FakeClock:
    """Controllable stand-in for ``time.time()`` (returns seconds; the handler converts to ms)."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(ph_module, "time", c)
    return c


# ---------------------------------------------------------------------------
# Basic state / version tracking
# ---------------------------------------------------------------------------


def test_initial_state():
    h = PatternHandler()
    assert h.has_active_pattern is False
    assert h.is_paused is False
    assert h.pattern_version == 0
    assert h.get_pattern_time() == 0.0
    assert h.get_pattern_values(123) == (0, 0)


def test_has_active_pattern_reflects_pattern(clock):
    h = PatternHandler()
    h.set_pattern([(100, 5, 0)])
    assert h.has_active_pattern is True
    h.set_pattern([])
    assert h.has_active_pattern is False


def test_set_pattern_increments_version(clock):
    h = PatternHandler()
    assert h.pattern_version == 0
    h.set_pattern([(100, 5, 0)])
    assert h.pattern_version == 1
    h.set_pattern([(200, 1, 1)])
    assert h.pattern_version == 2


def test_set_paused_increments_version_on_change_only(clock):
    h = PatternHandler()
    h.set_pattern([(100, 5, 0)])
    version = h.pattern_version

    h.set_paused(True)
    assert h.pattern_version == version + 1

    h.set_paused(True)  # no-op: already paused
    assert h.pattern_version == version + 1

    h.set_paused(False)
    assert h.pattern_version == version + 2


# ---------------------------------------------------------------------------
# get_pattern_values (pure mapping of time -> intensities)
# ---------------------------------------------------------------------------


def test_values_empty_pattern_returns_zero():
    h = PatternHandler()
    assert h.get_pattern_values(0) == (0, 0)
    assert h.get_pattern_values(5000) == (0, 0)


def test_values_zero_total_duration_returns_zero(clock):
    h = PatternHandler()
    h.set_pattern([(0, 5, 5)])
    assert h.get_pattern_values(0) == (0, 0)


def test_values_single_segment(clock):
    h = PatternHandler()
    h.set_pattern([(100, 7, 3)])
    assert h.get_pattern_values(0) == (7, 3)
    assert h.get_pattern_values(99) == (7, 3)


def test_values_segment_boundaries(clock):
    # Boundaries belong to the *next* segment (strict `<` on the upper edge).
    h = PatternHandler()
    h.set_pattern([(100, 1, 2), (200, 3, 4), (100, 5, 6)])  # total 400
    assert h.get_pattern_values(0) == (1, 2)
    assert h.get_pattern_values(99) == (1, 2)
    assert h.get_pattern_values(100) == (3, 4)
    assert h.get_pattern_values(299) == (3, 4)
    assert h.get_pattern_values(300) == (5, 6)
    assert h.get_pattern_values(399) == (5, 6)


def test_values_wraparound(clock):
    h = PatternHandler()
    h.set_pattern([(100, 1, 2), (200, 3, 4), (100, 5, 6)], wraparound=True)  # total 400
    assert h.get_pattern_values(400) == (1, 2)  # wraps to 0
    assert h.get_pattern_values(450) == (1, 2)  # wraps to 50
    assert h.get_pattern_values(700) == (5, 6)  # wraps to 300
    assert h.get_pattern_values(800) == (1, 2)  # wraps to 0 again


def test_values_no_wraparound_past_end_returns_zero(clock):
    h = PatternHandler()
    h.set_pattern(
        [(100, 1, 2), (200, 3, 4), (100, 5, 6)], wraparound=False
    )  # total 400
    assert h.get_pattern_values(399) == (5, 6)
    assert h.get_pattern_values(400) == (0, 0)  # exactly at end
    assert h.get_pattern_values(999) == (0, 0)  # past end


# ---------------------------------------------------------------------------
# Elapsed-time / pause / resume timing
# ---------------------------------------------------------------------------


def test_elapsed_time_advances_with_clock(clock):
    h = PatternHandler()
    h.set_pattern([(10_000, 5, 0)])
    assert h.get_pattern_time() == 0.0
    clock.advance(0.5)
    assert h.get_pattern_time() == 500.0
    clock.advance(1.0)
    assert h.get_pattern_time() == 1500.0


def test_pause_freezes_elapsed_time(clock):
    h = PatternHandler()
    h.set_pattern([(10_000, 5, 0)])
    clock.advance(0.5)  # 500 ms in

    h.set_paused(True)
    assert h.get_pattern_time() == 500.0

    clock.advance(2.0)  # time passes while paused
    assert h.get_pattern_time() == 500.0  # still frozen


def test_resume_excludes_paused_duration(clock):
    h = PatternHandler()
    h.set_pattern([(10_000, 5, 0)])
    clock.advance(0.5)  # 500 ms
    h.set_paused(True)
    clock.advance(2.0)  # paused gap (must not count)
    h.set_paused(False)

    assert h.get_pattern_time() == 500.0
    clock.advance(0.3)
    assert h.get_pattern_time() == 800.0  # 500 + 300, paused 2 s excluded


def test_pause_with_no_pattern_does_not_freeze_a_phantom_segment(clock):
    h = PatternHandler()
    h.set_paused(True)  # no active pattern
    assert h.is_paused is True
    assert h.get_pattern_time() == 0.0

    # Setting a pattern while paused must not start the timer until resumed.
    h.set_pattern([(10_000, 5, 0)])
    clock.advance(1.0)
    assert h.get_pattern_time() == 0.0
    h.set_paused(False)
    clock.advance(0.4)
    assert h.get_pattern_time() == 400.0


def test_set_pattern_reset_time_resets_elapsed(clock):
    h = PatternHandler()
    h.set_pattern([(10_000, 5, 0)])
    clock.advance(1.0)
    assert h.get_pattern_time() == 1000.0

    h.set_pattern([(10_000, 9, 0)], reset_time=True)
    assert h.get_pattern_time() == 0.0


def test_set_pattern_no_reset_time_keeps_elapsed(clock):
    h = PatternHandler()
    h.set_pattern([(10_000, 5, 0)])
    clock.advance(1.0)
    h.set_paused(True)  # freeze elapsed at 1000 ms
    assert h.get_pattern_time() == 1000.0

    h.set_pattern([(10_000, 9, 0)], reset_time=False)
    assert h.get_pattern_time() == 1000.0  # preserved


# ---------------------------------------------------------------------------
# get_pattern_data snapshot
# ---------------------------------------------------------------------------


def test_get_pattern_data_contents(clock):
    h = PatternHandler()
    pattern = [(100, 1, 2), (200, 3, 4)]
    h.set_pattern(pattern, wraparound=False)
    clock.advance(0.05)

    data, wraparound, is_paused, elapsed = h.get_pattern_data()
    assert data == pattern
    assert wraparound is False
    assert is_paused is False
    assert elapsed == 50.0


def test_get_pattern_data_returns_a_copy(clock):
    h = PatternHandler()
    h.set_pattern([(100, 1, 2)])
    data, *_ = h.get_pattern_data()
    data.append((999, 9, 9))  # mutate the returned list
    # internal pattern must be untouched
    data_again, *_ = h.get_pattern_data()
    assert data_again == [(100, 1, 2)]
