"""Tests for the High-Level :class:`ToyHub` orchestration."""

import asyncio
import gc
import json
import threading
import time
import weakref
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tikal.high_level import ToyHub
from tikal.high_level.toy_controller import LovenseController
from tikal.low_level import Toy, ToyData


def make_toy(toy_id="a1", model="Nora", name="LVS-A1", brand="Lovense"):
    toy = AsyncMock(spec=Toy)
    toy.toy_id = toy_id
    toy.model_name = model
    toy.name = name
    toy.brand = brand
    toy.max_intensity = 20
    return toy


def lovense_data(toy_id="a1", model="Nora", name="LVS-A1"):
    return ToyData(name, toy_id, model, "Lovense")


@pytest.fixture
def mock_builder():
    builder = MagicMock()
    builder.discover_toys = AsyncMock(return_value=[])
    builder.create_toys = AsyncMock(return_value=[])
    builder.create_toy = AsyncMock()
    builder.start_continuous = AsyncMock()
    builder.stop_continuous = AsyncMock()
    builder.retrieve_continuous = AsyncMock(return_value=[])
    return builder


@pytest.fixture
def hub_factory(mock_builder):
    """Build ToyHubs whose ConnectionBuilder is `mock_builder`; all hubs are shut down on teardown."""
    hubs = []

    def _make(**kwargs):
        with patch(
            "tikal.high_level.toy_hub.ConnectionBuilder", return_value=mock_builder
        ):
            hub = ToyHub(logger_name="test", **kwargs)
        hubs.append(hub)
        return hub

    yield _make

    for hub in hubs:
        try:
            hub.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Discovery + cache fill
# ---------------------------------------------------------------------------


def test_discover_fills_default_model(hub_factory, mock_builder):
    mock_builder.discover_toys.return_value = [lovense_data(model="")]
    hub = hub_factory(default_model="PICK_ME")

    toys = hub.discover_toys_blocking(0.1)

    assert len(toys) == 1
    assert toys[0].model_name == "PICK_ME"


def test_discover_fills_model_from_cache(hub_factory, mock_builder, tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text('{"LVS-A1": "Lush"}', encoding="utf-8")
    mock_builder.discover_toys.return_value = [lovense_data(model="")]
    hub = hub_factory(toy_cache_path=cache_file, default_model="DEFAULT")

    toys = hub.discover_toys_blocking(0.1)

    assert toys[0].model_name == "Lush"


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


def test_connect_registers_controllers_and_updates_cache(
    hub_factory, mock_builder, tmp_path
):
    cache_file = tmp_path / "cache.json"
    mock_builder.create_toys.return_value = [make_toy()]
    hub = hub_factory(toy_cache_path=cache_file)

    result = hub.connect_toys_blocking([lovense_data()])

    assert len(result) == 1
    assert isinstance(result[0], LovenseController)
    assert hub.is_running is True
    assert json.loads(cache_file.read_text(encoding="utf-8"))["LVS-A1"] == "Nora"


def test_connect_passes_through_exceptions(hub_factory, mock_builder):
    err = ConnectionError("nope")
    mock_builder.create_toys.return_value = [make_toy(toy_id="a1"), err]
    hub = hub_factory()

    result = hub.connect_toys_blocking(
        [lovense_data(toy_id="a1"), lovense_data(toy_id="b2", name="LVS-B2")]
    )

    assert isinstance(result[0], LovenseController)
    assert result[1] is err


# ---------------------------------------------------------------------------
# Disconnect / power-off
# ---------------------------------------------------------------------------


def test_disconnect_unregisters_and_stops_loop(hub_factory, mock_builder):
    toy = make_toy()
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory()
    hub.connect_toys_blocking([lovense_data()])
    assert hub.is_running is True

    hub.disconnect_toys_blocking(["a1"])

    toy.disconnect.assert_awaited()
    assert hub.is_running is False  # last toy gone -> loop stops


def test_power_off_unregisters_and_fires_callback(hub_factory, mock_builder):
    fired = []
    toy = make_toy()
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_power_off=fired.append)
    hub.connect_toys_blocking([lovense_data()])

    hub._handle_power_off("a1")

    assert fired == ["a1"]
    assert "a1" not in hub._toy_controllers


