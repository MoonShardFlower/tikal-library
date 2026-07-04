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

import pytest
import pytest_asyncio
import websockets

from tikal.websocket.toy_server import ToyServer

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

    async def wait_scan_update(self, toy_id: str, timeout: float = 5.0) -> dict:
        """Wait for a ``scan_update`` whose ``discovered`` list contains ``toy_id``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for event in self.events:
                if event.get("event") == "scan_update" and any(
                    d["toy_id"] == toy_id for d in event["data"].get("discovered", [])
                ):
                    return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"{toy_id} not discovered within {timeout}s")
            msg = json.loads(await asyncio.wait_for(self.raw.recv(), timeout=remaining))
            if "event" in msg:
                self.events.append(msg)


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


# ---------------------------------------------------------------------------
# Heartbeat watchdog
# ---------------------------------------------------------------------------


async def test_heartbeat_timeout_stops_toys(ws_server):
    server, connect = ws_server
    server._heartbeat_timeout = 0.3
    server._heartbeat_check_interval = 0.05

    client = await connect()
    await _scan_and_add(client, "Thunder_ID", "Thunder")
    await client.request("intensity1", {"toy_id": "Thunder_ID", "intensity": 50})

    # Enable the heartbeat but never send one -> the watchdog must stop the toy.
    await client.request("enable_heartbeat", {"enable": True})
    event = await client.wait_event("heartbeat_timeout")
    assert event["success"] is True

    state = await client.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [0, 0]


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


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
