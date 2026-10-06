"""
Tests for the heartbeat watchdog of the WebSocket server.

The watchdog acts through two callables (block every toy, broadcast an event), so no server is needed here.
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

TRIPPED = ["block", "heartbeat_timeout:timeout"]
VANISHED = ["block", "heartbeat_timeout:disconnect"]


class _Recorder:
    """Stands in for the hub and the clients: records every time the toys get blocked and every event, in order."""

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.blocked_toy_ids: list[str] = ["Thunder_ID"]
        self.failed_toy_ids: list[str] = []
        self.block_error: Exception | None = None
        self.block_gate: asyncio.Event | None = None  # if set, blocking waits for it

    async def block_all(self) -> tuple[list[str], list[str]]:
        self.actions.append("block")
        if self.block_gate is not None:
            await self.block_gate.wait()
        if self.block_error is not None:
            raise self.block_error
        return list(self.blocked_toy_ids), list(self.failed_toy_ids)

    async def broadcast(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, data))
        reason = data.get("reason")
        self.actions.append(f"{name}:{reason}" if reason else name)


@pytest_asyncio.fixture
async def watchdog():
    recorder = _Recorder()
    dog = _HeartbeatWatchdog(
        recorder.block_all,
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


async def test_heartbeats_in_time_block_nothing(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)

    for _ in range(12):  # well past the deadline in total
        await asyncio.sleep(0.03)
        await dog.beat(client)

    assert recorder.actions == []


async def test_an_overdue_client_blocks_the_toys_once(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)

    await _until(lambda: recorder.actions == TRIPPED)
    assert recorder.events[0][1] == {
        "message": "Heartbeat timeout. All toys were blocked.",
        "reason": "timeout",
        "blocked_toy_ids": ["Thunder_ID"],
        "failed_toy_ids": [],
    }
    await _quiet(dog)
    assert recorder.actions == TRIPPED  # once per overdue client, not on every check


async def test_a_client_that_is_back_lifts_nothing(watchdog):
    """Unblocking is up to the user: the client's next heartbeat neither unblocks nor announces anything."""
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.beat(client)
    assert recorder.actions == TRIPPED
    assert not dog._overdue


async def test_the_watchdog_stays_armed_after_a_recovery(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)
    await dog.beat(client)

    # No further heartbeat: the same client goes overdue again, and trips the watchdog again.
    await _until(lambda: recorder.actions == TRIPPED + TRIPPED)


