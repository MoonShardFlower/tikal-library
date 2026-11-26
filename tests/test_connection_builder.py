from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.scanner import AdvertisementData

from tikal.low_level import (
    LOVENSE_UUID_PREFIXES,
    BLEConnectionBuilder,
    ConnectionBuilder,
    InvalidModelError,
    LovenseToy,
    ToyData,
    ValidationError,
)
from tikal.low_level.brands.lovense import LovenseHandler
from tikal.low_level.brands.lovense.data import LOVENSE_TOY_SPECIFICATIONS
from tikal.low_level.connection_builder import StaleDeviceError
from tikal.mock import MockBleakClient, MockBleakScanner
from tikal.mock.mock_lovense import MockBLEDevice, MockCharacteristic, MockService


class FakeBLEDevice:
    def __init__(self, name="LVS-Test", address="AA:BB:CC"):
        self.name = name
        self.address = address
        self.services = []

    async def connect(self):
        return

    async def disconnect(self):
        return


def _lovense_service(prefix: str, version: str = "0023") -> str:
    """The Lovense service UUID that starts with *prefix*, e.g., ``42410001-0023-...`` for ``"4241"``."""
    return f"{prefix}0001-{version}-4bd4-bbd5-a6920e4c5653"


_BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"


def _advertisement(
    *service_uuids: str, local_name: str | None = None
) -> AdvertisementData:
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={},
        service_data={},
        service_uuids=list(service_uuids),
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


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
    other_device = FakeBLEDevice(name="Headphones", address="other")

    mock_scanner = MagicMock()
    mock_scanner.discover = AsyncMock(
        return_value={
            "addr": (fake_device, _advertisement()),
            "other": (other_device, _advertisement(_BATTERY_SERVICE)),
        }
    )

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
        self.scanning = False

    async def discover_toys(self, _=None):
        return list(self._toy_data)

    async def start_continuous(self, on_update=None):
        self._on_update = on_update
        if self.scanning:
            return  # like the real builders: a running scan continues, with the new callback
        self.scanning = True
        if on_update:
            on_update([])  # mirror real builders clearing their cache on start

    async def stop_continuous(self):
        self.stopped = True
        self.scanning = False

    async def retrieve_continuous(self):
        return list(self._toy_data)

    def handles_toy(self, _=None):
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


@pytest.mark.asyncio
async def test_connection_builder_start_while_scanning_keeps_the_toys_found(callbacks):
    builder = ConnectionBuilder(*callbacks, logger_name="test")
    first, second = FakeBuilder(), FakeBuilder()
    builder._builders = [first, second]
    earlier: list = []
    await builder.start_continuous(earlier.append)
    a = ToyData("LVS-A", "a", "", "Lovense")
    b = ToyData("LVS-B", "b", "", "Lovense")
    first.emit([a])
    second.emit([b])

    updates: list = []
    await builder.start_continuous(updates.append)

    assert updates == [[a, b]]  # at once, and without a leading empty list
    first.emit([])
    assert updates[-1] == [b] and earlier[-1] == [
        a,
        b,
    ]  # later changes go to the new callback only


# ---------------------------------------------------------------------------
# LovenseHandler: connection + TX/RX UUID resolution
# ---------------------------------------------------------------------------

_LOVENSE_BASE_UUID = "40300001-0023-4bd4-bbd5-a6920e4c5653"
_LOVENSE_TX_UUID = "40300002-0023-4bd4-bbd5-a6920e4c5653"
_LOVENSE_RX_UUID = "40300003-0023-4bd4-bbd5-a6920e4c5653"


def _client_with_lovense_services():
    """A stand-in client exposing a Lovense GATT service (base + tx + rx characteristics)."""
    client = MagicMock()
    client.address = "addr"
    client.services = [
        MockService(
            _LOVENSE_BASE_UUID,
            [
                MockCharacteristic(_LOVENSE_TX_UUID),
                MockCharacteristic(_LOVENSE_RX_UUID),
            ],
        )
    ]
    return client


@pytest.mark.asyncio
async def test_find_uuid_by_type_tx(handler):
    client = _client_with_lovense_services()
    assert await handler._find_uuid_by_type(client, "tx") == _LOVENSE_TX_UUID.upper()


