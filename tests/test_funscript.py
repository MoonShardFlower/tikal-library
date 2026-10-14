"""Tests for :mod:`tikal.funscript` (parsing funscripts and converting them to patterns)."""

import json
import random

import pytest
from pydantic import ValidationError

from tikal._private.pattern_handler import PatternHandler
from tikal.funscript import Funscript, funscript_to_pattern


def convert(actions, min_segment_ms=100, **kwargs):
    """Convert a list of (at, pos) pairs with a Lovense-like maximum of 20."""
    kwargs.setdefault("max_intensity", 20)
    return funscript_to_pattern(
        Funscript(actions=actions), min_segment_ms=min_segment_ms, **kwargs
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_from_json_reads_the_file_format_and_ignores_other_fields():
    text = json.dumps(
        {
            "version": "1.0",
            "inverted": True,
            "range": 90,
            "metadata": {"title": "Test", "creator": "someone"},
            "actions": [{"at": 0, "pos": 10}, {"at": 400, "pos": 90}],
        }
    )
    script = Funscript.from_json(text)
    assert script.actions == ((0, 10), (400, 90))
    assert script.inverted is True


def test_load_reads_a_file(tmp_path):
    path = tmp_path / "video.funscript"
    path.write_text(json.dumps({"actions": [{"at": 0, "pos": 0}]}), encoding="utf-8")
    assert Funscript.load(path).actions == ((0, 0),)


def test_load_accepts_a_byte_order_mark(tmp_path):
    path = tmp_path / "video.funscript"
    path.write_text(
        json.dumps({"actions": [{"at": 5, "pos": 50}]}), encoding="utf-8-sig"
    )
    assert Funscript.load(str(path)).actions == ((5, 50),)


def test_load_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        Funscript.load(tmp_path / "missing.funscript")


def test_actions_as_pairs():
    assert Funscript(actions=[[0, 0], (100, 100)]).actions == ((0, 0), (100, 100))


def test_actions_are_sorted_by_time():
    script = Funscript(actions=[(300, 30), (0, 0), (100, 10)])
    assert script.actions == ((0, 0), (100, 10), (300, 30))


def test_last_action_at_the_same_time_wins():
    script = Funscript(actions=[(0, 0), (100, 10), (100, 90)])
    assert script.actions == ((0, 0), (100, 90))


def test_fractional_values_are_rounded():
    script = Funscript.from_json('{"actions": [{"at": 99.6, "pos": 49.8}]}')
    assert script.actions == ((100, 50),)


def test_inverted_defaults_to_false():
    assert Funscript(actions=[]).inverted is False


def test_duration_is_the_time_of_the_last_action():
    assert Funscript(actions=[(500, 0), (100, 0)]).duration_ms == 500
    assert Funscript(actions=[]).duration_ms == 0


def test_script_is_immutable():
    script = Funscript(actions=[(0, 0)])
    with pytest.raises(ValidationError):
        script.inverted = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "actions",
    [
        [{"at": 0, "pos": 101}],
        [{"at": 0, "pos": -1}],
        [{"at": -5, "pos": 0}],
        [{"at": 0}],
        [{"pos": 0}],
        [[0, 0, 0]],
        [5],
        [{"at": "0", "pos": 0}],
        [{"at": 0, "pos": True}],
        [{"at": float("nan"), "pos": 0}],
        [{"at": 0, "pos": float("inf")}],
        "not a list",
    ],
)
def test_malformed_actions_are_rejected(actions):
    with pytest.raises(ValidationError):
        Funscript(actions=actions)


def test_validation_error_is_a_value_error():
    with pytest.raises(ValueError):
        Funscript(actions=[(0, 200)])


def test_missing_actions_are_rejected():
    with pytest.raises(ValidationError):
        Funscript.from_json('{"version": "1.0"}')


def test_invalid_json_is_rejected():
    with pytest.raises(ValidationError):
        Funscript.from_json("{not json")


# ---------------------------------------------------------------------------
# Speed mapping (the default)
# ---------------------------------------------------------------------------


def test_full_stroke_at_max_speed_gives_max_intensity():
    # 100 positions in 250 ms = 400 per second, the default max_speed
    assert convert([(0, 0), (250, 100)], min_segment_ms=250) == [(250, 20, 0)]


def test_slower_stroke_gives_proportionally_less():
    # 100 positions in one second = 100 per second, a quarter of max_speed
    assert convert([(0, 0), (1000, 100)], min_segment_ms=1000) == [(1000, 5, 0)]


def test_direction_does_not_matter():
    assert convert([(0, 100), (1000, 0)], min_segment_ms=1000) == [(1000, 5, 0)]


def test_speed_above_max_is_capped():
    assert convert([(0, 0), (50, 100)], min_segment_ms=50) == [(50, 20, 0)]


def test_holding_a_position_gives_zero():
    assert convert([(0, 40), (2000, 40)], min_segment_ms=500) == [(2000, 0, 0)]


def test_max_speed_scales_the_mapping():
    pattern = convert([(0, 0), (1000, 100)], min_segment_ms=1000, max_speed=200)
    assert pattern == [(1000, 10, 0)]


def test_max_intensity_scales_the_mapping():
    pattern = convert([(0, 0), (250, 100)], min_segment_ms=250, max_intensity=100)
    assert pattern == [(250, 100, 0)]


def test_toy_is_idle_before_the_first_action():
    pattern = convert([(1000, 0), (1250, 100)], min_segment_ms=250)
    assert pattern == [(1000, 0, 0), (250, 20, 0)]


def test_inverted_does_not_change_speed():
    actions = [(0, 0), (500, 100), (1000, 30)]
    assert funscript_to_pattern(
        Funscript(actions=actions, inverted=True), max_intensity=20, min_segment_ms=100
    ) == convert(actions)


