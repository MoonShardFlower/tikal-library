"""
Async core: :class:`_ToyHub` manages every toy: discovery, connection, state, pattern playback, battery polls, and
reconnection.

Both the WebSocket server and the High-Level ``ToyHub`` built on it.
The module also defines :class:`ToyStatus` and the exceptions the hub raises.
"""

import asyncio
import copy
import logging
import traceback
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

from bleak import BleakClient, BleakScanner

from .._private import (
    BATTERY_UPDATE_INTERVAL,
    COMMUNICATION_INTERVAL,
    retry_within_window,
)
from ..funscript import Funscript, FunscriptMode
from ..low_level import BRANDS
from ..low_level import BadModelError as LowLevelBadModelError
from ..low_level import ConnectionBuilder
from ..low_level import InvalidModelError as LowLevelInvalidModelError
from ..low_level import Toy, ToyData
from ..mock import MockBleakClient, MockBleakScanner
from .toy_cache import ToyCache
from .toy_controller import _CONTROLLER_BY_BRAND, _ToyController

# Pause before the one retry of a failed command. Equal to COMMUNICATION_INTERVAL by chance, not by design.
_RETRY_DELAY = 0.05  # seconds

T = TypeVar("T")


class ToyStatus(StrEnum):
    """Connection status of an added toy (see ``_ToyHub.get_status``). A str."""

    #: Connected. Reported after a successful reconnect, right after the toy's (stopped) state.
    CONNECTED = "connected"
    #: The connection dropped, or a command failed twice.
    #: The hub reconnects for up to ``RECONNECT_WINDOW``. Nothing is sent to the toy meanwhile.
    RECONNECTING = "reconnecting"
    #: No reconnect attempt succeeded. The toy is removed.
    LOST = "lost"
    #: The toy was switched off with its button. It is removed.
    POWERED_OFF = "powered_off"


class UndiscoveredToyError(ValueError):
    """The toy to add was never discovered. Carries ``toy_id`` and ``model_name``."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class UnknownToyError(ValueError):
    """The hub does not manage the toy: it was never added (connected), or it was removed. Carries ``toy_id``."""

    def __init__(self, toy_id: str):
        self.toy_id = toy_id


class ToyBlockedError(ValueError):
    """The toy is blocked, so a command that could drive it was refused. Nothing was sent. Carries ``toy_id``."""

    def __init__(self, toy_id: str):
        self.toy_id = toy_id


class ToyAlreadyAddedError(ConnectionError):
    """The toy is added (connected) already, or being added. Carries ``toy_id`` and ``model_name``."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class InvalidModelError(ValueError):
    """
    The model name is not a model of the toy's brand (case-insensitive).
    Carries ``toy_id``, ``model_name`` and ``brand``.
    """

    def __init__(self, toy_id: str, model_name: str, brand: str):
        self.toy_id = toy_id
        self.model_name = model_name
        self.brand = brand


class BadModelError(ConnectionError):
    """
    The model name is valid, but the toy refused or did not answer its commands: the model is wrong for the toy, or the
    library's commands for it are. Carries ``toy_id`` and ``model_name``.
    """

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class AddConnectionError(ConnectionError):
    """
    Connecting the toy failed, e.g., it is off or connected elsewhere.
    Carries ``toy_id`` and ``model_name``.
    """

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class ToyConnectionError(ConnectionError):
    """
    A command to a toy failed, even after one retry.

    Attributes:
        toy_id: The toy.
        model_name: Its model name.
        cmd: The command that failed.
    """

    def __init__(self, toy_id: str, model_name: str, cmd: str):
        self.toy_id = toy_id
        self.model_name = model_name
        self.cmd = cmd


class ToyNotConnectedError(ToyConnectionError):
    """
    The toy is not connected (e.g., reconnecting), so the command sent nothing.

    A state change the command asked for (block, pause, pattern, limit) holds anyway.
    Reconnecting stops the toy before it is used again.

    Attributes:
        status: The toy's :class:`ToyStatus`, or None if it has none.
    """

    def __init__(
        self, toy_id: str, model_name: str, cmd: str, status: ToyStatus | None
    ):
        super().__init__(toy_id, model_name, cmd)
        self.status = status


class UnavailableToyError(ConnectionError):
    """
    The toy to add was discovered earlier, but the running scan does not see it (anymore).
    Carries ``toy_id`` and ``model_name``.
    """

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class DiscoveryStartError(ConnectionError):
    """The scan could not start, e.g., Bluetooth is off. ``tb`` holds the traceback."""

    def __init__(self, tb: str):
        self.tb = tb


class DiscoveryError(ConnectionError):
    """A running scan failed. ``tb`` holds the traceback."""

    def __init__(self, tb: str):
        self.tb = tb


