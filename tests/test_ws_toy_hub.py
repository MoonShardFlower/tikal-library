"""
Characterization tests for the WebSocket :class:`_ToyHub` driven directly (no websocket layer).

``_ToyHub(mock_toys=True)`` gives a fully in-memory backend (the fictional MockEstimToys brand), so the command
surface can be exercised deterministically. Failure/reconnect paths that the mock backend never hits on its own are
reached by injecting failing methods onto the added controller.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tikal.websocket._toy_controller import _MockEstimController
from tikal.websocket._toy_hub import (
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    ToyAlreadyAddedError,
    ToyConnectionError,
    ToyStatus,
    UndiscoveredToyError,
    UnknownToyError,
    _ToyHub,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast_mock_scan(monkeypatch):
    """Shrink the mock BLE scan interval so hub teardown (which awaits the scan loop) is near-instant."""
    from tikal.mock import mock_lovense

    monkeypatch.setattr(mock_lovense.MockBleakScanner, "_SCAN_INTERVAL", 0.05)


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest_asyncio.fixture
async def bare_hub():
    """A started _ToyHub with no scan running."""
    hub = _ToyHub(mock_toys=True, log_name="test")
    await hub.startup()
    try:
        yield hub
    finally:
        await hub.shutdown()


@pytest_asyncio.fixture
async def hub(bare_hub):
    """A started _ToyHub that has already discovered the two mock-estim toys (ready to ``add``)."""
    discovered = asyncio.Event()

    def on_update(update):
        if not isinstance(update, Exception):
            ids = {d["toy_id"] for d in update}
            if {"Thunder_ID", "Lightning_ID"} <= ids:
                discovered.set()

    await bare_hub.start_scan(on_update)
    await asyncio.wait_for(discovered.wait(), 5)
    return bare_hub


async def _add_thunder(hub) -> _MockEstimController:
    await hub.add("Thunder_ID", "Thunder")
    return hub._toys["Thunder_ID"]


# ---------------------------------------------------------------------------
# Happy-path command surface
# ---------------------------------------------------------------------------


async def test_get_brands(bare_hub):
    brands = await bare_hub.get_brands()
    assert brands["MockEstimToys"] == ["Thunder", "Lightning"]


async def test_add_then_toy_ids_and_status(hub):
    assert await hub.get_toy_ids() == []
    await _add_thunder(hub)
    assert await hub.get_toy_ids() == ["Thunder_ID"]
    assert await hub.get_status("Thunder_ID") == ToyStatus.CONNECTED


async def test_intensity_then_stop(hub):
    await _add_thunder(hub)
    await hub.intensity1("Thunder_ID", 50)
    assert (await hub.get_state("Thunder_ID"))["current_intensities"] == [50, 0]

    await hub.stop("Thunder_ID")
    assert (await hub.get_state("Thunder_ID"))["current_intensities"] == [0, 0]


async def test_intensity2_on_dual_channel(hub):
    await hub.add("Lightning_ID", "Lightning")
    assert await hub.intensity2("Lightning_ID", 30) is True
    assert (await hub.get_state("Lightning_ID"))["current_intensities"] == [0, 30]


async def test_intensity2_on_single_channel_is_noop(hub):
    await _add_thunder(hub)
    # Thunder has a single channel: the command is accepted-but-does-nothing (returns False).
    assert await hub.intensity2("Thunder_ID", 30) is False


async def test_toggle_pause_and_block(hub):
    await _add_thunder(hub)
    await hub.set_pattern("Thunder_ID", [(1000, 10, 0)], True, True)

    await hub.toggle_pause("Thunder_ID")
    assert (await hub.get_state("Thunder_ID"))["is_paused"] is True

    await hub.toggle_block("Thunder_ID")
    state = await hub.get_state("Thunder_ID")
    assert state["is_blocked"] is True and state["is_paused"] is False


async def test_set_paused_and_blocked_idempotent(hub):
    await _add_thunder(hub)
    await hub.set_paused("Thunder_ID", True)
    await hub.set_paused("Thunder_ID", True)  # no-op second call
    assert (await hub.get_state("Thunder_ID"))["is_paused"] is True

    await hub.set_blocked("Thunder_ID", True)
    await hub.set_blocked("Thunder_ID", True)  # no-op second call
    assert (await hub.get_state("Thunder_ID"))["is_blocked"] is True


async def test_intensity_limits_clamp(hub):
    await hub.add("Lightning_ID", "Lightning")
    await hub.set_intensity1_limit("Lightning_ID", 10)
    await hub.set_intensity2_limit("Lightning_ID", 5)

    await hub.intensity1("Lightning_ID", 99)
    await hub.intensity2("Lightning_ID", 99)
    assert (await hub.get_state("Lightning_ID"))["current_intensities"] == [10, 5]


async def test_set_pattern_and_state(hub):
    await _add_thunder(hub)
    await hub.set_pattern("Thunder_ID", [(1000, 10, 0), (500, 0, 0)], True, True)
    assert (await hub.get_state("Thunder_ID"))["pattern"] == [
        (1000, 10, 0),
        (500, 0, 0),
    ]


async def test_get_info_full_and_get_all_full(hub):
    await _add_thunder(hub)
    info = await hub.get_info("Thunder_ID", full=True)
    assert info["brand"] == "MockEstimToys"

    all_data = await hub.get_all("Thunder_ID", full=True)
    assert all_data["model_name"] == "Thunder"
    assert all_data["connection_status"] == ToyStatus.CONNECTED


async def test_get_battery(hub):
    await _add_thunder(hub)
    assert await hub.get_battery("Thunder_ID") == 77


async def test_direct_command(hub):
    await _add_thunder(hub)
    assert await hub.direct_command("Thunder_ID", "DeviceType") == "MockEstim"


async def test_change_rotation_direction_unsupported(hub):
    await _add_thunder(hub)
    assert await hub.change_rotation_direction("Thunder_ID") is False


async def test_set_model_success_fires_model_change(hub):
    changes = []
    hub._on_model_change = lambda change: changes.append(change)
    await _add_thunder(hub)

    await hub.set_model("Thunder_ID", "Lightning")
    assert (await hub.get_all("Thunder_ID", full=False))["model_name"] == "Lightning"
    assert changes and changes[-1]["model_name"] == "Lightning"


async def test_remove_toy(hub):
    await _add_thunder(hub)
    await hub.remove("Thunder_ID")
    assert await hub.get_toy_ids() == []


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


async def test_add_undiscovered_raises(hub):
    with pytest.raises(UndiscoveredToyError):
        await hub.add("Ghost_ID", "Thunder")


async def test_add_invalid_model_raises(hub):
    with pytest.raises(InvalidModelError):
        await hub.add("Thunder_ID", "Bogus")


async def test_add_already_added_raises(hub):
    await _add_thunder(hub)
    with pytest.raises(ToyAlreadyAddedError):
        await hub.add("Thunder_ID", "Thunder")


async def test_set_model_invalid_raises(hub):
    await _add_thunder(hub)
    with pytest.raises(InvalidModelError):
        await hub.set_model("Thunder_ID", "Bogus")


async def test_remove_unknown_raises(hub):
    with pytest.raises(UnknownToyError):
        await hub.remove("nope")


async def test_commands_on_unknown_toy_raise(hub):
    for coro in (
        hub.get_state("nope"),
        hub.get_battery("nope"),
        hub.intensity1("nope", 1),
        hub.stop("nope"),
        hub.get_status("nope"),
    ):
        with pytest.raises(UnknownToyError):
            await coro


# ---------------------------------------------------------------------------
# Discovery lifecycle
# ---------------------------------------------------------------------------


async def test_start_scan_error_wraps_in_discovery_start_error(bare_hub):
    bare_hub._connection_builder.start_continuous = AsyncMock(
        side_effect=RuntimeError("bt off")
    )
    with pytest.raises(DiscoveryStartError):
        await bare_hub.start_scan(lambda _: None)


async def test_apply_discovery_error_delivers_discovery_error(bare_hub):
    delivered = []
    await bare_hub._apply_discovery(RuntimeError("scan died"), delivered.append)
    assert len(delivered) == 1 and isinstance(delivered[0], DiscoveryError)


async def test_set_toy_status_only_fires_on_change(hub):
    events = []
    hub._on_status_change = lambda tid, status: events.append((tid, status))
    await _add_thunder(hub)  # already CONNECTED

    await hub._set_toy_status("Thunder_ID", ToyStatus.CONNECTED)  # no change -> silent
    assert events == []

    await hub._set_toy_status("Thunder_ID", ToyStatus.RECONNECTING)  # change -> fires
    assert events == [("Thunder_ID", ToyStatus.RECONNECTING)]


# ---------------------------------------------------------------------------
# Reconnect / power-off / battery poll (failure-injection)
# ---------------------------------------------------------------------------


async def test_command_failure_triggers_reconnect(hub):
    controller = await _add_thunder(hub)
    # The intensity command fails, but reconnect() and stop() (used by recovery) still work.
    controller.intensity1 = AsyncMock(side_effect=ConnectionError("dropped"))

    with pytest.raises(ToyConnectionError):
        await hub.intensity1("Thunder_ID", 5)

    # Recovery reconnects and returns the toy to CONNECTED without removing it.
    assert await _wait_until(
        lambda: hub._toy_status.get("Thunder_ID") == ToyStatus.CONNECTED
        and not hub._reconnect_tasks
    )
    assert "Thunder_ID" in hub._toys


async def test_reconnect_failure_marks_lost_and_removes(hub):
    controller = await _add_thunder(hub)
    controller.reconnect = AsyncMock(side_effect=ConnectionError("still gone"))

    await hub._on_disconnect("Thunder_ID")

    assert await _wait_until(lambda: "Thunder_ID" not in hub._toys)


async def test_power_off_removes_toy(hub):
    await _add_thunder(hub)
    await hub._on_power_off("Thunder_ID")
    assert await _wait_until(lambda: "Thunder_ID" not in hub._toys)


async def test_poll_batteries_reports_changes(hub):
    updates = []
    hub._on_battery_change = lambda batteries: updates.append(batteries)
    controller = await _add_thunder(hub)
    controller.fetch_and_update_battery = AsyncMock(return_value=55)  # changed from 77

    await hub._poll_all_batteries()
    assert updates == [{"Thunder_ID": 55}]
