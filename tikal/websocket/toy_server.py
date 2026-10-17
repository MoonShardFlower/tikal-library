"""
WebSocket API: :class:`ToyServer`, a JSON-over-WebSocket server for toy control.

Clients in any language, in other processes, can control the toys. The protocol is described in ``docs/websocket/``:
clients send requests, the server replies to each, and broadcasts events about changes. Several clients can connect at
once; this is less tested than a single client.

Request, reply, and event::

    {"request": "get_battery", "id": "1", "data": {"toy_id": "00:00:00:00:00:01"}}
    {"reply": "get_battery", "id": "1", "success": true, "data": {"battery": 85, "toy_id": "00:00:00:00:00:01"}}
    {"event": "connection_status_changed", "success": true, "data": {"toy_id": "00:00:00:00:00:01", "status": "lost"}}

Start it with ``tikal-server`` (see :mod:`tikal.websocket.cli`), or create a :class:`ToyServer` and await
:meth:`ToyServer.serve`.
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
    ToyAlreadyAddedError,
    ToyBlockedError,
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
    SeekData,
    SetBlockedData,
    SetFunscriptData,
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
    How the server runs one command. :meth:`ToyServer._build_commands` lists them all.

    Attributes:
        req_model: Validates the request's ``data``.
        resp_model: Validates the reply's ``data``.
        run: Runs the command for a client and returns the reply's ``data``.
    """

    req_model: type[BaseModel]
    resp_model: type[BaseModel]
    run: _Run


def _ack(_result: Any, data: Any) -> dict[str, Any]:
    """
    Reply of a command without a result of its own.

    Args:
        _result: The command's result. Unused.
        data: The validated request data. Names the toy.

    Returns:
        An acknowledgement with the ``toy_id``.
    """
    return {"ack": True, "toy_id": data.toy_id}


def _as_is(result: Any, _data: Any) -> dict[str, Any]:
    """
    Reply of a command whose result is the reply's data, e.g., a toy's state.

    Args:
        result: The command's result.
        _data: The validated request data. Unused.

    Returns:
        *result*.
    """
    reply: dict[str, Any] = result
    return reply


