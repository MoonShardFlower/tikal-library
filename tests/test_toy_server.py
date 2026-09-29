"""
Integration tests for the WebSocket :class:`ToyServer`.

Each test starts a real ``ToyServer`` (with ``mock_toys=True`` for a deterministic backend) and drives it over a real
websocket connection, testing the JSON dispatch, error mapping, scan subscription, per-client intensity limits,
the heartbeat watchdog, and shutdown.
"""

import asyncio
import contextlib
import json
import socket
from typing import Callable
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import websockets

from tikal._core import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    ToyConnectionError,
    ToyStatus,
    UnavailableToyError,
)
from tikal._private import COMMUNICATION_INTERVAL
from tikal.low_level import ToyData
from tikal.websocket.toy_server import InsecureBindError, ToyServer

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    """Grab a currently free localhost TCP port."""
    sock = socket.socket()
    try:
        sock.bind(("localhost", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _Client:
    """
    Thin JSON helper over a raw websocket.

    ``request`` sends a command and returns the matching reply, buffering any broadcast events that arrive in the
    meantime so ``wait_event`` can find them.
    """

    def __init__(self, ws: websockets.ClientConnection):
        self.raw = ws
        self._counter = 0
        self.events: list[dict] = []

    async def request(
        self, command: str, data: dict | None = None, timeout: float = 5.0
    ) -> dict:
        self._counter += 1
        req_id = str(self._counter)
        await self.raw.send(
            json.dumps({"request": command, "id": req_id, "data": data or {}})
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"no reply to {command!r} within {timeout}s")
            msg = json.loads(await asyncio.wait_for(self.raw.recv(), timeout=remaining))
            if "event" in msg:
                self.events.append(msg)
                continue
            assert msg.get("id") == req_id, f"reply id {msg.get('id')} != {req_id}"
            return msg

    async def wait_event(self, name: str, timeout: float = 5.0) -> dict:
        for event in self.events:
            if event.get("event") == name:
                return event
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"event {name!r} not seen within {timeout}s")
            msg = json.loads(await asyncio.wait_for(self.raw.recv(), timeout=remaining))
            if "event" in msg:
                self.events.append(msg)
                if msg["event"] == name:
                    return msg

    async def wait_scan(
        self, predicate: Callable[[set[str]], bool], timeout: float = 5.0
    ) -> dict:
        """Wait for a ``scan_update`` whose set of discovered toy_ids satisfies ``predicate``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for event in self.events:
                if event.get("event") == "scan_update":
                    ids = {d["toy_id"] for d in event["data"].get("discovered", [])}
                    if predicate(ids):
                        return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"no matching scan_update within {timeout}s")
            msg = json.loads(await asyncio.wait_for(self.raw.recv(), timeout=remaining))
            if "event" in msg:
                self.events.append(msg)

    async def wait_scan_update(self, toy_id: str, timeout: float = 5.0) -> dict:
        """Wait for a ``scan_update`` whose ``discovered`` list contains ``toy_id``."""
        return await self.wait_scan(lambda ids: toy_id in ids, timeout)


@pytest_asyncio.fixture
async def ws_server():
    """
    Start a mock-backed ToyServer and yield ``(server, connect)``.

    ``connect`` is a factory returning a connected :class:`_Client`. Idle-shutdown is pushed far out so it doesn't fire
    mid-test; is pushed far out so it never fires mid-test; teardown closes every client and shuts the server down.
    """
    port = _free_port()
    server = ToyServer(
        host="localhost",
        port=port,
        mock_toys=True,
        idle_shutdown_delay=3600.0,
        log_name="test_ws",
    )
    serve_task = asyncio.create_task(server.serve())
    for _ in range(250):  # wait until the server is actually listening
        if server._server is not None:
            break
        await asyncio.sleep(0.02)
    else:
        serve_task.cancel()
        raise RuntimeError("ToyServer did not start listening")

    opened: list[websockets.ClientConnection] = []

    async def connect() -> _Client:
        ws = await websockets.connect(f"ws://localhost:{port}")
        opened.append(ws)
        return _Client(ws)

    try:
        yield server, connect
    finally:
        for ws in opened:
            with contextlib.suppress(Exception):
                await ws.close()
        await asyncio.sleep(0.05)  # let server-side disconnect handlers run
        with contextlib.suppress(Exception):
            await server._shutdown()
        for task in (serve_task, server._shutdown_task, server._heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


async def _scan_and_add(client: _Client, toy_id: str, model: str) -> dict:
    """Start a scan, wait for the mock toy to surface, then add it."""
    await client.request("start_scan")
    await client.wait_scan_update(toy_id)
    reply = await client.request("add", {"toy_id": toy_id, "model_name": model})
    assert reply["success"] is True
    return reply


# ---------------------------------------------------------------------------
# Basic reads
# ---------------------------------------------------------------------------


async def test_get_brands_lists_supported_brands(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("get_brands")
    assert reply["reply"] == "get_brands"
    assert reply["success"] is True
    brands = reply["data"]["brands"]
    assert "Lovense" in brands
    assert brands["MockEstimToys"] == ["Thunder", "Lightning"]


async def test_get_toy_ids_starts_empty(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("get_toy_ids")
    assert reply["success"] is True
    assert reply["data"]["toy_ids"] == []


# ---------------------------------------------------------------------------
# Error handling / dispatch
# ---------------------------------------------------------------------------


async def test_malformed_envelope_is_rejected(ws_server):
    _, connect = ws_server
    client = await connect()
    await client.raw.send("this is not json")
    msg = json.loads(await asyncio.wait_for(client.raw.recv(), 5))
    assert msg["success"] is False
    assert msg["data"]["error"] == "Malformed Request"


async def test_unknown_command(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("does_not_exist")
    assert reply["success"] is False
    assert reply["data"]["error"] == "Unknown Command"


async def test_invalid_data_missing_toy_id(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("get_state", {})  # toy_id is required
    assert reply["success"] is False
    assert reply["data"]["error"] == "Invalid Data"


async def test_unknown_toy(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("get_battery", {"toy_id": "nope"})
    assert reply["success"] is False
    assert reply["data"]["error"] == "Unknown Toy"


async def test_add_undiscovered_toy(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("add", {"toy_id": "Ghost_ID", "model_name": "Thunder"})
    assert reply["success"] is False
    assert reply["data"]["error"] == "Undiscovered Toy"


async def test_add_invalid_model(ws_server):
    _, connect = ws_server
    client = await connect()
    await client.request("start_scan")
    await client.wait_scan_update("Thunder_ID")
    reply = await client.request("add", {"toy_id": "Thunder_ID", "model_name": "Bogus"})
    assert reply["success"] is False
    assert reply["data"]["error"] == "Invalid Model"


# ---------------------------------------------------------------------------
# Scan subscription
# ---------------------------------------------------------------------------


async def test_scan_surfaces_mock_toys(ws_server):
    _, connect = ws_server
    client = await connect()
    ack = await client.request("start_scan")
    assert ack["success"] is True and ack["data"]["ack"] is True
    update = await client.wait_scan_update("Thunder_ID")
    ids = {d["toy_id"] for d in update["data"]["discovered"]}
    assert {"Thunder_ID", "Lightning_ID"} <= ids


async def test_connected_toy_drops_from_scan(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    # Once connected, Thunder stops appearing in scan results; Lightning still does.
    event = await client.wait_scan(
        lambda ids: "Thunder_ID" not in ids and "Lightning_ID" in ids
    )
    assert "Thunder_ID" not in {d["toy_id"] for d in event["data"]["discovered"]}


async def test_removed_toy_reappears_in_scan(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.wait_scan(
        lambda ids: "Thunder_ID" not in ids
    )  # hidden while connected

    # Removing the toy (which disconnects via strict_disconnect) must re-advertise it.
    client.events.clear()  # only consider scan updates emitted after removal
    remove = await client.request("remove", {"toy_id": "Thunder_ID"})
    assert remove["success"] is True and remove["data"]["ack"] is True

    event = await client.wait_scan(lambda ids: "Thunder_ID" in ids)
    assert "Thunder_ID" in {d["toy_id"] for d in event["data"]["discovered"]}


# ---------------------------------------------------------------------------
# Add / control / state
# ---------------------------------------------------------------------------


async def test_add_control_and_state_flow(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    ids = await client.request("get_toy_ids")
    assert ids["data"]["toy_ids"] == ["Thunder_ID"]

    status = await client.request("get_connection_status", {"toy_id": "Thunder_ID"})
    assert status["data"]["connection_status"] == "connected"

    await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})
    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [50, 0]

    all_info = await client.request("get_all", {"toy_id": "Thunder_ID", "full": False})
    assert all_info["data"]["brand"] == "MockEstimToys"
    assert all_info["data"]["model_name"] == "Thunder"


async def test_add_emits_toy_ids_changed_event(ws_server):
    _, connect = ws_server
    client = await connect()
    await client.request("start_scan")
    await client.wait_scan_update("Thunder_ID")
    await client.request("add", {"toy_id": "Thunder_ID", "model_name": "Thunder"})
    event = await client.wait_event("toy_ids_changed")
    assert "Thunder_ID" in event["data"]["toy_ids"]


async def test_remove_toy(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    reply = await client.request("remove", {"toy_id": "Thunder_ID"})
    assert reply["success"] is True and reply["data"]["ack"] is True

    ids = await client.request("get_toy_ids")
    assert ids["data"]["toy_ids"] == []


async def test_set_pattern_updates_state(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    reply = await client.request(
        "set_pattern",
        {
            "toy_id": "Thunder_ID",
            "pattern": [[1000, 10, 0], [500, 0, 0]],
            "wraparound": True,
            "reset_time": True,
        },
    )
    assert reply["success"] is True

    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["pattern"] == [[1000, 10, 0], [500, 0, 0]]


# ---------------------------------------------------------------------------
# Per-client intensity limits
# ---------------------------------------------------------------------------


async def test_intensity_limit_clamps(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    await client.request("set_intensity1_limit", {"toy_id": "Thunder_ID", "limit": 10})
    await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})

    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [10, 0]


async def test_second_clients_limit_reins_in_a_running_toy(ws_server):
    """A limit set by a second client must apply to what the toy is already doing, not just to later commands."""
    _, connect = ws_server
    driver = await connect()
    supervisor = await connect()
    await _scan_and_add(driver, "Thunder_ID", "Thunder")

    await driver.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 90})
    state = await driver.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [90, 0]

    # The supervisor joins and imposes a ceiling. The toy must come down immediately.
    await supervisor.request(
        "set_intensity1_limit", {"toy_id": "Thunder_ID", "limit": 5}
    )
    state = await driver.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["intensity_limits"][0] == 5
    assert state["data"]["current_intensities"] == [5, 0]


async def test_limit_applies_to_an_already_running_pattern(ws_server):
    """Regression: limits used to be baked into the pattern at set time, so a later limit did nothing."""
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    await client.request(
        "set_pattern",
        {
            "toy_id": "Thunder_ID",
            "pattern": [[60_000, 90, 0]],
            "wraparound": True,
            "reset_time": True,
        },
    )
    await client.request("set_paused", {"toy_id": "Thunder_ID", "pause": False})
    await asyncio.sleep(COMMUNICATION_INTERVAL * 3)
    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [90, 0]

    await client.request("set_intensity1_limit", {"toy_id": "Thunder_ID", "limit": 7})
    await asyncio.sleep(COMMUNICATION_INTERVAL * 3)

    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [7, 0]
    # The pattern itself is untouched, so withdrawing the limit restores its own values.
    assert state["data"]["pattern"] == [[60_000, 90, 0]]

    await client.request(
        "set_intensity1_limit", {"toy_id": "Thunder_ID", "limit": None}
    )
    await asyncio.sleep(COMMUNICATION_INTERVAL * 3)
    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [90, 0]


# ---------------------------------------------------------------------------
# Heartbeat watchdog / safety hold
# ---------------------------------------------------------------------------


def _speed_up_watchdog(server: ToyServer) -> None:
    """Shrink the heartbeat deadline so a timeout fires within a few hundred milliseconds."""
    server._heartbeat_timeout = 0.3
    server._heartbeat_check_interval = 0.05


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _state(client: _Client, toy_id: str = "Thunder_ID") -> dict:
    return (await client.request("get_state", {"toy_id": toy_id}))["data"]


async def _play(client: _Client, toy_id: str = "Thunder_ID", level: int = 100) -> None:
    """Start a long, constant pattern on the toy."""
    await client.request(
        "set_pattern",
        {
            "toy_id": toy_id,
            "pattern": [[10_000, level, 0]],
            "wraparound": True,
            "reset_time": True,
        },
    )
    await client.request("set_paused", {"toy_id": toy_id, "pause": False})


async def test_heartbeat_timeout_stops_toys(ws_server):
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})

    # Enable the heartbeat but never send one -> the watchdog must stop the toy.
    await client.request("enable_heartbeat", {"enable": True})
    event = await client.wait_event("heartbeat_timeout")
    assert event["success"] is True

    state = await _state(client)
    assert state["current_intensities"] == [0, 0]


async def test_heartbeat_timeout_holds_toys_until_the_client_returns(ws_server):
    """
    A timeout must not silently disarm the watchdog: the overdue client stays watched and the toys stay held.

    The hold only mutes the output. The pattern keeps running underneath, block and pause are untouched, and once a
    heartbeat ends the hold, the pattern drives the toy again.
    """
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await _play(client)
    toy = server._hub._toys["Thunder_ID"]
    assert await _wait_until(lambda: toy.current_intensities == (100, 0))
    await client.request("enable_heartbeat", {"enable": True})

    event = await client.wait_event("heartbeat_timeout")
    assert event["data"]["reason"] == "timeout"
    assert event["data"]["failed_toy_ids"] == []
    state = await _state(client)
    assert state["is_held"] is True
    assert state["is_blocked"] is False and state["is_paused"] is False
    assert state["current_intensities"] == [0, 0]

    # Nothing gets the toy moving while the client is overdue.
    reply = await client.request(
        "intensity1", {"toy_id": "Thunder_ID", "intensity": 50}
    )
    assert reply["data"]["ack"] is False
    await asyncio.sleep(COMMUNICATION_INTERVAL * 4)
    assert toy.current_intensities == (0, 0)
    assert server._heartbeat_timed_out, "watchdog disarmed itself after firing"

    await client.request("heartbeat")
    await client.wait_event("hold_released")
    assert await _wait_until(lambda: toy.current_intensities == (100, 0))
    assert (await _state(client))["is_held"] is False


async def test_hold_leaves_block_and_pause_untouched(ws_server):
    """
    A toy driven by hand (a manual intensity pauses its pattern) and a toy the user blocked are exactly as they were
    once the hold ends: nothing starts playing on its own, and the manual level is not replayed.
    """
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.wait_scan_update("Lightning_ID")
    reply = await client.request(
        "add", {"toy_id": "Lightning_ID", "model_name": "Lightning"}
    )
    assert reply["success"] is True
    for toy_id in ("Thunder_ID", "Lightning_ID"):
        await _play(client, toy_id)
    await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})
    await client.request("set_blocked", {"toy_id": "Lightning_ID", "block": True})
    await client.request("enable_heartbeat", {"enable": True})

    await client.wait_event("heartbeat_timeout")
    thunder = await _state(client, "Thunder_ID")
    lightning = await _state(client, "Lightning_ID")
    assert thunder["is_held"] and thunder["is_paused"] and not thunder["is_blocked"]
    assert lightning["is_held"] and lightning["is_blocked"]
    assert not lightning["is_paused"]
    assert thunder["current_intensities"] == [0, 0]

    await client.request("heartbeat")
    await client.wait_event("hold_released")
    await asyncio.sleep(COMMUNICATION_INTERVAL * 4)
    thunder = await _state(client, "Thunder_ID")
    lightning = await _state(client, "Lightning_ID")
    assert thunder["is_paused"] and thunder["current_intensities"] == [0, 0]
    assert lightning["is_blocked"] and lightning["current_intensities"] == [0, 0]


async def test_decisions_made_during_a_hold_stand_after_it(ws_server):
    """Block, pause, and patterns stay editable while held, and whatever a client set then applies afterwards."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await _play(client)
    toy = server._hub._toys["Thunder_ID"]
    await client.request("enable_heartbeat", {"enable": True})
    await client.wait_event("heartbeat_timeout")

    await _play(client, level=60)
    await client.request("set_blocked", {"toy_id": "Thunder_ID", "block": True})
    state = await _state(client)
    assert state["is_held"] and state["is_blocked"]

    await client.request("heartbeat")
    await client.wait_event("hold_released")
    await asyncio.sleep(COMMUNICATION_INTERVAL * 4)
    assert toy.current_intensities == (0, 0)  # still blocked, as the client decided

    await client.request("set_blocked", {"toy_id": "Thunder_ID", "block": False})
    assert await _wait_until(lambda: toy.current_intensities == (60, 0))


async def test_direct_command_is_refused_during_a_hold(ws_server):
    """A raw command could drive the toy, so none pass the hold."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.request("enable_heartbeat", {"enable": True})
    await client.wait_event("heartbeat_timeout")

    command = {"toy_id": "Thunder_ID", "command": "DeviceType"}
    reply = await client.request("direct_command", command)
    assert reply["success"] is False
    assert reply["data"]["error"] == "Safety Hold"
    assert reply["data"]["toy_id"] == "Thunder_ID"

    await client.request("heartbeat")
    await client.wait_event("hold_released")
    assert (await client.request("direct_command", command))["success"] is True


async def test_heartbeat_timeout_reports_a_toy_it_could_not_stop(ws_server):
    """A toy the watchdog cannot reach may still be running, so the event names it. It is held regardless."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    controller = server._hub._toys["Thunder_ID"]
    controller._toy.strict_stop = AsyncMock(side_effect=ConnectionError("radio gone"))

    await client.request("enable_heartbeat", {"enable": True})
    event = await client.wait_event("heartbeat_timeout")
    assert event["data"]["failed_toy_ids"] == ["Thunder_ID"]
    assert controller.is_held


async def test_disconnect_while_the_hold_goes_on_still_holds_every_toy(ws_server):
    """
    The check loop is cancelled when the last armed client leaves, which can happen while it is still putting on the
    hold after that client's timeout. The hold must still be applied completely.
    """
    server, connect = ws_server
    _speed_up_watchdog(server)
    observer = await connect()
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})
    toy = server._hub._toys["Thunder_ID"]

    real_set_safety_hold = server._hub.set_safety_hold
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def slow_set_safety_hold(held: bool) -> list[str]:
        if held:
            entered.set()
            await proceed.wait()
        return await real_set_safety_hold(held)

    server._hub.set_safety_hold = slow_set_safety_hold

    await client.request("enable_heartbeat", {"enable": True})
    await asyncio.wait_for(entered.wait(), 5)  # the timeout trip is under way
    await client.raw.close()  # the armed client leaves: the check loop is cancelled
    assert await _wait_until(lambda: server._heartbeat_task is None)
    proceed.set()

    await observer.wait_event("heartbeat_timeout")
    assert await _wait_until(lambda: toy.current_intensities == (0, 0))
    assert toy.is_held


