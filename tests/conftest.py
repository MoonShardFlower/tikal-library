"""Fixtures shared by several test modules."""

import time
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from tikal.high_level import ToyController, ToyHub


@pytest.fixture
def short_reconnect_window(monkeypatch):
    """Shrink the reconnect policy (both API layers) so giving up on a toy takes a fraction of a second."""
    monkeypatch.setattr("tikal._private.reconnect.RECONNECT_WINDOW", 0.3)
    monkeypatch.setattr("tikal._private.reconnect.RECONNECT_PAUSE", 0.05)


@pytest.fixture
def short_reconnect_pause(monkeypatch):
    """Keep the full reconnect window, but retry almost immediately, for tests that expect a later attempt to succeed."""
    monkeypatch.setattr("tikal._private.reconnect.RECONNECT_PAUSE", 0.05)


@pytest.fixture
def mock_hub():
    """
    Build High-Level ToyHubs whose only toys are the in-memory MockEstimToys (Thunder_ID, Lightning_ID).

    The Bluetooth scanner finds nothing, so tests are fast and deterministic. Every hub is shut down on teardown.
    """
    hubs: list[ToyHub] = []

    def make(**kwargs) -> ToyHub:
        scanner = MagicMock()
        scanner.discover = AsyncMock(return_value=[])
        hub = ToyHub(
            logger_name="test", bluetooth_scanner=scanner, mock_toys=True, **kwargs
        )
        hubs.append(hub)
        return hub

    yield make

    for hub in hubs:
        hub.shutdown()


def connect_mock_toy(
    hub: ToyHub, toy_id: str = "Thunder_ID", model_name: str = "Thunder"
) -> ToyController:
    """Discover the MockEstimToys and connect one of them."""
    toy_data = next(t for t in hub.discover_toys_blocking(0.1) if t.toy_id == toy_id)
    toy_data.model_name = model_name
    controller = hub.connect_toys_blocking([toy_data])[0]
    assert isinstance(controller, ToyController), controller
    return controller


def wait_until(predicate: Callable[[], object], timeout: float = 5.0) -> bool:
    """Poll *predicate* from the test thread until it is truthy (work runs on the hub's background thread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())
