import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tikal.high_level import ToyHub
from tikal.high_level.toy_controller import CONTROLLER_BY_BRAND, MockEstimController
from tikal.low_level import (
    BadModelError,
    ConnectionBuilder,
    InvalidModelError,
    MockConnectionBuilder,
    MockEstimToy,
    MockTransport,
    Toy,
    ToyData,
)
from tikal.websocket._toy_controller import _CONTROLLER_BY_BRAND, _MockEstimController
from tikal.websocket._toy_hub import BadModelError as WsBadModelError
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


def _spy_on_commands(toy: MockEstimToy) -> list[str]:
    """
    Record every command the toy puts on the wire. Returns the (live) list of commands.

    Commands are captured before ``_send_command`` appends the ``;`` terminator, so they appear as e.g. "Channel1:0".
    """
    sent: list[str] = []
    original_send = toy._send_command

    async def spy(command: str) -> None:
        sent.append(command)
        await original_send(command)

    toy._send_command = spy
    return sent


@pytest.mark.asyncio
async def test_model_switch_releases_the_dropped_channel_and_keeps_the_other(callbacks):
    # Regression: Lightning -> Thunder used to leave channel 2 energised. Thunder has no intensity2
    # command, so nothing could ever switch it off again - not even stop().
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_lightning())
    await toy.intensity1(80)
    await toy.intensity2(80)
    assert toy.current_intensities == (80, 80)

    sent = _spy_on_commands(toy)
    await toy.set_model_name("Thunder")

    assert toy.model_name == "Thunder"
    # Channel2 was released through Lightning's command, while it was still addressable.
    assert "Channel2:0" in sent
    # Channel1 is shared by both models, so it keeps its level and is never zeroed.
    assert "Channel1:0" not in sent
    assert toy.current_intensities == (80, 0)


@pytest.mark.asyncio
async def test_model_switch_validates_the_new_models_commands(callbacks):
    # Regression: the intensity replay used to run before the new model was assigned, so it exercised
    # the *old* model's channels and could never surface a BadModelError for the new one.
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_thunder())

    sent = _spy_on_commands(toy)
    await toy.set_model_name("Lightning")

    assert toy.model_name == "Lightning"
    # Lightning's second channel must actually have been exercised by the validation replay.
    assert "Channel2:0" in sent


@pytest.mark.asyncio
async def test_model_switch_is_refused_when_a_dropped_channel_cannot_be_reached(
    callbacks,
):
    # An *undeliverable* release rolls the change back: while Lightning is still in force, stop() can
    # de-energise Channel2, so keeping the old model beats committing to one that cannot reach it.
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_lightning())
    await toy.intensity1(80)
    await toy.intensity2(80)

    original_send = toy._send_command

    async def link_dies_on_channel2(command: str) -> None:
        if command.startswith("Channel2"):
            raise ConnectionError("link down")
        await original_send(command)

    toy._send_command = link_dies_on_channel2
    with pytest.raises(ConnectionError):
        await toy.set_model_name("Thunder")

    # Model and tracked levels left alone, so stop() can still reach channel 2 and the caller can retry.
    assert toy.model_name == "Lightning"
    assert toy.current_intensities == (80, 80)


@pytest.mark.asyncio
async def test_model_switch_completes_when_a_dropped_channel_refuses_the_release(
    callbacks,
):
    # A *refused* release must not block the change: a command the device refuses is one the old model
    # could not have used either, so rolling back would preserve no way of reaching the channel.
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_lightning())
    await toy.intensity1(80)
    await toy.intensity2(80)

    # Make the simulated device answer Channel2 with something other than OK: delivered and answered,
    # but refused, which is what the toy layer turns into UnexpectedToyResponse.
    device = toy._transport
    original_respond = device._respond

    def refuse_channel2(command: str) -> str | None:
        if command.startswith("Channel2"):
            return "ERR"
        return original_respond(command)

    device._respond = refuse_channel2
    sent = _spy_on_commands(toy)

    await toy.set_model_name("Thunder")

    # The release was attempted and refused, and the change went through anyway.
    assert "Channel2:0" in sent
    assert toy.model_name == "Thunder"
    assert toy.current_intensities == (80, 0)


@pytest.mark.asyncio
async def test_setting_the_same_model_keeps_the_toy_running(callbacks):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    toy = await builder.create_toy(_thunder())
    await toy.intensity1(60)

    sent = _spy_on_commands(toy)
    await toy.set_model_name("thunder")  # case-insensitive

    assert toy.model_name == "Thunder"
    assert toy.current_intensities == (60, 0)
    # Only the validating re-send of the level the device already holds.
    assert sent == ["Channel1:60"]


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


