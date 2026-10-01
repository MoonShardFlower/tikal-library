"""
Characterization tests for the core :class:`_ToyHub`, driven directly (without the WebSocket server or the High-Level
wrapper built on it).

``_ToyHub(mock_toys=True)`` gives a fully in-memory backend (the fictional MockEstimToys brand), so the command
surface can be exercised deterministically. Failure/reconnect paths that the mock backend never hits on its own are
reached by injecting failing methods onto the added controller.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tikal._core import (
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    SafetyHoldError,
    ToyAlreadyAddedError,
    ToyConnectionError,
    ToyNotConnectedError,
    ToyStatus,
    UndiscoveredToyError,
    UnknownToyError,
    _MockEstimController,
    _ToyHub,
)
from tikal._private import COMMUNICATION_INTERVAL
from tikal.low_level import ToyData

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
            ids = {d.toy_id for d in update}
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


@pytest.mark.parametrize(
    "command, args, flag",
    [
        ("set_blocked", (True,), "is_blocked"),
        ("toggle_block", (), "is_blocked"),
        ("set_paused", (True,), "is_paused"),
        ("toggle_pause", (), "is_paused"),
    ],
)
async def test_block_and_pause_survive_a_retried_stop(hub, command, args, flag):
    """
    A stop that fails once is retried, and the retry must finish the change instead of flipping it back.

    Regression: the hub used to retry the *toggle*, so a transient Bluetooth error while blocking left the toy
    unblocked, while the command still reported success.
    """
    controller = await _add_thunder(hub)
    await hub.set_pattern("Thunder_ID", [(10_000, 10, 0)], True, True)
    controller._toy.strict_stop = AsyncMock(
        side_effect=[ConnectionError("transient"), True]
    )

    await getattr(hub, command)("Thunder_ID", *args)

    assert (await hub.get_state("Thunder_ID"))[flag] is True
    assert controller._toy.strict_stop.await_count == 2  # the retry re-sent the stop


async def test_safety_hold_mutes_toys_and_leaves_their_state(hub):
    controller = await _add_thunder(hub)
    await hub.set_pattern("Thunder_ID", [(10_000, 10, 0)], True, True)
    assert await _wait_until(lambda: controller.current_intensities == (10, 0))

    assert await hub.set_safety_hold(True) == []
    state = await hub.get_state("Thunder_ID")
    assert state["is_held"] is True and state["current_intensities"] == [0, 0]
    assert state["is_paused"] is False and state["is_blocked"] is False
    assert await hub.intensity1("Thunder_ID", 5) is False
    with pytest.raises(SafetyHoldError):
        await hub.direct_command("Thunder_ID", "DeviceType")
    await asyncio.sleep(COMMUNICATION_INTERVAL * 4)  # playback cannot drive a held toy
    assert controller.current_intensities == (0, 0)

    # Releasing restores nothing: the pattern was never paused, so playback simply drives the toy again.
    assert await hub.set_safety_hold(False) == []
    assert await _wait_until(lambda: controller.current_intensities == (10, 0))


async def test_toy_added_during_safety_hold_is_held(hub):
    await hub.set_safety_hold(True)
    await hub.add("Lightning_ID", "Lightning")
    assert (await hub.get_state("Lightning_ID"))["is_held"] is True

    await hub.set_safety_hold(False)
    assert (await hub.get_state("Lightning_ID"))["is_held"] is False


async def test_safety_hold_reports_a_toy_it_could_not_stop(hub):
    controller = await _add_thunder(hub)
    controller._toy.strict_stop = AsyncMock(side_effect=ConnectionError("radio gone"))

    assert await hub.set_safety_hold(True) == ["Thunder_ID"]
    assert (
        controller.is_held
    )  # held regardless, so nothing drives it once it is reachable again


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
    # A readable traceback, not the repr of a list of lines.
    assert delivered[0].tb.startswith("RuntimeError: scan died")


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


async def test_reconnect_failure_marks_lost_and_removes(hub, short_reconnect_window):
    controller = await _add_thunder(hub)
    controller.reconnect = AsyncMock(side_effect=ConnectionError("still gone"))

    await hub._on_disconnect("Thunder_ID")

    assert await _wait_until(lambda: "Thunder_ID" not in hub._toys)
    assert hub._toy_status.get("Thunder_ID") == ToyStatus.LOST
    assert controller.reconnect.await_count > 1  # retried before giving up


async def test_reconnect_retries_until_the_toy_is_back(hub, short_reconnect_pause):
    """A failed attempt, or a stop that fails right after reconnecting, is retried instead of giving the toy up."""
    controller = await _add_thunder(hub)
    controller.reconnect = AsyncMock(side_effect=[ConnectionError("gone"), None, None])
    controller._toy.strict_stop = AsyncMock(
        side_effect=[ConnectionError("flaky"), True]
    )

    await hub._on_disconnect("Thunder_ID")

    assert await _wait_until(
        lambda: hub._toy_status.get("Thunder_ID") == ToyStatus.CONNECTED
        and not hub._reconnect_tasks
    )
    assert "Thunder_ID" in hub._toys
    assert controller.reconnect.await_count == 3
    assert (
        controller._toy.strict_stop.await_count == 2
    )  # the toy was stopped in the end


async def test_reconnect_window_bounds_a_hanging_attempt(hub, short_reconnect_window):
    """A connect attempt that never returns is cut off when the window closes: the toy cannot stay RECONNECTING."""
    controller = await _add_thunder(hub)

    async def hang() -> None:
        await asyncio.Event().wait()

    controller.reconnect = hang

    await hub._on_disconnect("Thunder_ID")

    assert await _wait_until(lambda: "Thunder_ID" not in hub._toys, timeout=2.0)
    assert hub._toy_status.get("Thunder_ID") == ToyStatus.LOST


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


# ---------------------------------------------------------------------------
# Discovery without a scan, and adding a toy from its ToyData
# ---------------------------------------------------------------------------


async def test_discover_fills_in_cached_model_names(bare_hub):
    bare_hub._toy_cache.update({"Thunder1": "Lightning"})

    found = {data.toy_id: data for data in await bare_hub.discover(0.1)}

    assert found["Thunder_ID"].model_name == "Lightning"  # from the cache
    assert found["Lightning_ID"].model_name == ""  # the default model


async def test_discover_leaves_the_builders_toy_data_alone(bare_hub):
    # A connection builder may hand out the same ToyData again (the continuous scan does), so the model name is
    # filled in on a copy.
    shared = ToyData("Thunder1", "Thunder_ID", "", "MockEstimToys")
    bare_hub._toy_cache.update({"Thunder1": "Lightning"})
    bare_hub._connection_builder.discover_toys = AsyncMock(return_value=[shared])

    found = await bare_hub.discover(0.1)

    assert found[0].model_name == "Lightning"
    assert shared.model_name == ""


async def test_start_scan_delivers_toy_data_with_cached_model_names(bare_hub):
    bare_hub._toy_cache.update({"Thunder1": "Lightning"})
    updates = []
    await bare_hub.start_scan(updates.append)

    def thunder_found():
        return [d for u in updates for d in u if d.toy_id == "Thunder_ID"]

    assert await _wait_until(thunder_found)  # toys show up one scan report at a time
    thunder = thunder_found()[0]
    assert thunder.name == "Thunder1" and thunder.model_name == "Lightning"


async def test_add_toy_data_connects_a_toy_found_without_a_scan(bare_hub):
    ids_changes = []
    bare_hub._on_toy_ids_change = ids_changes.append
    thunder = next(d for d in await bare_hub.discover(0.1) if d.toy_id == "Thunder_ID")
    thunder.model_name = "Thunder"

    await bare_hub.add_toy_data(thunder)

    assert await bare_hub.get_toy_ids() == ["Thunder_ID"]
    assert await bare_hub.get_status("Thunder_ID") == ToyStatus.CONNECTED
    assert ids_changes == [["Thunder_ID"]]
    assert bare_hub._toy_cache.get_model_name("Thunder1") == "Thunder"
    with pytest.raises(ToyAlreadyAddedError):
        await bare_hub.add_toy_data(thunder)


async def test_add_toy_data_rejects_an_invalid_model(bare_hub):
    lightning = next(
        d for d in await bare_hub.discover(0.1) if d.toy_id == "Lightning_ID"
    )
    lightning.model_name = "Bogus"
    with pytest.raises(InvalidModelError):
        await bare_hub.add_toy_data(lightning)
    assert await bare_hub.get_toy_ids() == []  # and it can be tried again
    lightning.model_name = "Lightning"
    await bare_hub.add_toy_data(lightning)
    assert await bare_hub.get_toy_ids() == ["Lightning_ID"]


async def test_injected_bluetooth_classes_are_used():
    from tikal.mock import MockBleakClient, MockBleakScanner

    # Without mock_toys, only the injected classes make the mock Lovense toys appear (and no MockEstimToys).
    hub = _ToyHub(
        log_name="test",
        bluetooth_scanner=MockBleakScanner,
        bluetooth_client=MockBleakClient,
    )
    await hub.startup()
    try:
        names = {data.name for data in await hub.discover(0.1)}
    finally:
        await hub.shutdown()
    assert "LVS-Solace" in names
    assert "Thunder1" not in names


# ---------------------------------------------------------------------------
# Battery on demand
# ---------------------------------------------------------------------------


async def test_fetch_battery_asks_the_toy_and_reports_a_change(hub):
    updates = []
    hub._on_battery_change = updates.append
    controller = await _add_thunder(hub)
    controller._toy.strict_get_battery_level = AsyncMock(side_effect=[55, 55])

    assert await hub.fetch_battery("Thunder_ID") == 55
    assert await hub.get_battery("Thunder_ID") == 55
    assert await hub.fetch_battery("Thunder_ID") == 55  # unchanged: no second event
    assert updates == [{"Thunder_ID": 55}]


# ---------------------------------------------------------------------------
# Commands while a toy is not connected
# ---------------------------------------------------------------------------

_TOY_IO = (
    "strict_intensity1",
    "strict_intensity2",
    "strict_stop",
    "strict_direct_command",
    "strict_get_battery_level",
    "strict_change_rotation_direction",
    "strict_get_status",
    "set_model_name",
)


async def _reconnecting_thunder(hub) -> _MockEstimController:
    """An added Thunder, running a pattern, whose status says it is reconnecting (no reconnect task runs, so nothing changes that back)."""
    controller = await _add_thunder(hub)
    await hub.set_pattern("Thunder_ID", [(10_000, 10, 0)], True, True)
    assert await _wait_until(lambda: controller.current_intensities == (10, 0))
    hub._toy_status["Thunder_ID"] = ToyStatus.RECONNECTING
    for name in _TOY_IO:
        setattr(controller._toy, name, AsyncMock())
    return controller


def _nothing_sent(controller) -> bool:
    return not any(getattr(controller._toy, name).called for name in _TOY_IO)


@pytest.mark.parametrize(
    "command, args",
    [
        ("intensity1", (5,)),
        ("intensity2", (5,)),
        ("direct_command", ("DeviceType",)),
        ("change_rotation_direction", ()),
        ("get_info", (True,)),
        ("get_all", (True,)),
        ("fetch_battery", ()),
        ("set_model", ("Lightning",)),
    ],
)
async def test_commands_that_need_the_toy_are_refused_while_it_reconnects(
    hub, command, args
):
    controller = await _reconnecting_thunder(hub)

    with pytest.raises(ToyNotConnectedError) as refused:
        await getattr(hub, command)("Thunder_ID", *args)

    assert refused.value.status == ToyStatus.RECONNECTING
    assert _nothing_sent(controller)
    assert controller.model_name == "Thunder"
    assert (
        controller.is_paused is False
    )  # a refused intensity does not pause the pattern
    assert not hub._reconnect_tasks  # and no second reconnect is started


@pytest.mark.parametrize(
    "command, args, check",
    [
        ("set_blocked", (True,), lambda s: s["is_blocked"]),
        ("toggle_block", (), lambda s: s["is_blocked"]),
        ("set_paused", (True,), lambda s: s["is_paused"]),
        ("toggle_pause", (), lambda s: s["is_paused"]),
        ("stop", (), lambda s: s["is_paused"]),
        ("set_pattern", ([], True, True), lambda s: s["pattern"] == []),
        ("set_intensity1_limit", (5,), lambda s: s["intensity_limits"][0] == 5),
    ],
)
async def test_state_changes_are_recorded_while_the_toy_reconnects(
    hub, command, args, check
):
    """The state holds even though the command cannot be sent: the reconnect stops the toy before it is used again."""
    controller = await _reconnecting_thunder(hub)

    with pytest.raises(ToyNotConnectedError):
        await getattr(hub, command)("Thunder_ID", *args)

    assert check(await hub.get_state("Thunder_ID"))
    assert _nothing_sent(controller)


async def test_state_changes_that_need_no_command_succeed_while_the_toy_reconnects(
    hub,
):
    controller = await _reconnecting_thunder(hub)

    await hub.set_pattern("Thunder_ID", [(500, 3, 0)], True, True)
    await hub.set_intensity1_limit("Thunder_ID", 50)  # the toy runs below it
    await hub.set_blocked("Thunder_ID", False)  # already unblocked

    state = await hub.get_state("Thunder_ID")
    assert state["pattern"] == [(500, 3, 0)] and state["intensity_limits"][0] == 50
    assert _nothing_sent(controller)


async def test_safety_hold_reports_a_reconnecting_toy_without_trying_to_stop_it(hub):
    controller = await _reconnecting_thunder(hub)

    assert await hub.set_safety_hold(True) == ["Thunder_ID"]
    assert controller.is_held is True
    assert _nothing_sent(controller)


async def test_pausing_a_blocked_toy_unblocks_it_even_if_the_stop_fails(hub):
    """Regression: the block used to be cleared only after the stop got through, leaving the toy paused and blocked."""
    controller = await _add_thunder(hub)
    await hub.set_blocked("Thunder_ID", True)
    controller._toy.strict_stop = AsyncMock(side_effect=ConnectionError("gone"))

    with pytest.raises(ToyConnectionError):
        await hub.set_paused("Thunder_ID", True)

    state = await hub.get_state("Thunder_ID")
    assert state["is_paused"] is True and state["is_blocked"] is False


# ---------------------------------------------------------------------------
# apply_state / send: a state change first, the command that follows it later
# ---------------------------------------------------------------------------


async def test_apply_state_does_not_wait_for_a_command_in_flight(hub):
    # No pattern, so playback sends no stop of its own and every stop counted below is the one from send().
    controller = await _add_thunder(hub)
    cmd_lock = hub._toy_cmd_locks["Thunder_ID"]
    controller._toy.strict_stop = AsyncMock(return_value=True)

    async with cmd_lock:  # a command is in flight on the toy
        # Synchronous: it cannot wait for the lock, so it applies at once.
        needs_stop = hub.apply_state("Thunder_ID", lambda toy: toy.apply_paused(True))
        assert needs_stop is True and controller.is_paused is True

        sending = asyncio.create_task(
            hub.send("Thunder_ID", "stop", lambda toy: toy.stop_output())
        )
        await asyncio.sleep(COMMUNICATION_INTERVAL * 2)
        assert not sending.done()  # the command waits its turn
        controller._toy.strict_stop.assert_not_called()

    assert await asyncio.wait_for(sending, 1) is True
    controller._toy.strict_stop.assert_awaited_once()


async def test_apply_state_works_while_the_toy_reconnects_and_send_is_refused(hub):
    controller = await _reconnecting_thunder(hub)

    assert hub.apply_state("Thunder_ID", lambda toy: toy.apply_blocked(True))
    assert controller.is_blocked is True
    with pytest.raises(ToyNotConnectedError):
        await hub.send("Thunder_ID", "stop", lambda toy: toy.stop_output())
    assert _nothing_sent(controller)


async def test_send_retries_and_reconnects_like_every_command(
    hub, short_reconnect_window
):
    controller = await _add_thunder(hub)
    controller._toy.strict_stop = AsyncMock(side_effect=ConnectionError("gone"))

    with pytest.raises(ToyConnectionError):
        await hub.send("Thunder_ID", "stop", lambda toy: toy.stop_output())

    assert controller._toy.strict_stop.await_count >= 2  # retried once
    assert hub._toy_status["Thunder_ID"] == ToyStatus.RECONNECTING


async def test_apply_state_reports_the_new_state(hub):
    states = []
    hub._on_toy_state_change = states.append
    await _add_thunder(hub)

    hub.apply_state("Thunder_ID", lambda toy: toy.apply_blocked(True))

    assert await _wait_until(lambda: states and states[-1]["is_blocked"] is True)
    with pytest.raises(UnknownToyError):
        hub.apply_state("ghost", lambda toy: toy.apply_blocked(True))


async def test_a_manual_intensity_is_not_undone_by_the_pattern_it_pauses(hub):
    """Regression: the next playback tick used to send the stop for the paused pattern, dropping the manual level."""
    controller = await _add_thunder(hub)
    await hub.set_pattern("Thunder_ID", [(60_000, 10, 0)], True, True)
    assert await _wait_until(lambda: controller.current_intensities == (10, 0))

    await hub.intensity1("Thunder_ID", 50)
    await asyncio.sleep(COMMUNICATION_INTERVAL * 4)  # a few playback ticks

    assert controller.current_intensities == (50, 0)
    await hub.set_paused("Thunder_ID", False)  # the pattern takes over again
    assert await _wait_until(lambda: controller.current_intensities == (10, 0))
