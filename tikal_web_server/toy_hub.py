"""
Private

Contains all the ToyManagement logic needed by ToyServer so ToyServer can focus on defining the public API alone.
Comparable to ToyHub of the tikal library but is mostly async instead of sync. Makes use of private ToyControllers, which
unlike the tikal ToyHub are not exposed to users -> all logic routed through ToyHub instead.
"""

import asyncio
import copy
import logging
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from bleak import BleakClient, BleakScanner

from tikal import BRANDS
from tikal import BadModelError as LowLevelBadModelError
from tikal import BLEConnectionBuilder
from tikal import InvalidModelError as LowLevelInvalidModelError
from tikal import Lovense, ToyCache, ToyData
from tikal.mock import MockBleakClient, MockBleakScanner
from tikal_web_server.toy_controller import LovenseController, ToyController

_RETRY_DELAY = 0.05  # seconds,
_PROCESS_INTERVAL = 0.05  # seconds
_BATTERY_UPDATE_INTERVAL = 120.0  # seconds

T = TypeVar("T")


from enum import StrEnum


class ToyStatus(StrEnum):
    """Represents the connection status of a toy managed by ToyHub."""

    # reconnection succeeded. Always preceded by a toy_state_change event (so clients stay synchronized with the ToyHub state)
    CONNECTED = "connected"
    # on_disconnect fired, command failed. Reconnection is automatically attempted
    RECONNECTING = "reconnecting"
    # reconnect failed, toy will be removed automatically
    LOST = "lost"
    # toy powered off, toy will be removed automatically
    POWERED_OFF = "powered_off"


class UndiscoveredToyError(ValueError):
    """Raised when trying to add a toy that has never been discovered."""

    def __init__(self, toy_id: str, model_name: str):
        self.toy_id = toy_id
        self.model_name = model_name


class UnknownToyError(ValueError):
    """Raised when trying to interact with a toy that is not known to ToyHub."""

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


async def _retry(fn, *args):
    """Call ``await fn(*args)``. On a *first* ConnectionError, wait _RETRY_DELAY seconds and try once more. A second ConnectionError propagates to the caller."""
    try:
        return await fn(*args)
    except ConnectionError:
        await asyncio.sleep(_RETRY_DELAY)
        return await fn(*args)


