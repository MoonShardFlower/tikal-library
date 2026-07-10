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

from tikal.websocket._toy_hub import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    ToyConnectionError,
    ToyStatus,
    UnavailableToyError,
)
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


async def test_heartbeat_client_disconnect_stops_toys(ws_server):
    """Dead-man's switch: an armed heartbeat client vanishing stops all toys and notifies the others."""
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

    state = await observer.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [0, 0]


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
    state = await observer.request("get_state", {"toy_id": "Thunder_ID"})
    assert state["data"]["current_intensities"] == [50, 0]
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
        await server._on_scan_update([{"toy_id": "T_ID"}])

    assert calls[0][0] == "scan_update"
    assert calls[0][1]["error"] == "Discovery Error" and calls[0][2] is False
    assert calls[1][1]["error"] == "Developer Error"
    assert calls[2][1] == {"discovered": [{"toy_id": "T_ID"}]} and calls[2][2] is True


async def test_send_raw_swallows_send_error(ws_server):
    server, _ = ws_server
    dead_ws = AsyncMock()
    dead_ws.send = AsyncMock(side_effect=ConnectionError("gone"))
    await server._send_raw(dead_ws, "msg")  # must not raise
