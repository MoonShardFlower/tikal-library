"""
WebSocket JSON-based server that exposes ToyHub to connected clients.
Offers an alternative to the Low-Level / High-Level API defined by the tikal library.
Here information is exchanged via a websocket. Significantly harder to use than the Low-Level / High-Level APIs but
offers some advantages:
- Process separation
- Service can be used in applications written in other programming languages (assuming websockets are supported)
- Multiple clients can modify the same state (experimental, untested)

**Protocol**
Request  (client -> server):
    {"request": "some_command", "id": "some_id", "data": {...}}

Response (server -> client):
    {"reply": "some_command", "id": "some_id", "success": true,  "data": { ... }}
    {"reply": "some_command", "id": "some_id", "success": false, "data": {"error": "...", "message": "...", ...}}

Event (server -> all clients / scan subscribers):
    {"event": "some_event", "success": true,  "data": {...}}
    {"event": "some_event", "success": false, "data": {"error": "...", "message": "..."}}

The success field lets you branch between error handling / normal operation without having to inspect data.

**Examples**:

Request:
{
    "request": "get_battery",
    "id": "some_id",
    "data": {"toy_id": "some_toy_id"}
}

Response:
{
    "reply": "get_battery",
    "id": "some_id",
    "success": True,
    "data": {"battery": 85, "toy_id": "some_toy_id"}
}

Error response:
{
    "reply": "get_battery",
    "id": "some_id",
    "success": False,
    "data": {"error": "UnknownToyError", "message": "Unable to execute 'get_battery' on 'some_toy_id'. Please add the toy first."}
}

Event:
{
    "event": "on_status_change",
    "success": True,
    "data": {"toy_id": "some_toy_id", "status": "RECONNECTING"}
}

**Architecture**
Each command is described by a CommandEntry dataclass that bundles:
  - Request_model   :Pydantic model that validates the incoming data object.
  - Response_model  :Pydantic model that validates (and serializes) the outgoing data.
  - Handler         :async callable(hub: ToyHub, data: req_model) -> dict that performs the actual work and returns the raw result dict.

_handle_message is a *generic* dispatcher: validate -> look up entry -> validate inner data -> call handler -> validate response -> send.
The commands start_scan / stop_scan are a special case as they require a reference to the per-client WebSocket connection.
They are handled by dedicated methods flagged via CommandEntry.is_scan.
"""

import asyncio
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import websockets
from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import ServerConnection, serve

from tikal_web_server.toy_hub import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    ToyAlreadyAddedError,
    ToyConnectionError,
    ToyHub,
    ToyStatus,
    UnavailableToyError,
    UndiscoveredToyError,
    UnknownToyError,
)
from tikal_web_server.toy_server_models import (
    AckData,
    AddRequestData,
    BatteryResponseData,
    BrandsData,
    ConnectionStatusResponseData,
    DirectCommandData,
    DirectCommandResponseData,
    ErrorData,
    EventEnvelope,
    GetAllResponseData,
    GetInfoData,
    InfoResponseData,
    IntensityData,
    RequestEnvelope,
    ResponseEnvelope,
    SetBlockedData,
    SetModelData,
    SetPatternData,
    SetPausedData,
    ToyIdData,
    ToyIdsData,
    ToyStateData,
    _EmptyData,
    _ErrMsg,
)

# How long (seconds) to wait after the last client disconnects before shutting down.
_SHUTDOWN_DELAY = 3.0


# -----------------------------------------------------------------------------
# Handler functions
# -----------------------------------------------------------------------------


async def _cmd_get_brands(hub: ToyHub, _: _EmptyData) -> dict:
    """
    Return the full brand -> model-name mapping from ToyHub.

    Args:
        hub: ToyHub instance.
        _: Unused empty data payload.

    Returns:
         A mapping of brand names to lists of supported model names, e.g. {"Lovense": ["Gush", "Solace"]}.
    """
    return {"brands": await hub.get_brands()}


