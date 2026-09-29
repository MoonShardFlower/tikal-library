"""Tests for the WebSocket :class:`_ToyController` (async toy control with intensity limits)."""

from unittest.mock import AsyncMock

import pytest

from tikal._core import _ToyController
from tikal.low_level import Toy


@pytest.fixture
def mock_toy():
    toy = AsyncMock(spec=Toy)
    toy.toy_id = "toy-1"
    toy.name = "Thunder1"
    toy.brand = "MockEstimToys"
    toy.model_name = "Thunder"
    toy.max_intensity = 100
    toy.current_intensities = (0, 0)
    toy.intensity_names = ("Stim A", "Stim B")
    toy.change_rotation_direction_available = False
    toy.recommended_min_interval = 100
    toy.strict_intensity1.return_value = True
    toy.strict_intensity2.return_value = True
    toy.strict_stop.return_value = True
    toy.strict_get_battery_level.return_value = 77
    return toy


@pytest.fixture
def controller(mock_toy):
    return _ToyController(mock_toy, initial_battery=77)


async def _set_limit1(controller, level):
    """What _ToyHub.set_intensity1_limit does: record the limit, then bring the toy down if it runs above it."""
    if controller.apply_intensity1_limit(level):
        await controller.enforce_intensity1_limit()


async def _set_limit2(controller, level):
    """What _ToyHub.set_intensity2_limit does. See _set_limit1."""
    if controller.apply_intensity2_limit(level):
        await controller.enforce_intensity2_limit()


# ---------------------------------------------------------------------------
# Intensity limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_pattern_stores_pattern_unclamped_and_limits_on_playback(
    controller, mock_toy
):
    # The pattern is kept exactly as given; the limits are applied per tick instead.
    await _set_limit1(controller, 10)
    await _set_limit2(controller, 5)

    controller.apply_pattern([(100, 20, 20), (200, 3, 99)])

    assert controller.get_state()["pattern"] == [(100, 20, 20), (200, 3, 99)]

    await controller.process_communication()
    mock_toy.strict_intensity1.assert_awaited_once_with(10)
    mock_toy.strict_intensity2.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_lowering_limit_reins_in_a_running_pattern(controller, mock_toy):
    # Regression: a limit lowered mid-playback used to leave the running pattern at its original values,
    # because set_pattern baked the limits in once and nothing re-clamped afterwards.
    controller.apply_pattern([(60_000, 90, 90)])
    await controller.process_communication()
    mock_toy.current_intensities = (90, 90)

    mock_toy.reset_mock()
    await _set_limit1(controller, 5)
    await _set_limit2(controller, 7)

    # Brought down straight away, without waiting for a playback tick or the next segment.
    mock_toy.strict_intensity1.assert_awaited_once_with(5)
    mock_toy.strict_intensity2.assert_awaited_once_with(7)
    assert controller.get_state()["intensity_limits"] == [5, 7]

    mock_toy.current_intensities = (5, 7)
    mock_toy.reset_mock()
    await controller.process_communication()  # already at the ceiling -> nothing to resend
    mock_toy.strict_intensity1.assert_not_called()
    mock_toy.strict_intensity2.assert_not_called()


@pytest.mark.asyncio
async def test_raising_limit_restores_the_patterns_own_values(controller, mock_toy):
    controller.apply_pattern([(60_000, 90, 90)])
    await _set_limit1(controller, 5)
    await controller.process_communication()
    mock_toy.current_intensities = (5, 90)

    mock_toy.reset_mock()
    await _set_limit1(controller, None)  # withdraw the limit
    await controller.process_communication()
    mock_toy.strict_intensity1.assert_awaited_once_with(90)


@pytest.mark.asyncio
async def test_lowering_limit_reins_in_a_manual_intensity(controller, mock_toy):
    # No pattern is running, so nothing else would ever send a corrective command.
    await controller.intensity1(90)
    mock_toy.current_intensities = (90, 0)

    mock_toy.reset_mock()
    await _set_limit1(controller, 4)
    mock_toy.strict_intensity1.assert_awaited_once_with(4)


