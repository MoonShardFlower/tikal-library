"""Tests for the High-Level :class:`ToyHub` orchestration."""

import json
import threading
import time
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


def test_handle_disconnect_reconnect_failure_disconnects_promptly(
    hub_factory, mock_builder
):
    # Regression: the failure path must clean up the toy without blocking the runner loop.
    # The old code called the blocking run_async() from the loop thread and deadlocked ~4s.
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