async def _cmd_get_toy_ids(hub: ToyHub, _: _EmptyData) -> dict:
    """
    Return a snapshot of all toy identifiers currently managed by the Server.

    Args:
        hub: ToyHub instance.
        _: Unused empty data payload.

    Returns:
        dict with key "toy_ids": a list of toy identifier strings.
    """
    return {"toy_ids": await hub.get_toy_ids()}


async def _cmd_get_state(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Returns the current state of a toy. This is an inexpensive in-memory read (no BLE communication).

    State information contains:
    -  `toy_id` (str) Unique identifier of the toy
    -  `current_intensity` (list[int, int]) Current intensity values. The second value is always zero if the toy only has one intensity.
    -  `is_blocked` (bool) Whether the toy is currently blocked (toy's intensities are forced to zero)
    -  `pattern_version` (int) Each time the pattern state changes, the version number is incremented
    -  `pattern` (list[tuple[int, int, int]]) List of tuples (duration, intensity1, intensity2) defining the pattern segment
    -  `wraparound` (bool)  Whether the pattern repeats from the beginning after completing the last segment. If False, both Intensities are 0 after the last segment
    -  `is_paused` (bool) Whether the toy is currently paused (patterns do not advance)
    -  `elapsed` (float) Time elapsed since the start of the pattern or last wraparound in ms
    Args:
        hub: ToyHub instance managing the toy
        data: Validated ToyIdData instance, containing the toy_id

    Raises:
        UnknownToyError: The toy was not added before.

    Returns:
        dict with keys as described above
    """
    return await hub.get_state(data.toy_id)


async def _cmd_get_connection_status(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Return the current connection status of a toy.

    Args:
        hub: ToyHub instance.
        data: Validated ToyIdData containing the toy_id.

    Raises:
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys "connection_status" (string) and "toy_id".
    """
    return {
        "connection_status": await hub.get_status(data.toy_id),
        "toy_id": data.toy_id,
    }


async def _cmd_get_battery(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Retrieve the in-memory battery level for a toy. No communication to the toy is performed. Kept up to date in the background
    Args:
        hub: ToyHub instance managing the toy
        data: Validated ToyIdData instance, containing the toy_id

    Raises:
        UnknownToyError: The toy was not added before.
    """
    return {
        "battery": await hub.get_battery(data.toy_id),
        "toy_id": data.toy_id,
    }


async def _cmd_get_info(hub: ToyHub, data: GetInfoData) -> dict:
    """
    Gather information about the toy.

    Info gathered (always, also if full=False):
    -  `toy_id` (str) unique identifier of the toy, e.g., Bluetooth address
    -  `name` (str) human-readable identifier of the toy, e.g., Bluetooth advertisement name
    -  `model_name` (str) model name of the toy. Typically, not retrieved from the toy itself but set by you when adding the toy. This returns this set name.
    -  `brand` (str) brand of the toy, e.g., Lovense
    -  `intensity_names` (list of str). Two human-readable strings. The second string is "None" if the toy only has one intensity.y.
    -  `supports_rotation` (bool) whether the toy supports changing the rotation direction
    -  `max_intensity` (int) maximum intensity value

    Args:
        hub:   ToyHub instance managing the toy
        data:   Validated GetInfoData instance, containing the toy_id and full flag.
                If full is True, additional brand-dependent information is returned.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy was not added before.

    Returns:
        dict: dictionary containing the gathered info. Empty dict if the command could not be delivered.
    """
    return await hub.get_info(data.toy_id, data.full)


async def _cmd_get_all(hub: ToyHub, data: GetInfoData) -> dict:
    """Return combined state, info, connection status, get_battery for a toy."""
    return await hub.get_all(data.toy_id, data.full)


async def _cmd_direct_command(hub: ToyHub, data: DirectCommandData) -> dict:
    """
    Send a raw command string directly to a toy. Use this to access toy functionality not exposed by the API.
    Do not use it to change the tracked state (e.g., intensities) as ToyHub will not be aware of the resulting state change.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated DirectCommandData containing toy_id and the raw command string.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with key response (holding as the toy's raw reply string) and key toy_id.
    """
    return {
        "response": await hub.direct_command(data.toy_id, data.command),
        "toy_id": data.toy_id,
    }


async def _cmd_change_rotation_direction(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Toggle the rotation direction of a toy if the toy supports it.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with key ack (True if the toy supports rotation, else False) and key toy_id
    """
    return {
        "ack": await hub.change_rotation_direction(data.toy_id),
        "toy_id": data.toy_id,
    }


async def _cmd_add(hub: ToyHub, data: AddRequestData) -> dict:
    """
    Connect to a discovered toy and register it on the Server.

    Args:
        hub: ToyHub instance.
        data: Validated AddRequestData containing toy_id and model_name.

    Raises:
        UndiscoveredToyError: The toy_id was never seen during scanning.
        UnavailableToyError: The toy was discovered previously but is no longer advertising.
        ToyAlreadyAddedError: The toy is already added or a connection attempt is in progress.
        InvalidModelError: The model_name is not valid for the toy's brand.
        BadModelError: The model_name is valid, but the toy does not respond correctly to commands.
        AddConnectionError: Could not establish a connection to the toy.

    Returns:
        dict with key "ack" (Always True) and "toy_id".
    """
    await hub.add(data.toy_id, data.model_name)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_remove(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Disconnect a toy, then deregister it from the Server.

    Args:
        hub: ToyHub instance.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Proper disconnect failed. Only for info purposes. Toy is still removed.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.remove(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_model(hub: ToyHub, data: SetModelData) -> dict:
    """
    Change the model name assigned to an already-added toy.

    Args:
        hub: ToyHub instance.
        data: Validated SetModelData containing toy_id and the new model_name.

    Raises:
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack ( Always True) and toy_id.
    """
    await hub.set_model(data.toy_id, data.model_name)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_stop(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Stop the toy by setting both intensities to zero and pausing any active pattern.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.stop(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_intensity1(hub: ToyHub, data: IntensityData) -> dict:
    """
    Set the primary intensity of a toy and pause any active pattern.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated IntensityData containing toy_id and intensity (0 – max_intensity). Automatically clamped.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.

    Note:
        Sending a manual intensity command automatically pauses pattern playback. Use toggle_pause or set_paused to resume.
    """
    ack = await hub.intensity1(data.toy_id, data.intensity)
    return {"ack": ack, "toy_id": data.toy_id}


async def _cmd_intensity2(hub: ToyHub, data: IntensityData) -> dict:
    """
    Set the secondary intensity of a toy and pause any active pattern. For single-intensity toys, this command has no effect.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated IntensityData containing toy_id and intensity (0 – max_intensity). Automatically clamped.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (bool) and toy_id (str).

    Note:
        Sending a manual intensity command automatically pauses pattern playback. Use toggle_pause or set_paused to resume.
    """
    ack = await hub.intensity2(data.toy_id, data.intensity)
    return {"ack": ack, "toy_id": data.toy_id}


async def _cmd_toggle_pause(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Toggle the pause state of a toy's pattern playback.

    When paused, the pattern timer freezes and both intensities are set to zero.
    When resumed, playback continues from the elapsed time at the point of pausing.
    Pausing a blocked toy clears its blocked state (A toy cannot be both paused and blocked simultaneously).

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated ``ToyIdData`` containing the target ``toy_id``.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (True) and toy_id.
    """
    await hub.toggle_pause(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_toggle_block(hub: ToyHub, data: ToyIdData) -> dict:
    """
    Toggle the blocked state of a toy.

    When blocked, both intensities are forced to zero regardless of any active pattern or
    manual commands. Blocking a paused toy clears its pause state (a toy cannot be both paused and blocked simultaneously).
    Unblocking restores normal operation.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.toggle_block(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_paused(hub: ToyHub, data: SetPausedData) -> dict:
    """
    Set the paused state of a toy's pattern playback.

    Pausing freezes the pattern timer and sets both intensities to zero.
    Resuming continues playback from the elapsed time at the point of pausing.
    Pausing a blocked toy clears its blocked state (A toy cannot be both paused and blocked simultaneously).

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated SetPausedData containing toy_id and pause (True to pause, False to resume).

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.set_paused(data.toy_id, data.pause)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_blocked(hub: ToyHub, data: SetBlockedData) -> dict:
    """
    Set the blocked state of a toy.

    When blocked, both intensities are forced to zero regardless of any active pattern or
    manual commands. Blocking a paused toy clears its pause state (a toy cannot be both paused and blocked simultaneously).

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated SetBlockedData containing toy_id and block (True to block, False to unblock).

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.set_blocked(data.toy_id, data.block)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_pattern(hub: ToyHub, data: SetPatternData) -> dict:
    """
    Load a new intensity pattern onto a toy and start playback.

    Each segment of the pattern specifies intensity1, intensity2, duration_ms.
    The toy steps through segments in order, advancing every duration_ms millisecond.
    If wraparound is True, the pattern loops back to the first segment upon completing the last segment; else both intensities are set to zero and playback stops.

    Args:
        hub: ToyHub instance managing the toy.
        data: Validated SetPatternData containing:
            - toy_id: Identifier of the target toy.
            - pattern: Sequence of (intensity1, intensity2, duration_ms) tuples.
            - wraparound: Whether the pattern loops after its final segment.
            - reset_time: If True, reset the elapsed-time counter before starting playback; if False, continue from the current elapsed time.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """

    await hub.set_pattern(data.toy_id, data.pattern, data.wraparound, data.reset_time)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_scan_noop(hub: ToyHub, data: _EmptyData) -> dict:  # pragma: no cover
    """
    Placeholder handler for scan commands (start_scan / stop_scan).

    Scan commands need access to the per-client WebSocket connection for subscription management and are intercepted by
    _handle_scan before the generic dispatcher reaches this handler. This function should never be reached,
    it only serves as a placeholder for the CommandEntry.handler slot in _COMMAND_REGISTRY.
    """
    return {"ack": True}


# -----------------------------------------------------------------------------
# Command registry
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandEntry:
    """
    Descriptor for a single command.

    Bundles everything the dispatcher needs to handle a command: A Pydantic model to validate the incoming data payload,
    a Pydantic model to validate and serialize the outgoing result, and the async handler that performs the actual work.

    Attributes:
        req_model:  The Pydantic model used to validate (and coerce) the data field of the incoming RequestEnvelope.
        resp_model: The Pydantic model used to validate the dict returned by the handler before it is serialized into the ResponseEnvelope.
        handler:    Async callable with the signature (hub: ToyHub, data: req_model) -> dict that performs the command and returns a result dict.
        is_scan:    If True, the command is a scan subscription command (start_scan / stop_scan) and routed to _handle_scan
                    instead of the generic dispatcher. Handler is unused in that case.
    """

    req_model: type[BaseModel]
    resp_model: type[BaseModel]
    handler: Callable
    is_scan: bool = False


_COMMAND_REGISTRY: dict[str, CommandEntry] = {
    # Read-only commands
    "get_brands": CommandEntry(_EmptyData, BrandsData, _cmd_get_brands),
    "get_toy_ids": CommandEntry(_EmptyData, ToyIdsData, _cmd_get_toy_ids),
    "get_state": CommandEntry(ToyIdData, ToyStateData, _cmd_get_state),
    "get_battery": CommandEntry(ToyIdData, BatteryResponseData, _cmd_get_battery),
    "get_connection_status": CommandEntry(
        ToyIdData, ConnectionStatusResponseData, _cmd_get_connection_status
    ),
    "get_info": CommandEntry(GetInfoData, InfoResponseData, _cmd_get_info),
    "get_all": CommandEntry(GetInfoData, GetAllResponseData, _cmd_get_all),
    "direct_command": CommandEntry(
        DirectCommandData, DirectCommandResponseData, _cmd_direct_command
    ),
    "change_rotation_direction": CommandEntry(
        ToyIdData, AckData, _cmd_change_rotation_direction
    ),
    # Mutating commands
    "add": CommandEntry(AddRequestData, AckData, _cmd_add),
    "remove": CommandEntry(ToyIdData, AckData, _cmd_remove),
    "set_model": CommandEntry(SetModelData, AckData, _cmd_set_model),
    "stop": CommandEntry(ToyIdData, AckData, _cmd_stop),
    "intensity1": CommandEntry(IntensityData, AckData, _cmd_intensity1),
    "intensity2": CommandEntry(IntensityData, AckData, _cmd_intensity2),
    "toggle_pause": CommandEntry(ToyIdData, AckData, _cmd_toggle_pause),
    "toggle_block": CommandEntry(ToyIdData, AckData, _cmd_toggle_block),
    "set_paused": CommandEntry(SetPausedData, AckData, _cmd_set_paused),
    "set_blocked": CommandEntry(SetBlockedData, AckData, _cmd_set_blocked),
    "set_pattern": CommandEntry(SetPatternData, AckData, _cmd_set_pattern),
    # Scan subscription commands
    "start_scan": CommandEntry(_EmptyData, AckData, _cmd_scan_noop, is_scan=True),
    "stop_scan": CommandEntry(_EmptyData, AckData, _cmd_scan_noop, is_scan=True),
}


# -----------------------------------------------------------------------------
# ToyServer
# -----------------------------------------------------------------------------


class ToyServer:
    """
    WebSocket server that wraps a ToyHub instance.

    The server owns the ToyHub and wires itself up as all of its callbacks. Create it, then await self.serve() to start accepting connections.
    """

    def __init__(
        self,
        toy_cache_path: Path = Path(),
        host: str = "localhost",
        port: int = 8142,
        mock_toys: bool = False,
        log_name: str = "tikal_ws",
    ) -> None:
        """
        Initialize the server and wire it up to a new ToyHub instance. Call await self.serve() to begin accepting connections.

        Args:
            toy_cache_path: Path to the toy-cache file used by ToyHub to persist previously added toys across restarts. If empty, no persistent cache is used.
            host:           Network interface to bind the WebSocket server to. Defaults to "localhost".
                            There are no security measures, so using anything other than localhost is not recommended.
            port:           TCP port to listen on. Defaults to 8142.
            mock_toys:      If True, ToyHub uses mock toys instead of real toys.
            log_name:       Name of the Python logger used by both the server and the underlying ToyHub instance. Defaults to "tikal_ws".
        """

        self._host = host
        self._port = port
        self._log = logging.getLogger(log_name)

        # All currently connected WebSocket clients.
        self._clients: set[ServerConnection] = set()
        # Subset of _clients that have subscribed to scan results.
        self._scan_subscribers: set[ServerConnection] = set()
        # Lock that serializes start_scan/stop_scan calls.
        self._scan_lock = asyncio.Lock()
        # Task that fires _SHUTDOWN_DELAY seconds after the last client leaves.
        self._shutdown_task: asyncio.Task | None = None
        # The underlying websockets server object; set in serve().
        self._server: websockets.Server | None = None

        self._hub = ToyHub(
            on_status_change=self._on_status_change,
            on_toy_ids_change=self._on_toy_ids_change,
            on_toy_state_change=self._on_toy_state_change,
            on_model_change=self._on_model_change,
            on_battery_change=self._on_battery_change,
            toy_cache_path=toy_cache_path,
            default_model="",
            log_name=log_name,
            mock_toys=mock_toys,
        )

    # Lifecycle

    async def serve(self) -> None:
        """Start the WebSocket server and block until it shuts itself down."""
        self._server = await serve(self._handle_connection, self._host, self._port)
        self._log.info("ToyServer listening on ws://%s:%d", self._host, self._port)
        self._shutdown_task = asyncio.create_task(self._idle_shutdown())
        await self._server.wait_closed()
        await self._hub.shutdown()
        self._log.info("ToyServer stopped.")

    async def _idle_shutdown(self) -> None:
        """Sleep for _SHUTDOWN_DELAY, then tear down ToyHub and close the server."""
        try:
            await asyncio.sleep(_SHUTDOWN_DELAY)
        except asyncio.CancelledError:
            return
        self._log.info(
            "No clients connected for %.1fs. Shutting down.", _SHUTDOWN_DELAY
        )
        await self._hub.shutdown()
        if self._server is not None:
            self._server.close()

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """
        Manage the full lifecycle of a single WebSocket client connection.

        On connect: Cancels any pending idle-shutdown timer, registers the client in _clients, and calls ToyHub.startup().

        While connected: Reads messages from the client in a loop, spawning a new task per message, so slow commands don't block later ones.

        On disconnect (normal close or connection error):
        - Removes the client from _clients and _scan_subscribers.
        - Stops the BLE scan if this was the last scan subscriber.
        - Starts the idle-shutdown timer if no other clients remain.

        Args:
            ws: The WebSocket connection object for the newly connected client.
        """
        if self._shutdown_task is not None:
            self._shutdown_task.cancel()
            self._shutdown_task = None

        self._clients.add(ws)
        await self._hub.startup()
        self._log.debug("Client connected (%d total).", len(self._clients))

        try:
            async for raw in ws:
                # Spawn a task per message so the receiver loop stays responsive.
                asyncio.get_running_loop().create_task(
                    self._handle_message(ws, raw), name="handle-message"
                )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            async with self._scan_lock:
                self._scan_subscribers.discard(ws)
                if not self._scan_subscribers:
                    try:
                        await self._hub.stop_scan()
                    except Exception:
                        pass

            self._log.debug("Client disconnected (%d remaining).", len(self._clients))

            if not self._clients:
                self._shutdown_task = asyncio.get_running_loop().create_task(
                    self._idle_shutdown(), name="idle-shutdown"
                )

    async def _handle_message(self, ws: ServerConnection, raw_msg: str) -> None:
        """
        Parse, validate, and dispatch a single incoming message.

        Args:
            ws: The WebSocket connection object of the client that sent the message.
            raw_msg: The raw message string received from the client.
        """

        # 1. Parse and validate the outer envelope.
        try:
            envelope = RequestEnvelope.model_validate_json(raw_msg)
        except ValidationError as e:
            details = traceback.format_exc()
            self._log.warning(
                "Invalid request envelope: '%s' with details: '%s'", e, details
            )
            await self._send_raw(
                ws,
                ResponseEnvelope(
                    reply="?",
                    id="?",
                    success=False,
                    data=ErrorData(
                        error="Malformed Request",
                        message=_ErrMsg.MALFORMED_REQUEST,
                        traceback=details,
                    ).model_dump(),
                ).model_dump_json(),
            )
            return

        cmd = envelope.request
        req_id = envelope.id

        # 2. Look up the command in the registry.
        entry = _COMMAND_REGISTRY.get(cmd)
        if entry is None:
            self._log.warning("Unknown command: '%s' encountered.", cmd)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Unknown Command",
                    message=_ErrMsg.UNKNOWN_COMMAND.format(cmd=cmd),
                    traceback=None,
                ).model_dump(),
                success=False,
            )
            return

        # 3. Validate the inner data payload.
        try:
            data = entry.req_model(**envelope.data)
        except ValueError as e:
            self._log.warning("Invalid request data for '%s': '%s'", cmd, e)
            tb = traceback.format_exc()
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Invalid Data",
                    message=_ErrMsg.INVALID_DATA.format(cmd=cmd, detail=tb),
                    traceback=tb,
                ).model_dump(),
                success=False,
            )
            return

        # 4. Dispatch.
        try:
            if entry.is_scan:
                await self._handle_scan(ws, req_id, cmd, entry.resp_model)
                return

            result = await entry.handler(self._hub, data)
            await self._send_response(
                ws, req_id, cmd, entry.resp_model(**result).model_dump(), success=True
            )

        except UndiscoveredToyError as e:
            tb = traceback.format_exc()
            self._log.warning("Attempted to add undiscovered toy: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Undiscovered Toy",
                    message=_ErrMsg.UNDISCOVERED_TOY_ERROR.format(toy_id=e.toy_id),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                ).model_dump(),
                success=False,
            )
        except UnavailableToyError as e:
            tb = traceback.format_exc()
            self._log.warning("Attempted to add unavailable toy: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Unavailable Toy",
                    message=_ErrMsg.UNAVAILABLE_TOY_ERROR.format(toy_id=e.toy_id),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                ).model_dump(),
                success=False,
            )
        except ToyAlreadyAddedError as e:
            tb = traceback.format_exc()
            self._log.warning("Attempted to add already-added toy: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Toy Already Added",
                    message=_ErrMsg.TOY_ALREADY_ADDED_ERROR.format(toy_id=e.toy_id),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                ).model_dump(),
                success=False,
            )
        except AddConnectionError as e:
            tb = traceback.format_exc()
            self._log.warning(
                "Attempted to add toy, encountered Connection Issue: '%s'", tb
            )
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Connection Error",
                    message=_ErrMsg.ADD_CONNECTION_ERROR.format(toy_id=e.toy_id),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                ).model_dump(),
                success=False,
            )
        except InvalidModelError as e:
            tb = traceback.format_exc()
            self._log.warning("Attempted to add toy with invalid model_name: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Invalid Model",
                    message=_ErrMsg.INVALID_MODEL_ERROR.format(
                        model_name=e.model_name, toy_id=e.toy_id
                    ),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                    brand=e.brand,
                ).model_dump(),
                success=False,
            )
        except BadModelError as e:
            tb = traceback.format_exc()
            self._log.error("Encountered Bad Model: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Bad Model",
                    message=_ErrMsg.BAD_MODEL_ERROR.format(
                        model_name=e.model_name, toy_id=e.toy_id
                    ),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                ).model_dump(),
                success=False,
            )
        except UnknownToyError as e:
            tb = traceback.format_exc()
            self._log.warning("Attempted to operate on unknown toy: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Unknown Toy",
                    message=_ErrMsg.UNKNOWN_TOY_ERROR.format(toy_id=e.toy_id, cmd=cmd),
                    traceback=tb,
                    toy_id=e.toy_id,
                ).model_dump(),
                success=False,
            )
        except ToyConnectionError as e:
            tb = traceback.format_exc()
            self._log.warning("Failed to send command to toy: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Connection Error",
                    message=_ErrMsg.TOY_CONNECTION_ERROR.format(
                        toy_id=e.toy_id, cmd=e.cmd
                    ),
                    traceback=tb,
                    toy_id=e.toy_id,
                    model_name=e.model_name,
                ).model_dump(),
                success=False,
            )
        except Exception:
            tb = traceback.format_exc()
            self._log.error("Unhandled exception: '%s'", tb)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Developer Error",
                    message=_ErrMsg.DEVELOPER_ERROR.format(details=tb),
                    traceback=tb,
                ).model_dump(),
                success=False,
            )

    async def _handle_scan(
        self, ws: ServerConnection, req_id: str, cmd: str, resp_model: type[BaseModel]
    ) -> None:
        """Route start_scan / stop_scan to the appropriate subscription method."""
        if cmd == "start_scan":
            await self._handle_subscribe(ws, req_id, cmd, resp_model)
        else:
            await self._handle_unsubscribe(ws, req_id, cmd, resp_model)

    async def _handle_subscribe(
        self, ws: ServerConnection, req_id: str, cmd: str, resp_model: type[BaseModel]
    ) -> None:
        """Subscribe *ws* to scan results, starting the scan if this is the first subscriber."""
        try:
            async with self._scan_lock:
                self._scan_subscribers.add(ws)
                if len(self._scan_subscribers) == 1:
                    await self._hub.start_scan(self._on_scan_update)
            await self._send_response(
                ws, req_id, cmd, resp_model(ack=True).model_dump(), success=True
            )
        except DiscoveryStartError:
            tb = traceback.format_exc()
            self._scan_subscribers.discard(ws)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Discovery Start Error",
                    message=_ErrMsg.DISCOVERY_START_ERROR,
                    traceback=tb,
                ).model_dump(),
                success=False,
            )
        except Exception:
            tb = traceback.format_exc()
            self._scan_subscribers.discard(ws)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Developer Error",
                    message=_ErrMsg.DEVELOPER_ERROR.format(details=tb),
                    traceback=tb,
                ).model_dump(),
                success=False,
            )

    async def _handle_unsubscribe(
        self, ws: ServerConnection, req_id: str, cmd: str, resp_model: type[BaseModel]
    ) -> None:
        """Unsubscribe *ws* from scan results, stopping the scan when no subscribers remain."""
        async with self._scan_lock:
            self._scan_subscribers.discard(ws)
            if not self._scan_subscribers:
                try:
                    await self._hub.stop_scan()
                except Exception:
                    pass
        await self._send_response(
            ws, req_id, cmd, resp_model(ack=True).model_dump(), success=True
        )

    # Messaging helpers

    async def _send_response(
        self, ws: ServerConnection, req_id: str, cmd: str, data: dict, *, success: bool
    ) -> None:
        """Serialize and send a response envelope to *ws*."""
        resp = ResponseEnvelope(reply=cmd, id=req_id, success=success, data=data)
        await self._send_raw(ws, resp.model_dump_json())

    @staticmethod
    async def _send_raw(ws: ServerConnection, msg: str) -> None:
        """Send a JSON string to a single client, silently dropping if already gone."""
        try:
            await ws.send(msg)
        except Exception:
            pass

    async def _broadcast(
        self, event_name: str, payload: dict, *, success: bool = True
    ) -> None:
        """Broadcast an event to all connected clients."""
        clients = set(self._clients)
        if not clients:
            return
        msg = EventEnvelope(
            event=event_name, success=success, data=payload
        ).model_dump_json()
        await asyncio.gather(*(ws.send(msg) for ws in clients), return_exceptions=True)

    async def _broadcast_to_subscribers(
        self, event_name: str, payload: dict, *, success: bool = True
    ) -> None:
        """Broadcast an event to scan-subscribed clients only."""
        subscribers = set(self._scan_subscribers)
        if not subscribers:
            return
        msg = EventEnvelope(
            event=event_name, success=success, data=payload
        ).model_dump_json()
        await asyncio.gather(
            *(ws.send(msg) for ws in subscribers), return_exceptions=True
        )

    # ToyHub callbacks -> event broadcasts

    async def _on_status_change(self, toy_id: str, status: ToyStatus) -> None:
        """Broadcast a ``connection_status_changed`` event to all connected clients when a toy's connection status changes."""
        await self._broadcast(
            "connection_status_changed", dict(toy_id=toy_id, status=status.name)
        )

    async def _on_toy_ids_change(self, toy_ids: list[str]) -> None:
        """Broadcast a ``toy_ids_changed`` event to all connected clients when the set of managed toys changes."""
        await self._broadcast("toy_ids_changed", dict(toy_ids=toy_ids))

    async def _on_toy_state_change(self, state: dict) -> None:
        """Broadcast a ``toy_state_changed`` event to all connected clients when any part of a toy's state changes."""
        await self._broadcast("toy_state_changed", state)

    async def _on_model_change(self, update: dict) -> None:
        """Broadcast a ``model_changed`` event to all connected clients when a toy's assigned model name changes."""
        await self._broadcast("model_changed", update)

    async def _on_battery_change(self, updates: dict[str, int | None]) -> None:
        """Broadcast a ``battery_changed`` event to all connected clients when one or more toys report a new battery level."""
        await self._broadcast("battery_changed", updates)

    async def _on_scan_update(self, update: Exception | list[dict]) -> None:
        """Forward a ``scan_update`` event to scan-subscribed clients only."""
        if isinstance(update, DiscoveryError):
            await self._broadcast_to_subscribers(
                "scan_update",
                dict(
                    error="Discovery Error",
                    message=_ErrMsg.DISCOVER_ERROR,
                    traceback=update.tb,
                ),
                success=False,
            )
        elif isinstance(update, Exception):
            details = traceback.format_exception(update)
            await self._broadcast_to_subscribers(
                "scan_update",
                dict(
                    error="Developer Error",
                    message=_ErrMsg.DEVELOPER_ERROR.format(details=details),
                    traceback=details,
                ),
            )
        else:
            await self._broadcast_to_subscribers(
                "scan_update", dict(discovered=update), success=True
            )