class ToyHub:

    def __init__(
        self,
        on_status_change: Callable[[str, ToyStatus], Any] | None = None,
        on_toy_ids_change: Callable[[list[str]], Any] | None = None,
        on_toy_state_change: Callable[[dict], Any] | None = None,
        on_model_change: Callable[[dict], Any] | None = None,
        on_battery_change: Callable[[dict[str, int | None]], Any] | None = None,
        toy_cache_path: Path = Path(),
        default_model: str = "",
        log_name: str = "tikal.ws",
        mock_toys: bool = False,
    ) -> None:
        """
        Manager for all toys.

        Handles discovery, connection, state tracking, pattern playback, and automatic reconnection.
        All methods that modify a toy state are thread‑safe and properly serialized per toy.

        Args:
            on_status_change: Called with toy_id, status whenever the status of a toy changes.
            on_toy_ids_change: Called with a full list of all managed toy_ids whenever the set of managed toys changes.
            on_toy_state_change: Called with toy_state whenever the state of a toy changes.
            on_model_change: Called with a dict containing toy_id, model_name whenever the model of a toy changes.
            on_battery_change: Called with a dict of toy_id : battery_level pairs of all toys whose battery level changed..
            toy_cache_path: If not empty, writes / reads toy_id -> model_name mappings to this file. This allows the library to fill out the model_name of discovered toys.
            default_model: This model_name will be used for toys that do not have a model_name in the toy_cache.
            log_name: Name of the logger used for logging.
            mock_toys: If true, uses MockBleakScanner and MockBleakClient instead of BleakScanner and BleakClient (For testing).
        """
        self._log = logging.getLogger(log_name)
        self._log.info(
            f"Initializing ToyHub with toy_cache_path={toy_cache_path} and mock_toys={mock_toys}"
        )

        # Toy state
        self._toy_cache = ToyCache(toy_cache_path, default_model, log_name)
        self._toys: dict[str, ToyController] = {}
        self._toy_lock = asyncio.Lock()
        # Per-toy command lock: held for the full check-command-callback sequence of every mutating operation on a single toy.
        # This prevents races (e.g., two clients racing on set_paused) and serializes concurrent BLE commands to the same toy.
        self._toy_cmd_locks: dict[str, asyncio.Lock] = {}
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

        # Discovery lifecycle
        self._shutting_down: bool = False
        self._discovery_retry_task: asyncio.Task | None = None

        # Toy builder
        if mock_toys:
            scanner = MockBleakScanner
            client = MockBleakClient
        else:
            scanner = BleakScanner
            client = BleakClient

        def on_disconnect_helper(toy_id: str):
            asyncio.create_task(self._on_disconnect(toy_id))

        self._ble_builder = BLEConnectionBuilder(
            on_disconnect_helper, self._on_power_off, log_name, scanner, client
        )

        # Background loop tasks
        self._process_task: asyncio.Task | None = None
        self._battery_poll_task: asyncio.Task | None = None

        # at most one reconnect task per toy. Keyed by toy_id.
        self._reconnect_tasks: dict[str, asyncio.Task] = {}

    # -------------------------------------------------------------------------
    # Startup / Shutdown
    # -------------------------------------------------------------------------

    async def startup(self) -> None:
        """Start the background processing and battery polling loops. Call before using ToyHub. Idempotent."""
        self._log.info("Starting ToyHub.")
        self._shutting_down = False
        if self._process_task is None or self._process_task.done():
            self._process_task = asyncio.get_running_loop().create_task(
                self._process_loop(), name="toy-process-loop"
            )
        if self._battery_poll_task is None or self._battery_poll_task.done():
            self._battery_poll_task = asyncio.get_running_loop().create_task(
                self._battery_poll_loop(), name="toy-battery-poll"
            )

    async def shutdown(self) -> None:
        """Stop the background processing and battery polling loops. Disconnects all toys. Call when finished using ToyHub. Idempotent."""
        self._log.info("Shutting down ToyHub.")
        self._shutting_down = True

        # Idempotent. Safe to call even when no scan is running
        await self._ble_builder.stop_continuous()

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

        async with self._toy_lock:
            snapshot = list(self._toys.values())
            self._toys.clear()
            self._toy_cmd_locks.clear()
            self._pending_toy_ids.clear()

        await asyncio.gather(
            *[toy.disconnect() for toy in snapshot],
            return_exceptions=True,
        )

    async def start_scan(self, callback: Callable[[Exception | list[dict]], Any]):
        """
        Start continuous background discovery of Toys.

        Args:
            callback:   Called with a snapshot of all discovered Toys whenever the available toys change.
                        Called with an exception if the continuous scan encounters an error.

        Raises:
            DiscoveryStartError: The scan could not be started (e.g., Bluetooth not available)
            RuntimeError: Could not get the asyncio event loop. Developer Error
        """
        self._log.info("Starting toy discovery")
        loop = asyncio.get_running_loop()

        def on_discovery(update: Exception | list[ToyData]):
            # Dispatch the async update onto the event loop.
            # Using call_soon_threadsafe makes this safe even if Bleak calls this callback from a worker thread.
            loop.call_soon_threadsafe(
                lambda: loop.create_task(self._apply_discovery(update, callback))
            )

        try:
            await self._ble_builder.start_continuous(on_discovery)
        except Exception as e:
            raise DiscoveryStartError(traceback.format_exc()) from e

    async def stop_scan(self):
        """Stop continuous background discovery of Toys. After this call, the callback provided to `start_scan` will no longer be invoked."""
        self._log.info("Stopping toy discovery")
        async with self._toy_lock:
            self._toy_data.clear()
        await self._ble_builder.stop_continuous()

    # -------------------------------------------------------------------------
    # Private
    # -------------------------------------------------------------------------

    async def _apply_discovery(
        self,
        update: Exception | list[ToyData],
        callback: Callable[[Exception | list[dict]], Any],
    ) -> None:
        """
        Updates the discovery state under self._toy_lock, serializes ToyData to dicts, and delivers them to callback
        Args:
            update: Either an exception indicating an error, or a list of discovered ToyData objects.
            callback: The user callback that will receive a list of serialized ToyData dicts.
        """
        async with self._toy_lock:
            if isinstance(update, Exception):
                self._toy_data.clear()
                result = DiscoveryError(str(traceback.format_exception(update)))
            else:
                self._toy_data = {toy.toy_id: toy for toy in update}
                self._all_seen_toy_ids.update(data.toy_id for data in update)
                result = [
                    dict(
                        toy_id=data.toy_id,
                        name=data.name,
                        brand=data.brand,
                        model_name=self._toy_cache.get_model_name(data.name),
                    )
                    for data in update
                ]
        self._log.debug("Discovery update: %s", result)
        await self._fire_callback(callback, result)

    async def _set_toy_status(self, toy_id: str, new_status: ToyStatus) -> None:
        """
        Update the status of a toy and invoke the on_status_change callback if changed.

        Args:
            toy_id: Identifier of the toy.
            new_status: New status to set.
        """
        self._log.debug("Setting status of %s to %s", toy_id, new_status.value)
        async with self._toy_lock:
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
        async with self._toy_lock:
            toy = self._toys.get(toy_id)
        if toy is None:
            return
        self._ensure_reconnect_task(toy)
        await self._set_toy_status(toy_id, ToyStatus.RECONNECTING)

    async def _on_power_off(self, toy_id: str) -> None:
        """
        Handle a toy power‑off event.

        Sets ToyStatus to POWERED_OFF and removes the Toy from ToyHub.

        Args:
            toy_id: Identifier of the toy that powered off.
        """
        async with self._toy_lock:
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
        Runs every `_PROCESS_INTERVAL` seconds. Only toys with status CONNECTED are processed.
        """
        while True:
            await asyncio.sleep(_PROCESS_INTERVAL)
            async with self._toy_lock:
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

    async def _process_one_locked(
        self, toy: ToyController, cmd_lock: asyncio.Lock
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

    async def _process_one(self, toy: ToyController) -> None:
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

    async def _handle_command_failure(self, toy: ToyController) -> None:
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

    def _ensure_reconnect_task(self, toy: ToyController) -> None:
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

    def _prune_reconnect_task(self, toy_id: str, task: asyncio.Task) -> None:
        """
        Remove a completed reconnect task from the tracking dictionary if it is still the current entry. Call after completing a reconnect task.

        Args:
            toy_id: Identifier of the toy.
            task: The task that completed.
        """
        if self._reconnect_tasks.get(toy_id) is task:
            self._reconnect_tasks.pop(toy_id, None)

    async def _reconnect_toy(self, toy: ToyController) -> None:
        """
        Attempt to reconnect a lost toy. If successful, sets its ToyStatus to CONNECTED; else sets it to LOST and removes the toy.

        Args:
            toy: The toy controller to reconnect.
        """
        # Give some time for the connection to fix itself
        await asyncio.sleep(1.0)
        if toy.is_connected:
            await self._fire_callback(self._on_toy_state_change, toy.get_state())
            await self._set_toy_status(toy.toy_id, ToyStatus.CONNECTED)
            return

        # Try reconnecting:
        try:
            await toy.reconnect()
            await toy.stop()  # Always stop the toy after connection loss
            # Unstable connection if stop fails. Treat as lost.
            await self._fire_callback(self._on_toy_state_change, toy.get_state())
            await self._set_toy_status(toy.toy_id, ToyStatus.CONNECTED)
            return
        except Exception as exc:
            self._log.warning(
                "Reconnect failed for toy %s: %s with details: %s",
                toy.toy_id,
                exc,
                traceback.format_exc(),
            )
            await self._set_toy_status(toy.toy_id, ToyStatus.LOST)
            try:
                await self.remove(toy.toy_id, False)
            except Exception:
                pass

    async def _get_toy(self, toy_id: str) -> ToyController:
        """
        Retrieve a toy controller by its ID.

        Args:
            toy_id: Identifier of the toy.

        Raises:
            UnknownToyError: The toy has not been added.

        Returns:
            The ToyController instance.
        """
        async with self._toy_lock:
            toy = self._toys.get(toy_id)
            if toy is None:
                raise UnknownToyError(toy_id)
        return toy

    async def _get_toy_cmd(self, toy_id: str) -> tuple[ToyController, asyncio.Lock]:
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
        async with self._toy_lock:
            toy = self._toys.get(toy_id)
            if toy is None:
                raise UnknownToyError(toy_id)
            return toy, self._toy_cmd_locks[toy_id]

    async def _fire_callback(self, callback: Callable, payload) -> None:
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
        toy: ToyController,
        command_name: str,
        command: Callable[..., Awaitable[T]],
        *args,
    ) -> T:
        """
        Execute a toy command with one automatic retry on ConnectionError. If the command fails after retry, triggers reconnection and raises ToyConnectionError.

        Args:
            toy: The toy controller.
            command_name: Name of the command to execute.
            command: Async callable to execute.
            *args: Arguments to pass to the command.

        Raises:
            ToyConnectionError: The command failed after retry.

        Returns:
            The command's result
        """
        try:
            return await _retry(command, *args)
        except Exception as e:
            await self._handle_command_failure(toy)
            raise ToyConnectionError(toy.toy_id, toy.model_name, command_name) from e

    async def _battery_poll_loop(self) -> None:
        """
        Background loop that periodically polls battery levels of all connected toys.
        Runs every `_BATTERY_UPDATE_INTERVAL` seconds and fires the on_battery_change callback when any battery level changes.
        """
        while not self._shutting_down:
            try:
                await asyncio.sleep(_BATTERY_UPDATE_INTERVAL)
                if self._shutting_down:
                    break
                await self._poll_all_batteries()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.exception("Unexpected error in battery poll loop: %s", e)

    async def _poll_all_batteries(self) -> None:
        """
        Poll battery levels for all toys with status CONNECTED.
        Collects changes and invokes the on_battery_change callback with a dict of toy_id -> new_battery_level (or None if no battery).
        """
        # Snapshot of connected toys under lock
        async with self._toy_lock:
            toys_to_poll = [
                (toy_id, toy, self._toy_cmd_locks[toy_id])
                for toy_id, toy in self._toys.items()
                if self._toy_status.get(toy_id) == ToyStatus.CONNECTED
            ]
        if not toys_to_poll:
            return

        updates: dict[str, int | None] = {}

        # Poll each toy
        async def poll_one(toy_id: str, toy: ToyController, lock: asyncio.Lock) -> None:
            async with lock:
                old_battery = toy.battery
                new_battery = await toy.fetch_and_update_battery()
                if new_battery is not None and old_battery != new_battery:
                    updates[toy_id] = new_battery

        await asyncio.gather(*(poll_one(tid, t, lk) for tid, t, lk in toys_to_poll))

        if updates:
            await self._fire_callback(self._on_battery_change, updates)

    async def _create_toy(
        self, builder: BLEConnectionBuilder, toy_data: ToyData
    ) -> ToyController:
        """
        Adapter that converts BLEConnectionBuilder's result‑type errors to raised exceptions.

        Args:
            builder: The BLE connection builder.
            toy_data: Data describing the toy to create.

        Returns:
            A high-level ToyController instance.

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
        if isinstance(result, Lovense):
            try:
                # Unstable connection if the first command already fails. Treat as unable to connect.
                battery = await result.strict_get_battery_level()
            except Exception as e:
                raise AddConnectionError(toy_data.toy_id, toy_data.model_name) from e
            return LovenseController(result, battery)
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
        Adds a new toy to the system.

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
        async with self._toy_lock:
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
            self._pending_toy_ids.add(toy_id)

        try:
            toy = await self._create_toy(self._ble_builder, toy_data)
        except Exception as e:
            async with self._toy_lock:
                self._pending_toy_ids.discard(toy_id)
            raise e

        async with self._toy_lock:
            self._pending_toy_ids.discard(toy_id)
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
        async with self._toy_lock:
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
        """
        self._log.info(f"Setting model of {toy_id} to {model_name})")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            try:
                await toy.set_model_name(model_name)
            except LowLevelInvalidModelError as e:
                raise InvalidModelError(toy_id, model_name, toy.brand) from e
            except LowLevelBadModelError as e:
                raise BadModelError(toy_id, model_name) from e
            self._toy_cache.update({toy.name: model_name})
        change = dict(toy_id=toy_id, model_name=model_name)
        await self._fire_callback(self._on_model_change, change)

    async def stop(self, toy_id: str) -> None:
        """
        Stop all toy actions (set all intensities to zero). If a pattern is active and not paused, this method pauses the pattern.

        Args:
            toy_id: Identifier of the toy to stop.

        Raises:
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Stopping toy at {toy_id}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            await self._run_toy_command(toy, "stop", toy.stop)
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

    async def intensity1(self, toy_id: str, intensity: int) -> bool:
        """
        Set the intensity of the primary capability.

        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts.
        Will do nothing if the toy is blocked.

        Args:
            toy_id: Identifier of the toy on which to set the intensity.
            intensity: Intensity level. The valid range depends on the toy type. Values outside the range are clamped.

        Raises:
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting intensity1 of {toy_id} to {intensity}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
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
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting intensity2 of {toy_id} to {intensity}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
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
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Toggling pause of {toy_id}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            await self._run_toy_command(toy, "toggle_pause", toy.toggle_pause)
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
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Toggling block of {toy_id}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            await self._run_toy_command(toy, "toggle_block", toy.toggle_block)
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
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting pause of {toy_id} to {pause}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # Check and act inside the same lock section, making this atomic wrt. other commands on this toy (e.g., a concurrent set_paused from a second client).
            if toy.is_paused == pause:
                return
            await self._run_toy_command(toy, "set_paused", toy.toggle_pause)
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
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.
        """
        self._log.info(f"Setting block of {toy_id} to {block}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            # Same check-then-act guard as set_paused above.
            if toy.is_blocked == block:
                return
            await self._run_toy_command(toy, "set_blocked", toy.toggle_block)
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
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added.

        Note:
            Any manual intensity command will automatically pause pattern playback. Use `toggle_pause()` or `set_paused()` to resume.
        """
        self._log.info(f"Setting pattern of {toy_id} to {pattern}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            await self._run_toy_command(
                toy, "set_pattern", toy.set_pattern, pattern, wraparound, reset_time
            )
        await self._fire_callback(self._on_toy_state_change, toy.get_state())

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
        Retrieve a list of all toy_ids currently managed by ToyHub

        Returns:
            A list of toy_ids currently managed by ToyHub. Snapshot, the list will not update automatically.
        """
        async with self._toy_lock:
            return list(self._toys.keys())

    async def get_state(self, toy_id: str) -> dict:
        """
        Returns the current state of a toy in-memory, no BLE communication).

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
            toy_id: Identifier of the toy, that you want to get the state of.

        Raises:
            UnknownToyError: The toy was not added before.

        Returns:
            dict with keys as described above
        """
        toy = await self._get_toy(toy_id)
        return toy.get_state()

    async def get_battery(self, toy_id: str) -> int | None:
        """
        Get the current battery level of the toy (from memory, automatically updated by ToyHub).

        Args:
            toy_id: Identifier of the toy to get the battery level of.

        Raises:
            UnknownToyError: The toy was not added before.

        Returns:
            battery level (0-100) or None if the toy has no battery.
        """
        toy = await self._get_toy(toy_id)
        return toy.battery

    async def get_info(self, toy_id: str, full: bool) -> dict:
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

        Args:
            toy_id: Unique identifier of the toy that you want to gather info about.
            full: If True, returns all available info (making requests to the toy). Otherwise, returns only the "cheap" info described above.
            Cheap in the sense that the info is retrieved solely from the software representation.

        Raises:
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            dict: dictionary containing the gathered info. Empty dict if the command could not be delivered.
        """
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        if not full:
            return await toy.get_info(full=False)
        async with cmd_lock:
            return await self._run_toy_command(toy, "get_info", toy.get_info, True)

    async def get_all(self, toy_id: str, full: bool) -> dict:
        """
        Combines get_info, get_state, get_status, get_battery.

        The returned dict contains all keys from both methods.
        If full is True, it will return additional brand-dependent information. See self.get_info

        Raises:
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            dict: Merged info and state. Empty dict if the retrieval fails.
        """
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
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
        Send a raw command directly to the toy. Allows accessing functionalities that are not exposed by the ToyHub API.

        Args:
            toy_id: Unique identifier of the toy that you want to send the command to.
            command: Command to send to the toy.

        Raises:
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            toy response string. Empty string if the command could not be delivered.

        Note:
            Do not use this method to change any tracked state (intensity1, intensity2, etc.) as this method bypasses the ToyHub's state tracking.
        """
        self._log.info(f"Sending direct command to {toy_id}: {command}")
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            return await self._run_toy_command(
                toy, "direct_command", toy.direct_command, command
            )

    async def change_rotation_direction(self, toy_id: str) -> bool:
        """
        Switch the rotation direction of the toy (if supported)

        Args:
            toy_id: Unique identifier of the toy that you want to change the rotation direction.

        Raises:
            ToyConnectionError: Failed to send the command to the toy due to a connection issue. Reconnecting is attempted automatically.
            UnknownToyError: The toy was not added before.

        Returns:
            True if the toy supports rotation, False otherwise. Also returns False if the toy currently suffers a connection loss.

        Note:
            The toy does not provide a way to find out its current rotation direction.
            -> This does not modify the internal state, because I can't get any initial state.
        """
        toy, cmd_lock = await self._get_toy_cmd(toy_id)
        async with cmd_lock:
            return await self._run_toy_command(
                toy, "change_rotation_direction", toy.change_rotation_direction
            )