async def test_heartbeat_watchdog_stays_armed_after_recovery(ws_server):
    """After recovering, the client is still watched: going quiet again trips the watchdog a second time."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.request("enable_heartbeat", {"enable": True})

    await client.wait_event("heartbeat_timeout")
    await client.request("heartbeat")
    await client.wait_event("hold_released")

    client.events.clear()
    await client.wait_event("heartbeat_timeout")  # fires again without re-enabling
    assert client.raw.state is websockets.protocol.State.OPEN


async def test_heartbeat_opt_out_while_overdue_releases_the_hold(ws_server):
    """A deliberate opt-out is also proof of life, so it must not leave the toys held forever."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.request("enable_heartbeat", {"enable": True})
    await client.wait_event("heartbeat_timeout")

    await client.request("enable_heartbeat", {"enable": False})
    await client.wait_event("hold_released")
    assert (await _state(client))["is_held"] is False


async def test_heartbeat_client_disconnect_holds_toys_until_release_hold(ws_server):
    """Dead-man's switch: an armed client vanishing holds every toy until a client explicitly releases the hold."""
    server, connect = ws_server
    controller = await connect()  # arms the heartbeat and drives the toy
    observer = await connect()  # stays connected to observe the safety stop

    await _scan_and_add(controller, "Thunder_ID", "Thunder")
    await controller.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})
    await controller.request("enable_heartbeat", {"enable": True})

    # The controller crashes / drops the connection without disabling the heartbeat first.
    await controller.raw.close()

    event = await observer.wait_event("heartbeat_timeout")
    assert event["success"] is True
    assert event["data"]["reason"] == "disconnect"
    state = await _state(observer)
    assert state["current_intensities"] == [0, 0]
    assert state["is_held"] is True

    assert (await observer.request("release_hold"))["success"] is True
    await observer.wait_event("hold_released")
    assert (await _state(observer))["is_held"] is False


