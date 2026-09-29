"""
WebSocket JSON-based server that exposes _ToyHub to connected clients.
Offers an alternative to the Low-Level / High-Level API defined by the tikal library.
Here information is exchanged via a websocket. Significantly harder to use than the Low-Level / High-Level APIs but
offers some advantages:

- Process separation
- Service can be used in applications written in other programming languages (assuming websockets are supported)
- Multiple clients can modify the same state (experimental, untested)

**Protocol**

Request  (client -> server):

.. code-block:: json

    {"request": "some_command", "id": "some_id", "data": {...}}

Response (server -> client):

.. code-block:: json

    {"reply": "some_command", "id": "some_id", "success": true,  "data": { ... }}
    {"reply": "some_command", "id": "some_id", "success": false, "data": {"error": "...", "message": "...", ...}}

Event (server -> all clients / scan subscribers):

.. code-block:: json

    {"event": "some_event", "success": true,  "data": {...}}
    {"event": "some_event", "success": false, "data": {"error": "...", "message": "..."}}

The success field lets you branch between error handling / normal operation without having to inspect data.

**Examples**:

Request:

.. code-block:: json

    {
        "request": "get_battery",
        "id": "some_id",
        "data": {"toy_id": "some_toy_id"}
    }

Response:

.. code-block:: json

    {
        "reply": "get_battery",
        "id": "some_id",
        "success": True,
        "data": {"battery": 85, "toy_id": "some_toy_id"}
    }

Error response:

.. code-block:: json

    {
        "reply": "get_battery",
        "id": "some_id",
        "success": False,
        "data": {"error": "UnknownToyError", "message": "Unable to execute 'get_battery' on 'some_toy_id'. Please add the toy first."}
    }

Event:

.. code-block:: json

    {
        "event": "on_status_change",
        "success": True,
        "data": {"toy_id": "some_toy_id", "status": "RECONNECTING"}
    }

**Architecture**

Each command is described by a CommandEntry dataclass that bundles:
  - Request_model   : Pydantic model that validates the incoming data object.
  - Response_model  : Pydantic model that validates (and serializes) the outgoing data.
  - Handler         : async callable(hub: _ToyHub, data: req_model) -> dict that performs the actual work and returns the raw result dict.

_handle_message is a *generic* dispatcher: validate -> look up entry -> validate inner data -> call handler -> validate response -> send.
The commands start_scan / stop_scan are a special case as they require a reference to the per-client WebSocket connection.
They are handled by dedicated methods flagged via CommandEntry.is_scan.
"""

