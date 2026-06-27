"""Tests for the WebSocket :class:`_ToyController` (async toy control with intensity limits)."""

from unittest.mock import AsyncMock

import pytest

from tikal.low_level import Toy
from tikal.websocket._toy_controller import _ToyController


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


# ---------------------------------------------------------------------------
# Intensity limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_pattern_applies_intensity_limits(controller, mock_toy):
    await controller.set_intensity1_limit(10)
    await controller.set_intensity2_limit(5)

    await controller.set_pattern([(100, 20, 20), (200, 3, 99)])

    stored = controller.get_state()["pattern"]
    assert stored == [(100, 10, 5), (200, 3, 5)]


@pytest.mark.asyncio
async def test_manual_intensity_clamped_to_limit(controller, mock_toy):
    await controller.set_intensity1_limit(8)
    await controller.intensity1(20)
    mock_toy.strict_intensity1.assert_awaited_once_with(8)


@pytest.mark.asyncio
async def test_intensity_limit_none_resets_to_max(controller, mock_toy):
    await controller.set_intensity1_limit(5)
    await controller.set_intensity1_limit(None)  # reset to max_intensity (100)
    await controller.intensity1(20)
    mock_toy.strict_intensity1.assert_awaited_once_with(20)


# ---------------------------------------------------------------------------
# Block / pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_intensity_returns_false_without_command(controller, mock_toy):
    await controller.toggle_block()  # block on (calls stop)
    mock_toy.reset_mock()

    assert await controller.intensity1(10) is False
    assert await controller.intensity2(10) is False
    mock_toy.strict_intensity1.assert_not_called()
    mock_toy.strict_intensity2.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_pause_and_block_mutual_exclusion(controller):
    assert await controller.toggle_pause() is True
    assert controller.is_paused is True
    assert controller.is_blocked is False

    assert await controller.toggle_block() is True
    assert controller.is_blocked is True
    assert controller.is_paused is False

    assert await controller.toggle_pause() is True
    assert controller.is_paused is True
    assert controller.is_blocked is False


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
    await controller.set_pattern([(10_000, 5, 3)])  # constant, not paused

    await controller.process_communication()
    mock_toy.strict_intensity1.assert_awaited_once_with(5)
    mock_toy.strict_intensity2.assert_awaited_once_with(3)

    mock_toy.reset_mock()
    await controller.process_communication()  # unchanged -> no resend
    mock_toy.strict_intensity1.assert_not_called()
    mock_toy.strict_intensity2.assert_not_called()


@pytest.mark.asyncio
async def test_clearing_pattern_stops_toy(controller, mock_toy):
    await controller.set_pattern([(1000, 5, 3)])
    mock_toy.reset_mock()

    await controller.set_pattern([])
    mock_toy.strict_stop.assert_awaited()
    assert controller.get_state()["pattern"] == []