# ---------------------------------------------------------------------------
# Reconnection (success path)
# ---------------------------------------------------------------------------


def test_handle_disconnect_reconnect_success_reregisters(hub_factory, mock_builder):
    reconnected = threading.Event()
    disconnected = []
    toy = make_toy()
    toy.reconnect = AsyncMock(return_value=True)
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(
        on_disconnect=disconnected.append,
        on_reconnection_success=lambda tid: reconnected.set(),
    )
    hub.connect_toys_blocking([lovense_data()])

    hub._handle_disconnect("a1")

    assert reconnected.wait(timeout=3.0), "reconnection-success callback never fired"
    assert disconnected == ["a1"]
    assert "a1" in hub._toy_controllers  # re-registered after successful reconnect


def test_handle_disconnect_retries_until_the_toy_is_back(
    hub_factory, mock_builder, short_reconnect_pause
):
    # One failed attempt (or one that raises) must not cost the toy: reconnecting is retried.
    reconnected = threading.Event()
    toy = make_toy()
    toy.reconnect = AsyncMock(side_effect=[False, ConnectionError("hiccup"), True])
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_success=lambda tid: reconnected.set())
    hub.connect_toys_blocking([lovense_data()])

    hub._handle_disconnect("a1")

    assert reconnected.wait(timeout=3.0), "reconnection-success callback never fired"
    assert toy.reconnect.await_count == 3
    assert "a1" in hub._toy_controllers


def test_reconnect_stops_the_toy_and_pauses_its_pattern(hub_factory, mock_builder):
    # After up to a minute away the pattern must not resume on its own.
    reconnected = threading.Event()
    toy = make_toy()
    toy.reconnect = AsyncMock(return_value=True)
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_success=lambda tid: reconnected.set())
    hub.connect_toys_blocking([lovense_data()])
    controller = hub._toy_controllers["a1"]
    controller.set_pattern([(10_000, 10, 0)])
    assert controller.is_paused is False

    hub._handle_disconnect("a1")

    assert reconnected.wait(timeout=3.0), "reconnection-success callback never fired"
    assert toy.strict_stop.await_count == 1
    assert controller.is_paused is True


def test_commands_during_the_outage_are_rejected_right_away(hub_factory, mock_builder):
    # A command issued while the toy is reconnecting must not reach it up to a minute late: its callback hears None at
    # once, the way it hears about a failed command. Commands issued after the reconnect run normally.
    reconnected = threading.Event()
    release = threading.Event()
    results: list = []

    async def gated_reconnect() -> bool:
        # Hold the reconnect, so the command below is issued while the toy is certainly still disconnected.
        while not release.is_set():
            await asyncio.sleep(0.01)
        return True

    toy = make_toy()
    toy.reconnect = AsyncMock(side_effect=gated_reconnect)
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_success=lambda tid: reconnected.set())
    hub.connect_toys_blocking([lovense_data()])
    controller = hub._toy_controllers["a1"]

    hub._handle_disconnect("a1")
    controller.intensity1(5, callback=results.append)  # issued while disconnected
    assert results == [None]  # right away, not only once the reconnect ends
    release.set()

    assert reconnected.wait(timeout=3.0), "reconnection-success callback never fired"
    toy.intensity1.assert_not_awaited()

    controller.intensity1(7)
    deadline = time.time() + 2.0
    while toy.intensity1.await_count == 0 and time.time() < deadline:
        time.sleep(0.01)
    toy.intensity1.assert_awaited_once_with(7)


