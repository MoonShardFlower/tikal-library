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

.. code-block:: text

    {"request": "some_command", "id": "some_id", "data": {...}}

Response (server -> client):

.. code-block:: text

    {"reply": "some_command", "id": "some_id", "success": true,  "data": { ... }}
    {"reply": "some_command", "id": "some_id", "success": false, "data": {"error": "...", "message": "...", ...}}

Event (server -> all clients / scan subscribers):

.. code-block:: text

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
        "success": true,
        "data": {"battery": 85, "toy_id": "some_toy_id"}
    }

Error response:

.. code-block:: json

    {
        "reply": "get_battery",
        "id": "some_id",
        "success": false,
        "data": {
            "error": "Unknown Toy",
            "message": "Unable to execute 'get_battery' on 'some_toy_id'. Please add the toy first.",
            "traceback": null,
            "toy_id": "some_toy_id",
            "model_name": null,
            "brand": null
        }
    }

Event:

.. code-block:: json

    {
        "event": "connection_status_changed",
        "success": true,
        "data": {"toy_id": "some_toy_id", "status": "reconnecting"}
    }

**Architecture**

Each command is described by a _Command, which bundles:

- Request_model   : Pydantic model that validates the incoming data object.
- Response_model  : Pydantic model that validates (and serializes) the outgoing data.
- Run             : async callable(ws, data: req_model) -> dict that performs the actual work and returns the reply data.

ToyServer._build_commands lists every command. Most of them only call a _ToyHub method and shape its result into the
reply; the rest need the client's connection or the server's own state (scan subscriptions, the heartbeat watchdog,
per-client limits, shutdown) and are methods of ToyServer.