async def _retry(fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    """
    Await ``fn(*args)``.
    After a ConnectionError, wait ``_RETRY_DELAY`` and try once more: a second ConnectionError propagates.
    """
    try:
        return await fn(*args)
    except ConnectionError:
        await asyncio.sleep(_RETRY_DELAY)
        return await fn(*args)


class _ToyHub:
    """
    Manages every toy: discovery, connection, state, pattern playback, battery polls, and reconnection.

    - Use it from one event loop. Call :meth:`startup` before and :meth:`shutdown` after use.
    - Commands to one toy run one at a time, in the order they come (see :meth:`send`).
    - Callbacks may be sync or async. Exceptions they raise are logged.

    Args:
        on_status_change: Called with ``(toy_id, status)`` when a toy's :class:`ToyStatus` changes.
            Not called when a toy is added or removed.
        on_toy_ids_change: Called with the ids of all added toys after a toy was added or removed.
        on_toy_state_change: Called with a toy's state (see :meth:`get_state`) after it changed.
        on_model_change: Called with a toy's info (see :meth:`get_info`) after its model changed.
        on_battery_change: Called with ``{toy_id: level}`` for the toys whose battery level changed, in percent (None:
            no battery).
        toy_cache_path: JSON file that remembers each toy's model name (see :class:`ToyCache`).
            In memory-only if toy_cache_path is the empty Path.
        default_model: Model name of discovered toys that the toy cache does not know.
        log_name: Name of the logger.
        mock_toys: Use the fake Lovense toys of :mod:`tikal.mock`, and offer the fictional MockEstimToys brand.
        bluetooth_scanner: BleakScanner (sub)class to scan with, instead of the one *mock_toys* picks.
        bluetooth_client: BleakClient (sub)class to connect with, instead of the one *mock_toys* picks.
    """

    def __init__(
        self,
        on_status_change: Callable[[str, ToyStatus], Any] | None = None,
        on_toy_ids_change: Callable[[list[str]], Any] | None = None,
        on_toy_state_change: Callable[[dict[str, Any]], Any] | None = None,
        on_model_change: Callable[[dict[str, Any]], Any] | None = None,
        on_battery_change: Callable[[dict[str, int | None]], Any] | None = None,
        toy_cache_path: Path = Path(),
        default_model: str = "",
        log_name: str = "tikal.ws",
        mock_toys: bool = False,
        bluetooth_scanner: Any = None,
        bluetooth_client: Any = None,
    ) -> None:
        self._log = logging.getLogger(log_name)
        self._log.info(
            f"Initializing _ToyHub with toy_cache_path={toy_cache_path} and mock_toys={mock_toys}"
        )

        # Toy state
        self._toy_cache = ToyCache(toy_cache_path, default_model, log_name)
        self._toys: dict[str, _ToyController] = {}
        self._toy_data: dict[str, ToyData] = {}
        self._all_seen_toy_ids: set[str] = set()
        self._pending_toy_ids: set[str] = set()

        # Status tracking
        self._on_status_change = on_status_change or (lambda a, b: None)
        self._toy_status: dict[str, ToyStatus] = {}

        # Event callbacks
        self._on_toy_ids_change = on_toy_ids_change or (lambda a: None)
        self._on_toy_state_change = on_toy_state_change or (lambda a: None)
        self._on_model_change = on_model_change or (lambda a: None)
        self._on_battery_change = on_battery_change or (lambda a: None)

        scanner: Any = bluetooth_scanner
        if scanner is None:
            scanner = MockBleakScanner if mock_toys else BleakScanner
        client: Any = bluetooth_client
        if client is None:
            client = MockBleakClient if mock_toys else BleakClient

        self._loop: asyncio.AbstractEventLoop | None = None

        def on_disconnect_helper(toy_id: str) -> None:
            self._schedule_on_loop(self._on_disconnect(toy_id))

        def on_power_off_helper(toy_id: str) -> None:
            self._schedule_on_loop(self._on_power_off(toy_id))

        self._connection_builder = ConnectionBuilder(
            on_disconnect_helper,
            on_power_off_helper,
            log_name,
            scanner,
            client,
            mock_toys,
        )

        # Background loop tasks
        self._process_task: asyncio.Task[None] | None = None
        self._battery_poll_task: asyncio.Task[None] | None = None

        # At most one reconnect task per toy. Keyed by toy_id.
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}

    # -------------------------------------------------------------------------
    # Startup / Shutdown
    # -------------------------------------------------------------------------

    async def startup(self) -> None:
        """
        Start the playback and battery-poll loops.
        Call it before anything else, on the loop the hub is used from! Idempotent.
        """
        self._log.info("Starting _ToyHub.")
        self._loop = asyncio.get_running_loop()
        if self._process_task is None or self._process_task.done():
            self._process_task = asyncio.get_running_loop().create_task(
                self._process_loop(), name="toy-process-loop"
            )
        if self._battery_poll_task is None or self._battery_poll_task.done():
            self._battery_poll_task = asyncio.get_running_loop().create_task(
                self._battery_poll_loop(), name="toy-battery-poll"
            )

    async def shutdown(self) -> None:
        """
        Stop the scan and the background loops, and disconnect every toy. Errors are logged. No callbacks are called.
        Idempotent.
        """
        self._log.info("Shutting down _ToyHub.")

        # Idempotent
        await self._connection_builder.stop_continuous()

        for task in [self._process_task, self._battery_poll_task]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._process_task = None
        self._battery_poll_task = None

        # Cancel any in-flight reconnect tasks before disconnecting toys.
        reconnect_tasks = list(self._reconnect_tasks.values())
        self._reconnect_tasks.clear()
        for task in reconnect_tasks:
            task.cancel()
        await asyncio.gather(*reconnect_tasks, return_exceptions=True)

        snapshot = list(self._toys.values())
        self._toys.clear()
        self._pending_toy_ids.clear()

        results = await asyncio.gather(
            *[toy.disconnect() for toy in snapshot],
            return_exceptions=True,
        )
        for toy, result in zip(snapshot, results):
            if isinstance(result, BaseException):
                # The toy is dropped all the same; this is only for the record.
                self._log.warning(
                    "Error while disconnecting %s during shutdown: %r",
                    toy.toy_id,
                    result,
                )

    async def discover(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Scan for toys once and return what was found. A one-shot alternative to :meth:`start_scan`.

        Args:
            timeout: Scan duration in seconds.

        Raises:
            RuntimeError: A continuous scan (see :meth:`start_scan`) is in progress.
            Exception: Any exception from the underlying scanner (e.g., Bluetooth not available).

        Returns:
            The toys found, with model names from the toy cache (or the default model). Copy: Changing them does
            not affect the hub. Connect one with :meth:`add_toy_data`.
        """
        self._log.info(f"Discovering toys for {timeout} s")
        found = await self._connection_builder.discover_toys(timeout)
        return [self._with_cached_model(data) for data in found]

    async def start_scan(
        self, callback: Callable[[Exception | list[ToyData]], Any]
    ) -> None:
        """
        Start scanning in the background. Toys it finds can be added with :meth:`add`.

        Args:
            callback: Called with all toys found whenever they change, with model names from the toy cache.
                Called with a :class:`DiscoveryError` if the scan fails. May be sync or async.

        Raises:
            DiscoveryStartError: The scan could not start, e.g., Bluetooth is off.
        """
        self._log.info("Starting toy discovery")
        loop = asyncio.get_running_loop()

        def on_discovery(update: Exception | list[ToyData]) -> None:
            # Dispatch the async update onto the event loop.
            # Using call_soon_threadsafe makes this safe even if Bleak calls this callback from a worker thread.
            loop.call_soon_threadsafe(
                lambda: loop.create_task(self._apply_discovery(update, callback))
            )

        try:
            await self._connection_builder.start_continuous(on_discovery)
        except Exception as e:
            raise DiscoveryStartError(traceback.format_exc()) from e

    async def stop_scan(self) -> None:
        """Stop the background scan. Its callback is not called anymore, and :meth:`add` refuses until the next scan."""
        self._log.info("Stopping toy discovery")
        self._toy_data.clear()
        await self._connection_builder.stop_continuous()

    # -------------------------------------------------------------------------
    # Private
    # -------------------------------------------------------------------------

    def _schedule_on_loop(self, coro: Coroutine[Any, Any, Any]) -> None:
        """
        Run *coro* on the hub's loop, from any thread: the BLE callbacks may come from a worker thread.
        Before :meth:`startup` is called, *coro* is closed unrun, so call this first!
        """
        loop = self._loop
        if loop is None:
            coro.close()
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(coro))

    async def _apply_discovery(
        self,
        update: Exception | list[ToyData],
        callback: Callable[[Exception | list[ToyData]], Any],
    ) -> None:
        """
        Updates the discovery state, fills in the cached model names, and delivers the toys to callback

        Args:
            update: The toys found, or the exception that ended the scan.
            callback: Receives the toys with cached model names (copies), or a :class:`DiscoveryError`.
        """
        result: DiscoveryError | list[ToyData]
        if isinstance(update, Exception):
            self._toy_data.clear()
            result = DiscoveryError("".join(traceback.format_exception(update)))
        else:
            self._toy_data = {toy.toy_id: toy for toy in update}
            self._all_seen_toy_ids.update(data.toy_id for data in update)
            result = [self._with_cached_model(data) for data in update]
        self._log.debug("Discovery update: %s", result)
        await self._fire_callback(callback, result)

    def _with_cached_model(self, data: ToyData) -> ToyData:
        """A copy of *data* with the model name from the toy cache (or the default model)."""
        filled = copy.copy(data)
        filled.model_name = self._toy_cache.get_model_name(data.name)
        return filled

    async def _set_toy_status(self, toy_id: str, new_status: ToyStatus) -> None:
        """
        Update the status of a toy and invoke the on_status_change callback if changed.

        Args:
            toy_id: Identifier of the toy.
            new_status: New status to set.
        """
        self._log.debug("Setting status of %s to %s", toy_id, new_status.value)
        old_status = self._toy_status.get(toy_id)
        if old_status == new_status:
            return
        self._toy_status[toy_id] = new_status
        await self._fire_callback(self._on_status_change, toy_id, new_status)

    async def _on_disconnect(self, toy_id: str) -> None:
        """
        Unexpected disconnection: Sets ToyStatus to RECONNECTING and starts a background reconnection attempt.

        Args:
            toy_id: Identifier of the disconnected toy.
        """
        toy = self._toys.get(toy_id)
        if toy is None:
            return
        await self._start_reconnecting(toy, "Connection lost")

    async def _on_power_off(self, toy_id: str) -> None:
        """
        Toy was turned off. Sets ToyStatus to POWERED_OFF and removes the Toy from _ToyHub.

        Args:
            toy_id: Identifier of the toy that powered off.
        """
        toy = self._toys.get(toy_id)
        if toy is None:
            return
        await self._set_toy_status(toy_id, ToyStatus.POWERED_OFF)
        try:
            await self.remove(toy_id)
        except Exception:
            pass

    async def _process_loop(self) -> None:
        """Every ``COMMUNICATION_INTERVAL``, run a playback tick for every connected toy, concurrently."""
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            connected = [
                toy
                for toy_id, toy in self._toys.items()
                if self._toy_status.get(toy_id) == ToyStatus.CONNECTED
            ]
            if connected:
                await asyncio.gather(
                    *(self._process_one(toy) for toy in connected),
                    return_exceptions=True,
                )
            # Sleep only the time left in this interval
            await asyncio.sleep(
                max(0.0, COMMUNICATION_INTERVAL - (loop.time() - start))
            )

    async def _process_one(self, toy: _ToyController) -> None:
        """
        Run one playback tick for *toy*, under its command lock, with one retry. Failures are logged.

        Args:
            toy: The toy controller whose process_communication method will be called.
        """

        try:
            async with toy.cmd_lock:
                await _retry(toy.process_communication)
        except ConnectionError as e:
            self._log.warning(
                "process_communication failed for toy %s after retry: %s",
                toy.toy_id,
                e,
            )
        except Exception as e:
            self._log.error(
                "Unexpected error in process_communication for toy %s: %s",
                toy.toy_id,
                e,
                exc_info=True,
            )

    async def _start_reconnecting(self, toy: _ToyController, reason: str) -> None:
        """
        Set RECONNECTING and start reconnecting *toy*. Does nothing if a reconnect task already runs.

        Args:
            toy: The toy whose connection failed.
            reason: For the log, e.g., "Command failed".
        """
        self._log.warning("%s for toy %s. Reconnecting...", reason, toy.toy_id)
        toy_id = toy.toy_id
        existing = self._reconnect_tasks.get(toy_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.get_running_loop().create_task(
            self._reconnect_toy(toy), name=f"toy-reconnect-{toy_id}"
        )
        # Prune the entry when this specific task finishes
        task.add_done_callback(lambda t: self._prune_reconnect_task(toy_id, t))
        self._reconnect_tasks[toy_id] = task
        await self._set_toy_status(toy.toy_id, ToyStatus.RECONNECTING)

    def _prune_reconnect_task(self, toy_id: str, task: asyncio.Task[None]) -> None:
        """Forget a finished reconnect task, unless a newer one replaced it."""
        if self._reconnect_tasks.get(toy_id) is task:
            self._reconnect_tasks.pop(toy_id, None)

    async def _reconnect_toy(self, toy: _ToyController) -> None:
        """
        Reconnect a toy, or give it up (see ``retry_within_window``).

        Each attempt reconnects and then stops the toy (also pauses its pattern). On success, the toy's state is
        reported and the ToyStatus is set to CONNECTED. Otherwise, the ToyStatus is set to LOST and the toy removed.
        """

        async def attempt() -> None:
            await toy.reconnect()
            await toy.stop()  # Always stop the toy after connection loss

        if await retry_within_window(attempt, toy.toy_id, self._log):
            await self._report_state(toy)
            await self._set_toy_status(toy.toy_id, ToyStatus.CONNECTED)
            return

        await self._set_toy_status(toy.toy_id, ToyStatus.LOST)
        try:
            await self.remove(toy.toy_id, False)
        except Exception:
            pass

    def _get_toy(self, toy_id: str) -> _ToyController:
        """
        The controller of an added toy by its *toy_id*.

        Raises:
            UnknownToyError: The toy is not added.
        """
        toy = self._toys.get(toy_id)
        if toy is None:
            raise UnknownToyError(toy_id)
        return toy

    async def _fire_callback(self, callback: Callable[..., Any], *args: Any) -> None:
        """Call a sync or async callback. Exceptions it raises are logged."""
        try:
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self._log.exception(
                "Event callback %r raised an exception: %s", callback, e
            )

    async def _run_toy_command(
        self,
        toy: _ToyController,
        command_name: str,
        command: Callable[..., Awaitable[T]],
        *args: Any,
    ) -> T:
        """
        Execute a toy command with one automatic retry on ConnectionError. Triggers reconnection and raises
        ToyConnectionError if the command fails after retry. Every command the hub sends to a toy goes through here.

        Args:
            toy: The toy controller.
            command_name: Name of the command to execute.
            command: Async callable to execute.
            *args: Arguments to pass to the command.

        Raises:
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The command failed twice. Reconnecting has started.

        Returns:
            The command's result.
        """
        self._require_connected(toy, command_name)
        try:
            return await _retry(command, *args)
        except Exception as e:
            await self._start_reconnecting(toy, "Command failed")
            raise ToyConnectionError(toy.toy_id, toy.model_name, command_name) from e

    async def _report_state(self, toy: _ToyController) -> None:
        """Pass the toy's state to *on_toy_state_change*."""
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def _change_state(
        self,
        toy_id: str,
        command_name: str,
        transition: Callable[[_ToyController], bool | None],
        follow_up: Callable[
            [_ToyController], Awaitable[Any]
        ] = _ToyController.stop_output,
        report_failure: bool = False,
    ) -> None:
        """
        Run a state-changing command: apply *transition*, send what it calls for, report the new state.

        Holds the toy's command lock throughout, so commands to the toy cannot interleave (e.g., two clients pausing
        it). The new state holds even if the send fails. *transition* returns:

        - True: the toy needs a command: *follow_up* is sent (and retried once).
        - False: the state changed. Nothing is sent.
        - None: nothing changed. Nothing is sent or reported.

        Args:
            toy_id: The toy.
            command_name: Name of the command, for errors and logs.
            transition: Changes the state of the toy's controller, without I/O.
            follow_up: Sends the command, given the controller. A stop unless given.
            report_failure: Also report the new state if *follow_up* fails.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected, so *follow_up* was not sent. The new state holds.
            ToyConnectionError: *follow_up* failed twice. The new state holds. Reconnecting has started.
        """
        toy = self._get_toy(toy_id)
        try:
            async with toy.cmd_lock:
                needs_follow_up = transition(toy)
                if needs_follow_up is None:
                    return
                if needs_follow_up:
                    await self._run_toy_command(toy, command_name, follow_up, toy)
        except ToyConnectionError:
            if report_failure:
                await self._report_state(toy)
            raise
        await self._report_state(toy)

    def _require_connected(self, toy: _ToyController, command_name: str) -> None:
        """
        Refuse a command for a toy that is not CONNECTED.

        Args:
            toy: The toy controller.
            command_name: Name of the command, for the error.

        Raises:
            ToyNotConnectedError: The toy's status is not CONNECTED.
        """
        status = self._toy_status.get(toy.toy_id)
        if status != ToyStatus.CONNECTED:
            raise ToyNotConnectedError(toy.toy_id, toy.model_name, command_name, status)

    async def _battery_poll_loop(self) -> None:
        """Every ``BATTERY_UPDATE_INTERVAL``, poll the battery of every connected toy."""
        # Runs until shutdown() cancels it.
        while True:
            await asyncio.sleep(BATTERY_UPDATE_INTERVAL)
            try:
                await self._poll_all_batteries()
            except Exception as e:
                self._log.exception("Unexpected error in battery poll loop: %s", e)

    async def _poll_all_batteries(self) -> None:
        """
        Poll the battery of every connected toy and pass the changed levels to *on_battery_change*.
        A failed poll keeps the old level.
        """
        toys_to_poll = [
            toy
            for toy_id, toy in self._toys.items()
            if self._toy_status.get(toy_id) == ToyStatus.CONNECTED
        ]
        if not toys_to_poll:
            return

        updates: dict[str, int | None] = {}

        # Poll each toy
        async def poll_one(toy: _ToyController) -> None:
            async with toy.cmd_lock:
                old_battery = toy.battery
                new_battery = await toy.fetch_and_update_battery()
                if new_battery is not None and old_battery != new_battery:
                    updates[toy.toy_id] = new_battery

        await asyncio.gather(*(poll_one(toy) for toy in toys_to_poll))

        if updates:
            await self._fire_callback(self._on_battery_change, updates)

    async def _create_toy(
        self, builder: ConnectionBuilder, toy_data: ToyData
    ) -> _ToyController:
        """
        Connect a toy and wrap it in its brand's controller. The battery query is its first command.

        Args:
            builder: The connection builder.
            toy_data: Data describing the toy to create.

        Raises:
            InvalidModelError: The model name is not a model of the toy's brand.
            BadModelError: The toy refused or did not answer the model's commands.
            AddConnectionError: Connecting or the battery query failed.
            RuntimeError: Any other failure, e.g., *toy_data* was never discovered.

        Returns:
            A high-level _ToyController instance.
        """
        self._log.info(
            f"Creating new toy with parameters toy_id={toy_data.toy_id}, model_name={toy_data.model_name})"
        )
        result = await builder.create_toy(toy_data)
        if isinstance(result, Toy):
            try:
                # Unstable connection if the first command already fails. Treat as unable to connect.
                battery = await result.strict_get_battery_level()
            except Exception as e:
                raise AddConnectionError(toy_data.toy_id, toy_data.model_name) from e
            controller_cls = _CONTROLLER_BY_BRAND[result.brand]
            return controller_cls(result, battery)
        if isinstance(result, LowLevelInvalidModelError):
            raise InvalidModelError(
                toy_data.toy_id, toy_data.model_name, toy_data.brand
            ) from result
        if isinstance(result, LowLevelBadModelError):
            raise BadModelError(toy_data.toy_id, toy_data.model_name) from result
        if isinstance(result, ConnectionError):
            raise AddConnectionError(toy_data.toy_id, toy_data.model_name) from result
        raise RuntimeError(f"Unexpected result from create_toy: {result!r}")

    # ------------------------------
    # Toy Modification
    # ------------------------------

    async def add(self, toy_id: str, model_name: str) -> None:
        """
        Connect a toy that the running scan found (see :meth:`start_scan`), and add it. See :meth:`add_toy_data`.

        Args:
            toy_id: The toy.
            model_name: Its model name, case-insensitive.

        Raises:
            ToyAlreadyAddedError: The toy is added already, or being added.
            UndiscoveredToyError: The toy was never discovered.
            UnavailableToyError: The running scan does not see the toy (anymore).
            InvalidModelError: The model name is not a model of the toy's brand.
            BadModelError: The toy refused or did not answer the model's commands.
            AddConnectionError: Connecting failed.
            RuntimeError: Any other failure.
        """
        self._log.info(f"Adding toy at {toy_id} as {model_name}.")
        if toy_id in self._toys or toy_id in self._pending_toy_ids:
            raise ToyAlreadyAddedError(toy_id, model_name)
        if toy_id not in self._all_seen_toy_ids:
            raise UndiscoveredToyError(toy_id, model_name)
        toy_data = self._toy_data.get(toy_id)
        if toy_data is None:
            raise UnavailableToyError(toy_id, model_name)
        # Copy it. The discovery snapshot is shared, and _apply_discovery can replace it at any await below.
        toy_data = copy.copy(toy_data)
        toy_data.model_name = model_name
        await self.add_toy_data(toy_data)

    async def add_toy_data(self, toy_data: ToyData) -> None:
        """
        Connect a discovered toy, e.g., one from :meth:`discover`, and add it.

        The toy is CONNECTED, the toy cache remembers its model, and *on_toy_ids_change* is called.

        Args:
            toy_data: The toy, with its model name set (case-insensitive).

        Raises:
            ToyAlreadyAddedError: The toy is added already, or being added.
            InvalidModelError: The model name is not a model of the toy's brand.
            BadModelError: The toy refused or did not answer the model's commands.
            AddConnectionError: Connecting failed.
            RuntimeError: Any other failure, e.g., *toy_data* was never discovered.
        """
        toy_id = toy_data.toy_id
        # Our own copy, so a caller changing its ToyData while we connect cannot affect the toy we add.
        toy_data = copy.copy(toy_data)
        if toy_id in self._toys or toy_id in self._pending_toy_ids:
            raise ToyAlreadyAddedError(toy_id, toy_data.model_name)
        self._pending_toy_ids.add(toy_id)

        try:
            toy = await self._create_toy(self._connection_builder, toy_data)
        except Exception as e:
            self._pending_toy_ids.discard(toy_id)
            raise e

        self._pending_toy_ids.discard(toy_id)
        self._toys[toy_id] = toy
        self._toy_status[toy_id] = ToyStatus.CONNECTED
        self._toy_cache.update({toy.name: toy.model_name})
        ids_snapshot = list(self._toys.keys())

        await self._fire_callback(self._on_toy_ids_change, ids_snapshot)

    async def remove(
        self, toy_id: str, remove_from_connection_status: bool = True
    ) -> None:
        """
        Disconnect a toy and remove it. *on_toy_ids_change* is called.

        Args:
            toy_id: The toy.
            remove_from_connection_status: Forget the toy's status.
                If False, :meth:`get_status` still reports it (e.g., LOST).

        Raises:
            UnknownToyError: The toy is not added.
            ToyConnectionError: Disconnecting cleanly failed. The toy is removed anyway.
        """
        self._log.info(f"Removing {toy_id}")
        toy = self._toys.get(toy_id)
        if toy is None:
            raise UnknownToyError(toy_id)
        del self._toys[toy_id]
        if remove_from_connection_status:
            self._toy_status.pop(toy_id, None)
        ids_snapshot = list(self._toys.keys())
        try:
            await toy.disconnect()
        except Exception as e:
            self._log.warning(
                "Failed to disconnect toy %s: %s", toy_id, e, exc_info=True
            )
            await self._fire_callback(self._on_toy_ids_change, ids_snapshot)
            raise ToyConnectionError(toy_id, toy.model_name, "remove") from e

        await self._fire_callback(self._on_toy_ids_change, ids_snapshot)

    async def set_model(self, toy_id: str, model_name: str) -> None:
        """
        Change the model of an added toy. Capabilities both models share keep running, see
        :meth:`tikal.low_level.Toy.set_model_name`.

        On success, the toy cache remembers the model, and *on_model_change* is called.

        Args:
            toy_id: Unique identifier of the toy.
            model_name: New model name, case-insensitive.

        Raises:
            UnknownToyError: The toy is not added.
            InvalidModelError: The model name is not a model of the toy's brand. Nothing changed.
            BadModelError: The toy refused or did not answer the new model's commands. Nothing changed.
            ToyNotConnectedError: The toy is not connected. Nothing changed.
            ToyConnectionError: A command switching off a capability the new model replaces could not be delivered.
                Nothing changed. Reconnecting has started.
        """
        self._log.info(f"Setting model of {toy_id} to {model_name})")
        toy = self._get_toy(toy_id)
        async with toy.cmd_lock:
            # A model switch talks to the toy, so it needs the toy connected.
            self._require_connected(toy, "set_model")
            try:
                await toy.set_model_name(model_name)
            except LowLevelInvalidModelError as e:
                raise InvalidModelError(toy_id, model_name, toy.brand) from e
            except LowLevelBadModelError as e:
                raise BadModelError(toy_id, model_name) from e
            except ConnectionError as e:
                # A model switch stops the toy on its old commands first. That stop could not be delivered. The
                # model was left as-is and the toy may still be running.
                await self._start_reconnecting(toy, "Command failed")
                raise ToyConnectionError(toy_id, model_name, "set_model") from e
            self._toy_cache.update({toy.name: model_name})
        change = await toy.get_info(full=False)
        await self._fire_callback(self._on_model_change, change)

    async def stop(self, toy_id: str) -> None:
        """
        Set both intensities to 0 and pause the pattern. The block stays as it is.

        Args:
            toy_id: Unique identifier of the toy.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected, so it was not stopped. The pattern is paused anyway.
            ToyConnectionError: Stopping failed twice. The pattern is paused anyway. Reconnecting has started.
        """
        self._log.info(f"Stopping toy at {toy_id}")
        await self._change_state(toy_id, "stop", _ToyController.apply_stop)

    async def intensity(self, toy_id: str, channel: int, intensity: int) -> bool:
        """
        Set one capability manually. Pauses the pattern.

        Args:
            toy_id: Unique identifier of the toy.
            channel: 0 for intensity1, 1 for intensity2.
            intensity: The level, clamped to 0 to max_intensity and capped at the channel's limit.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected. Nothing was sent, and the pattern keeps playing.
            ToyConnectionError: The command failed twice. Reconnecting has started.

        Returns:
            True if the toy took the level. False while blocked (nothing changes), or without that capability (nothing
            is sent, but the pattern is paused).
        """
        name = f"intensity{channel + 1}"
        self._log.info(f"Setting {name} of {toy_id} to {intensity}")

        def command(toy: _ToyController) -> Awaitable[bool]:
            return toy.intensity(channel, max(0, min(intensity, toy.max_intensity)))

        return await self.send(toy_id, name, command)

    async def toggle_pause(self, toy_id: str) -> None:
        """Pause a playing pattern or resume a paused one, see :meth:`set_paused`. Raises like :meth:`set_paused`."""
        self._log.info(f"Toggling pause of {toy_id}")
        # The toggle is resolved to a target state once, and only the stop is retried: a retry cannot flip it back.
        await self._change_state(
            toy_id, "toggle_pause", lambda toy: toy.apply_paused(not toy.is_paused)
        )

    async def toggle_block(self, toy_id: str) -> None:
        """Block or unblock the toy, whichever it is not, see :meth:`set_blocked`. Raises like :meth:`set_blocked`."""
        self._log.info(f"Toggling block of {toy_id}")
        # Same as toggle_pause: resolve the toggle once so the retry cannot undo it.
        await self._change_state(
            toy_id, "toggle_block", lambda toy: toy.apply_blocked(not toy.is_blocked)
        )

    async def set_paused(self, toy_id: str, pause: bool) -> None:
        """
        Pause or resume pattern playback. Does nothing if the toy is in that state already.

        - Pausing stops the toy, freezes the pattern time, and unblocks the toy.
        - Resuming continues where the pattern was paused.
        - Manual intensity commands still work while paused.

        Args:
            toy_id: Unique identifier of the toy.
            pause: True to pause, False to resume.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected, so it was not stopped. The new state holds.
            ToyConnectionError: Stopping failed twice. The new state holds. Reconnecting has started.
        """
        self._log.info(f"Setting pause of {toy_id} to {pause}")
        # None when the toy already is in that state: then nothing is sent or reported.
        await self._change_state(
            toy_id,
            "set_paused",
            lambda toy: None if toy.is_paused == pause else toy.apply_paused(pause),
        )

    async def set_blocked(self, toy_id: str, block: bool) -> None:
        """
        Block or unblock the toy. Does nothing if it is in that state already.

        While blocked, the toy stays at 0: manual intensity commands return False, and :meth:`direct_command` is
        refused. The pattern keeps advancing without driving the toy. Blocking stops the toy and unpauses the pattern.

        Args:
            toy_id: Unique identifier of the toy.
            block: True to block, False to unblock.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected, so it was not stopped. The new state holds.
            ToyConnectionError: Stopping failed twice. The new state holds. Reconnecting has started.
        """
        self._log.info(f"Setting block of {toy_id} to {block}")
        # Same check-then-act guard as set_paused above.
        await self._change_state(
            toy_id,
            "set_blocked",
            lambda toy: None if toy.is_blocked == block else toy.apply_blocked(block),
        )

    async def block_all_for_safety(self) -> tuple[list[str], list[str]]:
        """
        Block every toy not blocked yet, and stop it. Used by the WebSocket server's heartbeat watchdog when a
        controlling client fails.

        - Unlike :meth:`set_blocked`, the pause stays as it is: once unblocked, a paused toy stays paused and a playing
          pattern continues.
        - Only :meth:`set_blocked` or :meth:`toggle_block` lift the block, toy by toy.
        - All toys are blocked at once, then stopped concurrently. A toy that is not connected is blocked, but not
          stopped.

        Returns:
            ``(blocked, not_stopped)`` with the ids of the toys this blocked, and of those among them that could not be
            stopped. These may still run until the hub reaches them again (reconnecting stops a toy).
        """
        blocked = [toy_id for toy_id, toy in self._toys.items() if not toy.is_blocked]
        self._log.info(f"Blocking every toy for safety: {blocked}")
        for toy_id in blocked:
            self._toys[toy_id].apply_blocked(True, keep_pause=True)

        async def stop(toy_id: str) -> bool:
            """Stop one toy that was just blocked, and report its new state. False if the stop failed."""
            try:
                toy = self._get_toy(toy_id)
            except UnknownToyError:
                return True  # removed in the meantime
            stopped = True
            try:
                async with toy.cmd_lock:
                    await self._run_toy_command(toy, "stop", toy.stop_output)
            except ToyConnectionError:
                self._log.warning(f"Could not stop {toy_id} after blocking it.")
                stopped = False
            await self._report_state(toy)
            return stopped

        results = await asyncio.gather(*(stop(toy_id) for toy_id in blocked))
        return blocked, [toy_id for toy_id, ok in zip(blocked, results) if not ok]

    async def set_intensity_limit(
        self, toy_id: str, channel: int, level: int | None
    ) -> None:
        """
        Set the limit of one channel. Manual levels and pattern values are capped at it. A toy running above the new
        limit is brought down immediately.

        The new state is reported even if bringing the toy down fails.

        Args:
            toy_id: Unique identifier of the toy.
            channel: 0 for intensity1, 1 for intensity2.
            level: The limit, clamped to 0 to max_intensity. Set to None to remove it.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy runs above the limit but is not connected, so it was not brought down. The
                limit is still in force.
            ToyConnectionError: Bringing the toy down failed. The limit is still in force. Reconnecting has started.
        """
        name = f"set_intensity{channel + 1}_limit"
        self._log.info(f"Setting intensity{channel + 1} limit of {toy_id} to {level}")
        await self._change_state(
            toy_id,
            name,
            lambda toy: toy.apply_limit(channel, level),
            lambda toy: toy.enforce_limit(channel),
            # Report the new ceiling even when enforcing it failed: it is in force from here on.
            report_failure=True,
        )

    async def set_pattern(
        self,
        toy_id: str,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> None:
        """
        Set a pattern. An empty list clears it and stops the toy. The pause stays as it is.

        Args:
            toy_id: Unique identifier of the toy.
            pattern: Segments as ``(duration_ms, intensity1, intensity2)``. Stored as given: limits apply during
                playback.
            wraparound: Loop the pattern. If False, it plays once, then the intensities are 0.
            reset_time: Start at the beginning. If False, keep the current pattern time.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The pattern was cleared, but the toy is not connected, so it was not stopped.
            ToyConnectionError: The pattern was cleared, but stopping failed twice. Reconnecting has started.
        """
        # A converted funscript can have thousands of segments: the full pattern only goes to the debug log.
        self._log.info(f"Setting pattern of {toy_id} ({len(pattern)} segments)")
        self._log.debug(f"Pattern of {toy_id}: {pattern}")
        await self._change_state(
            toy_id,
            "set_pattern",
            lambda toy: toy.apply_pattern(pattern, wraparound, reset_time),
        )

    async def set_funscript(
        self,
        toy_id: str,
        script: Funscript,
        *,
        mode: FunscriptMode = "speed",
        max_speed: float = 400.0,
        secondary: Funscript | None = None,
        start_ms: float = 0.0,
    ) -> None:
        """
        Play a funscript: convert it into a pattern for this toy and set it, played once. The pause stays as it is.

        The conversion uses the toy's maximum intensity and recommended minimum interval (see
        :func:`tikal.funscript.funscript_to_pattern`). Runs in a worker thread. The result is an ordinary pattern.

        Args:
            toy_id: The toy.
            script: The script for the primary capability.
            mode: ``"speed"`` (the intensity follows the stroke speed) or ``"position"``.
            max_speed: Stroke speed in positions per second that maps to the maximum intensity in ``"speed"`` mode.
            secondary: Optional script for the secondary capability, e.g., a ``.twist.funscript``.
            start_ms: Pattern time to start at in ms, e.g., the video's position. Negative values count as 0.

        Raises:
            UnknownToyError: The toy is not added.
            ValueError: *max_speed* is not positive, *mode* is unknown, or *start_ms* is not a finite number. Nothing
                changed.
            ToyNotConnectedError: The script has no action after 0 ms, which clears the pattern, but the toy is not
                connected, so it was not stopped.
            ToyConnectionError: The pattern was cleared, but stopping failed twice. Reconnecting has started.
        """
        self._log.info(
            f"Setting funscript of {toy_id} ({len(script.actions)} actions, starting at {start_ms} ms)"
        )
        pattern = await asyncio.to_thread(
            self._get_toy(toy_id).convert_funscript,
            script,
            mode=mode,
            max_speed=max_speed,
            secondary=secondary,
        )
        await self._change_state(
            toy_id,
            "set_funscript",
            lambda toy: toy.apply_funscript(pattern, start_ms),
        )

    async def seek(self, toy_id: str, pattern_time: float) -> None:
        """
        Move a toy's playback to a position, e.g., to follow a video that jumped. Nothing is sent: the next playback
        tick plays the new position. A paused pattern stays paused there.

        Args:
            toy_id: The toy.
            pattern_time: The new pattern time in ms. Negative values count as 0. Past the end of a wraparound pattern,
                playback wraps around.

        Raises:
            UnknownToyError: The toy is not added.
            ValueError: *pattern_time* is not a finite number. Nothing changed.
        """
        self._log.info(f"Moving pattern of {toy_id} to {pattern_time} ms")
        await self._change_state(
            toy_id, "seek", lambda toy: toy.apply_seek(pattern_time)
        )

    def apply_state(self, toy_id: str, transition: Callable[[_ToyController], T]) -> T:
        """
        Apply a state transition right away and return its result. Call it on the hub's loop thread.

        Unlike the command methods, it does not wait for a command in flight, and it works whatever the toy's connection
        status. Being synchronous, it also works in a callback on the loop. Send the command the transition calls for
        with :meth:`send`. The new state is reported asynchronously.

        Args:
            toy_id: The toy.
            transition: Changes the state of the toy's controller, without I/O: one of its ``apply_*`` methods or
                ``accept_manual_intensity``.

        Raises:
            UnknownToyError: The toy is not added.

        Returns:
            Whatever *transition* returns.
        """
        toy = self._get_toy(toy_id)
        result = transition(toy)
        self._schedule_on_loop(self._report_state(toy))
        return result

    def get_controller(self, toy_id: str) -> _ToyController | None:
        """
        The toy's controller, or None if the toy is not added.

        Read-Only. Change it on the event loop, through :meth:`apply_state` or the commands.
        """
        return self._toys.get(toy_id)

    def is_connected(self, toy_id: str) -> bool:
        """Whether the toy is added and CONNECTED."""
        return (
            toy_id in self._toys and self._toy_status.get(toy_id) == ToyStatus.CONNECTED
        )

    async def send(
        self,
        toy_id: str,
        command_name: str,
        command: Callable[[_ToyController], Awaitable[T]],
        report_state: bool = True,
    ) -> T:
        """
        Send a command to a toy.

       Sends it under the toy's command lock (in order with its other commands), refused while the toy is not
       connected. Failed commands are retried once after a ConnectionError, followed by reconnecting if it fails again.

        Args:
            toy_id: Unique Identifier of the toy.
            command_name: Name of the command, for errors and logs.
            command: Sends the command, given the toy's controller, e.g., ``lambda toy: toy.stop_output()``.
            report_state: Report the toy's state afterward. False for queries.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The command failed twice. Reconnecting has started.

        Returns:
            The command's result.
        """
        toy = self._get_toy(toy_id)
        async with toy.cmd_lock:
            result = await self._run_toy_command(toy, command_name, command, toy)
        if report_state:
            await self._report_state(toy)
        return result

    async def fetch_battery(self, toy_id: str) -> int | None:
        """
        Ask the toy for its battery level.
        Unlike :meth:`get_battery` this asks the toy, instead of returning the last polled level.
        *on_battery_change* is called if the level changed.

        Args:
            toy_id: Unique identifier of the toy.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The query failed twice. Reconnecting has started.

        Returns:
            The level in percent, or None if the toy has no battery.
        """
        toy = self._get_toy(toy_id)
        async with toy.cmd_lock:
            old_battery = toy.battery
            battery = await self._run_toy_command(
                toy, "fetch_battery", toy.refresh_battery
            )
        if battery != old_battery:
            await self._fire_callback(self._on_battery_change, {toy_id: battery})
        return battery

    # ---------------------------------------------
    # Toy State request and commands that do not modify the internal state
    # -------------------------------------------

    @staticmethod
    async def get_brands() -> dict[str, list[str]]:
        """Every supported brand and its model names, see :data:`tikal.low_level.BRANDS`."""
        return BRANDS

    async def get_toy_ids(self) -> list[str]:
        """
        Retrieve a list of all toy_ids currently managed by _ToyHub

        Returns:
            A list of toy_ids currently managed by _ToyHub. Snapshot, the list will not update automatically.
        """
        return list(self._toys.keys())

    async def get_state(self, toy_id: str) -> dict[str, Any]:
        """
        The toy's state. See :meth:`_ToyController.get_state` for the keys.

        Args:
            toy_id: Unique identifier of the toy.

        Raises:
            UnknownToyError: The toy is not added.
        """
        toy = self._get_toy(toy_id)
        return toy.get_state()

    async def get_battery(self, toy_id: str) -> int | None:
        """
        The toy's last known battery level in percent, polled every ``BATTERY_UPDATE_INTERVAL``.
        None if the toy has no battery.

        Args:
            toy_id: Unique identifier of the toy.

        Raises:
            UnknownToyError: The toy is not added.
        """
        toy = self._get_toy(toy_id)
        return toy.battery

    async def get_info(self, toy_id: str, full: bool) -> dict[str, Any]:
        """
        Information about the toy. See :meth:`_ToyController.get_info` for the keys.

        Args:
            toy_id: Unique identifier of the toy.
            full: Also ask the toy for brand-specific information

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: With *full*: the toy is not connected. Nothing was sent.
            ToyConnectionError: With *full*: a query failed (connection issue). Reconnecting has started.

        Returns:
            The information.
        """
        if not full:
            return await self._get_toy(toy_id).get_info(full=False)
        return await self.send(
            toy_id, "get_info", lambda toy: toy.get_info(True), report_state=False
        )

    async def get_all(self, toy_id: str, full: bool) -> dict[str, Any]:
        """
        :meth:`get_info`, :meth:`get_state`, :meth:`get_status`), and :meth:`get_battery` combined.

        Args:
            toy_id: Unique identifier of the toy.
            full: Also ask the toy for brand-specific information, see :meth:`get_info`.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: With *full*: the toy is not connected. Nothing was sent.
            ToyConnectionError: With *full*: a query failed twice. Reconnecting has started.

        Returns:
            The combined dict.
        """
        toy = self._get_toy(toy_id)
        status = self._toy_status[toy_id]
        info = await toy.get_info(full=False)
        if full:
            info = await self.send(
                toy_id, "get_all", lambda toy: toy.get_info(True), report_state=False
            )
        state = toy.get_state()
        merged = {**state, **info, "connection_status": status, "battery": toy.battery}
        return merged

    async def get_status(self, toy_id: str) -> str:
        """
        The toy's connection status:
        ``"connected"``, ``"reconnecting"``, ``"lost"`` or ``"powered_off"`` (a :class:`ToyStatus`).

        Args:
            toy_id: Unique identifier of the toy.

        Raises:
            UnknownToyError: The hub knows no status of the toy.
        """
        try:
            return self._toy_status[toy_id]
        except KeyError:
            raise UnknownToyError(toy_id)

    async def direct_command(self, toy_id: str, command: str) -> str:
        """
        Send a raw command. Use only for commands the library does not implement.
        The tracked state does not update by this. Don't use it for commands that the library implements.

        Args:
            toy_id: Unique identifier of the toy.
            command: The command in the toy's protocol, e.g., ``"DeviceType"``.

        Raises:
            UnknownToyError: The toy is not added.
            ToyBlockedError: The toy is blocked, and a raw command could drive it. Nothing was sent.
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The command failed twice. Reconnecting has started.

        Returns:
            The toy's reply.
        """
        self._log.info(f"Sending direct command to {toy_id}: {command}")
        toy = self._get_toy(toy_id)
        async with toy.cmd_lock:
            # A raw command can drive the toy, and the hub cannot tell which ones would, so none pass a block.
            if toy.is_blocked:
                raise ToyBlockedError(toy_id)
            return await self._run_toy_command(
                toy, "direct_command", toy.direct_command, command
            )

    async def change_rotation_direction(self, toy_id: str) -> bool:
        """
        Reverse the rotation direction. Not part of the toy's state: toys do not report their direction.

        Args:
            toy_id: Unique identifier of the toy.

        Raises:
            UnknownToyError: The toy is not added.
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: Failed to send the command (connection issue). Reconnecting starts automatically.

        Returns:
            True, or False without rotation support (nothing is sent).
        """
        return await self.send(
            toy_id,
            "change_rotation_direction",
            _ToyController.change_rotation_direction,
            report_state=False,
        )
