"""
Tests for the heartbeat watchdog of the WebSocket server.

The watchdog acts through two callables (set the hold, broadcast an event), so no server is needed here.
We use a recorder and stand-ins for its clients.
"""

import asyncio
import logging
from typing import Any, Callable
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from tikal.websocket._heartbeat_watchdog import (
    CLOSE_HEARTBEAT_OVERDUE,
    _HeartbeatWatchdog,
)

pytestmark = pytest.mark.asyncio

TRIPPED = ["hold timeout", "heartbeat_timeout:timeout"]
RELEASED = ["hold off", "hold_released"]


class _Recorder:
    """
    Stands in for the hub and the clients: records every hold change (as "hold <reasons>" or "hold off") and every
    event, in order.
    """

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.failed_toy_ids: list[str] = []
        self.hold_error: Exception | None = None
        self.hold_gate: asyncio.Event | None = (
            None  # if set, putting the hold on waits for it
        )

    async def set_safety_hold(self, reasons: list[str]) -> list[str]:
        self.actions.append(f"hold {','.join(reasons)}" if reasons else "hold off")
        if reasons and self.hold_gate is not None:
            await self.hold_gate.wait()
        if self.hold_error is not None:
            raise self.hold_error
        return list(self.failed_toy_ids) if reasons else []

    async def broadcast(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, data))
        reason = data.get("reason")
        self.actions.append(f"{name}:{reason}" if reason else name)


@pytest_asyncio.fixture
async def watchdog():
    recorder = _Recorder()
    dog = _HeartbeatWatchdog(
        recorder.set_safety_hold,
        recorder.broadcast,
        logging.getLogger("tikal.tests.watchdog"),
    )
    dog.timeout = 0.2
    dog.check_interval = 0.01
    yield dog, recorder
    dog.shutdown()
    for client_close in list(dog._close_tasks):
        client_close.cancel()
    await asyncio.sleep(0)


def _client() -> Any:
    """A stand-in for a client connection. The watchdog only ever closes it."""
    client = Mock()
    client.close = AsyncMock()
    return client


async def _until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _quiet(dog: _HeartbeatWatchdog) -> None:
    """Let several deadline checks pass."""
    await asyncio.sleep(dog.check_interval * 8)


# ---------------------------------------------------------------------------
# Overdue clients
# ---------------------------------------------------------------------------


async def test_heartbeats_in_time_keep_the_hold_off(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)

    for _ in range(12):  # well past the deadline in total
        await asyncio.sleep(0.03)
        await dog.beat(client)

    assert recorder.actions == []


async def test_an_overdue_client_holds_the_toys_until_its_next_heartbeat(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)

    await _until(lambda: recorder.actions == TRIPPED)
    assert recorder.events[0][1] == {
        "message": "Heartbeat timeout. All toys held until a heartbeat is received again.",
        "reason": "timeout",
        "failed_toy_ids": [],
    }
    await _quiet(dog)
    assert recorder.actions == TRIPPED  # once per overdue client, not on every check

    await dog.beat(client)
    assert recorder.actions == TRIPPED + RELEASED
    assert recorder.events[-1] == (
        "hold_released",
        {"message": "Safety hold released. Toys follow their own state again."},
    )


async def test_the_watchdog_stays_armed_after_a_recovery(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)
    await dog.beat(client)

    # No further heartbeat: the same client goes overdue again.
    await _until(lambda: recorder.actions == TRIPPED + RELEASED + TRIPPED)