# ---------------------------------------------------------------------------
# Grid / segment length
# ---------------------------------------------------------------------------


def test_short_strokes_are_averaged_over_a_slot():
    # 0-100 ms at max speed (20), 100-200 ms holding still (0): the 200 ms slot averages to 10
    assert convert([(0, 0), (100, 40), (200, 40)], min_segment_ms=200) == [(200, 10, 0)]


def test_equal_neighbouring_slots_are_merged():
    pattern = convert([(0, 0), (1000, 100)], min_segment_ms=100)
    assert pattern == [(1000, 5, 0)]


def test_last_slot_ends_with_the_script():
    pattern = convert([(0, 0), (250, 100), (500, 0)], min_segment_ms=200)
    assert [duration for duration, _, _ in pattern] == [500]  # one merged segment
    pattern = convert([(0, 0), (250, 100), (500, 100)], min_segment_ms=200)
    # slot 200-400: 50 ms at 20, 150 ms at 0 -> 5; slot 400-500: 0
    assert pattern == [(200, 20, 0), (200, 5, 0), (100, 0, 0)]


def test_segments_respect_the_minimum_length_on_a_dense_script():
    rng = random.Random(7)
    at, actions = 0, []
    for _ in range(2000):
        actions.append((at, rng.randint(0, 100)))
        at += rng.randint(20, 400)
    script = Funscript(actions=actions)

    pattern = funscript_to_pattern(script, max_intensity=20, min_segment_ms=200)

    durations = [duration for duration, _, _ in pattern]
    assert all(duration >= 200 for duration in durations[:-1])
    assert 0 < durations[-1]
    assert sum(durations) == script.duration_ms
    assert all(0 <= level <= 20 for _, level, _ in pattern)
    assert all(level2 == 0 for _, _, level2 in pattern)
    # Neighbours always differ, otherwise they would have been merged
    assert all(a[1:] != b[1:] for a, b in zip(pattern, pattern[1:]))


def test_script_without_movement_time_gives_an_empty_pattern():
    assert convert([]) == []
    assert convert([(0, 50)]) == []


def test_single_action_later_on_gives_an_idle_pattern():
    assert convert([(500, 50)]) == [(500, 0, 0)]


# ---------------------------------------------------------------------------
# Position mapping
# ---------------------------------------------------------------------------


def test_position_mode_averages_the_position():
    # Position rises linearly 0 -> 100: averages 25 and 75 over the two halves
    pattern = convert([(0, 0), (1000, 100)], min_segment_ms=500, mode="position")
    assert pattern == [(500, 5, 0), (500, 15, 0)]


def test_position_mode_holding_gives_a_constant_level():
    pattern = convert([(0, 50), (1000, 50)], min_segment_ms=200, mode="position")
    assert pattern == [(1000, 10, 0)]


def test_position_mode_respects_inverted():
    script = Funscript(actions=[(0, 0), (1000, 0)], inverted=True)
    pattern = funscript_to_pattern(
        script, max_intensity=20, min_segment_ms=500, mode="position"
    )
    assert pattern == [(1000, 20, 0)]


def test_position_mode_is_idle_before_the_first_action():
    pattern = convert([(400, 100), (800, 100)], min_segment_ms=400, mode="position")
    assert pattern == [(400, 0, 0), (400, 20, 0)]


# ---------------------------------------------------------------------------
# Secondary script
# ---------------------------------------------------------------------------


def test_secondary_script_drives_intensity2():
    primary = Funscript(actions=[(0, 0), (1000, 100)])  # 5 throughout
    twist = Funscript(actions=[(0, 0), (500, 100), (1000, 100)])  # 10, then 0
    pattern = funscript_to_pattern(
        primary, max_intensity=20, min_segment_ms=500, secondary=twist
    )
    assert pattern == [(500, 5, 10), (500, 5, 0)]


def test_shorter_script_is_idle_after_its_end():
    primary = Funscript(actions=[(0, 0), (250, 100)])
    twist = Funscript(actions=[(0, 0), (1000, 100)])
    pattern = funscript_to_pattern(
        primary, max_intensity=20, min_segment_ms=250, secondary=twist
    )
    assert pattern == [(250, 20, 5), (750, 0, 5)]


def test_same_script_on_both_channels():
    script = Funscript(actions=[(0, 0), (250, 100)])
    pattern = funscript_to_pattern(
        script, max_intensity=20, min_segment_ms=250, secondary=script
    )
    assert pattern == [(250, 20, 20)]


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_intensity": 0},
        {"min_segment_ms": 0},
        {"max_speed": 0},
        {"max_speed": -1.0},
        {"mode": "depth"},
    ],
)
def test_invalid_arguments_are_rejected(kwargs):
    arguments = {"max_intensity": 20, "min_segment_ms": 100, **kwargs}
    with pytest.raises(ValueError):
        funscript_to_pattern(Funscript(actions=[(0, 0), (100, 100)]), **arguments)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


def test_converted_pattern_plays_back_in_sync_with_the_script():
    script = Funscript(actions=[(1000, 0), (1250, 100), (2250, 0), (4000, 0)])
    pattern = funscript_to_pattern(script, max_intensity=20, min_segment_ms=250)

    handler = PatternHandler()
    handler.set_pattern(pattern, wraparound=False)
    assert handler.get_pattern_values(500) == (0, 0)  # before the script starts
    assert handler.get_pattern_values(1100) == (20, 0)  # fast stroke up
    assert handler.get_pattern_values(2000) == (5, 0)  # slow stroke down
    assert handler.get_pattern_values(3000) == (0, 0)  # holding still
    assert handler.get_pattern_values(4000) == (0, 0)  # script over