async def test_heartbeat_does_not_end_a_disconnect_hold(ws_server):
    """A disconnected client can never prove it is back, so another client's recovery must not end its hold."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    survivor = await connect()
    crasher = await connect()
    await _scan_and_add(survivor, "Thunder_ID", "Thunder")
    await survivor.request("enable_heartbeat", {"enable": True})
    await crasher.request("enable_heartbeat", {"enable": True})
    await crasher.raw.close()  # crashes while armed
    assert await _wait_until(
        lambda: len(server._heartbeat_clients) == 1
        and bool(server._heartbeat_timed_out)
    )

    survivor.events.clear()
    await survivor.request("heartbeat")
    await asyncio.sleep(0.2)
    assert not any(e.get("event") == "hold_released" for e in survivor.events)
    assert (await _state(survivor))["is_held"] is True

    await survivor.request("release_hold")
    await survivor.wait_event("hold_released")
    assert (await _state(survivor))["is_held"] is False


async def test_release_hold_does_not_override_an_overdue_client(ws_server):
    """release_hold only ends a disconnect hold. While an armed client is overdue, the hold stays on."""
    server, connect = ws_server
    _speed_up_watchdog(server)

    overdue = await connect()
    other = await connect()
    await _scan_and_add(overdue, "Thunder_ID", "Thunder")
    await overdue.request("enable_heartbeat", {"enable": True})
    await other.wait_event("heartbeat_timeout")

    other.events.clear()
    assert (await other.request("release_hold"))["success"] is True
    await asyncio.sleep(0.2)
    assert not any(e.get("event") == "hold_released" for e in other.events)
    assert (await _state(other))["is_held"] is True

    await overdue.request("heartbeat")
    await other.wait_event("hold_released")


async def test_toy_added_during_a_hold_is_held(ws_server):
    """The hold covers every toy, including one connected while it is on."""
    server, connect = ws_server
    survivor = await connect()
    crasher = await connect()
    await crasher.request("enable_heartbeat", {"enable": True})
    await crasher.raw.close()
    await survivor.wait_event("heartbeat_timeout")

    await _scan_and_add(survivor, "Thunder_ID", "Thunder")
    assert (await _state(survivor))["is_held"] is True
    reply = await survivor.request(
        "intensity1", {"toy_id": "Thunder_ID", "intensity": 50}
    )
    assert reply["data"]["ack"] is False


async def test_overdue_client_is_disconnected_after_the_grace_period(ws_server):
    """
    A stuck client must not hold the toys forever: past the grace period the server treats it as disconnected and
    closes its connection, which turns the hold into one that release_hold can end.
    """
    server, connect = ws_server
    _speed_up_watchdog(server)
    server._heartbeat_grace_period = 0.3

    observer = await connect()
    stuck = await connect()
    await _scan_and_add(observer, "Thunder_ID", "Thunder")
    await stuck.request("enable_heartbeat", {"enable": True})
    first = await observer.wait_event("heartbeat_timeout")
    assert first["data"]["reason"] == "timeout"

    observer.events.clear()
    event = await observer.wait_event("heartbeat_timeout")
    assert event["data"]["reason"] == "disconnect"
    await asyncio.wait_for(stuck.raw.wait_closed(), 5)
    assert stuck.raw.close_code == 4000
    assert stuck.raw.close_reason == "Heartbeat overdue"
    assert (await _state(observer))["is_held"] is True

    await observer.request("release_hold")
    await observer.wait_event("hold_released")
    assert (await _state(observer))["is_held"] is False


async def test_a_disconnected_overdue_client_is_ignored_while_it_closes(ws_server):
    """
    Closing a stuck client can take a while (it may never answer the closing handshake). Whatever it sends in the
    meantime must not count: it may not end the hold it caused, nor come back as a watched client.
    """
    server, connect = ws_server
    _speed_up_watchdog(server)
    server._heartbeat_grace_period = 0.3

    observer = await connect()
    stuck = await connect()
    await _scan_and_add(observer, "Thunder_ID", "Thunder")
    await stuck.request("enable_heartbeat", {"enable": True})
    server_side = next(iter(server._heartbeat_clients))
    original_close = server_side.close
    server_side.close = AsyncMock()  # the closing handshake never completes
    try:
        assert await _wait_until(lambda: server_side in server._heartbeat_kicked)

        for command in ("release_hold", "heartbeat"):
            with pytest.raises(
                asyncio.TimeoutError
            ):  # no reply: the message was dropped
                await stuck.request(command, timeout=0.3)
        server_side.close.assert_awaited_once()
        assert not server._heartbeat_clients
        assert (await _state(observer))["is_held"] is True
    finally:
        server_side.close = original_close


async def test_heartbeat_disabled_client_disconnect_does_not_stop_toys(ws_server):
    """A client that unsubscribes before leaving must NOT trigger the safety stop."""
    server, connect = ws_server
    controller = await connect()
    observer = await connect()

    await _scan_and_add(controller, "Thunder_ID", "Thunder")
    await controller.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})
    await controller.request("enable_heartbeat", {"enable": True})
    await controller.request("enable_heartbeat", {"enable": False})  # graceful opt-out
    await controller.raw.close()
    await asyncio.sleep(0.1)  # let the server-side disconnect handler run

    # No safety stop fired: the toy keeps its intensity.
    state = await _state(observer)
    assert state["current_intensities"] == [50, 0]
    assert state["is_held"] is False
    assert not any(e.get("event") == "heartbeat_timeout" for e in observer.events)


# ---------------------------------------------------------------------------
# Origin check (cross-site WebSocket hijacking guard)
# ---------------------------------------------------------------------------


async def test_origin_check_rejects_browser_origin(ws_server):
    """A browser-style Origin is rejected at the handshake; native clients (no Origin) connect fine."""
    server, connect = ws_server

    native = await connect()  # fixture client sends no Origin header
    assert (await native.request("get_brands"))["success"] is True

    with pytest.raises(websockets.exceptions.InvalidStatus):
        await websockets.connect(
            f"ws://localhost:{server._port}",
            additional_headers={"Origin": "http://evil.example"},
        )


# ---------------------------------------------------------------------------
# Insecure-bind guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.10", "toys.example.com", ""]
)
async def test_exposed_bind_without_insecure_is_refused(host):
    """A non-loopback bind is refused unless insecure=True (fail closed)."""
    with pytest.raises(InsecureBindError):
        ToyServer(host=host, port=8142, mock_toys=True, log_name="test_ws")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.5", "::1"])
async def test_loopback_bind_is_allowed(host):
    """Loopback binds construct without opting into insecure mode."""
    server = ToyServer(host=host, port=8142, mock_toys=True, log_name="test_ws")
    assert server._host == host


async def test_exposed_bind_with_insecure_is_allowed():
    """insecure=True permits a non-loopback bind (with a logged warning)."""
    server = ToyServer(
        host="0.0.0.0", port=8142, mock_toys=True, insecure=True, log_name="test_ws"
    )
    assert server._host == "0.0.0.0"


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_idle_shutdown_disabled_when_delay_zero():
    """idle_shutdown_delay <= 0 disables auto-shutdown: _idle_shutdown returns without tearing down."""
    server = ToyServer(
        host="localhost",
        port=_free_port(),
        mock_toys=True,
        idle_shutdown_delay=0.0,
        log_name="test_ws",
    )
    await server._idle_shutdown()  # must return immediately without shutting down
    assert server._shutdown_initiated is False


async def test_cancelling_serve_still_shuts_the_hub_down():
    """
    Ctrl+C must not leave toys running.

    ``asyncio.run`` cancels the task running ``serve()``; the hub teardown (which stops and disconnects every toy)
    lives in a ``finally``, so it has to run even then.
    """
    server = ToyServer(
        host="localhost",
        port=_free_port(),
        mock_toys=True,
        idle_shutdown_delay=3600.0,
        log_name="test_ws",
    )
    serve_task = asyncio.create_task(server.serve())
    for _ in range(250):
        if server._server is not None:
            break
        await asyncio.sleep(0.02)
    else:
        serve_task.cancel()
        raise RuntimeError("ToyServer did not start listening")

    with patch.object(server._hub, "shutdown", AsyncMock()) as hub_shutdown:
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        hub_shutdown.assert_awaited_once()


async def test_cancelling_serve_stops_connected_toys():
    """End-to-end companion to the test above: a running toy is actually stopped and disconnected on Ctrl+C."""
    port = _free_port()
    server = ToyServer(
        host="localhost",
        port=port,
        mock_toys=True,
        idle_shutdown_delay=3600.0,
        log_name="test_ws",
    )
    serve_task = asyncio.create_task(server.serve())
    for _ in range(250):
        if server._server is not None:
            break
        await asyncio.sleep(0.02)

    ws = await websockets.connect(f"ws://localhost:{port}")
    try:
        client = _Client(ws)
        await _scan_and_add(client, "Thunder_ID", "Thunder")
        await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 100})
        toy = server._hub._toys["Thunder_ID"]
        assert toy.current_intensities == (100, 0)

        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task

        assert toy.current_intensities == (0, 0)
        assert toy._toy.is_connected is False
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def test_last_client_leaving_stops_toys_even_without_idle_shutdown():
    """
    Nobody connected means nobody is watching: every toy must be stopped and its pattern frozen.

    Auto-shutdown is disabled here (``idle_shutdown_delay=0``), which used to leave a running pattern driving the
    toy indefinitely.
    """
    port = _free_port()
    server = ToyServer(
        host="localhost",
        port=port,
        mock_toys=True,
        idle_shutdown_delay=0.0,
        log_name="test_ws",
    )
    serve_task = asyncio.create_task(server.serve())
    for _ in range(250):
        if server._server is not None:
            break
        await asyncio.sleep(0.02)

    try:
        ws = await websockets.connect(f"ws://localhost:{port}")
        client = _Client(ws)
        await _scan_and_add(client, "Thunder_ID", "Thunder")
        await client.request(
            "set_pattern",
            {
                "toy_id": "Thunder_ID",
                "pattern": [[10_000, 100, 0]],
                "wraparound": True,
                "reset_time": True,
            },
        )
        await client.request("set_paused", {"toy_id": "Thunder_ID", "pause": False})
        toy = server._hub._toys["Thunder_ID"]
        for _ in range(50):  # let playback drive the toy up
            if toy.current_intensities == (100, 0):
                break
            await asyncio.sleep(COMMUNICATION_INTERVAL)
        assert toy.current_intensities == (100, 0)

        await ws.close()

        for _ in range(100):
            if toy.current_intensities == (0, 0):
                break
            await asyncio.sleep(0.05)
        assert toy.current_intensities == (0, 0)
        assert toy.get_state()["is_paused"] is True
        assert server._shutdown_initiated is False  # auto-shutdown really is off
    finally:
        with contextlib.suppress(Exception):
            await server._shutdown()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve_task


async def test_shutdown_sends_single_response(ws_server):
    """Regression: the shutdown command must reply exactly once (not twice)."""
    _, connect = ws_server
    client = await connect()
    reply = await client.request("shutdown")
    assert reply["reply"] == "shutdown"
    assert reply["success"] is True and reply["data"]["ack"] is True

    # No duplicate reply should follow before the client disconnects.
    with pytest.raises((asyncio.TimeoutError, websockets.ConnectionClosed)):
        await asyncio.wait_for(client.raw.recv(), 0.5)


# ---------------------------------------------------------------------------
# Remaining command handlers
# ---------------------------------------------------------------------------


async def test_get_info_direct_command_and_rotation(ws_server):
    """get_info (inexpensive and full), direct_command, and change_rotation_direction handlers."""
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    info = await client.request("get_info", {"toy_id": "Thunder_ID", "full": False})
    assert info["success"] and info["data"]["model_name"] == "Thunder"

    info_full = await client.request("get_info", {"toy_id": "Thunder_ID", "full": True})
    assert info_full["success"]

    dc = await client.request(
        "direct_command", {"toy_id": "Thunder_ID", "command": "Battery"}
    )
    assert dc["success"] and dc["data"]["toy_id"] == "Thunder_ID"

    rot = await client.request("change_rotation_direction", {"toy_id": "Thunder_ID"})
    # MockEstim toys have no rotation, so ack is False, but the handler ran.
    assert rot["success"] and rot["data"]["ack"] is False


async def test_control_toggles_and_setters(ws_server):
    """stop / intensity2 / toggle_pause / toggle_block / set_paused / set_blocked handlers."""
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Lightning_ID", "Lightning")  # dual-channel toy

    i2 = await client.request("intensity2", {"toy_id": "Lightning_ID", "intensity": 4})
    assert i2["success"] and i2["data"]["ack"] is True

    for cmd in ("stop", "toggle_pause", "toggle_block"):
        reply = await client.request(cmd, {"toy_id": "Lightning_ID"})
        assert reply["success"] and reply["data"]["ack"] is True

    paused = await client.request(
        "set_paused", {"toy_id": "Lightning_ID", "pause": True}
    )
    assert paused["success"]
    blocked = await client.request(
        "set_blocked", {"toy_id": "Lightning_ID", "block": True}
    )
    assert blocked["success"]


async def test_set_model_updates_and_broadcasts(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    reply = await client.request(
        "set_model", {"toy_id": "Thunder_ID", "model_name": "Lightning"}
    )
    assert reply["success"] and reply["data"]["ack"] is True

    event = await client.wait_event("model_changed")
    assert event["data"]["model_name"] == "Lightning"


# ---------------------------------------------------------------------------
# Error mapping: exception type -> error envelope
# ---------------------------------------------------------------------------


async def test_add_already_added_toy(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    reply = await client.request(
        "add", {"toy_id": "Thunder_ID", "model_name": "Thunder"}
    )
    assert reply["success"] is False
    assert reply["data"]["error"] == "Toy Already Added"


async def test_stop_scan_unsubscribes(ws_server):
    """stop_scan acks and unsubscribes the client (covers the unsubscribe handler)."""
    _, connect = ws_server
    client = await connect()
    await client.request("start_scan")
    await client.wait_scan_update("Thunder_ID")
    reply = await client.request("stop_scan")
    assert reply["success"] is True and reply["data"]["ack"] is True


async def test_add_unavailable_toy_maps_to_unavailable(ws_server):
    """A toy that is no longer advertising maps to 'Unavailable Toy'."""
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub,
        "add",
        new=AsyncMock(side_effect=UnavailableToyError("Thunder_ID", "Thunder")),
    ):
        reply = await client.request(
            "add", {"toy_id": "Thunder_ID", "model_name": "Thunder"}
        )
    assert reply["success"] is False
    assert reply["data"]["error"] == "Unavailable Toy"


async def test_add_connection_error_maps_to_connection_error(ws_server):
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub,
        "add",
        new=AsyncMock(side_effect=AddConnectionError("X", "Thunder")),
    ):
        reply = await client.request("add", {"toy_id": "X", "model_name": "Thunder"})
    assert reply["success"] is False
    assert reply["data"]["error"] == "Connection Error"


async def test_add_bad_model_maps_to_bad_model(ws_server):
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub, "add", new=AsyncMock(side_effect=BadModelError("X", "Thunder"))
    ):
        reply = await client.request("add", {"toy_id": "X", "model_name": "Thunder"})
    assert reply["success"] is False
    assert reply["data"]["error"] == "Bad Model"


async def test_command_connection_error_maps_to_connection_error(ws_server):
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub,
        "stop",
        new=AsyncMock(side_effect=ToyConnectionError("X", "Thunder", "stop")),
    ):
        reply = await client.request("stop", {"toy_id": "X"})
    assert reply["success"] is False
    assert reply["data"]["error"] == "Connection Error"


async def test_command_for_a_reconnecting_toy_is_refused_as_a_connection_error(
    ws_server,
):
    """
    Nothing is sent to a toy that is not connected. The error kind is the one of a failed command, so clients need no
    new handling; the message says what happened, including that a requested state change still took effect.
    """
    server, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    server._hub._toy_status["Thunder_ID"] = ToyStatus.RECONNECTING

    reply = await client.request(
        "intensity1", {"toy_id": "Thunder_ID", "intensity": 50}
    )
    assert reply["success"] is False
    assert reply["data"]["error"] == "Connection Error"
    assert reply["data"]["toy_id"] == "Thunder_ID"
    assert "not connected (reconnecting)" in reply["data"]["message"]

    reply = await client.request("set_blocked", {"toy_id": "Thunder_ID", "block": True})
    assert reply["success"] is False and reply["data"]["error"] == "Connection Error"
    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["is_blocked"] is True
    assert state["data"]["current_intensities"] == [
        0,
        0,
    ]  # the intensity was never sent


async def test_unexpected_error_maps_to_developer_error(ws_server):
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub, "get_toy_ids", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        reply = await client.request("get_toy_ids")
    assert reply["success"] is False
    assert reply["data"]["error"] == "Developer Error"


async def test_start_scan_discovery_start_error(ws_server):
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub, "start_scan", new=AsyncMock(side_effect=DiscoveryStartError("tb"))
    ):
        reply = await client.request("start_scan")
    assert reply["success"] is False
    assert reply["data"]["error"] == "Discovery Start Error"


async def test_start_scan_unexpected_error(ws_server):
    server, connect = ws_server
    client = await connect()
    with patch.object(
        server._hub, "start_scan", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        reply = await client.request("start_scan")
    assert reply["success"] is False
    assert reply["data"]["error"] == "Developer Error"


# ---------------------------------------------------------------------------
# Heartbeat enable / disable / received
# ---------------------------------------------------------------------------


async def test_heartbeat_enable_send_and_disable(ws_server):
    _, connect = ws_server
    client = await connect()
    assert (await client.request("enable_heartbeat", {"enable": True}))["success"]
    assert (await client.request("heartbeat"))["success"]  # received while subscribed
    assert (await client.request("enable_heartbeat", {"enable": False}))["success"]


async def test_heartbeat_without_subscription_is_ignored(ws_server):
    _, connect = ws_server
    client = await connect()
    reply = await client.request("heartbeat")  # never enabled -> acked, ignored
    assert reply["success"] and reply["data"]["ack"] is True


# ---------------------------------------------------------------------------
# Per-client intensity limits: axis 2, rollback, stale cleanup
# ---------------------------------------------------------------------------


async def test_set_intensity2_limit_clamps(ws_server):
    _, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Lightning_ID", "Lightning")

    await client.request("set_intensity2_limit", {"toy_id": "Lightning_ID", "limit": 3})
    await client.request("intensity2", {"toy_id": "Lightning_ID", "intensity": 50})

    state = await client.request("get_state", {"toy_id": "Lightning_ID"})
    assert state["data"]["current_intensities"][1] == 3


async def test_limit_rolls_back_on_hub_error(ws_server):
    server, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    with patch.object(
        server._hub,
        "set_intensity1_limit",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        reply = await client.request(
            "set_intensity1_limit", {"toy_id": "Thunder_ID", "limit": 5}
        )
    assert reply["success"] is False
    # The failed limit was rolled back, not left dangling.
    assert all("Thunder_ID" not in toys for toys in server._client_limits.values())


async def test_removing_toy_cleans_up_client_limits(ws_server):
    server, connect = ws_server
    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")

    await client.request("set_intensity1_limit", {"toy_id": "Thunder_ID", "limit": 5})
    assert any("Thunder_ID" in toys for toys in server._client_limits.values())

    await client.request("remove", {"toy_id": "Thunder_ID"})
    await client.wait_event("toy_ids_changed")
    # on_toy_ids_change pruned the now-stale limit.
    assert all("Thunder_ID" not in toys for toys in server._client_limits.values())


# ---------------------------------------------------------------------------
# HTTP status page, callback broadcasts, and low-level messaging helpers
# ---------------------------------------------------------------------------


async def test_http_request_serves_status_page_and_passes_through_upgrades(ws_server):
    server, _ = ws_server

    class _Req:
        def __init__(self, upgrade: str):
            self.headers = {"upgrade": upgrade}

    resp = await server._handle_http_request(None, _Req(upgrade=""))
    assert resp is not None and resp.status_code == 200
    assert b"<" in resp.body  # some HTML was rendered

    # A websocket upgrade must be passed through (None), not served a page.
    assert await server._handle_http_request(None, _Req(upgrade="websocket")) is None


async def test_status_and_battery_change_broadcasts(ws_server):
    server, connect = ws_server
    client = await connect()

    await server._on_status_change("T_ID", ToyStatus.RECONNECTING)
    status_evt = await client.wait_event("connection_status_changed")
    assert status_evt["data"] == {"toy_id": "T_ID", "status": "reconnecting"}

    await server._on_battery_change({"T_ID": 42})
    battery_evt = await client.wait_event("battery_changed")
    assert battery_evt["data"] == {"T_ID": 42}


async def test_scan_update_maps_errors_and_success(ws_server):
    server, _ = ws_server
    calls = []

    async def capture(event_name, payload, *, success=True):
        calls.append((event_name, payload, success))

    with patch.object(server, "_broadcast_to_subscribers", new=capture):
        await server._on_scan_update(DiscoveryError("trace"))
        await server._on_scan_update(RuntimeError("boom"))
        await server._on_scan_update([ToyData("T1", "T_ID", "Thunder", "Brand")])

    assert calls[0][0] == "scan_update"
    assert calls[0][1]["error"] == "Discovery Error" and calls[0][2] is False
    assert calls[1][1]["error"] == "Developer Error"
    assert calls[2][1] == {
        "discovered": [
            {"toy_id": "T_ID", "name": "T1", "brand": "Brand", "model_name": "Thunder"}
        ]
    }
    assert calls[2][2] is True


async def test_send_raw_swallows_send_error(ws_server):
    server, _ = ws_server
    dead_ws = AsyncMock()
    dead_ws.send = AsyncMock(side_effect=ConnectionError("gone"))
    await server._send_raw(dead_ws, "msg")  # must not raise