@pytest.mark.asyncio
async def test_find_uuid_by_type_rx(handler):
    client = _client_with_lovense_services()
    assert await handler._find_uuid_by_type(client, "rx") == _LOVENSE_RX_UUID.upper()


@pytest.mark.asyncio
async def test_find_uuid_service_matches_but_characteristic_missing(handler):
    """A matching service whose characteristics omit the target UUID still fails."""
    client = MagicMock()
    client.address = "addr"
    client.services = [MockService(_LOVENSE_BASE_UUID, [])]  # no characteristics
    with pytest.raises(ConnectionError):
        await handler._find_uuid_by_type(client, "tx")


@pytest.mark.asyncio
async def test_resolve_uuids_returns_tx_and_rx(handler):
    client = _client_with_lovense_services()
    assert await handler._resolve_uuids(client) == (
        _LOVENSE_TX_UUID.upper(),
        _LOVENSE_RX_UUID.upper(),
    )


@pytest.mark.asyncio
async def test_create_toy_success(callbacks):
    """create_toy connects via the (mock) client and returns a ready LovenseToy."""
    on_disconnect, on_power_off = callbacks
    MockBleakScanner.reset()
    handler = LovenseHandler(on_disconnect, on_power_off, client_class=MockBleakClient)
    device = MockBLEDevice("LVS-Gush", "00:00:00:00:00:02")
    td = ToyData("LVS-Gush", "00:00:00:00:00:02", "Gush", "Lovense")

    # MockBleakClient.connect() sleeps to imitate a real link; skip it to keep the test fast.
    with patch("tikal.mock.mock_lovense.asyncio.sleep", new_callable=AsyncMock):
        toy = await handler.create_toy(td, device)
    try:
        assert isinstance(toy, LovenseToy)
        assert toy.model_name == "Gush"
        assert toy.is_connected
    finally:
        MockBleakScanner.reset()


@pytest.mark.asyncio
async def test_create_toy_connection_failure_wraps_error(callbacks):
    """A client that fails to connect surfaces as a ConnectionError from create_toy."""
    on_disconnect, on_power_off = callbacks

    class _FailingClient:
        def __init__(self, device, disconnected_callback=None):
            self.address = device.address
            self.is_connected = False

        async def connect(self):
            raise RuntimeError("boom")

        async def disconnect(self):
            self.is_connected = False

    handler = LovenseHandler(on_disconnect, on_power_off, client_class=_FailingClient)
    device = FakeBLEDevice(name="LVS-Gush", address="addr")
    td = ToyData("LVS-Gush", "addr", "Gush", "Lovense")

    with pytest.raises(ConnectionError):
        await handler.create_toy(td, device)


@pytest.mark.asyncio
async def test_create_toy_validation_error_disconnects_and_reraises(callbacks):
    """A ValidationError raised during connection setup disconnects and propagates unchanged."""
    on_disconnect, on_power_off = callbacks
    MockBleakScanner.reset()
    handler = LovenseHandler(on_disconnect, on_power_off, client_class=MockBleakClient)
    device = MockBLEDevice("LVS-Gush", "00:00:00:00:00:02")
    td = ToyData("LVS-Gush", "00:00:00:00:00:02", "Gush", "Lovense")

    with (
        patch("tikal.mock.mock_lovense.asyncio.sleep", new_callable=AsyncMock),
        patch.object(
            LovenseToy, "start_notifications", new_callable=AsyncMock
        ) as mock_notify,
    ):
        mock_notify.side_effect = ValidationError("bad model during setup")
        with pytest.raises(ValidationError):
            await handler.create_toy(td, device)
    MockBleakScanner.reset()


@pytest.mark.parametrize(
    "service_uuid",
    [
        _lovense_service(
            "5330"
        ),  # Lush: UUIDs not starting with 4 were not found before
        _lovense_service("5a30", "0024"),  # Hush
        _lovense_service("4241"),  # Solace Pro
        _lovense_service("455a"),  # Gush 2
        _LOVENSE_BASE_UUID.upper(),  # case does not matter
    ],
)
@pytest.mark.asyncio
async def test_find_uuid_by_type_in_any_lovense_service(handler, service_uuid):
    tx = f"{service_uuid[:7]}2{service_uuid[8:]}"
    rx = f"{service_uuid[:7]}3{service_uuid[8:]}"
    client = MagicMock()
    client.address = "addr"
    client.services = [
        MockService(
            _BATTERY_SERVICE,
            [MockCharacteristic("00002a19-0000-1000-8000-00805f9b34fb")],
        ),
        MockService(service_uuid, [MockCharacteristic(tx), MockCharacteristic(rx)]),
    ]
    assert await handler._resolve_uuids(client) == (tx.upper(), rx.upper())