def test_reconnect_leaves_a_block_alone(hub_factory, mock_builder):
    reconnected = threading.Event()
    toy = make_toy()
    toy.reconnect = AsyncMock(return_value=True)
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_success=lambda tid: reconnected.set())
    hub.connect_toys_blocking([lovense_data()])
    controller = hub._toy_controllers["a1"]
    controller.toggle_block()

    hub._handle_disconnect("a1")

    assert reconnected.wait(timeout=3.0), "reconnection-success callback never fired"
    assert controller.is_blocked is True


def test_a_failed_stop_after_reconnecting_is_retried(
    hub_factory, mock_builder, short_reconnect_pause
):
    reconnected = threading.Event()
    toy = make_toy()
    toy.reconnect = AsyncMock(return_value=True)
    toy.strict_stop = AsyncMock(side_effect=[ConnectionError("flaky"), True])
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_success=lambda tid: reconnected.set())
    hub.connect_toys_blocking([lovense_data()])

    hub._handle_disconnect("a1")

    assert reconnected.wait(timeout=3.0), "reconnection-success callback never fired"
    assert toy.strict_stop.await_count == 2
    assert toy.reconnect.await_count == 2


def test_handle_disconnect_reconnect_failure_disconnects_promptly(
    hub_factory, mock_builder, short_reconnect_window
):
    # Regression: the failure path must clean up the toy without blocking the runner loop.
    # The old code called the blocking run_async() from the loop thread and deadlocked ~4s.
    # (The shortened reconnect window keeps the retries themselves well below the 1 s bound checked here.)
    failed = threading.Event()
    toy = make_toy()
    toy.reconnect = AsyncMock(return_value=False)  # force the failure path
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_failure=lambda tid: failed.set())
    hub.connect_toys_blocking([lovense_data()])

    t0 = time.time()
    hub._handle_disconnect("a1")
    assert failed.wait(timeout=3.0), "reconnection-failure callback never fired"

    deadline = time.time() + 2.0
    while toy.disconnect.await_count == 0 and time.time() < deadline:
        time.sleep(0.01)
    elapsed = time.time() - t0

    assert toy.disconnect.await_count == 1
    assert elapsed < 1.0, f"disconnect took {elapsed:.2f}s (loop was blocked)"
    assert "a1" not in hub._toy_controllers  # not re-registered
    assert toy.reconnect.await_count > 1  # retried before giving up


def test_given_up_toy_reports_leftover_commands_and_rejects_new_ones(
    hub_factory, mock_builder, short_reconnect_window
):
    # The toy is gone for good. A command still queued from just before the connection was lost is reported as failed
    # (None) before the reconnection-failure callback, and a command issued after that is rejected right away.
    events: list = []
    failed = threading.Event()

    def on_failure(toy_id: str) -> None:
        events.append("lost")
        failed.set()

    toy = make_toy()
    toy.reconnect = AsyncMock(return_value=False)
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_failure=on_failure)
    hub.connect_toys_blocking([lovense_data()])
    controller = hub._toy_controllers["a1"]

    hub._handle_disconnect("a1")
    # A command that made it into the queue just before the disconnect was noticed: the race the drop exists for.
    leftover = AsyncMock()
    controller._command_queue.append(
        (leftover, lambda result: events.append(("leftover", result)))
    )

    assert failed.wait(timeout=3.0), "reconnection-failure callback never fired"
    assert events == [("leftover", None), "lost"]
    leftover.assert_not_awaited()

    controller.intensity1(5, callback=lambda result: events.append(("after", result)))
    assert events[-1] == ("after", None)
    toy.intensity1.assert_not_awaited()


def test_handle_disconnect_reconnect_raising_reports_failure(
    hub_factory, mock_builder, short_reconnect_window
):
    # If reconnect() itself keeps raising, every attempt counts as failed: once the window runs out the
    # failure callback must fire and the toy must still be cleaned up.
    failed = threading.Event()
    toy = make_toy()
    toy.reconnect = AsyncMock(side_effect=ConnectionError("boom"))
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory(on_reconnection_failure=lambda tid: failed.set())
    hub.connect_toys_blocking([lovense_data()])

    hub._handle_disconnect("a1")

    assert failed.wait(timeout=3.0), "reconnection-failure callback never fired"
    assert "a1" not in hub._toy_controllers