@pytest.mark.asyncio
async def test_setting_limit_below_current_value_is_a_noop_when_already_lower(
    controller, mock_toy
):
    mock_toy.current_intensities = (2, 0)
    await _set_limit1(controller, 50)
    mock_toy.strict_intensity1.assert_not_called()


@pytest.mark.asyncio
async def test_manual_intensity_clamped_to_limit(controller, mock_toy):
    await _set_limit1(controller, 8)
    await controller.intensity1(20)
    mock_toy.strict_intensity1.assert_awaited_once_with(8)


@pytest.mark.asyncio
async def test_intensity_limit_none_resets_to_max(controller, mock_toy):
    await _set_limit1(controller, 5)
    await _set_limit1(controller, None)  # reset to max_intensity (100)
    await controller.intensity1(20)
    mock_toy.strict_intensity1.assert_awaited_once_with(20)


@pytest.mark.asyncio
async def test_negative_limit_is_clamped_to_zero(controller, mock_toy):
    await _set_limit1(controller, -5)
    assert controller.get_state()["intensity_limits"][0] == 0
    await controller.intensity1(20)
    mock_toy.strict_intensity1.assert_awaited_once_with(0)


@pytest.mark.asyncio
async def test_set_model_name_invalidates_playback_tracking(controller, mock_toy):
    # The switch stops the toy on the old command set, so playback must re-send rather than assume
    # the toy still holds what it last sent.
    controller.apply_pattern([(60_000, 5, 3)])
    await controller.process_communication()

    await controller.set_model_name("Lightning")
    mock_toy.set_model_name.assert_awaited_once_with("Lightning")

    mock_toy.reset_mock()
    await controller.process_communication()
    mock_toy.strict_intensity1.assert_awaited_once_with(5)
    mock_toy.strict_intensity2.assert_awaited_once_with(3)


# ---------------------------------------------------------------------------
# Block / pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_intensity_returns_false_without_command(controller, mock_toy):
    controller.apply_blocked(True)

    assert await controller.intensity1(10) is False
    assert await controller.intensity2(10) is False
    mock_toy.strict_intensity1.assert_not_called()
    mock_toy.strict_intensity2.assert_not_called()


def test_pause_and_block_mutual_exclusion(controller, mock_toy):
    # Each transition takes full effect on its own, before any stop is sent: pausing a blocked toy unblocks it even if
    # the stop that follows fails.
    assert controller.apply_paused(True) is True  # a stop has to follow
    assert controller.is_paused is True
    assert controller.is_blocked is False

    assert controller.apply_blocked(True) is True
    assert controller.is_blocked is True
    assert controller.is_paused is False

    assert controller.apply_paused(True) is True
    assert controller.is_paused is True
    assert controller.is_blocked is False

    assert controller.apply_paused(False) is False  # resuming needs no command
    assert controller.apply_blocked(False) is False
    assert not mock_toy.mock_calls  # transitions never talk to the toy


@pytest.mark.asyncio
async def test_stop_pauses_pattern_and_sends_strict_stop(controller, mock_toy):
    assert await controller.stop() is True
    assert controller.is_paused is True
    mock_toy.strict_stop.assert_awaited()


# ---------------------------------------------------------------------------
# State / info snapshots
# ---------------------------------------------------------------------------


def test_get_state_contents(controller, mock_toy):
    mock_toy.current_intensities = (4, 2)
    state = controller.get_state()
    assert state["toy_id"] == "toy-1"
    assert state["current_intensities"] == [4, 2]
    assert state["intensity_limits"] == [100, 100]
    assert state["is_blocked"] is False
    assert state["is_paused"] is False
    assert state["pattern"] == []


