import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tikal.high_level import ToyHub
from tikal.high_level.toy_controller import CONTROLLER_BY_BRAND, MockEstimController
from tikal.low_level import (
    ConnectionBuilder,
    InvalidModelError,
    MockConnectionBuilder,
    MockEstimToy,
    MockTransport,
    ToyData,
)
from tikal.websocket._toy_controller import _CONTROLLER_BY_BRAND, _MockEstimController
from tikal.websocket._toy_hub import _ToyHub


@pytest.fixture
def callbacks():
    return MagicMock(), MagicMock()


def _thunder() -> ToyData:
    return ToyData("Thunder1", "Thunder_ID", "Thunder", "MockEstimToys")


def _lightning() -> ToyData:
    return ToyData("Lightning1", "Lightning_ID", "Lightning", "MockEstimToys")


# ---------------------------------------------------------------------------
# MockTransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_transport_responds_to_commands():
    transport = MockTransport("X_ID", "X1")
    received = []
    await transport.start_notify(received.append)

    await transport.send(b"Channel1:50;")
    assert received[-1] == b"OK;"

    await transport.send(b"Battery;")
    assert received[-1] == b"77;"


@pytest.mark.asyncio
async def test_mock_transport_disconnect_and_reconnect():
    transport = MockTransport("X_ID", "X1")
    await transport.disconnect()
    assert transport.is_connected is False

    with pytest.raises(ConnectionError):
        await transport.send(b"Channel1:10;")

    # Reconnect after an intentional disconnect is not allowed.
    with pytest.raises(RuntimeError):
        await transport.reconnect()


# ---------------------------------------------------------------------------
# MockConnectionBuilder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_returns_thunder_and_lightning(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toys = await builder.discover_toys()

    by_id = {t.toy_id: t for t in toys}
    assert set(by_id) == {"Thunder_ID", "Lightning_ID"}
    assert by_id["Thunder_ID"].name == "Thunder1"
    assert by_id["Thunder_ID"].model_name == "Thunder"
    assert by_id["Thunder_ID"].brand == "MockEstimToys"