def test_handle_disconnect_unknown_toy_is_ignored(hub_factory):
    hub = hub_factory()
    hub._handle_disconnect("ghost")  # must not raise


def test_handle_power_off_unknown_toy_is_ignored(hub_factory):
    hub = hub_factory()
    hub._handle_power_off("ghost")  # must not raise


# ---------------------------------------------------------------------------
# Callback setters
# ---------------------------------------------------------------------------


def test_callback_setters_replace_callbacks(hub_factory):
    hub = hub_factory()
    sentinel = lambda *a: None  # noqa: E731
    # None of these should raise; they just swap the stored callback.
    hub.battery_update_callback(sentinel)
    hub.error_callback(sentinel)
    hub.disconnect_callback(sentinel)
    hub.reconnection_failure_callback(sentinel)
    hub.reconnection_success_callback(sentinel)
    hub.power_off_callback(sentinel)
    assert hub._battery_update_callback is sentinel
    assert hub._power_off_callback is sentinel


# ---------------------------------------------------------------------------
# Continuous discovery (start_discovery / stop_discovery)
# ---------------------------------------------------------------------------


def test_start_discovery_fills_models_and_forwards(hub_factory, mock_builder):
    updates = []
    hub = hub_factory(default_model="DEF")
    hub.start_discovery(updates.append)

    # ToyHub wraps our callback; grab the wrapper it handed to the builder and drive it.
    wrapper = mock_builder.start_continuous.call_args.args[0]
    wrapper([lovense_data(model="")])

    assert len(updates) == 1
    assert updates[0][0].model_name == "DEF"  # filled from cache/default


def test_start_discovery_exception_reports_error_and_clears(hub_factory, mock_builder):
    errors = []
    updates = []
    hub = hub_factory(on_error=lambda e, ctx, tb: errors.append(e))
    hub.start_discovery(updates.append)

    wrapper = mock_builder.start_continuous.call_args.args[0]
    boom = RuntimeError("scan died")
    wrapper(boom)

    assert errors == [boom]
    assert updates == [[]]  # cleared on error


def test_stop_discovery_delegates(hub_factory, mock_builder):
    hub = hub_factory()
    hub.stop_discovery()
    mock_builder.stop_continuous.assert_awaited()


# ---------------------------------------------------------------------------
# Callback (non-blocking) variants
# ---------------------------------------------------------------------------


def test_discover_toys_callback_delivers_results(hub_factory, mock_builder):
    done = threading.Event()
    result = []
    mock_builder.discover_toys.return_value = [lovense_data(model="")]
    hub = hub_factory(default_model="DEF")

    def on_discovered(toys):
        result.append(toys)
        done.set()

    hub.discover_toys_callback(on_discovered, timeout=1.0)
    assert done.wait(3.0)
    assert result[0][0].model_name == "DEF"


def test_discover_toys_callback_delivers_exception(hub_factory, mock_builder):
    done = threading.Event()
    result = []
    mock_builder.discover_toys.side_effect = RuntimeError("bt off")
    hub = hub_factory()

    def on_discovered(res):
        result.append(res)
        done.set()

    hub.discover_toys_callback(on_discovered, timeout=1.0)
    assert done.wait(3.0)
    assert isinstance(result[0], Exception)


def test_connect_toys_callback_delivers_controllers(hub_factory, mock_builder):
    done = threading.Event()
    result = []
    mock_builder.create_toys.return_value = [make_toy()]
    hub = hub_factory()

    def on_connected(controllers):
        result.extend(controllers)
        done.set()

    hub.connect_toys_callback([lovense_data()], on_connected)
    assert done.wait(3.0)
    assert isinstance(result[0], LovenseController)
    assert "a1" in hub._toy_controllers