@pytest.mark.asyncio
async def test_get_info_basic(controller):
    info = await controller.get_info(full=False)
    assert info["toy_id"] == "toy-1"
    assert info["brand"] == "MockEstimToys"
    assert info["model_name"] == "Thunder"
    assert info["intensity_names"] == ["Stim A", "Stim B"]
    assert info["max_intensity"] == 100
    assert info["supports_rotation"] is False
    assert info["recommended_min_interval"] == 100


@pytest.mark.asyncio
async def test_get_info_single_capability_blanks_second_name(controller, mock_toy):
    mock_toy.intensity_names = ("Stim", None)
    info = await controller.get_info(full=False)
    assert info["intensity_names"] == ["Stim", ""]


# ---------------------------------------------------------------------------
# Battery / lifecycle / playback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_and_update_battery(mock_toy):
    controller = _ToyController(mock_toy, initial_battery=50)

    mock_toy.strict_get_battery_level.return_value = 80
    assert await controller.fetch_and_update_battery() == 80
    assert controller.battery == 80

    # unchanged value -> returns None, keeps value
    assert await controller.fetch_and_update_battery() is None
    assert controller.battery == 80

    # error -> returns None, keeps last known value
    mock_toy.strict_get_battery_level.side_effect = ConnectionError("boom")
    assert await controller.fetch_and_update_battery() is None
    assert controller.battery == 80


@pytest.mark.asyncio
async def test_disconnect_blocks_and_calls_strict_disconnect(controller, mock_toy):
    await controller.disconnect()
    assert controller.is_blocked is True
    mock_toy.strict_disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_pattern_playback_sends_changed_values_once(controller, mock_toy):
    controller.apply_pattern([(10_000, 5, 3)])  # constant, not paused

    await controller.process_communication()
    mock_toy.strict_intensity1.assert_awaited_once_with(5)
    mock_toy.strict_intensity2.assert_awaited_once_with(3)

    mock_toy.reset_mock()
    await controller.process_communication()  # unchanged -> no resend
    mock_toy.strict_intensity1.assert_not_called()
    mock_toy.strict_intensity2.assert_not_called()


def test_clearing_pattern_asks_for_a_stop(controller, mock_toy):
    assert controller.apply_pattern([(1000, 5, 3)]) is False
    assert controller.apply_pattern([]) is True  # the toy has to be stopped
    assert controller.get_state()["pattern"] == []
    assert controller.is_paused is True  # like every stop
    assert not mock_toy.mock_calls


@pytest.mark.asyncio
async def test_resume_after_pause_redrives_pattern(controller, mock_toy):
    # Drive -> pause (latches a stop, forgets last-sent values) -> resume must re-send the values.
    controller.apply_pattern([(10_000, 5, 3)])
    await controller.process_communication()

    controller.apply_paused(True)
    await controller.process_communication()  # playback stop, latch: last_values reset

    mock_toy.reset_mock()
    controller.apply_paused(False)  # resume
    await controller.process_communication()  # re-drive from scratch
    mock_toy.strict_intensity1.assert_awaited_once_with(5)
    mock_toy.strict_intensity2.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_blocked_pattern_latches_single_stop(controller, mock_toy):
    # While blocked, playback sends exactly one stop and then holds (no repeated stops).
    controller.apply_pattern([(10_000, 5, 3)])
    await controller.process_communication()  # drive
    controller.apply_blocked(True)

    mock_toy.reset_mock()
    await controller.process_communication()  # playback stop, latch
    mock_toy.strict_stop.assert_awaited_once()

    mock_toy.reset_mock()
    await controller.process_communication()  # latched -> no further stop
    mock_toy.strict_stop.assert_not_called()