async def test_arming_again_counts_as_proof_of_life(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.arm(client)
    assert recorder.actions == TRIPPED + RELEASED
    assert client in dog._last_beat  # still watched


async def test_disarming_while_overdue_ends_the_hold_and_the_checks(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.disarm(client)
    assert recorder.actions == TRIPPED + RELEASED
    assert dog._check_task is None

    await asyncio.sleep(dog.timeout + dog.check_interval * 5)
    assert recorder.actions == TRIPPED + RELEASED  # nobody is watched any more


async def test_every_overdue_client_is_announced_but_the_hold_is_put_on_once(watchdog):
    dog, recorder = watchdog
    first, second = _client(), _client()
    await dog.arm(first)
    await _until(lambda: recorder.actions == TRIPPED)
    await dog.arm(second)
    await _until(lambda: len(recorder.actions) == 3)
    assert recorder.actions == TRIPPED + ["heartbeat_timeout:timeout"]

    await dog.beat(first)
    assert len(recorder.actions) == 3  # the second client is still overdue
    await dog.beat(second)
    assert recorder.actions[3:] == RELEASED


async def test_a_heartbeat_from_a_client_that_did_not_arm_is_ignored(watchdog):
    dog, recorder = watchdog
    armed, stranger = _client(), _client()
    await dog.arm(armed)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.beat(stranger)

    assert recorder.actions == TRIPPED
    assert stranger not in dog._last_beat  # a heartbeat does not arm


# ---------------------------------------------------------------------------
# Disconnected clients and release_hold
# ---------------------------------------------------------------------------


async def test_an_armed_client_that_disconnects_leaves_a_hold_only_release_ends(
    watchdog,
):
    dog, recorder = watchdog
    crashed, other = _client(), _client()
    await dog.arm(crashed)
    await dog.arm(other)

    await dog.client_disconnected(crashed)
    assert recorder.actions == ["hold disconnect", "heartbeat_timeout:disconnect"]
    assert recorder.events[0][1]["message"] == (
        "Heartbeat client disconnected. All toys held until a client sends release_hold."
    )

    await dog.beat(other)  # nobody can vouch for the client that is gone
    assert len(recorder.actions) == 2

    await dog.release()
    assert recorder.actions[2:] == RELEASED


async def test_a_client_that_did_not_arm_can_leave_without_a_hold(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await dog.disarm(client)

    await dog.client_disconnected(client)
    await dog.client_disconnected(_client())

    assert recorder.actions == []
    assert dog._check_task is None


async def test_release_does_not_override_a_client_that_is_still_overdue(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.release()
    assert recorder.actions == TRIPPED

    await dog.beat(client)
    assert recorder.actions == TRIPPED + RELEASED


async def test_the_hold_needs_both_the_release_and_the_overdue_client_back(watchdog):
    dog, recorder = watchdog
    crashed, overdue = _client(), _client()
    await dog.arm(crashed)
    await dog.arm(overdue)
    await _until(lambda: recorder.actions == TRIPPED)  # both in one check: one trip

    await dog.client_disconnected(crashed)
    assert recorder.actions == TRIPPED + [
        "hold disconnect,timeout",
        "heartbeat_timeout:disconnect",
    ]

    await dog.beat(overdue)  # back, but the disconnect hold remains
    assert recorder.actions[4:] == ["hold disconnect"]
    await dog.release()
    assert recorder.actions[5:] == RELEASED


async def test_a_client_overdue_for_the_grace_period_is_given_up(watchdog):
    dog, recorder = watchdog
    dog.grace_period = 0.1
    client = _client()
    await dog.arm(client)

    await _until(lambda: "heartbeat_timeout:disconnect" in recorder.actions)
    # From now on it is a disconnect hold: what keeps the toys held changed, so the toys' state tells
    assert recorder.actions == TRIPPED + [
        "hold disconnect",
        "heartbeat_timeout:disconnect",
    ]
    await _until(lambda: client.close.await_count == 1)
    client.close.assert_awaited_once_with(
        code=CLOSE_HEARTBEAT_OVERDUE, reason="Heartbeat overdue"
    )
    assert CLOSE_HEARTBEAT_OVERDUE == 4000  # part of the protocol
    assert dog.is_kicked(client) is True
    assert client not in dog._last_beat

    await dog.beat(client)  # it is no longer watched, so this proves nothing
    assert len(recorder.actions) == 4
    await dog.release()  # now a disconnect hold, which any client may end
    assert recorder.actions[4:] == RELEASED

    await dog.client_disconnected(client)  # its connection finally closed
    assert dog.is_kicked(client) is False
    assert recorder.actions == (
        TRIPPED + ["hold disconnect", "heartbeat_timeout:disconnect"] + RELEASED
    )


# ---------------------------------------------------------------------------
# Failures and ordering
# ---------------------------------------------------------------------------


async def test_toys_that_could_not_be_stopped_are_named_in_the_event(watchdog):
    dog, recorder = watchdog
    recorder.failed_toy_ids = ["Thunder_ID"]
    await dog.arm(_client())

    await _until(lambda: recorder.actions == TRIPPED)
    assert recorder.events[0][1]["failed_toy_ids"] == ["Thunder_ID"]


async def test_a_failing_hold_neither_stops_the_watchdog_nor_the_events(watchdog):
    dog, recorder = watchdog
    recorder.hold_error = RuntimeError("hub is gone")
    client = _client()
    await dog.arm(client)

    await _until(lambda: recorder.actions == TRIPPED)
    assert recorder.events[0][1]["failed_toy_ids"] == []
    await dog.beat(client)
    assert recorder.actions == TRIPPED + RELEASED

    recorder.hold_error = None
    await _until(lambda: recorder.actions == TRIPPED + RELEASED + TRIPPED)


async def test_a_release_waits_for_a_trip_in_progress(watchdog):
    """heartbeat_timeout must never arrive after the hold_released that ends its hold."""
    dog, recorder = watchdog
    recorder.hold_gate = asyncio.Event()
    client = _client()
    await dog.arm(client)

    leaving = asyncio.create_task(dog.client_disconnected(client))
    await _until(
        lambda: recorder.actions == ["hold disconnect"]
    )  # stopping the toys takes a while
    releasing = asyncio.create_task(dog.release())
    await asyncio.sleep(0.05)
    assert recorder.actions == ["hold disconnect"]

    recorder.hold_gate.set()
    await asyncio.gather(leaving, releasing)
    assert (
        recorder.actions
        == [
            "hold disconnect",
            "heartbeat_timeout:disconnect",
        ]
        + RELEASED
    )


async def test_shutdown_forgets_every_client(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    dog.shutdown()

    assert not dog._last_beat and not dog._overdue and dog._check_task is None
    await asyncio.sleep(dog.timeout + dog.check_interval * 5)
    assert recorder.actions == TRIPPED


async def test_the_hold_of_a_given_up_client_outlasts_another_clients_recovery(
    watchdog,
):
    dog, recorder = watchdog
    dog.grace_period = 0.1
    stuck, other = _client(), _client()
    await dog.arm(stuck)
    await dog.arm(other)
    await _until(lambda: recorder.actions == TRIPPED)
    await dog.beat(other)

    gave_up = TRIPPED + ["hold disconnect", "heartbeat_timeout:disconnect"]
    await _until(lambda: recorder.actions == gave_up)
    # The other client goes overdue as well, and comes back: that must not end the hold the stuck client left.
    overdue_too = gave_up + ["hold disconnect,timeout", "heartbeat_timeout:timeout"]
    await _until(lambda: recorder.actions == overdue_too)
    await dog.beat(other)
    assert recorder.actions == overdue_too + ["hold disconnect"]

    await dog.release()
    assert recorder.actions == overdue_too + ["hold disconnect"] + RELEASED


# ---------------------------------------------------------------------------
# What the hold is on for
# ---------------------------------------------------------------------------


async def test_a_release_with_a_client_still_overdue_leaves_a_timeout_hold(watchdog):
    """The disconnect part ends and the timeout part stays: the toys' state has to tell what release_hold did."""
    dog, recorder = watchdog
    crashed, overdue = _client(), _client()
    await dog.arm(crashed)
    await dog.arm(overdue)
    await _until(lambda: recorder.actions == TRIPPED)
    await dog.client_disconnected(crashed)
    assert recorder.actions[-2:] == [
        "hold disconnect,timeout",
        "heartbeat_timeout:disconnect",
    ]

    await dog.release()
    assert (
        recorder.actions[-1] == "hold timeout"
    )  # still held, now only for the overdue client

    await dog.beat(overdue)
    assert recorder.actions[-2:] == RELEASED


async def test_a_trip_whose_cause_is_gone_puts_no_hold_on(watchdog):
    """
    The deadline check runs the trip as a task, so a heartbeat can arrive before it starts. Putting the hold on then
    would leave it on with nothing to take it off again.
    """
    dog, recorder = watchdog
    client = _client()
    dog._last_beat[client] = 0.0  # armed, without the deadline checks running
    dog._overdue.add(client)
    dog._overdue.discard(client)  # its heartbeat came first

    await dog._trip("timeout", "Heartbeat timeout.")

    assert recorder.actions == []
    dog._last_beat.clear()