def test_disconnect_toys_callback_disconnects(hub_factory, mock_builder):
    done = threading.Event()
    result = []
    toy = make_toy()
    toy.disconnect = AsyncMock(return_value=None)  # real Toy.disconnect returns None
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory()
    hub.connect_toys_blocking([lovense_data()])

    def on_disconnected(res):
        result.append(res)
        done.set()

    hub.disconnect_toys_callback(["a1"], on_disconnected)
    assert done.wait(3.0)
    toy.disconnect.assert_awaited()
    assert result[0] == [None]


def test_disconnect_blocking_ignores_empty_and_unknown(hub_factory):
    hub = hub_factory()
    assert hub.disconnect_toys_blocking([]) == []
    assert hub.disconnect_toys_blocking(["ghost"]) == []  # unknown -> skipped


# ---------------------------------------------------------------------------
# update_model_name
# ---------------------------------------------------------------------------


def test_update_model_name_unknown_toy_returns_valueerror(hub_factory):
    hub = hub_factory()
    result = hub.update_model_name("ghost", "Lush")
    assert isinstance(result, ValueError)


def test_update_model_name_success(hub_factory, mock_builder):
    toy = make_toy()
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory()
    (controller,) = hub.connect_toys_blocking([lovense_data()])

    result = hub.update_model_name("a1", "Lush")

    assert result is controller
    toy.set_model_name.assert_awaited_with("Lush")


def test_update_model_name_propagates_command_error(hub_factory, mock_builder):
    toy = make_toy()
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory()
    hub.connect_toys_blocking([lovense_data()])
    toy.set_model_name = AsyncMock(side_effect=ValueError("bad model"))

    result = hub.update_model_name("a1", "Bogus")
    assert isinstance(result, ValueError)


# ---------------------------------------------------------------------------
# Background battery polling
# ---------------------------------------------------------------------------


def test_battery_poll_reports_levels(hub_factory, mock_builder):
    got = threading.Event()
    levels = {}
    toy = make_toy()
    toy.get_battery_level = AsyncMock(return_value=88)
    mock_builder.create_toys.return_value = [toy]

    def on_battery(batteries):
        levels.update(batteries)
        got.set()

    hub = hub_factory(on_battery_update=on_battery)
    hub.connect_toys_blocking([lovense_data()])  # forces an immediate battery poll

    assert got.wait(3.0), "battery callback never fired"
    assert levels == {"a1": 88}


def test_communication_loop_error_goes_to_on_error(hub_factory, mock_builder):
    got = threading.Event()
    errors = []
    toy = make_toy()
    toy.get_battery_level = AsyncMock(return_value=50)
    mock_builder.create_toys.return_value = [toy]

    def on_battery(_):
        raise RuntimeError("callback boom")

    def on_error(exc, ctx, tb):
        errors.append(exc)
        got.set()

    hub = hub_factory(on_battery_update=on_battery, on_error=on_error)
    hub.connect_toys_blocking([lovense_data()])

    assert got.wait(3.0), "on_error never fired"
    assert isinstance(errors[0], RuntimeError)


def test_shutdown_logs_disconnect_errors(hub_factory, mock_builder):
    toy = make_toy()
    toy.disconnect = AsyncMock(side_effect=ConnectionError("cannot close"))
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory()
    hub.connect_toys_blocking([lovense_data()])

    hub.shutdown()  # must swallow the disconnect error, not raise
    assert hub.is_running is False


def test_shutdown_is_idempotent(hub_factory, mock_builder):
    toy = make_toy()
    mock_builder.create_toys.return_value = [toy]
    hub = hub_factory()
    hub.connect_toys_blocking([lovense_data()])

    hub.shutdown()
    t0 = time.time()
    hub.shutdown()  # no-op; must return promptly
    assert time.time() - t0 < 2.0
    assert hub.is_running is False