@pytest.mark.asyncio
async def test_held_pattern_latches_single_stop_and_resumes(controller, mock_toy):
    # The hold mutes playback like a block does, but leaves pause and block alone, so releasing re-drives the pattern.
    controller.apply_pattern([(10_000, 5, 3)])
    await controller.process_communication()  # drive
    controller.set_held(True)

    mock_toy.reset_mock()
    await controller.process_communication()  # playback stop, latch
    await controller.process_communication()  # latched -> no further stop
    mock_toy.strict_stop.assert_awaited_once()
    assert await controller.intensity1(7) is False
    assert await controller.intensity2(7) is False
    mock_toy.strict_intensity1.assert_not_called()
    assert controller.is_paused is False and controller.is_blocked is False

    controller.set_held(False)
    mock_toy.reset_mock()
    await controller.process_communication()
    mock_toy.strict_intensity1.assert_awaited_once_with(5)
    mock_toy.strict_intensity2.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_stop_output_leaves_pause_and_block_alone(controller, mock_toy):
    controller.apply_pattern([(10_000, 5, 3)])
    assert await controller.stop_output() is True
    mock_toy.strict_stop.assert_awaited_once()
    assert controller.is_paused is False and controller.is_blocked is False


@pytest.mark.asyncio
async def test_playback_propagates_connection_error(controller, mock_toy):
    # The strict playback path must let ConnectionError escape process_communication;
    # _ToyHub relies on this to trigger its retry / reconnect handling.
    controller.apply_pattern([(10_000, 5, 3)])
    mock_toy.strict_intensity1.side_effect = ConnectionError("boom")
    with pytest.raises(ConnectionError):
        await controller.process_communication()


# ---------------------------------------------------------------------------
# State transitions and the commands that follow them
# ---------------------------------------------------------------------------


def test_apply_stop_pauses_the_pattern_and_leaves_the_block_alone(controller, mock_toy):
    controller.apply_blocked(True)
    controller.apply_pattern([(10_000, 5, 3)])
    assert controller.apply_stop() is True  # a stop has to follow
    assert controller.is_paused is True
    assert controller.is_blocked is True
    assert not mock_toy.mock_calls


def test_manual_intensity_is_refused_while_blocked_or_held(controller):
    controller.apply_pattern([(10_000, 5, 3)])
    controller.apply_blocked(True)
    assert controller.accept_manual_intensity() is False
    controller.apply_blocked(False)
    controller.set_held(True)
    assert controller.accept_manual_intensity() is False
    assert controller.is_paused is False  # a refused command changes nothing

    controller.set_held(False)
    assert controller.accept_manual_intensity() is True
    assert controller.is_paused is True  # so the pattern does not override the command


@pytest.mark.asyncio
async def test_send_intensity_checks_block_and_limit_again(controller, mock_toy):
    # Between accepting a manual intensity and sending it, the toy can get blocked or a limit lowered.
    assert controller.accept_manual_intensity() is True
    controller.apply_intensity1_limit(6)
    assert await controller.send_intensity1(50) is True
    mock_toy.strict_intensity1.assert_awaited_once_with(6)

    controller.apply_blocked(True)
    assert await controller.send_intensity1(50) is False
    assert await controller.send_intensity2(50) is False
    mock_toy.strict_intensity1.assert_awaited_once()  # nothing more was sent
    mock_toy.strict_intensity2.assert_not_called()


def test_apply_limit_reports_whether_the_toy_runs_above_it(controller, mock_toy):
    mock_toy.current_intensities = (40, 10)
    assert controller.apply_intensity1_limit(30) is True
    assert controller.apply_intensity1_limit(40) is False  # at the limit is fine
    assert controller.apply_intensity2_limit(5) is True
    assert controller.apply_intensity2_limit(None) is False
    assert not mock_toy.strict_intensity1.called  # recording a limit sends nothing


@pytest.mark.asyncio
async def test_refresh_battery_remembers_the_level(controller, mock_toy):
    mock_toy.strict_get_battery_level.return_value = 42
    assert await controller.refresh_battery() == 42
    assert controller.battery == 42

    mock_toy.strict_get_battery_level.side_effect = ConnectionError("gone")
    with pytest.raises(ConnectionError):
        await controller.refresh_battery()
    assert controller.battery == 42  # the last known level is kept