@pytest.mark.asyncio
async def test_handles_toy(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    assert builder.handles_toy(_thunder())
    assert not builder.handles_toy(ToyData("LVS-X", "addr", "Lush", "Lovense"))


@pytest.mark.asyncio
async def test_connect_thunder_is_single_channel(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_thunder())

    assert isinstance(toy, MockEstimToy)
    assert toy.brand == "MockEstimToys"
    assert toy.max_intensity == 100
    assert toy.intensity_names == ("Stimulation", None)

    assert await toy.intensity1(50) is True
    # Single-channel model: intensity2 is an accepted no-op.
    assert await toy.intensity2(40) is True
    assert toy.current_intensities == (50, 0)
    assert await toy.get_battery_level() == 77

    await toy.disconnect()
    assert toy.is_connected is False


@pytest.mark.asyncio
async def test_connect_lightning_is_dual_channel(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_lightning())

    assert toy.intensity_names == ("Stimulation A", "Stimulation B")
    await toy.intensity1(50)
    await toy.intensity2(30)
    assert toy.current_intensities == (50, 30)


@pytest.mark.asyncio
async def test_intensity_is_clamped(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_thunder())

    await toy.intensity1(99999)
    assert toy.current_intensities[0] == 100
    await toy.intensity1(-5)
    assert toy.current_intensities[0] == 0


@pytest.mark.asyncio
async def test_create_toy_invalid_model(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    result = await builder.create_toy(
        ToyData("Thunder1", "Thunder_ID", "BadModel", "MockEstimToys")
    )
    assert isinstance(result, InvalidModelError)


@pytest.mark.asyncio
async def test_create_toy_unknown_device(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    result = await builder.create_toy(
        ToyData("Ghost", "Ghost_ID", "Thunder", "MockEstimToys")
    )
    assert isinstance(result, KeyError)


@pytest.mark.asyncio
async def test_continuous_reports_then_clears(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    assert await builder.retrieve_continuous() == []

    updates = []
    await builder.start_continuous(updates.append)

    assert updates[0] == []  # cache cleared first
    assert {t.toy_id for t in updates[-1]} == {"Thunder_ID", "Lightning_ID"}

    snapshot = await builder.retrieve_continuous()
    assert {t.toy_id for t in snapshot} == {"Thunder_ID", "Lightning_ID"}

    await builder.stop_continuous()
    assert await builder.retrieve_continuous() == []


@pytest.mark.asyncio
async def test_connected_toy_is_hidden_from_discovery(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    assert {t.toy_id for t in await builder.discover_toys()} == {
        "Thunder_ID",
        "Lightning_ID",
    }

    toy = await builder.create_toy(_thunder())
    # A connected toy stops "advertising" -> only Lightning remains discoverable.
    assert {t.toy_id for t in await builder.discover_toys()} == {"Lightning_ID"}

    await toy.disconnect()
    # Disconnecting makes it discoverable again.
    assert {t.toy_id for t in await builder.discover_toys()} == {
        "Thunder_ID",
        "Lightning_ID",
    }


@pytest.mark.asyncio
async def test_strict_disconnect_makes_toy_discoverable_again(callbacks):
    # strict_disconnect is the path taken by the High-Level/WebSocket remove flow, so it
    # must un-hide the toy just like the non-strict disconnect does.
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_thunder())
    assert {t.toy_id for t in await builder.discover_toys()} == {"Lightning_ID"}

    await toy.strict_disconnect()
    assert {t.toy_id for t in await builder.discover_toys()} == {
        "Thunder_ID",
        "Lightning_ID",
    }


@pytest.mark.asyncio
async def test_continuous_scan_re_emits_when_connection_changes(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    updates = []
    await builder.start_continuous(updates.append)
    assert {t.toy_id for t in updates[-1]} == {"Thunder_ID", "Lightning_ID"}

    toy = await builder.create_toy(_thunder())
    # Connecting re-emits a snapshot that excludes the now-connected toy.
    assert {t.toy_id for t in updates[-1]} == {"Lightning_ID"}

    await toy.disconnect()
    # Disconnecting re-emits with the toy present again.
    assert {t.toy_id for t in updates[-1]} == {"Thunder_ID", "Lightning_ID"}


# ---------------------------------------------------------------------------
# Composite ConnectionBuilder integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_surfaces_and_routes_mock_toys(callbacks):
    # BLE finds nothing; with mock_toys=True the fake toys still come through and get routed to MockConnectionBuilder.
    scanner = MagicMock()
    scanner.discover = AsyncMock(return_value=[])

    builder = ConnectionBuilder(
        *callbacks, logger_name="test", bluetooth_scanner=scanner, mock_toys=True
    )

    toys = await builder.discover_toys()
    assert {t.toy_id for t in toys} == {"Thunder_ID", "Lightning_ID"}

    results = await builder.create_toys(toys)
    assert all(isinstance(r, MockEstimToy) for r in results)


# ---------------------------------------------------------------------------
# High-Level controller integration
# ---------------------------------------------------------------------------


def test_controller_registries_include_mock_estim():
    assert CONTROLLER_BY_BRAND["MockEstimToys"] is MockEstimController
    assert _CONTROLLER_BY_BRAND["MockEstimToys"] is _MockEstimController


def test_brands_mapping_includes_mock_estim():
    from tikal.low_level import BRANDS

    assert BRANDS["MockEstimToys"] == ["Thunder", "Lightning"]


@pytest.mark.asyncio
async def test_high_level_controller_get_information(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_thunder())

    controller = MockEstimController(toy, "test")
    controller.is_connected = True
    assert controller.brand == "MockEstimToys"
    assert controller.max_intensity == 100
    assert controller.intensity_names == ("Stimulation", None)

    captured: dict = {}
    controller.get_information(captured.update)
    await controller.process_communication()  # drains the queued command

    assert captured["Brand"] == "MockEstimToys"
    assert captured["Model"] == "Thunder"
    assert captured["Battery level"] == "77%"

    await toy.disconnect()


@pytest.mark.asyncio
async def test_high_level_controller_set_model_name_is_executed(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_lightning())

    controller = MockEstimController(toy, "test")
    controller.is_connected = True

    captured: list = []
    controller.set_model_name("Thunder", captured.append)
    await controller.process_communication()  # drains the queued command

    assert captured == ["Thunder"]
    assert controller.model_name == "Thunder"

    # An invalid model reports None via the callback and leaves the model unchanged.
    captured.clear()
    controller.set_model_name("Nonexistent", captured.append)
    await controller.process_communication()

    assert captured == [None]
    assert controller.model_name == "Thunder"

    await toy.disconnect()


def test_toy_hub_connects_mock_estim_toy():
    # BLE finds nothing; with mock_toys=True the fake toys still come through ToyHub and connect to a MockEstimController.
    scanner = MagicMock()
    scanner.discover = AsyncMock(return_value=[])
    hub = ToyHub(logger_name="test", bluetooth_scanner=scanner, mock_toys=True)
    try:
        toys = hub.discover_toys_blocking(0.1)
        mock_toys = [t for t in toys if t.brand == "MockEstimToys"]
        assert {t.toy_id for t in mock_toys} == {"Thunder_ID", "Lightning_ID"}

        for td in mock_toys:
            td.model_name = "Thunder" if td.toy_id == "Thunder_ID" else "Lightning"

        controllers = hub.connect_toys_blocking(mock_toys)
        assert all(isinstance(c, MockEstimController) for c in controllers)
        assert {c.model_name for c in controllers} == {"Thunder", "Lightning"}
    finally:
        hub.shutdown()


# ---------------------------------------------------------------------------
# WebSocket controller integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_websocket_hub_adds_mock_estim_toy():
    hub = _ToyHub(mock_toys=True, log_name="test")
    await hub.startup()
    try:
        seen = asyncio.Event()

        def on_update(update):
            if not isinstance(update, Exception) and any(
                d["toy_id"] == "Thunder_ID" for d in update
            ):
                seen.set()

        await hub.start_scan(on_update)
        await asyncio.wait_for(seen.wait(), 5)

        await hub.add("Thunder_ID", "Thunder")
        assert isinstance(hub._toys["Thunder_ID"], _MockEstimController)

        info = await hub.get_all("Thunder_ID", full=False)
        assert info["brand"] == "MockEstimToys"
        assert info["model_name"] == "Thunder"

        await hub.intensity1("Thunder_ID", 50)
        state = await hub.get_state("Thunder_ID")
        assert state["current_intensities"] == [50, 0]
    finally:
        await hub.shutdown()
