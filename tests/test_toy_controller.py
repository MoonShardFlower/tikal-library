"""
Tests for the High-Level :class:`ToyController`: a synchronous handle over the async core.

State changes must be visible as soon as a method returns, commands must reach the toy in call order, and callbacks
must report None whenever a command could not be delivered. Driven end to end through a ToyHub with MockEstimToys.
"""

import asyncio
import threading
from unittest.mock import AsyncMock

from tikal._core import ToyStatus

from .conftest import connect_mock_toy, wait_until


def _core_toy(hub, toy_id="Thunder_ID"):
    return hub._core.get_controller(toy_id)


def _record_sends(hub, sent: list, toy_id="Thunder_ID"):
    """
    Record every intensity1 (once the toy took it) and stop (when it starts) that reaches the toy, in order.

    A higher level takes less time to send, so if commands could run side by side, later ones would overtake earlier
    ones and show up out of order.
    """
    toy = _core_toy(hub, toy_id)._toy
    original_intensity1, original_stop = toy.strict_intensity1, toy.strict_stop
    in_stop = False  # the mock toy stops by sending intensity 0 on each channel

    async def intensity1(level):
        if in_stop:
            return await original_intensity1(level)
        await asyncio.sleep(max(0.0, 0.03 - 0.001 * level))
        result = await original_intensity1(level)
        sent.append(level)
        return result

    async def stop():
        nonlocal in_stop
        sent.append("stop")
        in_stop = True
        try:
            return await original_stop()
        finally:
            in_stop = False

    toy.strict_intensity1 = intensity1
    toy.strict_stop = stop


# ---------------------------------------------------------------------------
# State changes
# ---------------------------------------------------------------------------


def test_state_changes_show_at_once_and_pause_and_block_exclude_each_other(mock_hub):
    toy = connect_mock_toy(mock_hub())

    assert toy.toggle_pause() is True
    assert toy.is_paused is True and toy.is_blocked is False

    assert toy.toggle_block() is True
    assert toy.is_blocked is True and toy.is_paused is False

    toy.set_paused(True)
    assert toy.is_paused is True and toy.is_blocked is False

    assert toy.toggle_pause() is False
    toy.set_blocked(True)
    toy.set_blocked(False)
    assert toy.is_blocked is False and toy.is_paused is False


def test_quick_toggles_do_not_undo_each_other(mock_hub):
    # Each toggle reads the state the previous one left, so an odd number of toggles ends paused.
    toy = connect_mock_toy(mock_hub())
    for _ in range(9):
        toy.toggle_pause()
    assert toy.is_paused is True


def test_set_pattern_is_visible_at_once_and_clearing_stops_the_toy(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)

    toy.set_pattern([(60_000, 30, 0)])
    assert toy.get_pattern_data()[0] == [(60_000, 30, 0)]
    assert wait_until(lambda: toy.current_intensities == (30, 0))

    toy.set_pattern([])  # leaves the pause alone
    assert toy.get_pattern_data()[0] == [] and toy.is_paused is False
    assert wait_until(lambda: toy.current_intensities == (0, 0))

    toy.set_pattern([(60_000, 30, 0)])  # plays right away, as no pause was left behind
    assert wait_until(lambda: toy.current_intensities == (30, 0))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_commands_reach_the_toy_in_call_order(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)
    sent: list = []
    _record_sends(hub, sent)

    for level in range(1, 21):
        toy.intensity1(level)
    toy.stop()
    toy.intensity1(42)

    assert wait_until(lambda: len(sent) == 22)
    assert sent == list(range(1, 21)) + ["stop", 42]
    assert wait_until(lambda: toy.current_intensities == (42, 0))


def test_intensity_reports_the_result(mock_hub):
    toy = connect_mock_toy(mock_hub(), "Lightning_ID", "Lightning")
    results: list = []

    toy.intensity1(150, results.append)  # clamped to max_intensity
    toy.intensity2(20, results.append)
    assert wait_until(lambda: len(results) == 2)
    assert toy.current_intensities == (
        100,
        20,
    )  # each command reached its own capability

    toy.stop(results.append)
    assert wait_until(lambda: len(results) == 3)
    assert results == [True, True, True]
    assert toy.current_intensities == (0, 0)
    assert toy.is_paused is True  # a stop pauses the pattern


def test_blocked_intensity_reports_false_right_away_and_sends_nothing(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)
    sent: list = []
    _record_sends(hub, sent)
    toy.set_blocked(True)

    results: list = []
    toy.intensity1(5, results.append)

    assert results == [False]  # in the caller's thread, before the method returned
    assert toy.is_paused is False  # a refused command changes nothing
    assert wait_until(lambda: sent == ["stop"])  # only the block's stop reached the toy
    assert not wait_until(lambda: len(sent) > 1, timeout=0.2)