# ---------------------------------------------------------------------------
# The "no toy runs under an unsupported model" invariant
# ---------------------------------------------------------------------------
#
# The release logic in Toy.set_model_name leans on this invariant: a non-zero tracked level is taken as proof that the
# toy accepts the current model's command for that capability, which is what lets a model change send the old command
# one last time without risking a refusal. That only holds if a toy can never end up live on a model whose commands the
# toy refuses. These tests pin that down on every path that assigns a model name, using a device that answers Channel2
# with something other than OK - i.e. a device that is really a Thunder while being labelled a Lightning.


def _no_ble_scanner() -> MagicMock:
    """A BLE scanner that finds nothing, so only the mock brand's fake devices come through."""
    scanner = MagicMock()
    scanner.discover = AsyncMock(return_value=[])
    return scanner


_REAL_RESPOND = MockTransport._respond


def _refuses_channel2(self, command: str):
    """Simulate a device with no second channel: the command is delivered and answered, but not with OK."""
    if command.startswith("Channel2"):
        return "ERR"
    return _REAL_RESPOND(self, command)


@pytest.fixture
def channel2_refusing_device():
    """Patch every MockTransport for the duration of a test so Channel2 commands are refused."""
    with patch.object(MockTransport, "_respond", _refuses_channel2):
        yield


@pytest.mark.asyncio
async def test_connect_under_unsupported_model_hands_out_no_toy(
    callbacks, channel2_refusing_device
):
    builder = MockConnectionBuilder(*callbacks, logger_name="test")
    result = await builder.create_toy(_lightning())
    # An exception, never a usable Toy: the caller cannot drive the device on a model it does not support.
    assert not isinstance(result, Toy)
    assert isinstance(result, BadModelError)


@pytest.mark.asyncio
async def test_ws_hub_add_under_unsupported_model_registers_nothing(
    channel2_refusing_device,
):
    hub = _ToyHub(mock_toys=True, log_name="test")
    await hub.startup()
    try:
        await hub.start_scan(lambda update: None)
        await asyncio.sleep(0.05)
        with pytest.raises(WsBadModelError):
            await hub.add("Lightning_ID", "Lightning")
        assert await hub.get_toy_ids() == []
    finally:
        await hub.shutdown()


@pytest.mark.asyncio
async def test_ws_hub_set_model_to_unsupported_model_keeps_the_working_one(
    channel2_refusing_device,
):
    hub = _ToyHub(mock_toys=True, log_name="test")
    await hub.startup()
    try:
        await hub.start_scan(lambda update: None)
        await asyncio.sleep(0.05)
        # Thunder only drives Channel1, so it is supported by this device.
        await hub.add("Lightning_ID", "Thunder")
        await hub.intensity1("Lightning_ID", 40)

        with pytest.raises(WsBadModelError):
            await hub.set_model("Lightning_ID", "Lightning")

        state = await hub.get_all("Lightning_ID", full=False)
        assert state["model_name"] == "Thunder"
        # The failed change did not disturb the running device either.
        assert state["current_intensities"] == [40, 0]
    finally:
        await hub.shutdown()


def test_high_level_update_model_name_to_unsupported_model_is_rejected(
    channel2_refusing_device,
):
    hub = ToyHub(
        logger_name="test", bluetooth_scanner=_no_ble_scanner(), mock_toys=True
    )
    try:
        toys = hub.discover_toys_blocking(1.0)
        td = next(t for t in toys if t.toy_id == "Lightning_ID")
        td.model_name = "Thunder"
        controller = hub.connect_toys_blocking([td])[0]
        assert not isinstance(controller, BaseException)

        result = hub.update_model_name("Lightning_ID", "Lightning")

        assert isinstance(result, BadModelError)
        assert controller.model_name == "Thunder"
    finally:
        hub.shutdown()


def test_high_level_queued_set_model_name_to_unsupported_model_is_rejected(
    channel2_refusing_device,
):
    hub = ToyHub(
        logger_name="test", bluetooth_scanner=_no_ble_scanner(), mock_toys=True
    )
    try:
        toys = hub.discover_toys_blocking(1.0)
        td = next(t for t in toys if t.toy_id == "Lightning_ID")
        td.model_name = "Thunder"
        controller = hub.connect_toys_blocking([td])[0]

        results: list = []
        controller.set_model_name("Lightning", results.append)
        deadline = time.monotonic() + 5.0
        while not results and time.monotonic() < deadline:
            time.sleep(0.02)

        # The queued command reports failure via the callback, and the toy keeps its supported model.
        assert results == [None]
        assert controller.model_name == "Thunder"
    finally:
        hub.shutdown()