import asyncio
import datetime
import http
import ipaddress
import logging
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import websockets
from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from ._status_page import ToyServerStatusPage
from ._toy_hub import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    SafetyHoldError,
    ToyAlreadyAddedError,
    ToyConnectionError,
    ToyStatus,
    UnavailableToyError,
    UndiscoveredToyError,
    UnknownToyError,
    _ToyHub,
)
from .toy_server_models import (
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
    HeartbeatEnableData,
    InfoResponseData,
    IntensityData,
    IntensityLimitData,
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

# -----------------------------------------------------------------------------
# Handler functions
# -----------------------------------------------------------------------------


async def _cmd_get_brands(hub: _ToyHub, _: _EmptyData) -> dict[str, Any]:
    """
    Return the full brand -> model-name mapping from _ToyHub.

    Args:
        hub: _ToyHub instance.
        _: Unused empty data payload.

    Returns:
         A mapping of brand names to lists of supported model names, e.g. {"Lovense": ["Gush", "Solace"]}.
    """
    return {"brands": await hub.get_brands()}


async def _cmd_get_toy_ids(hub: _ToyHub, _: _EmptyData) -> dict[str, Any]:
    """
    Return a snapshot of all toy identifiers currently managed by the Server.

    Args:
        hub: _ToyHub instance.
        _: Unused empty data payload.

    Returns:
        dict with key "toy_ids": a list of toy identifier strings.
    """
    return {"toy_ids": await hub.get_toy_ids()}


async def _cmd_get_state(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Returns the current state of a toy. This is an inexpensive in-memory read (no BLE communication).

    State information contains:
    -  `toy_id` (str) Unique identifier of the toy
    -  `current_intensity` (list[int, int]) Current intensity values. The second value is always zero if the toy only has one intensity.
    -  `intensity_limits` (list[int, int]) Current intensity limits. All intensity commands are clamped to these values.
    -  `is_blocked` (bool) Whether the toy is currently blocked (toy's intensities are forced to zero)
    -  `pattern_version` (int) Each time the pattern state changes, the version number is incremented
    -  `pattern` (list[tuple[int, int, int]]) List of tuples (duration, intensity1, intensity2) defining the pattern segment
    -  `wraparound` (bool)  Whether the pattern repeats from the beginning after completing the last segment. If False, both Intensities are 0 after the last segment
    -  `is_paused` (bool) Whether the toy is currently paused (patterns do not advance)
    -  `elapsed` (float) Time elapsed since the start of the pattern or last wraparound in ms
    Args:
        hub: _ToyHub instance managing the toy
        data: Validated ToyIdData instance, containing the toy_id

    Raises:
        UnknownToyError: The toy was not added before.

    Returns:
        dict with keys as described above
    """
    return await hub.get_state(data.toy_id)


async def _cmd_get_connection_status(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Return the current connection status of a toy.

    Args:
        hub: _ToyHub instance.
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


async def _cmd_get_battery(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Retrieve the in-memory battery level for a toy. No communication to the toy is performed. Kept up to date in the background
    Args:
        hub: _ToyHub instance managing the toy
        data: Validated ToyIdData instance, containing the toy_id

    Raises:
        UnknownToyError: The toy was not added before.
    """
    return {
        "battery": await hub.get_battery(data.toy_id),
        "toy_id": data.toy_id,
    }


async def _cmd_get_info(hub: _ToyHub, data: GetInfoData) -> dict[str, Any]:
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
        hub:   _ToyHub instance managing the toy
        data:   Validated GetInfoData instance, containing the toy_id and full flag.
                If full is True, additional brand-dependent information is returned.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy was not added before.

    Returns:
        dict: dictionary containing the gathered info.
    """
    return await hub.get_info(data.toy_id, data.full)


async def _cmd_get_all(hub: _ToyHub, data: GetInfoData) -> dict[str, Any]:
    """
    Return combined state, info, connection status, get_battery for a toy.

    Args:
        hub:   _ToyHub instance managing the toy
        data:   Validated GetInfoData instance, containing the toy_id and full flag.
                If full is True, additional brand-dependent information is returned.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy was not added before.

    Returns:
        dict: dictionary containing the gathered info.
    """
    return await hub.get_all(data.toy_id, data.full)


async def _cmd_direct_command(hub: _ToyHub, data: DirectCommandData) -> dict[str, Any]:
    """
    Send a raw command string directly to a toy. Use this to access toy functionality not exposed by the API.
    Do not use it to change the tracked state (e.g., intensities) as _ToyHub will not be aware of the resulting state change.

    Args:
        hub: _ToyHub instance managing the toy.
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


async def _cmd_change_rotation_direction(
    hub: _ToyHub, data: ToyIdData
) -> dict[str, Any]:
    """
    Toggle the rotation direction of a toy if the toy supports it.

    Args:
        hub: _ToyHub instance managing the toy.
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


async def _cmd_add(hub: _ToyHub, data: AddRequestData) -> dict[str, Any]:
    """
    Connect to a discovered toy and register it on the Server.

    Args:
        hub: _ToyHub instance.
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


async def _cmd_remove(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Disconnect a toy, then deregister it from the Server.

    Args:
        hub: _ToyHub instance.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Proper disconnect failed. Only for info purposes. Toy is still removed.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.remove(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_model(hub: _ToyHub, data: SetModelData) -> dict[str, Any]:
    """
    Change the model name assigned to an already-added toy.

    Args:
        hub: _ToyHub instance.
        data: Validated SetModelData containing toy_id and the new model_name.

    Raises:
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack ( Always True) and toy_id.
    """
    await hub.set_model(data.toy_id, data.model_name)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_stop(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Stop the toy by setting both intensities to zero and pausing any active pattern.

    Args:
        hub: _ToyHub instance managing the toy.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.stop(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_intensity1(hub: _ToyHub, data: IntensityData) -> dict[str, Any]:
    """
    Set the primary intensity of a toy and pause any active pattern.

    Args:
        hub: _ToyHub instance managing the toy.
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


async def _cmd_intensity2(hub: _ToyHub, data: IntensityData) -> dict[str, Any]:
    """
    Set the secondary intensity of a toy and pause any active pattern. For single-intensity toys, this command has no effect.

    Args:
        hub: _ToyHub instance managing the toy.
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


async def _cmd_toggle_pause(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Toggle the pause state of a toy's pattern playback.

    When paused, the pattern timer freezes and both intensities are set to zero.
    When resumed, playback continues from the elapsed time at the point of pausing.
    Pausing a blocked toy clears its blocked state (A toy cannot be both paused and blocked simultaneously).

    Args:
        hub: _ToyHub instance managing the toy.
        data: Validated ``ToyIdData`` containing the target ``toy_id``.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (True) and toy_id.
    """
    await hub.toggle_pause(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_toggle_block(hub: _ToyHub, data: ToyIdData) -> dict[str, Any]:
    """
    Toggle the blocked state of a toy.

    When blocked, both intensities are forced to zero regardless of any active pattern or
    manual commands. Blocking a paused toy clears its pause state (a toy cannot be both paused and blocked simultaneously).
    Unblocking restores normal operation.

    Args:
        hub: _ToyHub instance managing the toy.
        data: Validated ToyIdData containing the target toy_id.

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.toggle_block(data.toy_id)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_paused(hub: _ToyHub, data: SetPausedData) -> dict[str, Any]:
    """
    Set the paused state of a toy's pattern playback.

    Pausing freezes the pattern timer and sets both intensities to zero.
    Resuming continues playback from the elapsed time at the point of pausing.
    Pausing a blocked toy clears its blocked state (A toy cannot be both paused and blocked simultaneously).

    Args:
        hub: _ToyHub instance managing the toy.
        data: Validated SetPausedData containing toy_id and pause (True to pause, False to resume).

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.set_paused(data.toy_id, data.pause)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_blocked(hub: _ToyHub, data: SetBlockedData) -> dict[str, Any]:
    """
    Set the blocked state of a toy.

    When blocked, both intensities are forced to zero regardless of any active pattern or
    manual commands. Blocking a paused toy clears its pause state (a toy cannot be both paused and blocked simultaneously).

    Args:
        hub: _ToyHub instance managing the toy.
        data: Validated SetBlockedData containing toy_id and block (True to block, False to unblock).

    Raises:
        ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
        UnknownToyError: The toy has not been added.

    Returns:
        dict with keys ack (Always True) and toy_id.
    """
    await hub.set_blocked(data.toy_id, data.block)
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_set_pattern(hub: _ToyHub, data: SetPatternData) -> dict[str, Any]:
    """
    Load a new intensity pattern onto a toy and start playback.

    Each segment of the pattern specifies duration_ms, intensity1, intensity2.
    The toy steps through segments in order, advancing every duration_ms millisecond.
    If wraparound is True, the pattern loops back to the first segment upon completing the last segment; else both intensities are set to zero and playback stops.

    Args:
        hub: _ToyHub instance managing the toy.
        data: Validated SetPatternData containing:
            - toy_id: Identifier of the target toy.
            - pattern: Sequence of (duration_ms, intensity1, intensity2) tuples.
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


async def _cmd_limit_noop(
    hub: _ToyHub, data: IntensityLimitData
) -> dict[str, Any]:  # pragma: no cover
    """
    Placeholder handler for limit commands (set_intensity1_limit / set_intensity2_limit).

    Limit commands need per-client tracking and are intercepted by _handle_limit before
    the generic dispatcher reaches this handler.
    """
    return {"ack": True, "toy_id": data.toy_id}


async def _cmd_scan_noop(
    hub: _ToyHub, data: _EmptyData
) -> dict[str, Any]:  # pragma: no cover
    """
    Placeholder handler for scan commands (start_scan / stop_scan).

    Scan commands need access to the per-client WebSocket connection for subscription management and are intercepted by
    _handle_scan before the generic dispatcher reaches this handler. This function should never be reached,
    it only serves as a placeholder for the CommandEntry.handler slot in _COMMAND_REGISTRY.
    """
    return {"ack": True}


async def _cmd_heartbeat_noop(
    hub: _ToyHub, data: Any
) -> dict[str, Any]:  # pragma: no cover
    """
    Placeholder handler for watchdog commands (enable_heartbeat / heartbeat / release_hold).

    Watchdog commands need access to the per-client WebSocket connection and the watchdog state, and are intercepted
    by _handle_heartbeat before the generic dispatcher reaches this handler.
    """
    return {"ack": True}


async def _cmd_shutdown(
    hub: _ToyHub, data: _EmptyData
) -> dict[str, Any]:  # pragma: no cover
    """
    Prepare the server to shut down as soon as the requesting client disconnects.
    The actual shutdown is triggered in _handle_connection after the client leaves.
    """
    return {"ack": True, "toy_id": None}


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
        req_model:      The Pydantic model used to validate (and coerce) the data field of the incoming RequestEnvelope.
        resp_model:     The Pydantic model used to validate the dict returned by the handler before it is serialized into the ResponseEnvelope.
        handler:        Async callable with the signature (hub: _ToyHub, data: req_model) -> dict that performs the command and returns a result dict.
        is_scan:        If True, the command is a scan subscription command (start_scan / stop_scan) and routed to _handle_scan
                        instead of the generic dispatcher. Handler is unused in that case.
        is_shutdown:    If True, the command is a shutdown command and routed to _handle_shutdown instead of the generic dispatcher.
                        Handler is unused in that case.
        is_heartbeat:   If True, the command is a watchdog command (enable_heartbeat / heartbeat / release_hold) and routed to _handle_heartbeat.
        is_limit:       If True, the command is a limit command (set_intensity1_limit / set_intensity2_limit) and routed to _handle_limit.
    """

    req_model: type[BaseModel]
    resp_model: type[BaseModel]
    handler: Callable[..., Any]
    is_scan: bool = False
    is_shutdown: bool = False
    is_heartbeat: bool = False
    is_limit: bool = False


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
    "set_intensity1_limit": CommandEntry(
        IntensityLimitData, AckData, _cmd_limit_noop, is_limit=True
    ),
    "set_intensity2_limit": CommandEntry(
        IntensityLimitData, AckData, _cmd_limit_noop, is_limit=True
    ),
    # Scan subscription commands
    "start_scan": CommandEntry(_EmptyData, AckData, _cmd_scan_noop, is_scan=True),
    "stop_scan": CommandEntry(_EmptyData, AckData, _cmd_scan_noop, is_scan=True),
    # Heartbeat commands
    "enable_heartbeat": CommandEntry(
        HeartbeatEnableData, AckData, _cmd_heartbeat_noop, is_heartbeat=True
    ),
    "heartbeat": CommandEntry(
        _EmptyData, AckData, _cmd_heartbeat_noop, is_heartbeat=True
    ),
    "release_hold": CommandEntry(
        _EmptyData, AckData, _cmd_heartbeat_noop, is_heartbeat=True
    ),
    # Shutdown
    "shutdown": CommandEntry(_EmptyData, AckData, _cmd_shutdown, is_shutdown=True),
}

_NO_LIMIT = 0x7FFF_FFFF

# WebSocket close code (from the 4000-4999 range reserved for applications) sent to a client that stayed overdue for
# the whole heartbeat grace period.
_CLOSE_HEARTBEAT_OVERDUE = 4000


class InsecureBindError(ValueError):
    """
    Raised when a :class:`ToyServer` is asked to bind a non-loopback host without opting into insecure mode.
    See ``docs/websocket/security.md``.
    """


def _is_loopback_host(host: str | None) -> bool:
    """
    Return ``True`` only if *host* provably refers to the loopback interface (safe to serve without auth).

    Fails closed: anything not provably loopback -- ``0.0.0.0``/``::`` (all interfaces), a LAN IP, a hostname,
    or ``None``/``""`` -- is treated as exposed so the caller must opt into it explicitly.
    """
    if not host:
        return False  # None / "" binds all interfaces -> exposed
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # hostnames and other unparseable values -> treat as exposed


# -----------------------------------------------------------------------------
# ToyServer
# -----------------------------------------------------------------------------


class ToyServer:
    """
    WebSocket server that wraps a _ToyHub instance.

    The server owns the _ToyHub and wires itself up as all of its callbacks. Create it, then await self.serve() to start accepting connections.
    """

    def __init__(
        self,
        toy_cache_path: Path = Path(),
        host: str = "localhost",
        port: int = 8142,
        idle_shutdown_delay: float = 3.0,
        mock_toys: bool = False,
        log_name: str = "tikal_ws",
        insecure: bool = False,
    ) -> None:
        """
        Initialize the server and wire it up to a new _ToyHub instance. Call await self.serve() to begin accepting connections.

        Args:
            toy_cache_path: Path to the toy-cache file used by _ToyHub to persist previously added toys across restarts. If empty, no persistent cache is used.
            host:           Network interface to bind the WebSocket server to. Defaults to "localhost".
                            tikal performs no authentication, so binding a non-loopback host is refused unless
                            ``insecure=True``; the supported way to expose it is behind a reverse proxy (TLS + auth)
                            with tikal on localhost. See ``docs/websocket/security.md``.
            port:           TCP port to listen on. Defaults to 8142.
            idle_shutdown_delay:  How long to wait for clients to disconnect before shutting down the server. Defaults to 3 seconds. If 0, the Server does not shut down automatically.
            mock_toys:      If True, _ToyHub uses mock toys instead of real toys.
            log_name:       Name of the Python logger used by both the server and the underlying _ToyHub instance. Defaults to "tikal_ws".
            insecure:       Allow binding a non-loopback ``host`` even though tikal has no built-in authentication.
                            Off by default: an exposed bind raises :class:`InsecureBindError`. Only set this when the
                            server is protected another way (reverse proxy, firewall, trusted LAN, or testing).

        Raises:
            InsecureBindError: ``host`` is not a loopback interface and ``insecure`` is False.
        """

        self._host = host
        self._port = port
        self._log = logging.getLogger(log_name)

        # Never expose an unauthenticated server to the network by accident.
        if not _is_loopback_host(host):
            if not insecure:
                raise InsecureBindError(
                    f"Refusing to bind ToyServer to non-loopback host {host!r}: tikal has no built-in "
                    f"authentication, so this would expose full toy control to anyone who can reach the port. "
                    f"Put a reverse proxy (TLS + auth) in front and keep tikal on localhost "
                    f"(see docs/websocket/security.md), or pass insecure=True (CLI: --insecure) to override."
                )
            self._log.warning(
                "ToyServer is bound to non-loopback host %r WITHOUT authentication (insecure=True). "
                "Anyone who can reach this port has full control of connected toys. Prefer a reverse "
                "proxy (TLS + auth) in front of a localhost bind. See docs/websocket/security.md.",
                host,
            )

        # All currently connected WebSocket clients.
        self._clients: set[ServerConnection] = set()
        # Clients that requested shutdown; the server stops once they disconnect.
        self._shutdown_requested_clients: set[ServerConnection] = set()
        # Subset of _clients that have subscribed to scan results.
        self._scan_subscribers: set[ServerConnection] = set()
        # Lock that serializes start_scan/stop_scan calls.
        self._scan_lock = asyncio.Lock()
        # Task that fires idle_shutdown_delay seconds after the last client leaves and shuts down the server.
        self.idle_shutdown_delay = idle_shutdown_delay
        self._shutdown_task: asyncio.Task[None] | None = None
        # The underlying websockets server object; set in serve().
        self._server: websockets.Server | None = None

        self._shutdown_initiated = False

        # Heartbeat watchdog: maps subscribed ws -> last heartbeat timestamp (time.monotonic)
        self._heartbeat_clients: dict[ServerConnection, float] = {}
        # Armed clients that are currently past their deadline. They stay in _heartbeat_clients (and so stay watched)
        # until they disarm or disconnect; while any of them is overdue, the safety hold stays on.
        self._heartbeat_timed_out: set[ServerConnection] = set()
        # Set when an armed client disconnected. It can never prove it is back, so only release_hold clears this.
        self._disconnect_hold = False
        # Whether the safety hold is currently applied to the hub. Only changed under _hold_lock, which also keeps
        # heartbeat_timeout / hold_released broadcasts in the order the hold actually changed.
        self._hold_active = False
        self._hold_lock = asyncio.Lock()
        self._heartbeat_timeout = 3.0  # seconds
        self._heartbeat_check_interval = 1.0  # seconds
        # How long a client may stay overdue before it is treated as disconnected and its connection is closed.
        self._heartbeat_grace_period = 30.0  # seconds
        # Clients disconnected for staying overdue whose connection is still closing. Their messages are ignored.
        self._heartbeat_kicked: set[ServerConnection] = set()
        self._kick_tasks: set[asyncio.Task[None]] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Per-client intensity limits: ws -> toy_id -> [limit1_or_None, limit2_or_None]
        self._client_limits: dict[ServerConnection, dict[str, list[int | None]]] = {}

        self._hub = _ToyHub(
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
        self._status_page = ToyServerStatusPage(self._hub, host, port)

    # Lifecycle

    async def serve(self) -> None:
        """
        Start the WebSocket server and block until it shuts itself down.

        The teardown runs in a ``finally``: a toy keeps doing whatever it was last told once this process is gone, so
        the hub has to get its chance to stop and disconnect every toy even when this coroutine is **cancelled**
        (which is what ``asyncio.run`` does on Ctrl+C).
        """
        self._server = await serve(
            self._handle_connection,
            self._host,
            self._port,
            process_request=self._handle_http_request,
            # Spelled out rather than left to the library defaults, which the watchdog's timing relies on: a client
            # whose connection stops answering pings is closed after at most ping_interval + ping_timeout, and closing
            # a client that never answers the closing handshake takes close_timeout.
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        )
        self._log.info("ToyServer listening on ws://%s:%d", self._host, self._port)
        self._status_page.set_start_time(datetime.datetime.now())
        self._shutdown_task = asyncio.create_task(self._idle_shutdown())
        try:
            await self._server.wait_closed()
        finally:
            try:
                await self._hub.shutdown()  # idempotent; stops and disconnects every toy
            except Exception:
                self._log.exception("Error while shutting down the toy hub.")
            self._log.info("ToyServer stopped.")

    async def shutdown(self) -> None:
        """
        Stop the server and make every toy safe: stops and disconnects all toys, then closes the listening socket.

        Idempotent, and safe to call from a signal handler. :meth:`serve` returns once this completes.
        """
        await self._shutdown()

    async def _idle_shutdown(self) -> None:
        """Sleep for self.idle_shutdown_delay, then tear down _ToyHub, and close the server."""
        if self.idle_shutdown_delay <= 0:
            # Auto-shutdown disabled: the server stays up until an explicit shutdown request.
            self._log.info("Idle shutdown disabled (delay <= 0); server will stay up.")
            return
        self._log.info("Entering IdleShutdown")
        try:
            await asyncio.sleep(self.idle_shutdown_delay)
        except asyncio.CancelledError:
            return
        self._log.info(
            "No clients connected for %.1fs. Shutting down.", self.idle_shutdown_delay
        )
        await self._shutdown()

    async def _shutdown(self) -> None:
        """Tear down _ToyHub and close the server."""
        await self._hub.shutdown()  # idempotent
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        self._heartbeat_clients.clear()
        self._heartbeat_timed_out.clear()
        self._heartbeat_kicked.clear()
        self._disconnect_hold = False  # the hub has disconnected every toy, so there is nothing left to hold
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._server is not None:
            self._server.close()

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """
        Manage the full lifecycle of a single WebSocket client connection.

        On connect: Cancels any pending idle-shutdown timer, registers the client in _clients, and calls _ToyHub.startup().

        While connected: Reads messages from the client in a loop, spawning a new task per message, so slow commands don't block later ones.

        On disconnect (normal close or connection error):
        - Removes the client from _clients and _scan_subscribers.
        - Makes the toys safe: an armed heartbeat client vanishing puts on the safety hold until a client sends
          release_hold; the *last* client leaving stops every toy and pauses its pattern, because nothing is watching
          them any more and idle shutdown may be disabled or still seconds away. Both apply when the last client was
          an armed one.
        - Stops the BLE scan if this was the last scan subscriber.
        - Drops this client's intensity limits and re-applies the remaining clients' ceiling.
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
                if ws in self._heartbeat_kicked:
                    # The server gave up on this client (see _disconnect_overdue_client). Whatever it still sends
                    # while its connection closes must not count, e.g. a release_hold for the hold it caused.
                    continue
                # Spawn a task per message so the receiver loop stays responsive.
                message = raw if isinstance(raw, str) else raw.decode("utf-8")
                asyncio.get_running_loop().create_task(
                    self._handle_message(ws, message), name="handle-message"
                )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            self._heartbeat_kicked.discard(ws)
            was_heartbeat_client = self._heartbeat_clients.pop(ws, None) is not None
            if was_heartbeat_client:
                # Set before it leaves _heartbeat_timed_out, so the hold never looks releasable in between.
                self._disconnect_hold = True
            self._heartbeat_timed_out.discard(ws)
            if not self._heartbeat_clients and self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            if was_heartbeat_client:
                # Dead-man's switch: a client that armed the heartbeat vanished (crash or abrupt close). It can never
                # send the heartbeat that would end the hold, so the hold stays on until a client sends release_hold.
                await self._trip_watchdog(
                    "disconnect",
                    "Heartbeat client disconnected. All toys held until a client sends release_hold.",
                )
            if not self._clients:
                # Nobody is watching any more, so a running pattern would keep driving the toy indefinitely: idle
                # shutdown may be disabled (idle_shutdown_delay <= 0) or still seconds away. This has to happen
                # before the scan/limit cleanup below, which can block for seconds on stopping the BLE scan.
                try:
                    await self._stop_all_toys("the last client disconnected")
                except Exception:
                    self._log.exception(
                        "Failed to stop toys after the last client disconnected."
                    )
            async with self._scan_lock:
                self._scan_subscribers.discard(ws)
                if not self._scan_subscribers:
                    try:
                        await self._hub.stop_scan()
                    except Exception:
                        pass

            client_toys = self._client_limits.pop(ws, {})
            for toy_id, limits in client_toys.items():
                for axis in (0, 1):
                    if limits[axis] is not None:
                        effective = self._compute_effective_limit(toy_id, axis)
                        try:
                            if axis == 0:
                                await self._hub.set_intensity1_limit(toy_id, effective)
                            else:
                                await self._hub.set_intensity2_limit(toy_id, effective)
                        except Exception:
                            pass

            if ws in self._shutdown_requested_clients:
                self._shutdown_requested_clients.discard(ws)
                await self._shutdown()
                self._log.info("Client disconnected (shutdown requested).")
            else:
                self._log.info(
                    "Client disconnected (%d remaining).", len(self._clients)
                )
                if not self._clients:
                    # The toys were already stopped at the top of this block; only the shutdown decision is left.
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

            if entry.is_heartbeat:
                await self._handle_heartbeat(ws, req_id, cmd, data, entry.resp_model)
                return

            if entry.is_limit:
                await self._handle_limit(ws, req_id, cmd, data, entry.resp_model)
                return

            if entry.is_shutdown:
                self._shutdown_requested_clients.add(ws)
                await self._send_response(
                    ws,
                    req_id,
                    cmd,
                    entry.resp_model(ack=True).model_dump(),
                    success=True,
                )
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
        except SafetyHoldError as e:
            self._log.info("Refused '%s' on %s: safety hold.", cmd, e.toy_id)
            await self._send_response(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Safety Hold",
                    message=_ErrMsg.SAFETY_HOLD_ERROR.format(toy_id=e.toy_id, cmd=cmd),
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

    async def _handle_heartbeat(
        self,
        ws: ServerConnection,
        req_id: str,
        cmd: str,
        data: Any,
        resp_model: type[BaseModel],
    ) -> None:
        """Route enable_heartbeat / heartbeat / release_hold to the appropriate handler."""
        if cmd == "release_hold":
            # Ends a hold caused by a disconnected client. A client that is still overdue keeps the hold on until it
            # is back (or gone, which turns it into a disconnect hold that this command then releases).
            if self._disconnect_hold:
                self._log.info("release_hold received; clearing the disconnect hold.")
            self._disconnect_hold = False
            await self._release_hold_if_clear()
        elif cmd == "enable_heartbeat":
            if data.enable:
                self._heartbeat_clients[ws] = time.monotonic()
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.get_running_loop().create_task(
                        self._heartbeat_check_loop(), name="heartbeat-check"
                    )
                # Re-arming is proof of life just like a heartbeat is.
                await self._clear_heartbeat_timeout(ws)
            else:
                self._heartbeat_clients.pop(ws, None)
                await self._clear_heartbeat_timeout(ws)
                if not self._heartbeat_clients and self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None
        else:
            if ws in self._heartbeat_clients:
                self._heartbeat_clients[ws] = time.monotonic()
                await self._clear_heartbeat_timeout(ws)
            else:
                self._log.debug("Heartbeat received from non-subscribed client.")

        await self._send_response(
            ws, req_id, cmd, resp_model(ack=True).model_dump(), success=True
        )

    async def _clear_heartbeat_timeout(self, ws: ServerConnection) -> None:
        """
        Mark a previously overdue client as alive again, and end the safety hold if nothing else calls for it.

        Called from every path where an armed client proves it is still there (a ``heartbeat``, a re-arm, or a
        deliberate opt-out). A client that simply vanished never reaches this; it leaves a disconnect hold instead.
        """
        if ws not in self._heartbeat_timed_out:
            return
        self._heartbeat_timed_out.discard(ws)
        self._log.info(
            "Heartbeat client recovered; %d still overdue.",
            len(self._heartbeat_timed_out),
        )
        await self._release_hold_if_clear()

    async def _handle_limit(
        self,
        ws: ServerConnection,
        req_id: str,
        cmd: str,
        data: Any,
        resp_model: type[BaseModel],
    ) -> None:
        """Store per-client limit, compute effective minimum, and forward to _ToyHub."""
        toy_id = data.toy_id
        axis = 0 if cmd == "set_intensity1_limit" else 1

        client_toys = self._client_limits.setdefault(ws, {})
        toy_limits = client_toys.setdefault(toy_id, [None, None])
        old_value = toy_limits[axis]
        toy_limits[axis] = data.limit  # None = "I don't want to limit"

        effective = self._compute_effective_limit(toy_id, axis)
        try:
            if axis == 0:
                await self._hub.set_intensity1_limit(toy_id, effective)
            else:
                await self._hub.set_intensity2_limit(toy_id, effective)
        except ToyConnectionError:
            # The ceiling was recorded on the toy controller and governs every later command and playback tick. Only
            # the immediate corrective send failed (which already kicked off the reconnect path).
            raise
        except Exception:
            toy_limits[axis] = old_value
            if toy_limits == [None, None]:
                client_toys.pop(toy_id, None)
            if not client_toys:
                self._client_limits.pop(ws, None)
            raise

        await self._send_response(
            ws,
            req_id,
            cmd,
            resp_model(ack=True, toy_id=toy_id).model_dump(),
            success=True,
        )

    def _compute_effective_limit(self, toy_id: str, axis: int) -> int | None:
        """Minimum of all clients' limits for a toy axis, or None if no limit is set."""
        values: list[int] = []
        for client_toys in self._client_limits.values():
            limits = client_toys.get(toy_id)
            if limits is not None:
                value = limits[axis]
                if value is not None:
                    values.append(value)
        return min(values) if values else None

    async def _heartbeat_check_loop(self) -> None:
        """
        Background loop that checks heartbeat deadlines and puts on the safety hold on timeout.

        An overdue client stays in ``_heartbeat_clients``, so it stays watched and this loop keeps running: the
        watchdog never disarms itself just because it fired. It is tracked in ``_heartbeat_timed_out`` instead, which
        keeps the hold on (and stops this loop from firing again for it every interval) until that client proves it
        is alive again or disconnects. A client still overdue after the grace period is treated as disconnected (see
        :meth:`_disconnect_overdue_client`).
        """
        while self._heartbeat_clients:
            await asyncio.sleep(self._heartbeat_check_interval)
            now = time.monotonic()
            newly_timed_out = [
                ws
                for ws, last in self._heartbeat_clients.items()
                if (now - last) > self._heartbeat_timeout
                and ws not in self._heartbeat_timed_out
            ]
            if newly_timed_out:
                self._log.warning(
                    "Heartbeat timeout for %d client(s). Holding all toys.",
                    len(newly_timed_out),
                )
                self._heartbeat_timed_out.update(newly_timed_out)
                # Shielded: this loop is cancelled when the last armed client leaves, which is often right after it
                # went overdue (a frozen app gets killed). Cancelling mid-trip could leave toys that were never
                # stopped while the hold already counts as on.
                await asyncio.shield(
                    self._trip_watchdog(
                        "timeout",
                        "Heartbeat timeout. All toys held until a heartbeat is received again.",
                    )
                )

            # Measured again: the trip above awaited toy commands, and a heartbeat may have arrived meanwhile.
            now = time.monotonic()
            limit = self._heartbeat_timeout + self._heartbeat_grace_period
            given_up = [
                ws
                for ws, last in self._heartbeat_clients.items()
                if (now - last) > limit
            ]
            for ws in given_up:
                # Shielded for the same reason as the trip above.
                await asyncio.shield(self._disconnect_overdue_client(ws))

    async def _disconnect_overdue_client(self, ws: ServerConnection) -> None:
        """
        Give up on a client that stayed overdue for the whole grace period: treat it as disconnected and close it.

        A client whose app is stuck can keep its connection open (a browser answers pings even while the page's
        JavaScript is hung), and ``release_hold`` deliberately cannot override a client that is merely overdue. Without
        this, such a client could keep the hold on forever. From here on it is a disconnect hold, which any client can
        end with ``release_hold``.

        The client leaves the watchdog right away, and whatever it still sends while its connection closes is ignored
        (see :meth:`_handle_connection`), so it can neither end the hold itself nor re-arm. The close runs in the
        background: a stuck client may never answer the closing handshake, and waiting for ``close_timeout`` would
        delay the heartbeat checks of every other client.
        """
        # Set before it leaves _heartbeat_timed_out, so the hold never looks releasable in between.
        self._disconnect_hold = True
        self._heartbeat_clients.pop(ws, None)
        self._heartbeat_timed_out.discard(ws)
        self._heartbeat_kicked.add(ws)
        self._log.warning(
            "Heartbeat client overdue for more than %.0f s. Treating it as disconnected.",
            self._heartbeat_grace_period,
        )
        await self._trip_watchdog(
            "disconnect",
            "Heartbeat client stayed overdue and was disconnected. All toys held until a client sends release_hold.",
        )
        task = asyncio.get_running_loop().create_task(
            ws.close(code=_CLOSE_HEARTBEAT_OVERDUE, reason="Heartbeat overdue"),
            name="close-overdue-client",
        )
        # Keep a reference: a bare create_task may be garbage-collected before the close completes.
        self._kick_tasks.add(task)
        task.add_done_callback(self._kick_tasks.discard)

    async def _stop_all_toys(self, reason: str) -> list[str]:
        """
        Stop every managed toy, which also freezes its pattern playback.

        Never raises: a toy that cannot be reached is logged and reported back, because the remaining toys still
        have to be stopped.

        Args:
            reason: Short phrase for the log line explaining why the toys are being stopped.

        Returns:
            The ids of the toys that could **not** be stopped.
        """
        failed: list[str] = []
        for toy_id in await self._hub.get_toy_ids():
            try:
                await self._hub.stop(toy_id)
            except Exception:
                failed.append(toy_id)
                self._log.exception(
                    "Failed to stop toy %s (%s).",
                    toy_id,
                    reason,
                )
        if failed:
            self._log.error(
                "Could not stop %d toy(s) (%s): %s", len(failed), reason, failed
            )
        return failed

    async def _trip_watchdog(
        self, reason: Literal["timeout", "disconnect"], message: str
    ) -> None:
        """
        Dead-man's switch: make sure the safety hold is on, then tell every client.

        Shared by the heartbeat check (a client stopped sending heartbeats) and the disconnect handler (a client that
        armed the heartbeat vanished): both are "we lost the controlling client, make the toys safe". The hold itself
        lives in the hub (see ``_ToyHub.set_safety_hold``): every toy is kept at zero, while its block, pause, pattern
        and limits stay untouched. The caller has already recorded why (``_heartbeat_timed_out`` or
        ``_disconnect_hold``), so :meth:`_release_hold_if_clear` knows when the hold may end.

        ``heartbeat_timeout`` is broadcast on every trip, also when the hold was already on, so clients learn about each
        client that went overdue or vanished. Its ``reason`` tells them whether the hold can end on its own.

        Never raises: it runs in the heartbeat loop and in the disconnect handler, and neither may die.

        Args:
            reason: "timeout" for an overdue client, "disconnect" for one that vanished.
            message: Human-readable description, sent with the event.
        """
        async with self._hold_lock:
            failed: list[str] = []
            if not self._hold_active:
                self._hold_active = True
                try:
                    failed = await self._hub.set_safety_hold(True)
                except Exception:
                    self._log.exception("Failed to put on the safety hold.")
                if failed:
                    self._log.error(
                        "Could not stop %d toy(s) for the safety hold: %s",
                        len(failed),
                        failed,
                    )
            # Broadcast under the lock, so this event can never arrive after the hold_released that ends it.
            await self._broadcast(
                "heartbeat_timeout",
                dict(message=message, reason=reason, failed_toy_ids=failed),
            )

    async def _release_hold_if_clear(self) -> None:
        """
        End the safety hold once nothing calls for it any more: no armed client overdue, and no disconnect hold.

        Every toy then follows its own state again (a running pattern resumes, a paused or blocked toy stays that
        way), so nothing has to be restored. Broadcasts ``hold_released``.
        """
        async with self._hold_lock:
            if (
                not self._hold_active
                or self._heartbeat_timed_out
                or self._disconnect_hold
            ):
                return
            self._hold_active = False
            try:
                await self._hub.set_safety_hold(False)
            except Exception:
                self._log.exception("Failed to release the safety hold.")
            self._log.info("Safety hold released.")
            await self._broadcast(
                "hold_released",
                dict(
                    message="Safety hold released. Toys follow their own state again."
                ),
            )

    async def _handle_http_request(self, _: Any, request: Any) -> Response | None:
        """
        Intercept plain HTTP requests and serve a status page.
        WebSocket upgrade requests (Upgrade: websocket) are passed through by returning None.
        """
        if request.headers.get("upgrade", "").lower() == "websocket":
            if "origin" in request.headers:
                self._log.warning(
                    "Rejected WebSocket with Origin header: %s",
                    request.headers["origin"],
                )
                return Response(
                    status_code=http.HTTPStatus.FORBIDDEN.value,
                    reason_phrase=http.HTTPStatus.FORBIDDEN.phrase,
                    headers=Headers(),
                    body=b"Browser connections are not allowed",
                )
            return None

        body = (await self._status_page.build_html(len(self._clients))).encode("utf-8")
        headers = Headers(
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ]
        )
        return Response(
            status_code=http.HTTPStatus.OK.value,
            reason_phrase=http.HTTPStatus.OK.phrase,
            headers=headers,
            body=body,
        )

    # Messaging helpers

    async def _send_response(
        self,
        ws: ServerConnection,
        req_id: str,
        cmd: str,
        data: dict[str, Any],
        *,
        success: bool,
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
        self, event_name: str, payload: dict[str, Any], *, success: bool = True
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
        self, event_name: str, payload: dict[str, Any], *, success: bool = True
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

    # _ToyHub callbacks -> event broadcasts

    async def _on_status_change(self, toy_id: str, status: ToyStatus) -> None:
        """Broadcast a ``connection_status_changed`` event to all connected clients when a toy's connection status changes."""
        await self._broadcast(
            "connection_status_changed", dict(toy_id=toy_id, status=status.value)
        )

    async def _on_toy_ids_change(self, toy_ids: list[str]) -> None:
        """Broadcast a ``toy_ids_changed`` event to all connected clients when the set of managed toys changes."""
        current = set(toy_ids)
        # Clean up any stale intensity limits
        for client_toys in self._client_limits.values():
            for stale_id in list(client_toys.keys()):
                if stale_id not in current:
                    del client_toys[stale_id]
        await self._broadcast("toy_ids_changed", dict(toy_ids=toy_ids))

    async def _on_toy_state_change(self, state: dict[str, Any]) -> None:
        """Broadcast a ``toy_state_changed`` event to all connected clients when any part of a toy's state changes."""
        await self._broadcast("toy_state_changed", state)

    async def _on_model_change(self, update: dict[str, Any]) -> None:
        """Broadcast a ``model_changed`` event to all connected clients when a toy's assigned model name changes."""
        await self._broadcast("model_changed", update)

    async def _on_battery_change(self, updates: dict[str, int | None]) -> None:
        """Broadcast a ``battery_changed`` event to all connected clients when one or more toys report a new battery level."""
        await self._broadcast("battery_changed", updates)

    async def _on_scan_update(self, update: Exception | list[dict[str, Any]]) -> None:
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