@pytest.mark.asyncio
async def test_find_uuid_by_type_counts_on_from_the_service(handler):
    """tx and rx follow the service UUID's first group, also if it does not end in 1."""
    client = MagicMock()
    client.address = "addr"
    client.services = [
        MockService(
            "50300011-0023-4bd4-bbd5-a6920e4c5653",
            [
                MockCharacteristic("50300012-0023-4bd4-bbd5-a6920e4c5653"),
                MockCharacteristic("50300013-0023-4bd4-bbd5-a6920e4c5653"),
            ],
        )
    ]
    assert await handler._resolve_uuids(client) == (
        "50300012-0023-4BD4-BBD5-A6920E4C5653",
        "50300013-0023-4BD4-BBD5-A6920E4C5653",
    )


@pytest.mark.parametrize(
    ("service_uuid", "tx", "rx"),
    [
        (
            "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
            "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
            "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
        ),
        (
            "0000fff0-0000-1000-8000-00805f9b34fb",
            "0000fff2-0000-1000-8000-00805f9b34fb",
            "0000fff1-0000-1000-8000-00805f9b34fb",
        ),
    ],
)
@pytest.mark.asyncio
async def test_find_uuid_by_type_in_the_generic_services(handler, service_uuid, tx, rx):
    client = MagicMock()
    client.address = "addr"
    client.services = [
        MockService(service_uuid, [MockCharacteristic(tx), MockCharacteristic(rx)])
    ]
    assert await handler._resolve_uuids(client) == (tx.upper(), rx.upper())


@pytest.mark.asyncio
async def test_find_uuid_by_type_prefers_the_lovense_service(handler):
    client = _client_with_lovense_services()
    client.services.insert(
        0,
        MockService(
            "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
            [
                MockCharacteristic("6e400002-b5a3-f393-e0a9-e50e24dcca9e"),
                MockCharacteristic("6e400003-b5a3-f393-e0a9-e50e24dcca9e"),
            ],
        ),
    )
    assert await handler._find_uuid_by_type(client, "tx") == _LOVENSE_TX_UUID.upper()


@pytest.mark.asyncio
async def test_find_uuid_by_type_ignores_other_services(handler):
    client = MagicMock()
    client.address = "addr"
    client.services = [
        MockService(
            _BATTERY_SERVICE,
            [MockCharacteristic("00002a19-0000-1000-8000-00805f9b34fb")],
        )
    ]
    with pytest.raises(ConnectionError):
        await handler._find_uuid_by_type(client, "tx")


# ---------------------------------------------------------------------------
# LovenseHandler: the model from the advertisement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service_uuid", "model_name"),
    [
        ("42410001-0023-4bd4-bbd5-a6920e4c5653", "Solace"),  # seen on a Solace Pro
        ("455a0001-0023-4bd4-bbd5-a6920e4c5653", "Gush"),  # seen on a Gush 2
        ("42300001-0023-4bd4-bbd5-a6920e4c5653", "Max"),
        ("5A300001-0024-4BD4-BBD5-A6920E4C5653", "Hush"),  # case does not matter
        (
            "50300011-0023-4bd4-bbd5-a6920e4c5653",
            "Edge",
        ),  # only the first four digits count
        ("40300001-0023-4bd4-bbd5-a6920e4c5653", ""),  # the mock's: no known model
        ("42410001-0023-0000-0000-000000000000", ""),  # not a Lovense service
        ("6e400001-b5a3-f393-e0a9-e50e24dcca9e", ""),  # generic service: tells no model
        (_BATTERY_SERVICE, ""),
        ("4241", ""),
    ],
)
def test_create_toy_data_model_from_the_service_uuid(service_uuid, model_name):
    device = FakeBLEDevice(name="LVS-Test", address="addr")
    td = LovenseHandler.create_toy_data(device, _advertisement(service_uuid))
    assert td.model_name == model_name


