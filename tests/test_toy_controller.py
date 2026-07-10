"""
Tests for the High-Level :class:`ToyController` state machine: block/pause mutual exclusion,
the command queue, and pattern playback in ``process_communication``.
"""

from unittest.mock import AsyncMock

import pytest

from tikal.high_level.toy_controller import LovenseController, MockEstimController
from tikal.low_level import LovenseToy, Toy


@pytest.fixture
def mock_toy():
    toy = AsyncMock(spec=Toy)
    toy.toy_id = "toy-1"
    toy.intensity1.return_value = True
    toy.intensity2.return_value = True
    toy.stop.return_value = True
    return toy


@pytest.fixture
def controller(mock_toy):
    c = MockEstimController(mock_toy, "test")
    c.is_connected = True
    return c


# ---------------------------------------------------------------------------
# Blocking / pausing state machine (synchronous)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_rejects_intensity(controller, mock_toy):
    controller.toggle_block()  # block on (queues a stop)
    await controller.process_communication()  # drain that stop
    mock_toy.reset_mock()

    results = []
    controller.intensity1(15, results.append)
    assert results == [False]  # rejected synchronously, via callback

    await controller.process_communication()
    mock_toy.intensity1.assert_not_called()  # never reached the toy


def test_pause_and_block_are_mutually_exclusive(controller):
    assert controller.toggle_pause() is True
    assert controller.is_paused is True
    assert controller.is_blocked is False

    assert controller.toggle_block() is True  # blocking clears pause
    assert controller.is_blocked is True
    assert controller.is_paused is False

    assert controller.toggle_pause() is True  # pausing clears block
    assert controller.is_paused is True
    assert controller.is_blocked is False


def test_set_paused_clears_block(controller):
    controller.set_blocked(True)
    assert controller.is_blocked is True

    controller.set_paused(True)
    assert controller.is_paused is True
    assert controller.is_blocked is False


def test_set_paused_is_idempotent_on_version(controller):
    controller.set_paused(True)
    version = controller.pattern_version
    controller.set_paused(True)  # no-op
    assert controller.pattern_version == version


# ---------------------------------------------------------------------------
# Command queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_queue_runs_fifo_with_results(controller, mock_toy):
    mock_toy.intensity1.return_value = True
    mock_toy.intensity2.return_value = False

    order = []
    controller.intensity1(5, lambda r: order.append(("i1", r)))
    controller.intensity2(3, lambda r: order.append(("i2", r)))
    await controller.process_communication()

    assert order == [("i1", True), ("i2", False)]
    mock_toy.intensity1.assert_awaited_once_with(5)
    mock_toy.intensity2.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_command_error_delivers_none_to_callback(controller, mock_toy):
    mock_toy.intensity1.side_effect = ConnectionError("boom")

    results = []
    controller.intensity1(5, results.append)
    await controller.process_communication()  # must not raise

    assert results == [None]


# ---------------------------------------------------------------------------
# Pattern playback
# ---------------------------------------------------------------------------


def test_manual_intensity_pauses_pattern(controller):
    controller.set_pattern([(1000, 5, 5)])
    assert controller.is_paused is False
    controller.intensity1(7)
    assert controller.is_paused is True


@pytest.mark.asyncio
async def test_pattern_playback_sends_changed_values_once(controller, mock_toy):
    # Single long segment -> constant (5, 3) for the whole test window, never paused.
    controller.set_pattern([(10_000, 5, 3)])

    await controller.process_communication()
    mock_toy.intensity1.assert_awaited_once_with(5)
    mock_toy.intensity2.assert_awaited_once_with(3)

    mock_toy.reset_mock()
    await controller.process_communication()  # unchanged values -> no resend
    mock_toy.intensity1.assert_not_called()
    mock_toy.intensity2.assert_not_called()


@pytest.mark.asyncio
async def test_paused_pattern_does_not_resend_stop(controller, mock_toy):
    controller.set_pattern([(10_000, 5, 3)])
    controller.set_paused(True)  # queues a stop and will stop on pause
    await controller.process_communication()  # drains queued + pause stop

    mock_toy.reset_mock()
    await controller.process_communication()  # latched -> no further stop
    mock_toy.stop.assert_not_called()


@pytest.mark.asyncio
async def test_clearing_pattern_stops_toy(controller, mock_toy):
    controller.set_pattern([(1000, 5, 3)])
    mock_toy.reset_mock()

    controller.set_pattern([])  # clearing schedules a stop
    await controller.process_communication()

    mock_toy.stop.assert_awaited()
    assert controller.get_pattern_data()[0] == []  # pattern cleared


