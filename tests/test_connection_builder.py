from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tikal.connection_builder import (
    BLEConnectionBuilder,
    LovenseHandler,
    StaleDeviceError,
)
from tikal.toy_data import LovenseData, ValidationError


class FakeBLEDevice:
    def __init__(self, name="LVS-Test", address="AA:BB:CC"):
        self.name = name
        self.address = address


@pytest.fixture
def callbacks():
    return MagicMock(), MagicMock()


@pytest.fixture
def handler(callbacks):
    on_disconnect, on_power_off = callbacks
    return LovenseHandler(on_disconnect, on_power_off, client_class=MagicMock)


def test_handles_device_true():
    device = FakeBLEDevice(name="LVS-123")
    assert LovenseHandler.handles_device(device)


def test_handles_device_false():
    device = FakeBLEDevice(name="OTHER")
    assert not LovenseHandler.handles_device(device)


def test_handles_toy():
    td = LovenseData("LVS-Test", "addr", "")
    assert LovenseHandler.handles_toy(td)


def test_create_toy_data():
    device = FakeBLEDevice(name="LVS-Test", address="addr")
    td = LovenseHandler.create_toy_data(device)

    assert td.name == "LVS-Test"
    assert td.toy_id == "addr"


@pytest.mark.asyncio
async def test_create_toy_invalid_model(handler):
    device = FakeBLEDevice()
    td = LovenseData("LVS-Test", device.address, "INVALID")

    with pytest.raises(ValidationError):
        await handler.create_toy(td, device)


@pytest.mark.asyncio
async def test_find_uuid_invalid_type(handler):
    client = MagicMock()
    with pytest.raises(ValueError):
        await handler._find_uuid_by_type(client, "invalid")


@pytest.mark.asyncio
async def test_find_uuid_not_found(handler):
    client = MagicMock()
    client.services = []
    client.address = "addr"

    with pytest.raises(ConnectionError):
        await handler._find_uuid_by_type(client, "tx")


@pytest.mark.asyncio
async def test_discover_toys_filters_devices(callbacks):
    on_disconnect, on_power_off = callbacks

    fake_device = FakeBLEDevice(name="LVS-Test", address="addr")

    mock_scanner = MagicMock()
    mock_scanner.discover = AsyncMock(return_value=[fake_device])

    builder = BLEConnectionBuilder(
        on_disconnect,
        on_power_off,
        logger_name="test",
        scanner_class=mock_scanner,
    )

    toys = await builder.discover_toys()

    assert len(toys) == 1
    assert toys[0].toy_id == "addr"


@pytest.mark.asyncio
async def test_create_toy_not_discovered(callbacks):
    builder = BLEConnectionBuilder(*callbacks, logger_name="test")

    td = LovenseData("LVS-Test", "missing", "Nora")

    result = await builder.create_toy(td)
    assert isinstance(result, KeyError)


@pytest.mark.asyncio
async def test_create_toy_stale_device(callbacks):
    on_disconnect, on_power_off = callbacks

    builder = BLEConnectionBuilder(on_disconnect, on_power_off, "test")

    td = LovenseData("LVS-Test", "addr", "Nora")

    builder._all_seen_addresses.add("addr")
    # NOT in _ble_devices → stale

    result = await builder.create_toy(td)
    assert isinstance(result, StaleDeviceError)


@pytest.mark.asyncio
async def test_create_toys_mixed_results(callbacks):
    on_disconnect, on_power_off = callbacks

    builder = BLEConnectionBuilder(on_disconnect, on_power_off, "test")

    td = LovenseData("LVS-Test", "addr", "Nora")

    builder._all_seen_addresses.add("addr")
    builder._ble_devices["addr"] = FakeBLEDevice()

    with patch.object(
        builder._handlers[0], "create_toy", new=AsyncMock(return_value="OK")
    ):
        results = await builder.create_toys([td])

    assert results == ["OK"]


@pytest.mark.asyncio
async def test_retrieve_continuous_exception(callbacks):
    builder = BLEConnectionBuilder(*callbacks, logger_name="test")

    builder._continuous_exception = RuntimeError("fail")

    with pytest.raises(RuntimeError):
        await builder.retrieve_continuous()

    # second call should not raise
    result = await builder.retrieve_continuous()
    assert result == []