async def test_arming_again_counts_as_proof_of_life(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.arm(client)
    assert not dog._overdue
    assert client in dog._last_beat  # still watched
    assert recorder.actions == TRIPPED


async def test_disarming_while_overdue_ends_the_checks(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.disarm(client)
    assert dog._check_task is None

    await asyncio.sleep(dog.timeout + dog.check_interval * 5)
    assert recorder.actions == TRIPPED  # nobody is watched any more


async def test_every_overdue_client_is_announced(watchdog):
    dog, recorder = watchdog
    first, second = _client(), _client()
    await dog.arm(first)
    await _until(lambda: recorder.actions == TRIPPED)
    await dog.arm(second)
    await _until(lambda: len(recorder.actions) == 4)
    assert recorder.actions == TRIPPED + TRIPPED

    await dog.beat(first)
    await dog.beat(second)
    assert len(recorder.actions) == 4


async def test_a_heartbeat_from_a_client_that_did_not_arm_is_ignored(watchdog):
    dog, recorder = watchdog
    armed, stranger = _client(), _client()
    await dog.arm(armed)
    await _until(lambda: recorder.actions == TRIPPED)

    await dog.beat(stranger)

    assert recorder.actions == TRIPPED
    assert stranger not in dog._last_beat  # a heartbeat does not arm


# ---------------------------------------------------------------------------
# Disconnected clients
# ---------------------------------------------------------------------------


async def test_an_armed_client_that_disconnects_blocks_the_toys(watchdog):
    dog, recorder = watchdog
    crashed, other = _client(), _client()
    await dog.arm(crashed)
    await dog.arm(other)

    await dog.client_disconnected(crashed)
    assert recorder.actions == VANISHED
    assert recorder.events[0][1]["message"] == (
        "Heartbeat client disconnected. All toys were blocked."
    )

    await dog.beat(other)  # nothing to lift: the user unblocks
    assert recorder.actions == VANISHED


async def test_a_client_that_did_not_arm_can_leave_without_a_block(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await dog.disarm(client)

    await dog.client_disconnected(client)
    await dog.client_disconnected(_client())

    assert recorder.actions == []
    assert dog._check_task is None


async def test_a_client_overdue_for_the_grace_period_is_closed(watchdog):
    """Its connection is closed, so it can send no stale commands. The toys were blocked already: no new trip."""
    dog, recorder = watchdog
    dog.grace_period = 0.1
    client = _client()
    await dog.arm(client)

    await _until(lambda: client.close.await_count == 1)
    client.close.assert_awaited_once_with(
        code=CLOSE_HEARTBEAT_OVERDUE, reason="Heartbeat overdue"
    )
    assert CLOSE_HEARTBEAT_OVERDUE == 4000  # part of the protocol
    assert dog.is_kicked(client) is True
    assert client not in dog._last_beat
    assert recorder.actions == TRIPPED

    await dog.beat(client)  # it is no longer watched, so this proves nothing
    await dog.client_disconnected(
        client
    )  # its connection finally closed: no longer armed, so no trip
    assert dog.is_kicked(client) is False
    assert recorder.actions == TRIPPED


# ---------------------------------------------------------------------------
# Failures and ordering
# ---------------------------------------------------------------------------


async def test_toys_blocked_and_not_stopped_are_named_in_the_event(watchdog):
    dog, recorder = watchdog
    recorder.blocked_toy_ids = ["Thunder_ID", "Lightning_ID"]
    recorder.failed_toy_ids = ["Thunder_ID"]
    await dog.arm(_client())

    await _until(lambda: recorder.actions == TRIPPED)
    assert recorder.events[0][1]["blocked_toy_ids"] == ["Thunder_ID", "Lightning_ID"]
    assert recorder.events[0][1]["failed_toy_ids"] == ["Thunder_ID"]


async def test_a_failing_block_neither_stops_the_watchdog_nor_the_events(watchdog):
    dog, recorder = watchdog
    recorder.block_error = RuntimeError("hub is gone")
    client = _client()
    await dog.arm(client)

    await _until(lambda: recorder.actions == TRIPPED)
    assert recorder.events[0][1]["blocked_toy_ids"] == []
    assert recorder.events[0][1]["failed_toy_ids"] == []
    await dog.beat(client)

    recorder.block_error = None
    await _until(lambda: recorder.actions == TRIPPED + TRIPPED)


async def test_trips_are_carried_out_one_after_the_other(watchdog):
    """Blocking takes a while: a second trip waits, so the events arrive in the order the toys were blocked."""
    dog, recorder = watchdog
    recorder.block_gate = asyncio.Event()
    first, second = _client(), _client()
    await dog.arm(first)
    await dog.arm(second)

    leaving = asyncio.create_task(dog.client_disconnected(first))
    await _until(
        lambda: recorder.actions == ["block"]
    )  # stopping the toys takes a while
    also_leaving = asyncio.create_task(dog.client_disconnected(second))
    await asyncio.sleep(0.05)
    assert recorder.actions == ["block"]

    recorder.block_gate.set()
    await asyncio.gather(leaving, also_leaving)
    assert recorder.actions == VANISHED + VANISHED


async def test_shutdown_forgets_every_client(watchdog):
    dog, recorder = watchdog
    client = _client()
    await dog.arm(client)
    await _until(lambda: recorder.actions == TRIPPED)

    dog.shutdown()

    assert not dog._last_beat and not dog._overdue and dog._check_task is None
    await asyncio.sleep(dog.timeout + dog.check_interval * 5)
    assert recorder.actions == TRIPPED


async def test_a_timeout_whose_client_is_back_already_blocks_nothing(watchdog):
    """
    The deadline check runs the trip as a task, so a heartbeat can arrive before it starts. Blocking the toys then
    would block them for a client that is fine.
    """
    dog, recorder = watchdog
    client = _client()
    dog._last_beat[client] = 0.0  # armed, without the deadline checks running
    dog._overdue.add(client)
    dog._overdue.discard(client)  # its heartbeat came first

    await dog._trip("timeout", "Heartbeat timeout.")

    assert recorder.actions == []
    dog._last_beat.clear()