@pytest.mark.asyncio
async def test_resume_after_pause_redrives_pattern(controller, mock_toy):
    # Drive -> pause (latches a stop, forgets last-sent values) -> resume must re-send the values.
    controller.set_pattern([(10_000, 5, 3)])
    await controller.process_communication()

    controller.set_paused(True)
    await controller.process_communication()  # pause latch: stop + last_values reset

    mock_toy.reset_mock()
    controller.set_paused(False)
    await controller.process_communication()  # resume: re-drive from scratch
    mock_toy.intensity1.assert_awaited_once_with(5)
    mock_toy.intensity2.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_blocked_pattern_latches_single_stop(controller, mock_toy):
    # While blocked, playback sends exactly one stop and then holds (no repeated stops).
    controller.set_pattern([(10_000, 5, 3)])
    await controller.process_communication()  # drive
    controller.toggle_block()  # queues a stop + blocks
    await controller.process_communication()  # drain queued + playback stop, latch

    mock_toy.reset_mock()
    await controller.process_communication()  # latched -> no further stop
    mock_toy.stop.assert_not_called()
    mock_toy.intensity1.assert_not_called()


# ---------------------------------------------------------------------------
# Command surface (queued -> executed in process_communication)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_rotation_direction_queues_and_reports(controller, mock_toy):
    mock_toy.change_rotation_direction.return_value = True
    results = []
    controller.change_rotation_direction(results.append)
    await controller.process_communication()
    assert results == [True]
    mock_toy.change_rotation_direction.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_battery_level_queues_and_reports(controller, mock_toy):
    mock_toy.get_battery_level.return_value = 77
    results = []
    controller.get_battery_level(results.append)
    await controller.process_communication()
    assert results == [77]


@pytest.mark.asyncio
async def test_direct_command_queues_and_reports(controller, mock_toy):
    mock_toy.direct_command.return_value = "PONG"
    results = []
    controller.direct_command("DeviceType", results.append)
    await controller.process_communication()
    assert results == ["PONG"]
    mock_toy.direct_command.assert_awaited_once_with("DeviceType")


@pytest.mark.asyncio
async def test_intensity2_rejected_when_blocked(controller, mock_toy):
    controller.toggle_block()  # block on (queues a stop)
    await controller.process_communication()  # drain that stop
    mock_toy.reset_mock()

    results = []
    controller.intensity2(5, results.append)
    assert results == [False]  # rejected synchronously, via callback

    await controller.process_communication()
    mock_toy.intensity2.assert_not_called()  # never reached the toy


@pytest.mark.asyncio
async def test_process_communication_noop_when_disconnected(controller, mock_toy):
    controller.set_pattern([(10_000, 5, 3)])
    controller.is_connected = False
    await controller.process_communication()  # early return: nothing sent
    mock_toy.intensity1.assert_not_called()
    mock_toy.stop.assert_not_called()


def test_toggle_pause_second_toggle_unpauses(controller):
    assert controller.toggle_pause() is True
    assert controller.toggle_pause() is False  # second toggle unpauses
    assert controller.is_paused is False


def test_toggle_block_second_toggle_unblocks(controller):
    assert controller.toggle_block() is True
    assert controller.toggle_block() is False  # second toggle unblocks
    assert controller.is_blocked is False


def test_set_blocked_noop_when_unchanged(controller):
    # Already unblocked -> setting False again hits the early-return guard.
    controller.set_blocked(False)
    assert controller.is_blocked is False


def test_is_connected_and_rotation_available_passthrough(controller, mock_toy):
    assert controller.is_connected is True  # reads the getter
    mock_toy.change_rotation_direction_available = True
    assert controller.change_rotation_direction_available is True


# ---------------------------------------------------------------------------
# LovenseController.get_information
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lovense_controller_get_information():
    toy = AsyncMock(spec=LovenseToy)
    toy.toy_id = "toy-1"
    toy.name = "LVS-Z36D"
    toy.get_battery_level.return_value = 88
    toy.get_status.return_value = 2
    toy.get_batch_number.return_value = "240815"
    toy.get_device_type.return_value = "C:11:AAAA"

    controller = LovenseController(toy, "test")
    controller.is_connected = True

    results = []
    controller.get_information(results.append)
    await controller.process_communication()

    assert len(results) == 1
    info = results[0]
    assert info["Battery level"] == "88%"
    assert info["Status"] == "2"
    assert info["Batch number"] == "240815"
    assert info["Bluetooth Name"] == "LVS-Z36D"
    assert info["Device type"] == "C:11:AAAA"


@pytest.mark.asyncio
async def test_lovense_controller_get_information_handles_missing_battery():
    toy = AsyncMock(spec=LovenseToy)
    toy.toy_id = "toy-1"
    toy.name = "LVS-Z36D"
    toy.get_battery_level.return_value = None
    toy.get_status.return_value = None
    toy.get_batch_number.return_value = None
    toy.get_device_type.return_value = None

    controller = LovenseController(toy, "test")
    controller.is_connected = True

    results = []
    controller.get_information(results.append)
    await controller.process_communication()

    info = results[0]
    assert info["Battery level"] == "Unknown"
    assert info["Status"] == "Unknown"
