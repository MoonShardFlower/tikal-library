"""
Tests for the High-Level :class:`ToyHub`: the synchronous wrapper over the async core.

Driven end to end with MockEstimToys. Scans, reconnects and power-offs are started on the hub's core the way the
Bluetooth layer would start them.
"""

import gc
import json
import threading
import weakref
from unittest.mock import AsyncMock, patch

import pytest

from tikal._core import ToyStatus
from tikal.high_level import (
    AddConnectionError,
    DiscoveryStartError,
    InvalidModelError,
    ToyAlreadyAddedError,
    ToyController,
    ToyHub,
    UnknownToyError,
)
from tikal.low_level import ToyData

from .conftest import connect_mock_toy, wait_until


def _core_toy(hub, toy_id="Thunder_ID"):
    return hub._core.get_controller(toy_id)


def _on_loop(hub, coro):
    """Run a coroutine of the hub's core on its event loop (as the Bluetooth layer's callbacks do) and wait for it."""
    return hub._runner.run_async(coro)


def _thunder_data(model_name="Thunder") -> ToyData:
    return ToyData("Thunder1", "Thunder_ID", model_name, "MockEstimToys")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_fills_in_the_default_and_the_cached_model(mock_hub, tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text('{"Thunder1": "Lightning"}', encoding="utf-8")
    hub = mock_hub(toy_cache_path=cache_file, default_model="PICK_ME")

    found = {t.toy_id: t.model_name for t in hub.discover_toys_blocking(0.1)}

    assert found == {"Thunder_ID": "Lightning", "Lightning_ID": "PICK_ME"}


def test_corrupted_cache_does_not_break_discovery(mock_hub, tmp_path):
    """A cache entry that is not a model-name string must not break discovery for the other toys."""
    cache_path = tmp_path / "toys.json"
    cache_path.write_text(
        json.dumps({"Thunder1": 123, "Lightning1": "Thunder"}), encoding="utf-8"
    )
    hub = mock_hub(toy_cache_path=cache_path, default_model="unknown")

    found = {t.toy_id: t.model_name for t in hub.discover_toys_blocking(0.1)}

    assert found == {"Thunder_ID": "unknown", "Lightning_ID": "Thunder"}


def test_start_discovery_reports_toys_with_their_models(mock_hub, tmp_path):
    cache_path = tmp_path / "toys.json"
    cache_path.write_text(json.dumps({"Thunder1": ["Gush"]}), encoding="utf-8")
    hub = mock_hub(toy_cache_path=cache_path, default_model="DEF")
    builder = hub._core._connection_builder
    builder.start_continuous = AsyncMock()
    updates: list = []

    hub.start_discovery(updates.append)
    scanner_callback = builder.start_continuous.call_args.args[0]
    scanner_callback([_thunder_data(model_name="")])  # what the scanner does

    assert wait_until(lambda: updates)
    assert [(t.toy_id, t.model_name) for t in updates[0]] == [("Thunder_ID", "DEF")]


def test_a_failing_scan_goes_to_on_error_and_clears_the_toys(mock_hub):
    errors: list = []
    hub = mock_hub(on_error=lambda error, context, tb: errors.append((error, tb)))
    builder = hub._core._connection_builder
    builder.start_continuous = AsyncMock()
    updates: list = []

    hub.start_discovery(updates.append)
    builder.start_continuous.call_args.args[0](RuntimeError("scan died"))

    assert wait_until(lambda: updates and errors)
    assert updates == [[]]
    assert "scan died" in errors[0][1]  # the traceback of the scanner's error


def test_start_discovery_raises_when_the_scan_cannot_start(mock_hub):
    hub = mock_hub()
    hub._core._connection_builder.start_continuous = AsyncMock(
        side_effect=RuntimeError("bt off")
    )
    with pytest.raises(DiscoveryStartError):
        hub.start_discovery(lambda toys: None)


def test_stop_discovery_stops_the_scan(mock_hub):
    hub = mock_hub()
    hub._core._connection_builder.stop_continuous = AsyncMock()
    hub.stop_discovery()
    hub._core._connection_builder.stop_continuous.assert_awaited()


def test_discover_toys_callback_delivers_results_and_errors(mock_hub):
    hub = mock_hub(default_model="DEF")
    results: list = []

    hub.discover_toys_callback(results.append, timeout=0.1)
    assert wait_until(lambda: results)
    assert {t.toy_id for t in results[0]} == {"Thunder_ID", "Lightning_ID"}

    hub._core._connection_builder.discover_toys = AsyncMock(
        side_effect=RuntimeError("bt off")
    )
    hub.discover_toys_callback(results.append, timeout=0.1)
    assert wait_until(lambda: len(results) == 2)
    assert isinstance(results[1], RuntimeError)


# ---------------------------------------------------------------------------
# Connecting and disconnecting
# ---------------------------------------------------------------------------


def test_connect_hands_out_controllers_and_remembers_the_models(mock_hub, tmp_path):
    cache_file = tmp_path / "cache.json"
    hub = mock_hub(toy_cache_path=cache_file)

    toy = connect_mock_toy(hub, "Lightning_ID", "Thunder")

    assert isinstance(toy, ToyController) and toy.is_connected
    assert toy.model_name == "Thunder"
    assert json.loads(cache_file.read_text(encoding="utf-8"))["Lightning1"] == "Thunder"


def test_connect_reports_each_failure_in_its_place(mock_hub):
    hub = mock_hub()
    found = {t.toy_id: t for t in hub.discover_toys_blocking(0.1)}
    found["Thunder_ID"].model_name = "Thunder"
    found["Lightning_ID"].model_name = "Bogus"
    hub._core._connection_builder.create_toy = AsyncMock(
        wraps=hub._core._connection_builder.create_toy
    )

    thunder, lightning, again = hub.connect_toys_blocking(
        [found["Thunder_ID"], found["Lightning_ID"], found["Thunder_ID"]]
    )

    assert isinstance(thunder, ToyController)
    assert isinstance(lightning, InvalidModelError)
    assert isinstance(again, ToyAlreadyAddedError)


def test_connect_reports_a_toy_that_cannot_be_reached(mock_hub):
    hub = mock_hub()
    hub._core._connection_builder.create_toy = AsyncMock(
        return_value=ConnectionError("out of range")
    )
    (result,) = hub.connect_toys_blocking([_thunder_data()])
    assert isinstance(result, AddConnectionError)


def test_connect_toys_callback_delivers_the_controllers(mock_hub):
    hub = mock_hub()
    results: list = []
    hub.connect_toys_callback([_thunder_data()], results.append)
    assert wait_until(lambda: results)
    assert isinstance(results[0][0], ToyController)


def test_disconnect_reports_each_toy_in_its_place(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)

    assert hub.disconnect_toys_blocking([]) == []
    results = hub.disconnect_toys_blocking([toy.toy_id, "ghost"])

    assert results[0] is None
    assert isinstance(results[1], UnknownToyError)
    assert toy.is_connected is False


def test_disconnect_toys_callback_disconnects(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)
    results: list = []
    hub.disconnect_toys_callback([toy.toy_id], results.append)
    assert wait_until(lambda: results)
    assert results == [[None]] and toy.is_connected is False


def test_update_model_name(mock_hub, tmp_path):
    cache_file = tmp_path / "cache.json"
    hub = mock_hub(toy_cache_path=cache_file)
    toy = connect_mock_toy(hub, "Lightning_ID", "Lightning")

    assert hub.update_model_name(toy.toy_id, "Thunder") is toy
    assert toy.model_name == "Thunder"
    assert json.loads(cache_file.read_text(encoding="utf-8"))["Lightning1"] == "Thunder"

    assert isinstance(hub.update_model_name(toy.toy_id, "Bogus"), InvalidModelError)
    assert json.loads(cache_file.read_text(encoding="utf-8"))["Lightning1"] == "Thunder"
    assert isinstance(hub.update_model_name("ghost", "Thunder"), UnknownToyError)


# ---------------------------------------------------------------------------
# Connection loss, reconnection and power-off
# ---------------------------------------------------------------------------


def test_reconnect_success(mock_hub, short_reconnect_pause):
    """The toy comes back stopped with its pattern paused (a block stays), and the callbacks tell the story."""
    events: list = []
    hub = mock_hub(
        on_disconnect=lambda toy_id: events.append(("disconnect", toy_id)),
        on_reconnection_success=lambda toy_id: events.append(("back", toy_id)),
    )
    toy = connect_mock_toy(hub)
    toy.set_pattern([(60_000, 40, 0)])
    assert wait_until(lambda: toy.current_intensities == (40, 0))
    core_toy = _core_toy(hub)
    core_toy.reconnect = AsyncMock(side_effect=[ConnectionError("gone"), None])

    _on_loop(hub, hub._core._on_disconnect(toy.toy_id))
    results: list = []
    toy.intensity1(5, results.append)  # during the outage
    assert results == [None]

    assert wait_until(lambda: ("back", "Thunder_ID") in events)
    assert events == [("disconnect", "Thunder_ID"), ("back", "Thunder_ID")]
    assert toy.is_connected is True
    assert toy.is_paused is True and toy.current_intensities == (0, 0)
    assert core_toy.reconnect.await_count == 2

    toy.set_blocked(True)
    core_toy.reconnect = AsyncMock()  # back on the first attempt this time
    _on_loop(hub, hub._core._on_disconnect(toy.toy_id))
    assert wait_until(lambda: events.count(("back", "Thunder_ID")) == 2)
    assert toy.is_blocked is True  # a reconnect leaves the block alone


def test_reconnect_failure(mock_hub, short_reconnect_window):
    lost = threading.Event()
    hub = mock_hub(on_reconnection_failure=lambda toy_id: lost.set())
    toy = connect_mock_toy(hub)
    _core_toy(hub).reconnect = AsyncMock(side_effect=ConnectionError("gone"))

    _on_loop(hub, hub._core._on_disconnect(toy.toy_id))

    assert lost.wait(3.0), "reconnection-failure callback never fired"
    assert wait_until(lambda: hub._core.get_controller(toy.toy_id) is None)
    results: list = []
    toy.intensity1(5, results.append)
    assert results == [None]  # rejected right away


def test_a_command_that_fails_twice_starts_a_reconnect(mock_hub, short_reconnect_pause):
    events: list = []
    hub = mock_hub(
        on_disconnect=lambda toy_id: events.append("disconnect"),
        on_reconnection_success=lambda toy_id: events.append("back"),
    )
    toy = connect_mock_toy(hub)
    _core_toy(hub)._toy.strict_intensity1 = AsyncMock(
        side_effect=ConnectionError("radio gone")
    )
    results: list = []

    toy.intensity1(5, results.append)

    assert wait_until(lambda: results)
    assert results == [None]
    assert wait_until(lambda: events[:1] == ["disconnect"])


def test_power_off(mock_hub):
    fired: list = []
    hub = mock_hub(on_power_off=fired.append)
    toy = connect_mock_toy(hub)

    _on_loop(hub, hub._core._on_power_off(toy.toy_id))

    assert fired == ["Thunder_ID"]
    assert wait_until(lambda: not toy.is_connected)


# ---------------------------------------------------------------------------
# Battery, errors and callbacks
# ---------------------------------------------------------------------------


def test_battery_levels_are_reported_after_connecting_and_on_a_change(mock_hub):
    reports: list = []
    hub = mock_hub(on_battery_update=reports.append)
    toy = connect_mock_toy(hub)
    assert wait_until(lambda: reports)
    assert reports[-1] == {"Thunder_ID": 77}

    _core_toy(hub)._toy.strict_get_battery_level = AsyncMock(return_value=55)
    _on_loop(hub, hub._core._poll_all_batteries())

    assert wait_until(lambda: reports[-1] == {"Thunder_ID": 55})
    assert toy.is_connected


def test_a_battery_callback_that_raises_goes_to_on_error(mock_hub):
    errors: list = []

    def broken(levels):
        raise RuntimeError("callback boom")

    hub = mock_hub(
        on_battery_update=broken, on_error=lambda e, context, tb: errors.append(e)
    )
    connect_mock_toy(hub)
    assert wait_until(lambda: errors)
    assert isinstance(errors[0], RuntimeError)


def test_callback_setters_replace_callbacks(mock_hub):
    hub = mock_hub()
    fired: list = []
    hub.battery_update_callback(fired.append)
    hub.error_callback(lambda *args: None)
    hub.disconnect_callback(lambda toy_id: None)
    hub.reconnection_failure_callback(lambda toy_id: None)
    hub.reconnection_success_callback(lambda toy_id: None)
    hub.power_off_callback(fired.append)
    toy = connect_mock_toy(hub)

    _on_loop(hub, hub._core._on_power_off(toy.toy_id))
    assert wait_until(lambda: "Thunder_ID" in fired)
    assert {"Thunder_ID": 77} in fired


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_disconnects_and_is_idempotent(mock_hub):
    hub = mock_hub()
    toy = connect_mock_toy(hub)
    _core_toy(hub)._toy.strict_disconnect = AsyncMock(
        side_effect=ConnectionError("cannot close")
    )

    hub.shutdown()  # swallows the disconnect error
    hub.shutdown()  # no-op

    assert toy.is_connected is False
    assert toy.model_name == "Thunder"  # still readable
    results: list = []
    toy.intensity1(5, results.append)
    assert results == [None]
    assert toy.toggle_pause() is True  # and still usable without a hub behind it


# ---------------------------------------------------------------------------
# Exit safety net
# ---------------------------------------------------------------------------


def test_shutdown_is_registered_at_exit():
    """
    shutdown() is documented as mandatory, but a forgotten (or skipped) call must not leave toys running.

    The interpreter still runs atexit hooks after an uncaught exception or a KeyboardInterrupt, which is the last
    chance to stop and disconnect everything.
    """
    with patch("tikal.high_level.toy_hub.atexit") as fake_atexit:
        hub = ToyHub(logger_name="test")
    try:
        fake_atexit.register.assert_called_once()
        hook = fake_atexit.register.call_args.args[0]

        with patch.object(hub, "shutdown") as shutdown:
            hook()
        shutdown.assert_called_once()
    finally:
        hub.shutdown()


def test_explicit_shutdown_unregisters_the_atexit_hook():
    """A hub shut down properly must not run its safety net again at exit."""
    with patch("tikal.high_level.toy_hub.atexit") as fake_atexit:
        hub = ToyHub(logger_name="test")
        hub.shutdown()

    hook = fake_atexit.register.call_args.args[0]
    fake_atexit.unregister.assert_called_once_with(hook)


def test_atexit_hook_does_not_keep_the_hub_alive():
    """The hook holds only a weak reference, so it neither leaks the hub nor fires for a collected one."""
    with patch("tikal.high_level.toy_hub.atexit") as fake_atexit:
        hub = ToyHub(logger_name="test")

    hook = fake_atexit.register.call_args.args[0]
    hub.shutdown()
    hub_ref = weakref.ref(hub)
    del hub
    gc.collect()

    assert hub_ref() is None, "the atexit hook kept the ToyHub alive"
    hook()  # firing it after collection must be a harmless no-op


def test_status_names_match_the_callbacks():
    # _on_status_change maps every core status to a callback; a new status must not silently fall through.
    assert {s.value for s in ToyStatus} == {
        "connected",
        "reconnecting",
        "lost",
        "powered_off",
    }
