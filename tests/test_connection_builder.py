from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tikal.low_level import (
    BLEConnectionBuilder,
    ConnectionBuilder,
    InvalidModelError,
    ToyData,
)
from tikal.low_level.brands.lovense import LovenseHandler
from tikal.low_level.connection_builder import StaleDeviceError


class FakeBLEDevice:
    def __init__(self, name="LVS-Test", address="AA:BB:CC"):
        self.name = name
        self.address = address
        self.services = []

    async def connect(self):
        return

    async def disconnect(self):
        return


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
    td = ToyData("LVS-Test", "addr", "", "Lovense")
    assert LovenseHandler.handles_toy(td)


def test_create_toy_data():
    device = FakeBLEDevice(name="LVS-Test", address="addr")
    td = LovenseHandler.create_toy_data(device)

    assert td.name == "LVS-Test"
    assert td.toy_id == "addr"


@pytest.mark.asyncio
async def test_create_toy_invalid_model(handler):
    device = FakeBLEDevice()
    td = ToyData("LVS-Test", device.address, "INVALID", "Lovense")

    with pytest.raises(InvalidModelError):
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

    td = ToyData("LVS-Test", "missing", "Nora", "Lovense")

    result = await builder.create_toy(td)
    assert isinstance(result, KeyError)


@pytest.mark.asyncio
async def test_create_toy_stale_device(callbacks):
    on_disconnect, on_power_off = callbacks

    builder = BLEConnectionBuilder(on_disconnect, on_power_off, "test")

    td = ToyData("LVS-Test", "addr", "Nora", "Lovense")

    builder._all_seen_addresses.add("addr")
    # NOT in _ble_devices → stale

    result = await builder.create_toy(td)
    assert isinstance(result, StaleDeviceError)


@pytest.mark.asyncio
async def test_create_toys_mixed_results(callbacks):
    on_disconnect, on_power_off = callbacks

    builder = BLEConnectionBuilder(on_disconnect, on_power_off, "test")

    td = ToyData("LVS-Test", "addr", "Nora", "Lovense")

    builder._all_seen_addresses.add("addr")
    builder._ble_devices["addr"] = FakeBLEDevice()

    mock_toy = MagicMock()
    mock_toy.set_model_name = AsyncMock()

    with patch.object(
        builder._handlers[0], "create_toy", new=AsyncMock(return_value=mock_toy)
    ):
        results = await builder.create_toys([td])

    assert results == [mock_toy]
    mock_toy.set_model_name.assert_called_once_with("Nora")


@pytest.mark.asyncio
async def test_retrieve_continuous_exception(callbacks):
    builder = BLEConnectionBuilder(*callbacks, logger_name="test")

    builder._continuous_exception = RuntimeError("fail")

    with pytest.raises(RuntimeError):
        await builder.retrieve_continuous()

    # second call should not raise
    result = await builder.retrieve_continuous()
    assert result == []


# ---------------------------------------------------------------------------
# ConnectionBuilder (the transport-agnostic composite)
# ---------------------------------------------------------------------------


class FakeBuilder:
    """Stand-in for a per-transport connection builder, exercising the composite's routing/merging."""

    def __init__(self, toy_data=None, handles=True):
        self._toy_data = toy_data or []
        self._handles = handles
        self._on_update = None
        self.stopped = False

    async def discover_toys(self):
        return list(self._toy_data)

    async def start_continuous(self, on_update=None):
        self._on_update = on_update
        if on_update:
            on_update([])  # mirror real builders clearing their cache on start

    async def stop_continuous(self):
        self.stopped = True

    async def retrieve_continuous(self):
        return list(self._toy_data)

    def handles_toy(self):
        return self._handles

    async def create_toy(self, toy_data):
        return f"toy:{toy_data.toy_id}"

    def emit(self, snapshot):
        """Simulate this transport pushing a new discovery snapshot."""
        self._on_update(snapshot)


@pytest.mark.asyncio
async def test_connection_builder_discover_aggregates(callbacks):
    builder = ConnectionBuilder(*callbacks, logger_name="test")
    a = ToyData("LVS-A", "a", "", "Lovense")
    b = ToyData("LVS-B", "b", "", "Lovense")
    builder._builders = [FakeBuilder([a]), FakeBuilder([b])]

    assert await builder.discover_toys() == [a, b]


@pytest.mark.asyncio
async def test_connection_builder_create_toy_routes_to_owner(callbacks):
    builder = ConnectionBuilder(*callbacks, logger_name="test")
    td = ToyData("LVS-A", "a", "Nora", "Lovense")
    builder._builders = [FakeBuilder(handles=False), FakeBuilder(handles=True)]

    assert await builder.create_toy(td) == "toy:a"


@pytest.mark.asyncio
async def test_connection_builder_create_toy_no_owner(callbacks):
    builder = ConnectionBuilder(*callbacks, logger_name="test")
    td = ToyData("LVS-A", "a", "Nora", "Lovense")
    builder._builders = [FakeBuilder(handles=False)]

    result = await builder.create_toy(td)
    assert isinstance(result, RuntimeError)


@pytest.mark.asyncio
async def test_connection_builder_continuous_merges_snapshots(callbacks):
    builder = ConnectionBuilder(*callbacks, logger_name="test")
    first, second = FakeBuilder(), FakeBuilder()
    builder._builders = [first, second]

    updates = []
    await builder.start_continuous(updates.append)

    a = ToyData("LVS-A", "a", "", "Lovense")
    b = ToyData("LVS-B", "b", "", "Lovense")
    first.emit([a])
    second.emit([b])

    # The user callback always sees a single list spanning every transport.
    assert updates[-1] == [a, b]

    await builder.stop_continuous()
    assert first.stopped and second.stopped