_handle_message is a *generic* dispatcher: validate -> look up the command -> validate inner data -> run it -> validate response -> send.
An exception a command raises is turned into an error reply by one table, _ERROR_REPLIES.
"""

import asyncio
import datetime
import http
import ipaddress
import logging
import traceback
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets
from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.frames import CloseCode
from websockets.http11 import Response

from .._core import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    SafetyHoldError,
    ToyAlreadyAddedError,
    ToyConnectionError,
    ToyNotConnectedError,
    ToyStatus,
    UnavailableToyError,
    UndiscoveredToyError,
    UnknownToyError,
    _ToyHub,
)
from ..low_level import ToyData
from ._heartbeat_watchdog import _HeartbeatWatchdog
from ._status_page import ToyServerStatusPage
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
# Commands
# -----------------------------------------------------------------------------

#: Runs a command for a client (its connection, the validated request data) and returns the data of the reply.
_Run = Callable[[ServerConnection, Any], Awaitable[dict[str, Any]]]
#: Shapes a _ToyHub method's result into the data of the reply. Gets the result and the validated request data.
_Reply = Callable[[Any, Any], dict[str, Any]]


@dataclass(frozen=True)
class _Command:
    """
    Everything the dispatcher needs to handle one command. See ToyServer._build_commands for the list.

    Attributes:
        req_model:  The Pydantic model used to validate (and coerce) the data field of the incoming RequestEnvelope.
        resp_model: The Pydantic model used to validate the reply data before it is serialized into the ResponseEnvelope.
        run:        Performs the command for a client and returns the reply data.
    """

    req_model: type[BaseModel]
    resp_model: type[BaseModel]
    run: _Run


def _ack(_result: Any, data: Any) -> dict[str, Any]:
    """Reply for a command without a result of its own: acknowledge it."""
    return {"ack": True, "toy_id": data.toy_id}


def _as_is(result: Any, _data: Any) -> dict[str, Any]:
    """Reply for a command whose result already is the reply data (e.g., a toy's state)."""
    reply: dict[str, Any] = result
    return reply


def _named(key: str) -> _Reply:
    """Reply that carries the command's result under *key*, plus the toy_id if the request named a toy."""

    def reply(result: Any, data: Any) -> dict[str, Any]:
        named = {key: result}
        if hasattr(data, "toy_id"):
            named["toy_id"] = data.toy_id
        return named

    return reply


# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _ErrorReply:
    """
    How an exception raised by a command is reported to the client.

    Attributes:
        error:          Name of the error (the error field of ErrorData).
        message:        Message template. May use {toy_id}, {model_name}, {cmd}, {status} and {details} (the traceback).
        log_level:      Level the failure is logged at. From WARNING upwards, the log includes the traceback.
        with_traceback: Whether the reply carries the traceback.
    """

    error: str
    message: str
    log_level: int = logging.WARNING
    with_traceback: bool = True


#: Looked up along the exception's class hierarchy, so the most specific entry wins (a ToyNotConnectedError is also a
#: ToyConnectionError). See ToyServer._send_error_for.
_ERROR_REPLIES: dict[type, _ErrorReply] = {
    UndiscoveredToyError: _ErrorReply(
        "Undiscovered Toy", _ErrMsg.UNDISCOVERED_TOY_ERROR
    ),
    UnavailableToyError: _ErrorReply("Unavailable Toy", _ErrMsg.UNAVAILABLE_TOY_ERROR),
    ToyAlreadyAddedError: _ErrorReply(
        "Toy Already Added", _ErrMsg.TOY_ALREADY_ADDED_ERROR
    ),
    AddConnectionError: _ErrorReply("Connection Error", _ErrMsg.ADD_CONNECTION_ERROR),
    InvalidModelError: _ErrorReply("Invalid Model", _ErrMsg.INVALID_MODEL_ERROR),
    BadModelError: _ErrorReply("Bad Model", _ErrMsg.BAD_MODEL_ERROR, logging.ERROR),
    UnknownToyError: _ErrorReply("Unknown Toy", _ErrMsg.UNKNOWN_TOY_ERROR),
    SafetyHoldError: _ErrorReply(
        "Safety Hold", _ErrMsg.SAFETY_HOLD_ERROR, logging.INFO, with_traceback=False
    ),
    # Same error kind as a failed command, so clients handle both alike; only the message tells them apart.
    ToyNotConnectedError: _ErrorReply(
        "Connection Error", _ErrMsg.TOY_NOT_CONNECTED_ERROR, logging.INFO
    ),
    ToyConnectionError: _ErrorReply("Connection Error", _ErrMsg.TOY_CONNECTION_ERROR),
    DiscoveryStartError: _ErrorReply(
        "Discovery Start Error", _ErrMsg.DISCOVERY_START_ERROR
    ),
}

#: Any other exception is a bug in the server.
_UNEXPECTED_ERROR = _ErrorReply(
    "Developer Error", _ErrMsg.DEVELOPER_ERROR, logging.ERROR
)


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
        bluetooth_scanner: Any = None,
        bluetooth_client: Any = None,
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
            bluetooth_scanner: BLE scanner class to use instead of the one ``mock_toys`` picks (e.g., for tests).
            bluetooth_client: BLE client class to use instead of the one ``mock_toys`` picks (e.g., for tests).

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
        # Set once the server no longer accepts connections (see _stop_accepting), which is the first step of a shutdown.
        self._stopped_accepting = False

        self._shutdown_initiated = False

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
            bluetooth_scanner=bluetooth_scanner,
            bluetooth_client=bluetooth_client,
        )
        self._status_page = ToyServerStatusPage(self._hub, host, port)
        # Dead-man's switch for clients that armed the heartbeat.
        self._watchdog = _HeartbeatWatchdog(
            lambda reasons: self._hub.set_safety_hold(reasons),
            lambda event_name, payload: self._broadcast(event_name, payload),
            self._log,
        )

        self._commands = self._build_commands()

    # Lifecycle

    async def serve(self) -> None:
        """
        Start the WebSocket server and block until it shuts itself down.

        The teardown runs in a ``finally``: a toy keeps doing whatever it was last told once this process is gone, so
        the hub has to get its chance to stop and disconnect every toy even when this coroutine is **canceled**
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
        Stop the server and make every toy safe: stops accepting connections (which frees the port for a new server
        right away), stops and disconnects all toys, then closes the remaining connections.

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
        """Stop accepting connections, tear down _ToyHub, and close the remaining connections."""
        self._stop_accepting()
        await self._hub.shutdown()  # idempotent
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        self._watchdog.shutdown()
        if self._server is not None:
            # The server already stopped accepting (above), and closing it again does nothing, so the connections
            # still open are closed here, the way Server.close() would have closed them.
            await asyncio.gather(
                *(
                    connection.close(CloseCode.GOING_AWAY)
                    for connection in list(self._server.connections)
                ),
                return_exceptions=True,
            )

    def _stop_accepting(self) -> None:
        """
        Stop accepting new connections, so a new server can take over the port right away. Idempotent.

        Stopping and disconnecting every toy takes a while with real toys, and an application that is restarted
        meanwhile must not connect to a server that is about to go away. Its own server could not even start, because
        the port would still be taken. Connections already open stay open until the shutdown closes them.
        """
        if self._stopped_accepting or self._server is None:
            return
        self._stopped_accepting = True
        self._server.close(close_connections=False)
        self._log.info("Shutting down: no longer accepting connections.")

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """
        Manage the full lifecycle of a single WebSocket client connection.

        On connect: Cancels any pending idle-shutdown timer, registers the client in _clients, and calls _ToyHub.startup().

        While connected: Reads messages from the client in a loop, spawning a new task per message, so slow commands don't block later ones.

        On disconnect (normal close or connection error):

        - If this client requested the shutdown: stops accepting connections first, so the port is free at once.
        - Removes the client from _clients and _scan_subscribers.
        - Makes the toys safe: an armed heartbeat client vanishing puts on the safety hold until a client sends
          release_hold; the *last* client leaving stops every toy and pauses its pattern, as no one has control anymore
          and idle shutdown might be disabled or still seconds away.
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
                if self._watchdog.is_kicked(ws):
                    # The watchdog gave up on this client. Whatever it still sends while its connection closes must
                    # not count, e.g., a release_hold for the hold it caused.
                    continue
                # Spawn a task per message so the receiver loop stays responsive.
                message = raw if isinstance(raw, str) else raw.decode("utf-8")
                asyncio.get_running_loop().create_task(
                    self._handle_message(ws, message), name="handle-message"
                )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if ws in self._shutdown_requested_clients:
                # The shutdown begins now. The cleanup below can take seconds, and a new server may want the port.
                self._stop_accepting()
            self._clients.discard(ws)
            # An armed heartbeat client vanishing puts on the safety hold until a client sends release_hold.
            await self._watchdog.client_disconnected(ws)
            if not self._clients:
                # Nobody is watching anymore, so a running pattern would keep driving the toy indefinitely: idle
                # shutdown may be disabled (idle_shutdown_delay <= 0) or still seconds away. This has to happen
                # before the scan/limit cleanup below, which can block for seconds on stopping the BLE scan.
                try:
                    await self._stop_all_toys("the last client disconnected")
                except Exception:
                    self._log.exception(
                        "Failed to stop toys after the last client disconnected."
                    )
            await self._unsubscribe_from_scan(ws)

            client_toys = self._client_limits.pop(ws, {})
            for toy_id, limits in client_toys.items():
                for axis in (0, 1):
                    if limits[axis] is not None:
                        try:
                            await self._apply_effective_limit(toy_id, axis)
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
            await self._send_error(
                ws,
                "?",
                "?",
                ErrorData(
                    error="Malformed Request",
                    message=_ErrMsg.MALFORMED_REQUEST,
                    traceback=details,
                ),
            )
            return

        cmd = envelope.request
        req_id = envelope.id

        # 2. Look up the command.
        command = self._commands.get(cmd)
        if command is None:
            self._log.warning("Unknown command: '%s' encountered.", cmd)
            await self._send_error(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Unknown Command",
                    message=_ErrMsg.UNKNOWN_COMMAND.format(cmd=cmd),
                ),
            )
            return

        # 3. Validate the inner data payload.
        try:
            data = command.req_model(**envelope.data)
        except ValueError as e:
            self._log.warning("Invalid request data for '%s': '%s'", cmd, e)
            tb = traceback.format_exc()
            await self._send_error(
                ws,
                req_id,
                cmd,
                ErrorData(
                    error="Invalid Data",
                    message=_ErrMsg.INVALID_DATA.format(cmd=cmd, detail=tb),
                    traceback=tb,
                ),
            )
            return

        # 4. Run the command and reply.
        try:
            result = await command.run(ws, data)
            reply = command.resp_model(**result).model_dump()
        except Exception as e:
            await self._send_error_for(ws, req_id, cmd, e)
            return
        await self._send_response(ws, req_id, cmd, reply, success=True)

    async def _send_error_for(
        self, ws: ServerConnection, req_id: str, cmd: str, error: Exception
    ) -> None:
        """
        Report an exception that a command raised: look up how in _ERROR_REPLIES, log it, and send the error reply.

        The toy_id, model_name and brand of the reply (and the placeholders of the message) are taken from the
        exception where it has them.
        """
        spec = _UNEXPECTED_ERROR
        for cls in type(error).__mro__:
            if cls in _ERROR_REPLIES:
                spec = _ERROR_REPLIES[cls]
                break

        tb = "".join(traceback.format_exception(error))
        toy_id = getattr(error, "toy_id", None)
        model_name = getattr(error, "model_name", None)
        self._log.log(
            spec.log_level,
            "'%s' failed: %s (%r)",
            cmd,
            spec.error,
            error,
            exc_info=error if spec.log_level >= logging.WARNING else None,
        )
        await self._send_error(
            ws,
            req_id,
            cmd,
            ErrorData(
                error=spec.error,
                message=spec.message.format(
                    toy_id=toy_id,
                    model_name=model_name,
                    # The command the hub was running when it failed, if the exception names it.
                    cmd=getattr(error, "cmd", cmd),
                    status=getattr(error, "status", None),
                    details=tb,
                ),
                traceback=tb if spec.with_traceback else None,
                toy_id=toy_id,
                model_name=model_name,
                brand=getattr(error, "brand", None),
            ),
        )

    def _build_commands(self) -> dict[str, _Command]:
        """
        The commands of the protocol (see docs/websocket/actions.md)

        Most commands call a _ToyHub method. ``hub_command`` builds those from the call and from how its result
        is shaped into the reply (acknowledged by default). The server itself handles the commands at the end.
        """
        hub = self._hub

        def hub_command(
            req_model: type[BaseModel],
            resp_model: type[BaseModel],
            call: Callable[[Any], Awaitable[Any]],
            reply: _Reply = _ack,
        ) -> _Command:
            async def run(_ws: ServerConnection, data: Any) -> dict[str, Any]:
                return reply(await call(data), data)

            return _Command(req_model, resp_model, run)

        return {
            # Queries
            "get_brands": hub_command(
                _EmptyData, BrandsData, lambda d: hub.get_brands(), _named("brands")
            ),
            "get_toy_ids": hub_command(
                _EmptyData, ToyIdsData, lambda d: hub.get_toy_ids(), _named("toy_ids")
            ),
            "get_state": hub_command(
                ToyIdData, ToyStateData, lambda d: hub.get_state(d.toy_id), _as_is
            ),
            "get_battery": hub_command(
                ToyIdData,
                BatteryResponseData,
                lambda d: hub.get_battery(d.toy_id),
                _named("battery"),
            ),
            "get_connection_status": hub_command(
                ToyIdData,
                ConnectionStatusResponseData,
                lambda d: hub.get_status(d.toy_id),
                _named("connection_status"),
            ),
            "get_info": hub_command(
                GetInfoData,
                InfoResponseData,
                lambda d: hub.get_info(d.toy_id, d.full),
                _as_is,
            ),
            "get_all": hub_command(
                GetInfoData,
                GetAllResponseData,
                lambda d: hub.get_all(d.toy_id, d.full),
                _as_is,
            ),
            "direct_command": hub_command(
                DirectCommandData,
                DirectCommandResponseData,
                lambda d: hub.direct_command(d.toy_id, d.command),
                _named("response"),
            ),
            # Adding and removing toys
            "add": hub_command(
                AddRequestData, AckData, lambda d: hub.add(d.toy_id, d.model_name)
            ),
            "remove": hub_command(ToyIdData, AckData, lambda d: hub.remove(d.toy_id)),
            "set_model": hub_command(
                SetModelData, AckData, lambda d: hub.set_model(d.toy_id, d.model_name)
            ),
            # Controlling toys. Where the hub reports whether the command took effect, that is the ack.
            "stop": hub_command(ToyIdData, AckData, lambda d: hub.stop(d.toy_id)),
            "intensity1": hub_command(
                IntensityData,
                AckData,
                lambda d: hub.intensity(d.toy_id, 0, d.intensity),
                _named("ack"),
            ),
            "intensity2": hub_command(
                IntensityData,
                AckData,
                lambda d: hub.intensity(d.toy_id, 1, d.intensity),
                _named("ack"),
            ),
            "change_rotation_direction": hub_command(
                ToyIdData,
                AckData,
                lambda d: hub.change_rotation_direction(d.toy_id),
                _named("ack"),
            ),
            "toggle_pause": hub_command(
                ToyIdData, AckData, lambda d: hub.toggle_pause(d.toy_id)
            ),
            "toggle_block": hub_command(
                ToyIdData, AckData, lambda d: hub.toggle_block(d.toy_id)
            ),
            "set_paused": hub_command(
                SetPausedData, AckData, lambda d: hub.set_paused(d.toy_id, d.pause)
            ),
            "set_blocked": hub_command(
                SetBlockedData, AckData, lambda d: hub.set_blocked(d.toy_id, d.block)
            ),
            "set_pattern": hub_command(
                SetPatternData,
                AckData,
                lambda d: hub.set_pattern(
                    d.toy_id, d.pattern, d.wraparound, d.reset_time
                ),
            ),
            # Handled by the server itself: these need the client's connection or the server's own state.
            "set_intensity1_limit": _Command(
                IntensityLimitData, AckData, partial(self._cmd_set_limit, 0)
            ),
            "set_intensity2_limit": _Command(
                IntensityLimitData, AckData, partial(self._cmd_set_limit, 1)
            ),
            "start_scan": _Command(_EmptyData, AckData, self._cmd_start_scan),
            "stop_scan": _Command(_EmptyData, AckData, self._cmd_stop_scan),
            "enable_heartbeat": _Command(
                HeartbeatEnableData, AckData, self._cmd_enable_heartbeat
            ),
            "heartbeat": _Command(_EmptyData, AckData, self._cmd_heartbeat),
            "release_hold": _Command(_EmptyData, AckData, self._cmd_release_hold),
            "shutdown": _Command(_EmptyData, AckData, self._cmd_shutdown),
        }

    async def _cmd_shutdown(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """Shut the server down once *ws* disconnects (see _handle_connection)."""
        self._shutdown_requested_clients.add(ws)
        return {"ack": True}

    async def _cmd_start_scan(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """Subscribe *ws* to scan results, starting the scan if this is the first subscriber."""
        try:
            async with self._scan_lock:
                self._scan_subscribers.add(ws)
                if len(self._scan_subscribers) == 1:
                    await self._hub.start_scan(self._on_scan_update)
        except Exception:
            self._scan_subscribers.discard(ws)
            raise
        return {"ack": True}

    async def _cmd_stop_scan(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """Unsubscribe *ws* from scan results."""
        await self._unsubscribe_from_scan(ws)
        return {"ack": True}

    async def _unsubscribe_from_scan(self, ws: ServerConnection) -> None:
        """Unsubscribe *ws* from scan results, stopping the scan when no subscribers remain. Never raises."""
        async with self._scan_lock:
            self._scan_subscribers.discard(ws)
            if not self._scan_subscribers:
                try:
                    await self._hub.stop_scan()
                except Exception:
                    pass

    async def _cmd_release_hold(
        self, _ws: ServerConnection, _data: Any
    ) -> dict[str, Any]:
        """End a hold caused by a disconnected client (see _HeartbeatWatchdog.release)."""
        await self._watchdog.release()
        return {"ack": True}

    async def _cmd_enable_heartbeat(
        self, ws: ServerConnection, data: Any
    ) -> dict[str, Any]:
        """Arm or disarm the heartbeat watchdog for *ws*."""
        if data.enable:
            await self._watchdog.arm(ws)
        else:
            await self._watchdog.disarm(ws)
        return {"ack": True}

    async def _cmd_heartbeat(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """Record that the armed client *ws* is still there."""
        await self._watchdog.beat(ws)
        return {"ack": True}

    async def _cmd_set_limit(
        self, axis: int, ws: ServerConnection, data: Any
    ) -> dict[str, Any]:
        """
        Store the client's limit for one intensity, compute the effective minimum, and forward it to _ToyHub.

        Args:
            axis: 0 for intensity1, 1 for intensity2.
            ws: The client setting its limit.
            data: Validated IntensityLimitData.
        """
        toy_id = data.toy_id

        client_toys = self._client_limits.setdefault(ws, {})
        toy_limits = client_toys.setdefault(toy_id, [None, None])
        old_value = toy_limits[axis]
        toy_limits[axis] = data.limit  # None = "I don't want to limit"

        try:
            await self._apply_effective_limit(toy_id, axis)
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

        return {"ack": True, "toy_id": toy_id}

    async def _apply_effective_limit(self, toy_id: str, axis: int) -> None:
        """Forward the minimum of all clients' limits for a toy axis to _ToyHub (None if no client limits it)."""
        values = [
            client_toys[toy_id][axis]
            for client_toys in self._client_limits.values()
            if toy_id in client_toys
        ]
        effective = min((value for value in values if value is not None), default=None)
        await self._hub.set_intensity_limit(toy_id, axis, effective)

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

    async def _send_error(
        self, ws: ServerConnection, req_id: str, cmd: str, error: ErrorData
    ) -> None:
        """Send an unsuccessful response carrying *error* to *ws*."""
        await self._send_response(ws, req_id, cmd, error.model_dump(), success=False)

    @staticmethod
    async def _send_raw(ws: ServerConnection, msg: str) -> None:
        """Send a JSON string to a single client, silently dropping if already gone."""
        try:
            await ws.send(msg)
        except Exception:
            pass

    async def _broadcast(
        self,
        event_name: str,
        payload: dict[str, Any],
        *,
        success: bool = True,
        to: set[ServerConnection] | None = None,
    ) -> None:
        """Broadcast an event to all connected clients, or only to the clients in *to*."""
        recipients = set(self._clients if to is None else to)
        if not recipients:
            return
        msg = EventEnvelope(
            event=event_name, success=success, data=payload
        ).model_dump_json()
        await asyncio.gather(
            *(ws.send(msg) for ws in recipients), return_exceptions=True
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

    async def _on_scan_update(self, update: Exception | list[ToyData]) -> None:
        """Forward a ``scan_update`` event to scan-subscribed clients only."""
        if isinstance(update, DiscoveryError):
            await self._broadcast(
                "scan_update",
                dict(
                    error="Discovery Error",
                    message=_ErrMsg.DISCOVER_ERROR,
                    traceback=update.tb,
                ),
                success=False,
                to=self._scan_subscribers,
            )
        elif isinstance(update, Exception):
            details = "".join(traceback.format_exception(update))
            await self._broadcast(
                "scan_update",
                dict(
                    error="Developer Error",
                    message=_ErrMsg.DEVELOPER_ERROR.format(details=details),
                    traceback=details,
                ),
                success=False,
                to=self._scan_subscribers,
            )
        else:
            discovered = [
                dict(
                    toy_id=data.toy_id,
                    name=data.name,
                    brand=data.brand,
                    model_name=data.model_name,
                )
                for data in update
            ]
            await self._broadcast(
                "scan_update", dict(discovered=discovered), to=self._scan_subscribers
            )