def _named(key: str) -> _Reply:
    """
    Reply that carries the command's result under a key.

    Args:
        key: The key, e.g., ``"battery"``.

    Returns:
        The reply function. Its reply also has the ``toy_id`` if the request named a toy.
    """

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
        error: The ``error`` of the reply's :class:`ErrorData`.
        message: Message template. May use ``{toy_id}``, ``{model_name}``, ``{cmd}``, ``{status}`` and ``{details}``
            (the traceback).
        log_level: Level the failure is logged at. From WARNING on, with the traceback.
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
    ToyBlockedError: _ErrorReply(
        "Blocked Toy", _ErrMsg.BLOCKED_TOY_ERROR, logging.INFO, with_traceback=False
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
    A :class:`ToyServer` was asked to bind a non-loopback host without ``insecure=True``. See
    ``docs/websocket/security.md``.
    """


def _is_loopback_host(host: str | None) -> bool:
    """
    Return ``True`` only if *host* provably refers to the loopback interface (safe to serve without auth).

    Args:
        host: The host to bind, as given to :class:`ToyServer`.

    Returns:
        True for ``localhost`` and loopback addresses, e.g., ``127.0.0.1`` or ``::1``. False for all interfaces
        (``0.0.0.0``, ``::``, ``""``, None), other addresses, and other host names.
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
    JSON-over-WebSocket server for toy control (protocol: ``docs/websocket/``). Owns a toy hub; start it with
    :meth:`serve`.

    - Shuts down when no client is connected for *idle_shutdown_delay* seconds, or when a client that sent ``shutdown``
      disconnects.
    - Stops every toy when the last client disconnects.
    - Intensity limits are per client: a toy's limit is the lowest of all clients' limits. A client's limits end when
      it disconnects.
    - A client that armed the heartbeat (``enable_heartbeat``) and then misses a heartbeat or disconnects gets every toy
      blocked.
    - Has no authentication: it refuses a non-loopback *host* unless *insecure* (see ``docs/websocket/security.md``).
    - Serves a read-only status page to plain HTTP requests, and refuses WebSocket connections from browsers.

    Args:
        toy_cache_path: JSON file that remembers each toy's model name. ``Path()`` keeps it in memory only.
        host: Interface to bind.
        port: TCP port.
        idle_shutdown_delay: Seconds without clients until the server shuts down, counted from :meth:`serve` and from
            each time the last client left. 0 or less: never.
        mock_toys: Use the fake Lovense toys of :mod:`tikal.mock`, and offer the fictional MockEstimToys brand.
        log_name: Name of the logger, shared with the hub.
        insecure: Allow a non-loopback *host*. Only when something else protects the server: a reverse proxy, a
            firewall, a trusted LAN, or tests.
        bluetooth_scanner: BleakScanner (sub)class to scan with, instead of the one *mock_toys* picks.
        bluetooth_client: BleakClient (sub)class to connect with, instead of the one *mock_toys* picks.

    Raises:
        InsecureBindError: *host* is not a loopback interface, and *insecure* is False.
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
        # Serializes starting and stopping the scan.
        self._scan_lock = asyncio.Lock()
        #: Seconds without clients until the server shuts down (see *idle_shutdown_delay*). Read at each countdown.
        self.idle_shutdown_delay = idle_shutdown_delay
        # The countdown to the idle shutdown, while no client is connected.
        self._shutdown_task: asyncio.Task[None] | None = None
        # The underlying websockets server object; set in serve().
        self._server: websockets.Server | None = None
        # Set once the server stopped accepting connections (see _stop_accepting): the first step of a shutdown.
        self._stopped_accepting = False
        # Set by the first _shutdown that gets past the hub's shutdown. It alone closes the remaining connections.
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
            lambda: self._hub.block_all_for_safety(),
            lambda event_name, payload: self._broadcast(event_name, payload),
            self._log,
        )

        self._commands = self._build_commands()

    # Lifecycle

    async def serve(self) -> None:
        """
        Start the server and return once it shut down.

        Every toy is stopped and disconnected at the end, also when this coroutine is canceled (as ``asyncio.run`` does
        on Ctrl+C): a toy keeps doing what it was last told once the process is gone.

        Raises:
            OSError: The server could not listen on *host* and *port*, e.g., the port is in use.
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
        Shut the server down: stop accepting connections (which frees the port at once), stop and disconnect every toy,
        then close the remaining connections.

        Idempotent, and safe to call from a signal handler. :meth:`serve` returns once it completed.
        """
        await self._shutdown()

    async def _idle_shutdown(self) -> None:
        """Shut down after *idle_shutdown_delay* seconds, unless canceled. Returns at once if that is 0 or less."""
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
        """
        Stop accepting connections, shut the hub down (stopping and disconnecting every toy), then close the remaining
        connections. Idempotent.
        """
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
        Stop accepting connections, so a new server can take the port while this one still stops its toys. Open
        connections stay. Idempotent.
        """
        if self._stopped_accepting or self._server is None:
            return
        self._stopped_accepting = True
        self._server.close(close_connections=False)
        self._log.info("Shutting down: no longer accepting connections.")

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """
        Serve one client until it disconnects.

        Each message is handled in its own task, so a slow command does not hold up later ones. Once the client is gone:

        - If it armed the heartbeat, every toy gets blocked (see ``enable_heartbeat``).
        - If it was the last client, every toy is stopped, and the idle shutdown countdown starts.
        - Its scan subscription and intensity limits are dropped.
        - If it sent ``shutdown``, the server shuts down.

        Args:
            ws: The client's connection.
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
                    # Watchdog gave up on this client. Whatever it sends while the connection closes does not count.
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
            # An armed heartbeat client vanishing blocks every toy, until the user unblocks them.
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
            ws: The client's connection.
            raw_msg: The message.
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
        Log an exception a command raised and send the matching error reply (see ``_ERROR_REPLIES``).

        Args:
            ws: The client's connection.
            req_id: The request's id.
            cmd: The request's command name.
            error: The exception. Its ``toy_id``, ``model_name``, ``brand``, ``cmd``, and ``status`` attributes, if
                any, go into the reply.
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
        The protocol's commands (see ``docs/websocket/actions.md``).

        Returns:
            Request name -> its :class:`_Command`. Most call one ``_ToyHub`` method and shape its result into the reply.
            The others need the client's connection or the server's state, and are ``_cmd_*`` methods.
        """
        hub = self._hub

        def hub_command(
            req_model: type[BaseModel],
            resp_model: type[BaseModel],
            call: Callable[[Any], Awaitable[Any]],
            reply: _Reply = _ack,
        ) -> _Command:
            """A command that runs *call* with the request data and shapes its result with *reply*."""

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
            # The server converts the script with the toy's own intensity range and recommended minimum interval.
            "set_funscript": hub_command(
                SetFunscriptData,
                AckData,
                lambda d: hub.set_funscript(
                    d.toy_id,
                    d.script,
                    mode=d.mode,
                    max_speed=d.max_speed,
                    secondary=d.secondary,
                    start_ms=d.start_ms,
                ),
            ),
            "seek": hub_command(
                SeekData, AckData, lambda d: hub.seek(d.toy_id, d.pattern_time)
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
            "shutdown": _Command(_EmptyData, AckData, self._cmd_shutdown),
        }

    async def _cmd_shutdown(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """
        ``shutdown``: shut the server down once the client disconnects.

        Args:
            ws: The client.
            _data: The validated request data. Unused.

        Returns:
            The reply's data, an acknowledgement.
        """
        self._shutdown_requested_clients.add(ws)
        return {"ack": True}

    async def _cmd_start_scan(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """
        ``start_scan``: subscribe the client to ``scan_update`` events. The first subscriber starts the scan.

        Args:
            ws: The client.
            _data: The validated request data. Unused.

        Returns:
            The reply's data, an acknowledgement.

        Raises:
            DiscoveryStartError: The scan could not start, e.g., Bluetooth is off. The client is not subscribed.
        """
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
        """
        ``stop_scan``: unsubscribe the client from ``scan_update`` events. The last subscriber stops the scan.

        Args:
            ws: The client.
            _data: The validated request data. Unused.

        Returns:
            The reply's data, an acknowledgement.
        """
        await self._unsubscribe_from_scan(ws)
        return {"ack": True}

    async def _unsubscribe_from_scan(self, ws: ServerConnection) -> None:
        """
        Unsubscribe a client from ``scan_update`` events. The last subscriber stops the scan. Never raises.

        Args:
            ws: The client. Need not be subscribed.
        """
        async with self._scan_lock:
            self._scan_subscribers.discard(ws)
            if not self._scan_subscribers:
                try:
                    await self._hub.stop_scan()
                except Exception:
                    pass

    async def _cmd_enable_heartbeat(
        self, ws: ServerConnection, data: Any
    ) -> dict[str, Any]:
        """
        ``enable_heartbeat``: arm or disarm the heartbeat watchdog for the client.

        Args:
            ws: The client.
            data: The validated :class:`HeartbeatEnableData`.

        Returns:
            The reply's data, an acknowledgement.
        """
        if data.enable:
            await self._watchdog.arm(ws)
        else:
            await self._watchdog.disarm(ws)
        return {"ack": True}

    async def _cmd_heartbeat(self, ws: ServerConnection, _data: Any) -> dict[str, Any]:
        """
        ``heartbeat``: record that the client is still there. Ignored if it did not arm the heartbeat.

        Args:
            ws: The client.
            _data: The validated request data. Unused.

        Returns:
            The reply's data, an acknowledgement.
        """
        await self._watchdog.beat(ws)
        return {"ack": True}

    async def _cmd_set_limit(
        self, axis: int, ws: ServerConnection, data: Any
    ) -> dict[str, Any]:
        """
        ``set_intensity1_limit`` and ``set_intensity2_limit``: set the client's limit for one channel of a toy. The
        toy's limit is the lowest of all clients' limits.

        Args:
            axis: 0 for intensity1, 1 for intensity2.
            ws: The client.
            data: The validated :class:`IntensityLimitData`.

        Returns:
            The reply's data, an acknowledgement with the ``toy_id``.

        Raises:
            ToyConnectionError: The limit is in force, but bringing the toy down failed. The client's limit is kept.
            Exception: Anything else the hub raises, e.g., UnknownToyError. The client's previous limit is restored.
        """
        toy_id = data.toy_id

        client_toys = self._client_limits.setdefault(ws, {})
        toy_limits = client_toys.setdefault(toy_id, [None, None])
        old_value = toy_limits[axis]
        toy_limits[axis] = data.limit  # None withdraws this client's limit

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
        """
        Set a toy's limit for one channel to the lowest of all clients' limits, or remove it if no client limits it.

        Args:
            toy_id: The toy.
            axis: 0 for intensity1, 1 for intensity2.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy runs above the limit but is not connected, so it was not brought down. The
                limit is still in force.
            ToyConnectionError: Bringing the toy down failed. The limit is in force. Reconnecting has started.
        """
        values = [
            client_toys[toy_id][axis]
            for client_toys in self._client_limits.values()
            if toy_id in client_toys
        ]
        effective = min((value for value in values if value is not None), default=None)
        await self._hub.set_intensity_limit(toy_id, axis, effective)

    async def _stop_all_toys(self, reason: str) -> list[str]:
        """
        Stop every managed toy, which also pauses its pattern playback.

        Never raises: a toy that cannot be reached is logged and reported back.

        Args:
            reason: For the log, e.g., "the last client disconnected".

        Returns:
            The ids of the toys that could not be stopped.
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
        Answer plain HTTP requests with the status page, and let WebSocket handshakes through.

        A handshake with an ``Origin`` header (from a browser) is refused with 403, to prevent cross-site WebSocket
        hijacking.

        Args:
            _: The connection being opened. Unused.
            request: The HTTP request.

        Returns:
            The HTTP response, or None to continue the WebSocket handshake.
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
        """
        Send a reply to a client. Does nothing if the client is gone.

        Args:
            ws: The client.
            req_id: The request's id.
            cmd: The request's command name.
            data: The reply's data.
            success: Whether the command succeeded. False: *data* is an :class:`ErrorData`.
        """
        resp = ResponseEnvelope(reply=cmd, id=req_id, success=success, data=data)
        await self._send_raw(ws, resp.model_dump_json())

    async def _send_error(
        self, ws: ServerConnection, req_id: str, cmd: str, error: ErrorData
    ) -> None:
        """
        Send a failed reply to a client. Does nothing if the client is gone.

        Args:
            ws: The client.
            req_id: The request's id, ``"?"`` if unknown.
            cmd: The request's command name, ``"?"`` if unknown.
            error: The reply's data.
        """
        await self._send_response(ws, req_id, cmd, error.model_dump(), success=False)

    @staticmethod
    async def _send_raw(ws: ServerConnection, msg: str) -> None:
        """
        Send a message to one client. Does nothing if the client is gone.

        Args:
            ws: The client.
            msg: The JSON message.
        """
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
        """
        Send an event to clients. Clients that are gone are skipped.

        Args:
            event_name: The event, e.g., ``"toy_state_changed"``.
            payload: The event's data.
            success: False if *payload* reports an error.
            to: The recipients. None: every client.
        """
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
        """
        Broadcast ``connection_status_changed``.

        Args:
            toy_id: The toy.
            status: Its new status.
        """
        await self._broadcast(
            "connection_status_changed", dict(toy_id=toy_id, status=status.value)
        )

    async def _on_toy_ids_change(self, toy_ids: list[str]) -> None:
        """
        Broadcast ``toy_ids_changed``, and drop the clients' limits of removed toys.

        Args:
            toy_ids: The ids of all added toys.
        """
        current = set(toy_ids)
        # Clean up any stale intensity limits
        for client_toys in self._client_limits.values():
            for stale_id in list(client_toys.keys()):
                if stale_id not in current:
                    del client_toys[stale_id]
        await self._broadcast("toy_ids_changed", dict(toy_ids=toy_ids))

    async def _on_toy_state_change(self, state: dict[str, Any]) -> None:
        """
        Broadcast ``toy_state_changed``.

        Args:
            state: The toy's state, see :class:`ToyStateData`.
        """
        await self._broadcast("toy_state_changed", state)

    async def _on_model_change(self, update: dict[str, Any]) -> None:
        """
        Broadcast ``model_changed``.

        Args:
            update: The toy's info after the change, see :class:`InfoResponseData`.
        """
        await self._broadcast("model_changed", update)

    async def _on_battery_change(self, updates: dict[str, int | None]) -> None:
        """
        Broadcast ``battery_changed``.

        Args:
            updates: ``{toy_id: level}`` for the toys whose level changed. Level in percent, None without battery.
        """
        await self._broadcast("battery_changed", updates)

    async def _on_scan_update(self, update: Exception | list[ToyData]) -> None:
        """
        Forward a ``scan_update`` to the scan subscribers.

        Args:
            update: The toys found, or the error that ended the scan. A :class:`DiscoveryError` is reported as
                ``"Discovery Error"``, any other exception as ``"Developer Error"``.
        """
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