def test_every_uuid_prefix_maps_to_a_model():
    for prefix, model_name in LOVENSE_UUID_PREFIXES.items():
        assert model_name in LOVENSE_TOY_SPECIFICATIONS, prefix
        # Four lower case hex digits: the handler looks up the start of the lower-cased UUID.
        assert len(prefix) == 4 and prefix == prefix.lower(), prefix
        int(prefix, 16)


def test_handles_device_by_the_advertised_service():
    device = FakeBLEDevice(name=None, address="addr")
    assert LovenseHandler.handles_device(
        device, _advertisement(_lovense_service("4241"))
    )
    # Also a Lovense service of a model we do not know
    assert LovenseHandler.handles_device(
        device, _advertisement(_lovense_service("4030"))
    )
    assert not LovenseHandler.handles_device(device, _advertisement(_BATTERY_SERVICE))
    assert not LovenseHandler.handles_device(device, None)


@pytest.mark.parametrize(
    ("service_uuids", "model_name"),
    [
        ([_lovense_service("4241")], "Solace"),
        ([_lovense_service("455a")], "Gush"),
        ([_BATTERY_SERVICE, _lovense_service("5330")], "Lush"),
        ([_lovense_service("4030"), _lovense_service("5330")], "Lush"),
        ([_lovense_service("5959")], ""),  # a model we do not know
        ([], ""),
    ],
)
def test_create_toy_data_fills_in_the_advertised_model(service_uuids, model_name):
    # The Bluetooth name does not tell the model: this LVS-B12 is a Solace Pro.
    device = FakeBLEDevice(name="LVS-B12", address="addr")
    td = LovenseHandler.create_toy_data(device, _advertisement(*service_uuids))
    assert (td.name, td.toy_id, td.model_name, td.brand) == (
        "LVS-B12",
        "addr",
        model_name,
        "Lovense",
    )


def test_create_toy_data_without_a_device_name_takes_the_advertised_one():
    device = FakeBLEDevice(name=None, address="addr")
    td = LovenseHandler.create_toy_data(device, _advertisement(local_name="LVS-Gush"))
    assert td.name == "LVS-Gush"


# ---------------------------------------------------------------------------
# BLEConnectionBuilder: the model from the advertisement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_toys_fills_in_the_advertised_model(callbacks):
    MockBleakScanner.reset()
    builder = BLEConnectionBuilder(
        *callbacks, logger_name="test", scanner_class=MockBleakScanner
    )

    found = {td.name: td.model_name for td in await builder.discover_toys(0.1)}

    assert found["LVS-Solace"] == "Solace"  # advertises "H"
    assert found["LVS-Nora"] == "Nora"  # advertises "A"
    assert found["LVS-Gush"] == ""  # advertises nothing


@pytest.mark.asyncio
async def test_continuous_scan_fills_in_a_model_a_later_advertisement_reveals(
    callbacks,
):
    builder = BLEConnectionBuilder(*callbacks, logger_name="test")
    updates: list = []
    builder._on_update = updates.append
    device = FakeBLEDevice(name="LVS-B12", address="addr")

    await builder._on_device_detected(device, _advertisement())  # the name only
    first = updates[-1][0]
    await builder._on_device_detected(device, _advertisement(_lovense_service("4241")))

    assert len(updates) == 2
    assert updates[-1][0].model_name == "Solace"
    assert first.model_name == ""  # the snapshot handed out before is left alone


@pytest.mark.asyncio
async def test_continuous_scan_keeps_a_known_model(callbacks):
    builder = BLEConnectionBuilder(*callbacks, logger_name="test")
    updates: list = []
    builder._on_update = updates.append
    device = FakeBLEDevice(name="LVS-B12", address="addr")

    await builder._on_device_detected(device, _advertisement(_lovense_service("4241")))
    await builder._on_device_detected(device, _advertisement())
    await builder._on_device_detected(device, _advertisement(_lovense_service("455a")))

    assert len(updates) == 1  # nothing changed after the first
    assert [td.model_name for td, _ in builder._discovered.values()] == ["Solace"]