def test_manual_intensity_is_not_undone_by_the_pattern_it_pauses(mock_hub):
    """Regression: the next playback tick used to send the stop for the paused pattern, dropping the manual level."""
    toy = connect_mock_toy(mock_hub())
    toy.set_pattern([(60_000, 30, 0)])
    assert wait_until(lambda: toy.current_intensities == (30, 0))

    toy.intensity1(70)
    assert toy.is_paused is True
    assert wait_until(lambda: toy.current_intensities == (70, 0))
    assert not wait_until(lambda: toy.current_intensities != (70, 0), timeout=0.5)

    toy.set_paused(False)  # the pattern takes over again
    assert wait_until(lambda: toy.current_intensities == (30, 0))


def test_intensity_limits_cap_commands_and_the_pattern(mock_hub):
    toy = connect_mock_toy(mock_hub())
    toy.intensity1(80)
    assert wait_until(lambda: toy.current_intensities == (80, 0))

    toy.set_intensity1_limit(25)
    assert toy.intensity_limits == (25, 100)
    assert wait_until(
        lambda: toy.current_intensities == (25, 0)
    )  # brought down right away

    toy.set_pattern([(60_000, 90, 0)])
    toy.set_paused(False)
    assert not wait_until(lambda: toy.current_intensities[0] > 25, timeout=0.3)

    toy.set_intensity1_limit(None)
    assert wait_until(lambda: toy.current_intensities == (90, 0))


def test_queries_report_through_their_callbacks(mock_hub):
    toy = connect_mock_toy(mock_hub())
    battery, info, response, rotation = [], [], [], []

    toy.get_battery_level(battery.append)
    toy.get_information(info.append)
    toy.direct_command("DeviceType", response.append)
    toy.change_rotation_direction(rotation.append)

    assert wait_until(lambda: battery and info and response and rotation)
    assert battery == [77]
    assert info[0]["model_name"] == "Thunder" and info[0]["battery"] == 77
    assert response == ["MockEstim"]
    assert rotation == [False]  # Thunder cannot rotate


# ---------------------------------------------------------------------------
# Toys that are not connected
# ---------------------------------------------------------------------------


def test_commands_for_a_reconnecting_toy_report_none_right_away(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)
    send = _core_toy(hub)._toy.strict_intensity1 = AsyncMock(return_value=True)
    hub._core._toy_status["Thunder_ID"] = ToyStatus.RECONNECTING
    assert toy.is_connected is False

    results: list = []
    toy.intensity1(5, results.append)
    toy.get_battery_level(results.append)
    toy.stop(results.append)
    assert results == [None, None, None]  # right away, not once the reconnect ends
    send.assert_not_called()

    # State changes still take effect: the reconnect stops the toy, and from then on it follows them.
    toy.set_blocked(True)
    assert toy.is_blocked is True


def test_a_disconnected_toy_keeps_its_state_readable(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)
    toy.set_pattern([(1000, 5, 0)])
    hub.disconnect_toys_blocking([toy.toy_id])

    assert toy.is_connected is False
    assert toy.model_name == "Thunder"
    assert toy.get_pattern_data()[0] == [(1000, 5, 0)]
    assert toy.is_blocked is False  # disconnecting does not change the state it reports
    results: list = []
    toy.intensity1(5, results.append)
    assert results == [None]
    assert toy.toggle_block() is True  # only changes what this controller reports


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def test_a_callback_can_use_the_controller(mock_hub):
    # Callbacks run on the hub's event loop: state changes and commands from there must neither deadlock nor wait.
    toy = connect_mock_toy(mock_hub())
    toy.set_pattern([(60_000, 20, 0)])
    results: list = []
    done = threading.Event()

    def after_first(ok):
        results.append(ok)
        toy.set_paused(False)
        results.append(toy.is_paused)
        toy.intensity1(9, lambda second: (results.append(second), done.set()))

    toy.intensity1(3, after_first)

    assert done.wait(3.0)
    assert results == [True, False, True]
    assert toy.is_paused is True  # the second manual command paused the pattern again


def test_a_blocking_hub_call_from_a_callback_is_refused(mock_hub):
    errors: list = []
    hub = mock_hub(on_error=lambda error, context, tb: errors.append(error))
    toy = connect_mock_toy(hub)

    toy.get_battery_level(lambda level: hub.discover_toys_blocking(0.1))

    assert wait_until(lambda: errors)
    assert isinstance(errors[0], RuntimeError)  # instead of deadlocking the hub


def test_a_callback_that_raises_goes_to_on_error(mock_hub):
    errors: list = []
    hub = mock_hub(on_error=lambda error, context, tb: errors.append((error, context)))
    toy = connect_mock_toy(hub)

    def broken(_):
        raise ValueError("bug in the app")

    toy.intensity1(5, broken)
    toy.intensity1(6)  # the hub keeps working

    assert wait_until(lambda: errors)
    assert isinstance(errors[0][0], ValueError) and "intensity1" in errors[0][1]
    assert wait_until(lambda: toy.current_intensities == (6, 0))
