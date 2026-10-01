"""
Private Module of the async core

Contains all the toy management logic (discovery, connection, state tracking, pattern playback, reconnection), so
ToyServer can focus on defining the public API alone. Comparable to the High-Level ToyHub, but async instead of sync.
Makes use of private ToyControllers, which unlike the High-Level ToyControllers are not exposed to users -> all logic
routed through _ToyHub instead.
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
from ..low_level import BRANDS
from ..low_level import BadModelError as LowLevelBadModelError
from ..low_level import ConnectionBuilder
from ..low_level import InvalidModelError as LowLevelInvalidModelError
from ..low_level import Toy, ToyData
from ..mock import MockBleakClient, MockBleakScanner
from .toy_cache import ToyCache
from .toy_controller import _CONTROLLER_BY_BRAND, _ToyController

# Retry backoff after a ConnectionError. Intentionally separate from the loop cadence
# (COMMUNICATION_INTERVAL): it just happens to share the same value today.
_RETRY_DELAY = 0.05  # seconds

T = TypeVar("T")


class ToyStatus(StrEnum):
    """Represents the connection status of a toy managed by _ToyHub."""

    # reconnection succeeded. Always preceded by a toy_state_change event (so clients stay synchronized with the _ToyHub state)
    CONNECTED = "connected"
    # on_disconnect fired, command failed. Reconnection is attempted automatically, for up to RECONNECT_WINDOW
    RECONNECTING = "reconnecting"
    # no reconnect attempt succeeded within RECONNECT_WINDOW, toy will be removed automatically
    LOST = "lost"
    # toy powered off, toy will be removed automatically
    POWERED_OFF = "powered_off"


class UndiscoveredToyError(ValueError):
    """Raised when trying to add a toy that has never been discovered."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class UnknownToyError(ValueError):
    """Raised when trying to interact with a toy that is not known to _ToyHub."""

    def __init__(self, toy_id: str):
        self.toy_id = toy_id


class SafetyHoldError(ValueError):
    """Raised when a command that could drive a toy is refused because the toy is under the safety hold."""

    def __init__(self, toy_id: str):
        self.toy_id = toy_id


class ToyAlreadyAddedError(ConnectionError):
    """Raised when attempting to add a toy that is already added or pending addition."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class InvalidModelError(ValueError):
    """Raised when the assigned model_name (case-insensitive) is not one of the supported models for this brand"""

    def __init__(self, toy_id: str, model_name: str, brand: str):
        self.toy_id = toy_id
        self.model_name = model_name
        self.brand = brand


class BadModelError(ConnectionError):
    """Raised when the model name is valid, but the toy does not respond correctly to commands. Can be a sign of a bug in the library."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class AddConnectionError(ConnectionError):
    """Raised when unable to connect to the toy during an add command (e.g., toy is not responding)."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class ToyConnectionError(ConnectionError):
    """Raised when the established connection to a toy fails (e.g., toy is not responding)."""

    def __init__(self, toy_id: str, model_name: str, cmd: str):
        self.toy_id = toy_id
        self.model_name = model_name
        self.cmd = cmd


class ToyNotConnectedError(ToyConnectionError):
    """
    Raised instead of sending a command to a toy that is not connected (e.g., while it is reconnecting). Nothing was sent.

    A state change the command asked for (e.g., block, pause, pattern, limit) is recorded all the same: the reconnect
    stops the toy before it is used again, and from then on the toy follows the new state.
    """

    def __init__(
        self, toy_id: str, model_name: str, cmd: str, status: ToyStatus | None
    ):
        super().__init__(toy_id, model_name, cmd)
        self.status = status


class UnavailableToyError(ConnectionError):
    """Raised when trying to add a toy that was at some point discovered, but is no longer available."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class DiscoveryStartError(ConnectionError):
    """Raised when the discovery process cannot be started (e.g., Bluetooth/permission issue)."""

    def __init__(self, tb: str):
        self.tb = tb


class DiscoveryError(ConnectionError):
    """Raised when an ongoing discovery fails after having started successfully."""

    def __init__(self, tb: str):
        self.tb = tb