# ---------------------------------------------------------------------------
# Exit safety net
# ---------------------------------------------------------------------------


def test_shutdown_is_registered_at_exit(mock_builder):
    """
    shutdown() is documented as mandatory, but a forgotten (or skipped) call must not leave toys running.

    The interpreter still runs atexit hooks after an uncaught exception or a KeyboardInterrupt, which is the last
    chance to stop and disconnect everything.
    """
    with (
        patch("tikal.high_level.toy_hub.ConnectionBuilder", return_value=mock_builder),
        patch("tikal.high_level.toy_hub.atexit") as fake_atexit,
    ):
        hub = ToyHub(logger_name="test")
    try:
        fake_atexit.register.assert_called_once()
        hook = fake_atexit.register.call_args.args[0]

        with patch.object(hub, "shutdown") as shutdown:
            hook()
        shutdown.assert_called_once()
    finally:
        hub.shutdown()


def test_explicit_shutdown_unregisters_the_atexit_hook(mock_builder):
    """A hub shut down properly must not run its safety net again at exit."""
    with (
        patch("tikal.high_level.toy_hub.ConnectionBuilder", return_value=mock_builder),
        patch("tikal.high_level.toy_hub.atexit") as fake_atexit,
    ):
        hub = ToyHub(logger_name="test")
        hub.shutdown()

    hook = fake_atexit.register.call_args.args[0]
    fake_atexit.unregister.assert_called_once_with(hook)


def test_atexit_hook_does_not_keep_the_hub_alive(mock_builder):
    """The hook holds only a weak reference, so it neither leaks the hub nor fires for a collected one."""
    with (
        patch("tikal.high_level.toy_hub.ConnectionBuilder", return_value=mock_builder),
        patch("tikal.high_level.toy_hub.atexit") as fake_atexit,
    ):
        hub = ToyHub(logger_name="test")

    hook = fake_atexit.register.call_args.args[0]
    hub.shutdown()
    hub_ref = weakref.ref(hub)
    del hub
    gc.collect()

    assert hub_ref() is None, "the atexit hook kept the ToyHub alive"
    hook()  # firing it after collection must be a harmless no-op


# ---------------------------------------------------------------------------
# Untrusted toy cache
# ---------------------------------------------------------------------------


def test_corrupted_cache_does_not_break_blocking_discovery(
    hub_factory, mock_builder, tmp_path
):
    """A cache entry that is not a model-name string must not break discovery for the other toys."""
    cache_path = tmp_path / "toys.json"
    cache_path.write_text(
        json.dumps({"LVS-A1": 123, "LVS-B2": "Edge"}), encoding="utf-8"
    )
    mock_builder.discover_toys.return_value = [
        lovense_data(toy_id="a1", model="", name="LVS-A1"),
        lovense_data(toy_id="b2", model="", name="LVS-B2"),
    ]
    hub = hub_factory(toy_cache_path=cache_path, default_model="unknown")

    found = hub.discover_toys_blocking(0.1)

    assert [toy.model_name for toy in found] == ["unknown", "Edge"]


def test_corrupted_cache_does_not_break_continuous_discovery(
    hub_factory, mock_builder, tmp_path
):
    """
    Regression: the model name is filled in inside the scanner's callback.

    A non-str cache value used to raise TypeError there, far from the bad entry and on a Bluetooth worker thread.
    """
    cache_path = tmp_path / "toys.json"
    cache_path.write_text(json.dumps({"LVS-A1": ["Gush"]}), encoding="utf-8")
    hub = hub_factory(toy_cache_path=cache_path, default_model="unknown")

    updates = []
    hub.start_discovery(updates.append)
    on_update = mock_builder.start_continuous.call_args.args[0]

    on_update(
        [lovense_data(toy_id="a1", model="", name="LVS-A1")]
    )  # what the scanner does

    assert [toy.model_name for toy in updates[-1]] == ["unknown"]