async def _retry(fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    """Call ``await fn(*args)``. On a *first* ConnectionError, wait _RETRY_DELAY seconds and try once more. A second ConnectionError propagates to the caller."""
    try:
        return await fn(*args)
    except ConnectionError:
        await asyncio.sleep(_RETRY_DELAY)
        return await fn(*args)


class _ToyHub:

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
        """
        Manager for all toys.

        Handles discovery, connection, state tracking, pattern playback, and automatic reconnection.
        Must be used from one event loop. Commands to the same toy are serialized by a per-toy command lock.

        Args:
            on_status_change: Called with toy_id, status whenever the status of a toy changes.
            on_toy_ids_change: Called with a full list of all managed toy_ids whenever the set of managed toys changes.
            on_toy_state_change: Called with toy_state whenever the state of a toy changes.
            on_model_change: Called with a dict containing toy_id, model_name whenever the model of a toy changes.
            on_battery_change: Called with a dict of toy_id : battery_level pairs of all toys whose battery level changed..
            toy_cache_path: If not empty, writes / reads toy_id -> model_name mappings to this file. This allows the library to fill out the model_name of discovered toys.
            default_model: This model_name will be used for toys that do not have a model_name in the toy_cache.
            log_name: Name of the logger used for logging.
            mock_toys: If true, uses MockBleakScanner and MockBleakClient instead of BleakScanner and BleakClient, and
                offers the fictional MockEstimToys brand (For testing).
            bluetooth_scanner: BLE scanner class to use instead of the one ``mock_toys`` picks (e.g., for tests).
            bluetooth_client: BLE client class to use instead of the one ``mock_toys`` picks (e.g., for tests).
        """
        self._log = logging.getLogger(log_name)
        self._log.info(
            f"Initializing _ToyHub with toy_cache_path={toy_cache_path} and mock_toys={mock_toys}"
        )

        # Toy state
        self._toy_cache = ToyCache(toy_cache_path, default_model, log_name)
        self._toys: dict[str, _ToyController] = {}
        # Per-toy command lock: held for the full check-command-callback sequence of every mutating operation on a single toy.
        # This prevents races (e.g., two clients racing on set_paused) and serializes concurrent BLE commands to the same toy.
        self._toy_cmd_locks: dict[str, asyncio.Lock] = {}
        self._toy_data: dict[str, ToyData] = {}
        self._all_seen_toy_ids: set[str] = set()
        self._pending_toy_ids: set[str] = set()
        # Safety hold (see set_safety_hold). Changed in one go together with every toy's flag, so a toy added at the
        # same time can never miss it.
        self._held = False

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

        # at most one reconnect task per toy. Keyed by toy_id.
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}

    # -------------------------------------------------------------------------
    # Startup / Shutdown
    # -------------------------------------------------------------------------

    async def startup(self) -> None:
        """Start the background processing and battery polling loops. Call before using _ToyHub. Idempotent."""
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
        """Stop the background processing and battery polling loops. Disconnects all toys. Call when finished using _ToyHub. Idempotent."""
        self._log.info("Shutting down _ToyHub.")

        # Idempotent. Safe to call even when no scan is running
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
        self._toy_cmd_locks.clear()
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

        Each toy's model name is filled in from the ToyCache (or the default model). Found toys can be connected with
        :meth:`add_toy_data`.

        Args:
            timeout: How long to scan, in seconds.

        Raises:
            RuntimeError: A continuous scan (see :meth:`start_scan`) is in progress.
            Exception: Any exception from the underlying scanner (e.g., Bluetooth not available).

        Returns:
            The discovered toys. Copies, so changing them does not affect the hub.
        """
        self._log.info(f"Discovering toys for {timeout} s")
        found = await self._connection_builder.discover_toys(timeout)
        return [self._with_cached_model(data) for data in found]

    async def start_scan(
        self, callback: Callable[[Exception | list[ToyData]], Any]
    ) -> None:
        """
        Start continuous background discovery of Toys.

        Args:
            callback:   Called with a snapshot of all discovered Toys whenever the available toys change, each with its
                        model name filled in from the ToyCache (copies, so changing them does not affect the hub).
                        Called with an exception if the continuous scan encounters an error.

        Raises:
            DiscoveryStartError: The scan could not be started (e.g., Bluetooth not available)
            RuntimeError: Could not get the asyncio event loop. Developer Error
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
        """Stop continuous background discovery of Toys. After this call, the callback provided to `start_scan` will no longer be invoked."""
        self._log.info("Stopping toy discovery")
        self._toy_data.clear()
        await self._connection_builder.stop_continuous()

    # -------------------------------------------------------------------------
    # Private
    # -------------------------------------------------------------------------

    def _schedule_on_loop(self, coro: Coroutine[Any, Any, Any]) -> None:
        """
        Schedule *coro* on the hub's event loop from any thread.

        The low-level BLE callbacks (unexpected disconnect, power-off) can be invoked from a Bleak worker thread, so we
        marshal the coroutine onto the loop captured in :meth:`startup`. If the loop is not available yet
        (no toy can be connected before startup), the coroutine is closed to avoid a "coroutine was never awaited" warning.
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
            update: Either an exception indicating an error, or a list of discovered ToyData objects.
            callback: The user callback that will receive the list of discovered toys (copies with cached model names).
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
        """
        A copy of *data* with the model name the ToyCache remembers for it (or the default model).

        Copying keeps the connection builder's discovery snapshot, which it hands out again, from being changed.
        """
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
        try:
            task = self._on_status_change(toy_id, new_status)
            if asyncio.iscoroutine(task):
                await task
        except Exception:
            self._log.exception("on_status_change callback raised")

    async def _on_disconnect(self, toy_id: str) -> None:
        """
        Handle an unexpected disconnection from a toy.

        Sets ToyStatus to RECONNECTING and starts a background reconnection attempt.

        Args:
            toy_id: Identifier of the disconnected toy.
        """
        toy = self._toys.get(toy_id)
        if toy is None:
            return
        self._ensure_reconnect_task(toy)
        await self._set_toy_status(toy_id, ToyStatus.RECONNECTING)

    async def _on_power_off(self, toy_id: str) -> None:
        """
        Handle a toy power‑off event.

        Sets ToyStatus to POWERED_OFF and removes the Toy from _ToyHub.

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
        """
        Background task that periodically calls `process_communication` on all connected toys.
        Runs every `COMMUNICATION_INTERVAL` seconds. Only toys with status CONNECTED are processed.
        """
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            toys_and_locks = [
                (toy, self._toy_cmd_locks[toy_id])
                for toy_id, toy in self._toys.items()
                if self._toy_status.get(toy_id) == ToyStatus.CONNECTED
            ]
            if toys_and_locks:
                await asyncio.gather(
                    *(
                        self._process_one_locked(toy, lock)
                        for toy, lock in toys_and_locks
                    ),
                    return_exceptions=True,
                )
            # Sleep only the time left in this interval
            await asyncio.sleep(
                max(0.0, COMMUNICATION_INTERVAL - (loop.time() - start))
            )

    async def _process_one_locked(
        self, toy: _ToyController, cmd_lock: asyncio.Lock
    ) -> None:
        """
        Acquire the per‑toy command lock and run one `process_communication` tick.

        Holding the lock prevents interleaving with concurrent user commands on the same toy.

        Args:
            toy: The toy controller to process.
            cmd_lock: The toy's command lock.
        """
        async with cmd_lock:
            await self._process_one(toy)

    async def _process_one(self, toy: _ToyController) -> None:
        """
        Run process_communication for a single toy with one automatic retry.
        Errors after the retry are logged but not re-raised so the loop stays alive.
        Args:
            toy: The toy controller whose process_communication method will be called.
        """
        try:
            try:
                await toy.process_communication()
            except ConnectionError:
                await asyncio.sleep(_RETRY_DELAY)
                await toy.process_communication()
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

    async def _handle_command_failure(self, toy: _ToyController) -> None:
        """
        Handle a command failure after the built‑in retry.

        Set the ToyState to RECONNECTING and start a background reconnection attempt.
        The background task marks the toy as CONNECTED on success or LOST on failure.

        Args:
            toy: The toy controller that failed.
        """
        self._log.warning("Command failed for toy %s. Reconnecting...", toy.toy_id)
        self._ensure_reconnect_task(toy)
        await self._set_toy_status(toy.toy_id, ToyStatus.RECONNECTING)

    def _ensure_reconnect_task(self, toy: _ToyController) -> None:
        """
        Start a background reconnect task for a toy if none is already running.

        Args:
            toy: The toy controller that needs reconnection.
        """
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

    def _prune_reconnect_task(self, toy_id: str, task: asyncio.Task[None]) -> None:
        """
        Remove a completed reconnect task from the tracking dictionary if it is still the current entry. Call after completing a reconnect task.

        Args:
            toy_id: Identifier of the toy.
            task: The task that completed.
        """
        if self._reconnect_tasks.get(toy_id) is task:
            self._reconnect_tasks.pop(toy_id, None)

    async def _reconnect_toy(self, toy: _ToyController) -> None:
        """
        Win back a toy whose connection failed, or give it up. Sets its ToyStatus to CONNECTED on success; else sets it
        to LOST and removes the toy.

        Each attempt reconnects (a no-op if the link is still up) and then stops the toy, which also pauses its pattern:
        whatever the toy was last told no longer holds, and this stop is how a command that failed (e.g. the safety
        hold's stop) finally gets through. Attempts are repeated for up to RECONNECT_WINDOW (see
        :func:`tikal._private.retry_within_window`).

        Args:
            toy: The toy controller to reconnect.
        """

        async def attempt() -> None:
            await toy.reconnect()
            await toy.stop()  # Always stop the toy after connection loss

        if await retry_within_window(attempt, toy.toy_id, self._log):
            await self._fire_callback(self._on_toy_state_change, toy.get_state())
            await self._set_toy_status(toy.toy_id, ToyStatus.CONNECTED)
            return

        await self._set_toy_status(toy.toy_id, ToyStatus.LOST)
        try:
            await self.remove(toy.toy_id, False)
        except Exception:
            pass

    def _get_toy(self, toy_id: str) -> _ToyController:
        """
        Retrieve a toy controller by its ID.

        Args:
            toy_id: Identifier of the toy.

        Raises:
            UnknownToyError: The toy has not been added.

        Returns:
            The _ToyController instance.
        """
        toy = self._toys.get(toy_id)
        if toy is None:
            raise UnknownToyError(toy_id)
        return toy

    def _get_toy_cmd(self, toy_id: str) -> tuple[_ToyController, asyncio.Lock]:
        """
        Retrieve a toy controller and its per‑toy command lock.

        Callers must hold the returned lock for the full duration of their check‑command‑callback sequence to prevent interleaving.

        Args:
            toy_id: Identifier of the toy.

        Raises:
            UnknownToyError: The toy has not been added.

        Returns:
            A tuple (toy_controller, lock).
        """
        toy = self._toys.get(toy_id)
        if toy is None:
            raise UnknownToyError(toy_id)
        return toy, self._toy_cmd_locks[toy_id]

    async def _fire_callback(self, callback: Callable[..., Any], payload: Any) -> None:
        """
        Invoke a callback with the given payload, supporting both sync and async callables.
        Exceptions are logged but not raised, so a misbehaving callback cannot affect command execution.

        Args:
            callback: The callback to invoke.
            payload: The argument(s) to pass to the callback.
        """
        try:
            result = callback(payload)
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
        Execute a toy command with one automatic retry on ConnectionError. If the command fails after retry, triggers reconnection and raises ToyConnectionError.

        Every command the hub sends to a toy goes through here, so this is also where a toy that is not connected is
        refused (see :meth:`_require_connected`).

        Args:
            toy: The toy controller.
            command_name: Name of the command to execute.
            command: Async callable to execute.
            *args: Arguments to pass to the command.

        Raises:
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The command failed after retry.

        Returns:
            The command's result
        """
        self._require_connected(toy, command_name)
        try:
            return await _retry(command, *args)
        except Exception as e:
            await self._handle_command_failure(toy)
            raise ToyConnectionError(toy.toy_id, toy.model_name, command_name) from e

    def _require_connected(self, toy: _ToyController, command_name: str) -> None:
        """
        Refuse a command for a toy that is not connected, instead of sending it.

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
        """
        Background loop that periodically polls battery levels of all connected toys.
        Runs every `BATTERY_UPDATE_INTERVAL` seconds and fires the on_battery_change callback when any battery level changes.
        """
        # Runs until shutdown() cancels it.
        while True:
            await asyncio.sleep(BATTERY_UPDATE_INTERVAL)
            try:
                await self._poll_all_batteries()
            except Exception as e:
                self._log.exception("Unexpected error in battery poll loop: %s", e)

    async def _poll_all_batteries(self) -> None:
        """
        Poll battery levels for all toys with status CONNECTED.
        Collects changes and invokes the on_battery_change callback with a dict of toy_id -> new_battery_level (or None if no battery).
        """
        # Snapshot of connected toys under lock
        toys_to_poll = [
            (toy_id, toy, self._toy_cmd_locks[toy_id])
            for toy_id, toy in self._toys.items()
            if self._toy_status.get(toy_id) == ToyStatus.CONNECTED
        ]
        if not toys_to_poll:
            return

        updates: dict[str, int | None] = {}

        # Poll each toy
        async def poll_one(
            toy_id: str, toy: _ToyController, lock: asyncio.Lock
        ) -> None:
            async with lock:
                old_battery = toy.battery
                new_battery = await toy.fetch_and_update_battery()
                if new_battery is not None and old_battery != new_battery:
                    updates[toy_id] = new_battery

        await asyncio.gather(*(poll_one(tid, t, lk) for tid, t, lk in toys_to_poll))

        if updates:
            await self._fire_callback(self._on_battery_change, updates)

    async def _create_toy(
        self, builder: ConnectionBuilder, toy_data: ToyData
    ) -> _ToyController:
        """
        Adapter that converts ConnectionBuilder's result‑type errors to raised exceptions.

        Args:
            builder: The connection builder.
            toy_data: Data describing the toy to create.

        Returns:
            A high-level _ToyController instance.

        Raises:
            InvalidModelError: Model name not valid for the brand.
            BadModelError: Model name is valid, but toy commands still fail. Either the wrong model or developer error.
            AddConnectionError: Connection failed.
            RuntimeError: Unexpected result from create_toy (developer error).
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
        Adds a new toy found by the running scan (see :meth:`start_scan`) to the system.

        Args:
            toy_id: Unique identifier of the toy to add.
            model_name: model name of the toy.

        Raises:
            ToyAlreadyAddedError: The toy was already added.
            UndiscoveredToyError: The toy was not discovered before.
            UnavailableToyError: The toy was discovered at some point but is not available anymore.
            InvalidModelError: The model name is not valid for the toy brand.
            BadModelError: The model name is valid, but the toy still does not respond correctly to commands.
            AddConnectionError: Proper connection failed.
            RuntimeError: Unexpected result from create_toy. Development error.
        """
        self._log.info(f"Adding toy at {toy_id} as {model_name}.")
        if toy_id in self._toys or toy_id in self._pending_toy_ids:
            raise ToyAlreadyAddedError(toy_id, model_name)
        if toy_id not in self._all_seen_toy_ids:
            raise UndiscoveredToyError(toy_id, model_name)
        toy_data = self._toy_data.get(toy_id)
        if toy_data is None:
            raise UnavailableToyError(toy_id, model_name)
        # Shallow-copy so we don't mutate the shared discovery cache entry.
        # _apply_discovery may replace _toy_data[toy_id] at any await point below: keep our local model_name assignment independent.
        toy_data = copy.copy(toy_data)
        toy_data.model_name = model_name
        await self.add_toy_data(toy_data)

    async def add_toy_data(self, toy_data: ToyData) -> None:
        """
        Connect the toy that *toy_data* describes and add it to the system, as model ``toy_data.model_name``.

        Unlike :meth:`add`, the toy does not have to come from the running scan: any discovered ToyData will do, e.g.,
        from :meth:`discover`.

        Args:
            toy_data: The toy to connect, with its model name set.

        Raises:
            ToyAlreadyAddedError: The toy was already added.
            InvalidModelError: The model name is not valid for the toy brand.
            BadModelError: The model name is valid, but the toy still does not respond correctly to commands.
            AddConnectionError: Proper connection failed.
            RuntimeError: Unexpected result from create_toy. Development error.
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
        # A toy connected during a safety hold is held too. It is at rest after connecting, so no stop is needed.
        toy.set_held(self._held)
        self._toys[toy_id] = toy
        self._toy_cmd_locks[toy_id] = asyncio.Lock()
        self._toy_status[toy_id] = ToyStatus.CONNECTED
        self._toy_cache.update({toy.name: toy.model_name})
        ids_snapshot = list(self._toys.keys())

        await self._fire_callback(self._on_toy_ids_change, ids_snapshot)

    async def remove(
        self, toy_id: str, remove_from_connection_status: bool = True
    ) -> None:
        """
        Disconnects and removes a toy from the system.

        Args:
            toy_id: Identifier of the toy to remove.
            remove_from_connection_status: If True (Default), the toys' connection status is deleted. If False, the toys' connection status is retained.

        Raises:
            UnknownToyError: The toy was not added before.
            ToyConnectionError: Proper disconnect failed. Only for info purposes. Toy is still removed.
        """
        self._log.info(f"Removing {toy_id}")
        toy = self._toys.get(toy_id)
        if toy is None:
            raise UnknownToyError(toy_id)
        del self._toys[toy_id]
        self._toy_cmd_locks.pop(toy_id, None)
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
        Change the model name of an already-added toy.

        Args:
            toy_id: Identifier of the toy to set the model name of.
            model_name: New model name.

        Raises:
            UnknownToyError: The toy was not added before
            InvalidModelError: the model name is not valid for the toy brand.
            BadModelError: the model name is valid, but the toy still does not respond correctly to commands.
            ToyNotConnectedError: The toy is not connected, so the model was left unchanged.
            ToyConnectionError: The toy could not be stopped on its old command set, so the model was left unchanged.
                Reconnecting is attempted automatically.
        """
        self._log.info(f"Setting model of {toy_id} to {model_name})")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # A model switch talks to the toy (stop, then validate), so it needs the toy like any other command.
            self._require_connected(toy, "set_model")
            try:
                await toy.set_model_name(model_name)
            except LowLevelInvalidModelError as e:
                raise InvalidModelError(toy_id, model_name, toy.brand) from e
            except LowLevelBadModelError as e:
                raise BadModelError(toy_id, model_name) from e
            except ConnectionError as e:
                # A model switch stops the toy on its old commands first. That stop could not be delivered, so the
                # model was left as-is and the toy may still be running.
                await self._handle_command_failure(toy)
                raise ToyConnectionError(toy_id, model_name, "set_model") from e
            self._toy_cache.update({toy.name: model_name})
        change = await toy.get_info(full=False)
        await self._fire_callback(self._on_model_change, change)

    async def stop(self, toy_id: str) -> None:
        """
        Stop all toy actions (set all intensities to zero). If a pattern is active and not paused, this method pauses the pattern.

        Args:
            toy_id: Identifier of the toy to stop.

        Raises:
            ToyNotConnectedError: The toy is not connected, so nothing was sent. The pattern is paused all the same.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Stopping toy at {toy_id}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            toy.apply_stop()
            await self._run_toy_command(toy, "stop", toy.stop_output)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def intensity1(self, toy_id: str, intensity: int) -> bool:
        """
        Set the intensity of the primary capability.

        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts.
        Will do nothing if the toy is blocked or under the safety hold.

        Args:
            toy_id: Identifier of the toy on which to set the intensity.
            intensity: Intensity level. The valid range depends on the toy type. Values outside the range are clamped.

        Raises:
            ToyNotConnectedError: The toy is not connected. Nothing was sent, and the pattern keeps playing.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting intensity1 of {toy_id} to {intensity}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            level = max(0, min(intensity, toy.max_intensity))
            result = await self._run_toy_command(
                toy, "intensity1", toy.intensity1, level
            )
        await self._fire_callback(self._on_toy_state_change, toy.get_state())
        return result

    async def intensity2(self, toy_id: str, intensity: int) -> bool:
        """
        Set the intensity of the secondary capability.

        Behavior is identical to intensity1 but controls the secondary capability (e.g., rotation, air pump).
        Safe to call on toys without a secondary capability (will do nothing on them).

        Args:
            toy_id: Identifier of the toy on which to set the intensity.
            intensity: Intensity level. The valid range depends on the toy type. Values outside the range are clamped.

        Raises:
            ToyNotConnectedError: The toy is not connected. Nothing was sent, and the pattern keeps playing.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting intensity2 of {toy_id} to {intensity}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            level = max(0, min(intensity, toy.max_intensity))
            result = await self._run_toy_command(
                toy, "intensity2", toy.intensity2, level
            )
        await self._fire_callback(self._on_toy_state_change, toy.get_state())
        return result

    async def toggle_pause(self, toy_id: str) -> None:
        """
        Toggle the pause state.

        Pausing freezes the pattern timer and sets both intensities to zero.
        Resuming continues playback from the elapsed time at the point of pausing.
        Pausing a blocked toy clears its blocked state (A toy cannot be both paused and blocked simultaneously).
        Manual intensity commands can, even when the toy is paused, still set its intensity.

        Args:
            toy_id: Identifier of the toy to toggle the pause state of.

        Raises:
            ToyNotConnectedError: The toy is not connected, so nothing was sent. The new pause state is recorded all the same.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Toggling pause of {toy_id}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # The toggle is resolved to a target state once, and only the stop is retried: a retry cannot flip it back.
            if toy.apply_paused(not toy.is_paused):
                await self._run_toy_command(toy, "toggle_pause", toy.stop_output)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def toggle_block(self, toy_id: str) -> None:
        """
        Toggle the block state.

        When blocked, all intensity commands are rejected, toy intensities are forced to zero,
        any set pattern continues advancing but doesn't control the toy. Unblocking restores normal operation.
        Blocking a paused toy clears its pause state (a toy cannot be both paused and blocked simultaneously).

        Args:
            toy_id: Identifier of the toy to toggle the block state of.

        Raises:
            ToyNotConnectedError: The toy is not connected, so nothing was sent. The new block state is recorded all the same.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Toggling block of {toy_id}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # Same as toggle_pause: resolve the toggle once so the retry cannot undo it.
            if toy.apply_blocked(not toy.is_blocked):
                await self._run_toy_command(toy, "toggle_block", toy.stop_output)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def set_paused(self, toy_id: str, pause: bool) -> None:
        """
        Set the pause state.

        Pausing freezes the pattern timer and sets both intensities to zero.
        Resuming continues playback from the elapsed time at the point of pausing.
        Pausing a blocked toy clears its blocked state (A toy cannot be both paused and blocked simultaneously).
        Manual intensity commands can, even when the toy is paused, still set its intensity.

        Args:
            toy_id: Identifier of the toy to set the paused state for.
            pause: If true, the toy will be paused, else unpaused.

        Raises:
            ToyNotConnectedError: The toy is not connected, so nothing was sent. The new pause state is recorded all the same.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting pause of {toy_id} to {pause}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # Check and act inside the same lock section, making this atomic wrt. other commands on this toy (e.g., a concurrent set_paused from a second client).
            if toy.is_paused == pause:
                return
            if toy.apply_paused(pause):
                await self._run_toy_command(toy, "set_paused", toy.stop_output)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def set_blocked(self, toy_id: str, block: bool) -> None:
        """
        Set the block state.

        When blocked, all intensity commands are rejected, toy intensities are forced to zero, any set pattern continues
        advancing but doesn't control the toy. Unblocking restores normal operation.
        Blocking a paused toy clears its pause state (a toy cannot be both paused and blocked simultaneously).

        Args:
            toy_id: Identifier of the toy to set the block state for
            block: If true, the toy will be blocked, else unblocked.

        Raises:
            ToyNotConnectedError: The toy is not connected, so nothing was sent. The new block state is recorded all the same.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting block of {toy_id} to {block}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # Same check-then-act guard as set_paused above.
            if toy.is_blocked == block:
                return
            if toy.apply_blocked(block):
                await self._run_toy_command(toy, "set_blocked", toy.stop_output)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def set_safety_hold(self, held: bool) -> list[str]:
        """
        Put every toy under the safety hold, or take it off.

        The hold is independent of block and pause, and leaves both (and patterns and limits) untouched. While it is
        on, every toy, including one added later, is kept at zero: manual intensity commands are rejected, direct
        commands raise :class:`SafetyHoldError`, and patterns keep advancing without driving the toy. Taking it off
        lets every toy follow its own state again, so nothing has to be restored.

        All flags change at once (without an await in between), so no command or playback tick sees a half-applied
        hold. Putting the hold on then stops every toy, concurrently.

        Args:
            held: True to hold every toy, False to release them.

        Returns:
            Ids of the toys that could not be stopped when the hold was put on, including toys that were not connected
            (e.g., reconnecting). They are held regardless (nothing drives them once they are reachable again), but may
            still be running. Always empty when releasing.
        """
        self._log.info(f"Setting safety hold to {held}")
        self._held = held
        for toy in self._toys.values():
            toy.set_held(held)
        toy_ids = list(self._toys.keys())

        async def apply(toy_id: str) -> bool:
            """Stop one toy if the hold went on, and report its new state. False if the stop failed."""
            try:
                toy, cmd_lock = self._get_toy_cmd(toy_id)
            except UnknownToyError:
                return True  # removed in the meantime
            stopped = True
            if held:
                try:
                    async with cmd_lock:
                        await self._run_toy_command(toy, "stop", toy.stop_output)
                except ToyConnectionError:
                    self._log.warning(f"Could not stop {toy_id} for the safety hold.")
                    stopped = False
            await self._fire_callback(self._on_toy_state_change, toy.get_state())
            return stopped

        results = await asyncio.gather(*(apply(toy_id) for toy_id in toy_ids))
        return [toy_id for toy_id, ok in zip(toy_ids, results) if not ok]

    async def set_intensity1_limit(self, toy_id: str, level: int | None) -> None:
        """
        Set the upper limit for the primary intensity of a toy. All intensity1 commands and pattern values are clamped to it.

        A toy already running above the new limit is brought down to it immediately, so the command is sent under the
        per-toy command lock like any other toy-facing command.

        Args:
            toy_id: Identifier of the toy to set the intensity1 limit for.
            level: Maximum allowed intensity1 value (0 – max_intensity). Clamped to max_intensity. None is equal to max_intensity.

        Raises:
            UnknownToyError: The toy was not added before.
            ToyNotConnectedError: The limit was recorded, but the toy runs above it and is not connected, so it could
                not be brought down. The reconnect stops it before it is used again.
            ToyConnectionError: The limit was recorded, but the toy could not be brought down to it. Reconnecting is
                attempted automatically; the limit stays in force for every later command and playback tick.
        """
        self._log.info(f"Setting intensity1 limit of {toy_id} to {level}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        try:
            async with cmd_lock:
                if toy.apply_intensity1_limit(level):
                    await self._run_toy_command(
                        toy, "set_intensity1_limit", toy.enforce_intensity1_limit
                    )
        finally:
            # Report the new ceiling even when enforcing it failed: it *is* in force from here on.
            await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def set_intensity2_limit(self, toy_id: str, level: int | None) -> None:
        """
        Set the upper limit for the secondary intensity of a toy. All intensity2 commands and pattern values are clamped to it.

        Behaves like :meth:`set_intensity1_limit`, including bringing an already-running toy down to the new limit.

        Args:
            toy_id: Identifier of the toy to set the intensity2 limit for.
            level: Maximum allowed intensity2 value (0 – max_intensity). Clamped to max_intensity. None is equal to max_intensity.

        Raises:
            UnknownToyError: The toy was not added before.
            ToyNotConnectedError: The limit was recorded, but the toy runs above it and is not connected, so it could
                not be brought down. The reconnect stops it before it is used again.
            ToyConnectionError: The limit was recorded, but the toy could not be brought down to it. Reconnecting is
                attempted automatically; the limit stays in force for every later command and playback tick.
        """
        self._log.info(f"Setting intensity2 limit of {toy_id} to {level}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        try:
            async with cmd_lock:
                if toy.apply_intensity2_limit(level):
                    await self._run_toy_command(
                        toy, "set_intensity2_limit", toy.enforce_intensity2_limit
                    )
        finally:
            # Report the new ceiling even when enforcing it failed: it *is* in force from here on.
            await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def set_pattern(
        self,
        toy_id: str,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> None:
        """
        Set a time‑based pattern for automatic toy control.

        Patterns are lists of segments, each a tuple of `(duration_ms, intensity1, intensity2)`.

        - `duration_ms`: How long this segment lasts (milliseconds).
        - `intensity1`: Primary capability intensity (0‑max_intensity).
        - `intensity2`: Secondary capability intensity (0‑max_intensity).

        An empty list clears the pattern and stops the toy.

        Args:
            toy_id: Unique ídentifier of the toy for which to set the pattern
            pattern: List of pattern segments.
            wraparound: If True, the pattern loops indefinitely, else it stops after one playthrough.
            reset_time: If True, restart the pattern from the beginning, if False, start from the current elapsed time.

        Raises:
            ToyNotConnectedError: The pattern was cleared, but the toy is not connected, so it could not be stopped. The
                pattern is stored all the same.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added.

        Note:
            Any manual intensity command will automatically pause pattern playback. Use `toggle_pause()` or `set_paused()` to resume.
        """
        self._log.info(f"Setting pattern of {toy_id} to {pattern}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            if toy.apply_pattern(pattern, wraparound, reset_time):
                await self._run_toy_command(toy, "set_pattern", toy.stop_output)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    def apply_state(self, toy_id: str, transition: Callable[[_ToyController], T]) -> T:
        """
        Apply a state transition to a toy right away, without talking to the toy, and return its result.

        The transition is one of the controller's ``apply_*`` methods (or ``accept_manual_intensity``), which report
        whether a command has to follow; send that with :meth:`send`. Unlike the command methods above, this does not
        wait for a command already in flight on the toy, so the new state is visible as soon as this returns, and works
        whatever the toy's connection status. Synchronous, so it can also be used from a callback running on the event
        loop. It must be called on the event loop's thread.

        Args:
            toy_id: Identifier of the toy.
            transition: Called with the toy's controller. Must not do any I/O.

        Raises:
            UnknownToyError: The toy was not added before.

        Returns:
            Whatever *transition* returns.
        """
        toy = self._get_toy(toy_id)
        result = transition(toy)
        self._schedule_on_loop(
            self._fire_callback(self._on_toy_state_change, toy.get_state())
        )
        return result

    def get_controller(self, toy_id: str) -> _ToyController | None:
        """
        The toy's controller, or None if the toy is not added.

        For reading a toy's state only. Change it only on the event loop, through :meth:`apply_state` or the commands.
        """
        return self._toys.get(toy_id)

    def is_connected(self, toy_id: str) -> bool:
        """Whether the toy is added and its connection status is CONNECTED. A plain read of in-memory state."""
        return (
            toy_id in self._toys and self._toy_status.get(toy_id) == ToyStatus.CONNECTED
        )

    async def send(
        self,
        toy_id: str,
        command_name: str,
        command: Callable[[_ToyController], Awaitable[T]],
    ) -> T:
        """
        Send a command to a toy the way every command method does.

        That is: under the toy's command lock (so in order with every other command on it), refused while the toy is
        not connected, retried once after a ConnectionError, and followed by a reconnect if it fails again. Meant for
        the command that has to follow a transition from :meth:`apply_state`.

        Args:
            toy_id: Identifier of the toy.
            command_name: Name of the command, for errors and logs.
            command: Called with the toy's controller, returns the awaitable that sends (e.g., ``stop_output()``).

        Raises:
            UnknownToyError: The toy was not added before.
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The command failed after retry. Reconnecting is attempted automatically.

        Returns:
            Whatever the command returns.
        """
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            result = await self._run_toy_command(toy, command_name, command, toy)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())
        return result

    async def fetch_battery(self, toy_id: str) -> int | None:
        """
        Ask the toy for its battery level now, instead of waiting for the next poll (see :meth:`get_battery`).

        The level is remembered, and ``on_battery_change`` fires if it changed.

        Args:
            toy_id: Identifier of the toy.

        Raises:
            UnknownToyError: The toy was not added before.
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: The query failed after retry. Reconnecting is attempted automatically.

        Returns:
            battery level (0-100) or None if the toy has no battery.
        """
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
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
        """
        Retrieve the BRANDS constant mapping brands (keys) to supported model_names (values) e.g. {"Lovense": ["Gush", "Solace"]}.

        Returns:
            A dictionary mapping brands (keys) to lists of supported model_names (values).
        """
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
        Returns the current state of a toy in-memory, no BLE communication).

        State information contains:

        -  `toy_id` (str) Unique identifier of the toy
        -  `current_intensity` (list[int, int]) Current intensity values. The second value is always zero if the toy only has one intensity.
        -  `intensity_limits` (list[int, int]) Current intensity limits. All intensity commands are clamped to these values.
        -  `is_blocked` (bool) Whether the toy is currently blocked (toy's intensities are forced to zero)
        -  `is_held` (bool) Whether the toy is under the safety hold (toy's intensities are forced to zero)
        -  `pattern_version` (int) Each time the pattern state changes, the version number is incremented
        -  `pattern` (list[tuple[int, int, int]]) List of tuples (duration, intensity1, intensity2) defining the pattern segment
        -  `wraparound` (bool)  Whether the pattern repeats from the beginning after completing the last segment. If False, both Intensities are 0 after the last segment
        -  `is_paused` (bool) Whether the toy is currently paused (patterns do not advance)
        -  `elapsed` (float) Time elapsed since the start of the pattern or last wraparound in ms

        Args:
            toy_id: Identifier of the toy, that you want to get the state of.

        Raises:
            UnknownToyError: The toy was not added before.

        Returns:
            dict with keys as described above
        """
        toy = self._get_toy(toy_id)
        return toy.get_state()

    async def get_battery(self, toy_id: str) -> int | None:
        """
        Get the current battery level of the toy (from memory, automatically updated by _ToyHub).

        Args:
            toy_id: Identifier of the toy to get the battery level of.

        Raises:
            UnknownToyError: The toy was not added before.

        Returns:
            battery level (0-100) or None if the toy has no battery.
        """
        toy = self._get_toy(toy_id)
        return toy.battery

    async def get_info(self, toy_id: str, full: bool) -> dict[str, Any]:
        """
        Gather information about the toy.

        Info gathered:

        -  `toy_id` (str) unique identifier of the toy, e.g., Bluetooth address
        -  `name` (str) human-readable identifier of the toy, e.g., Bluetooth advertisement name
        -  `model_name` (str) model name of the toy. Typically, not retrieved from the toy itself but set by you when adding the toy. This returns this set name.
        -  `brand` (str) brand of the toy, e.g., Lovense
        -  `intensity_names` (list of str). Two human-readable strings. The second string is empty if the toy only has one intensity.
        -  `supports_rotation` (bool) whether the toy supports changing the rotation direction
        -  `max_intensity` (int) maximum intensity value
        -  `recommended_min_interval` (int) The recommended minimum interval between intensity commands (in ms). Especially useful for pattern playback.

        Args:
            toy_id: Unique identifier of the toy that you want to gather info about.
            full: If True, returns all available info (making requests to the toy). Otherwise, returns only the "cheap" info described above.
            Cheap in the sense that the info is retrieved solely from the software representation.

        Raises:
            ToyNotConnectedError: The toy is not connected (only with full=True). Nothing was sent.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            dict: dictionary containing the gathered info. Empty dict if the command could not be delivered.
        """
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        if not full:
            return await toy.get_info(full=False)
        async with cmd_lock:
            return await self._run_toy_command(toy, "get_info", toy.get_info, True)

    async def get_all(self, toy_id: str, full: bool) -> dict[str, Any]:
        """
        Combines get_info, get_state, get_status, get_battery.

        The returned dict contains all keys from all above methods.
        If full is True, it will return additional brand-dependent information. See self.get_info

        Raises:
            ToyNotConnectedError: The toy is not connected (only with full=True). Nothing was sent.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            dict: Merged info and state. Empty dict if the retrieval fails.
        """
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        status = self._toy_status[toy_id]
        info = await toy.get_info(full=False)
        if full:
            async with cmd_lock:
                info = await self._run_toy_command(toy, "get_all", toy.get_info, True)
        state = toy.get_state()
        merged = {**state, **info, "connection_status": status, "battery": toy.battery}
        return merged

    async def get_status(self, toy_id: str) -> str:
        """
        Returns the current connection status of the toy.

        Args:
            toy_id: Unique identifier of the toy that you want to the connection status of.

        Raises:
            UnknownToyError: The toy was not added before.

        Returns:
            Any of "connected", "reconnecting", "lost", "powered_off"
        """
        try:
            return self._toy_status[toy_id]
        except KeyError:
            raise UnknownToyError(toy_id)

    async def direct_command(self, toy_id: str, command: str) -> str:
        """
        Send a raw command directly to the toy. Allows accessing functionalities that are not exposed by the _ToyHub API.

        Args:
            toy_id: Unique identifier of the toy that you want to send the command to.
            command: Command to send to the toy.

        Raises:
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
            SafetyHoldError: The toy is under the safety hold (see :meth:`set_safety_hold`).

        Returns:
            toy response string. Empty string if the command could not be delivered.

        Note:
            Do not use this method to change any tracked state (intensity1, intensity2, etc.) as this method bypasses the _ToyHub's state tracking.
        """
        self._log.info(f"Sending direct command to {toy_id}: {command}")
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # A raw command can drive the toy, and the hub cannot tell which ones would, so none pass the hold.
            if toy.is_held:
                raise SafetyHoldError(toy_id)
            return await self._run_toy_command(
                toy, "direct_command", toy.direct_command, command
            )

    async def change_rotation_direction(self, toy_id: str) -> bool:
        """
        Switch the rotation direction of the toy (if supported)

        Args:
            toy_id: Unique identifier of the toy that you want to change the rotation direction.

        Raises:
            ToyNotConnectedError: The toy is not connected. Nothing was sent.
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            True if the toy supports rotation, False otherwise. Also returns False if the toy currently suffers a connection loss.

        Note:
            The toy does not provide a way to find out its current rotation direction.
            -> This does not modify the internal state, because I can't get any initial state.
        """
        toy, cmd_lock = self._get_toy_cmd(toy_id)
        async with cmd_lock:
            return await self._run_toy_command(
                toy, "change_rotation_direction", toy.change_rotation_direction
            )
