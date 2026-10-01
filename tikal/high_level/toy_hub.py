"""
Part of the High-Level API: Provides connection management for toy devices.

This module provides the ToyHub class, which serves as the entry point for all toy operations. The ToyHub manages:

- **Toy Discovery**: Scanning for available devices via Bluetooth
- **Connection Management**: Establishing and maintaining connections
- **Pattern Playback**: Managing time-based pattern execution
- **Battery Monitoring**: Automatic periodic battery level updates
- **Reconnection**: Automatic recovery from unexpected disconnects

Example:
    ::

        # Basic
        hub = ToyHub()
        toys = hub.discover_toys_blocking(5.0)
        toys[0].model_name = "Lush"
        controllers = hub.connect_toys_blocking(toys)
        controllers[0].intensity1(15)
        hub.shutdown()

        # With callbacks
        def on_error(exc, context, tb):
            print(f"Error {exc} while {context}. Traceback:{tb}")

        def on_battery(levels):
            for toy_id, level in levels.items():
                print(f"Toy {toy_id} has battery ({level}%)")

        hub = ToyHub(
            on_battery_update=on_battery,
            on_error=on_error,
            on_disconnect=lambda tid: print(f"{tid} disconnected"),
            on_reconnection_success=lambda tid: print(f"{tid} reconnected"),
            on_power_off=lambda tid: print(f"{tid} powered off"),
            logger_name="my_app",
            toy_cache_path=Path("./toys.json"),
            default_model="Please select a model"
        )
"""

from __future__ import annotations

import asyncio
import atexit
import traceback
import weakref
from logging import getLogger
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar

from bleak import BleakClient, BleakScanner

from .._core import (
    DiscoveryError,
    ToyConnectionError,
    ToyNotConnectedError,
    ToyStatus,
    UnknownToyError,
    _ToyHub,
)
from .._private import RECONNECT_WINDOW, AsyncRunner
from ..low_level import ToyData
from .toy_controller import ToyController

T = TypeVar("T")

#: How long a caller waits for a state change to be applied on the event loop. It needs no I/O, so this is only reached
#: if the loop is stuck (e.g., a callback that never returns).
_APPLY_TIMEOUT = 5.0  # seconds


class ToyHub:
    """
    Central interface for toy communication and lifecycle management.

    Part of the High-Level API: Handles discovery, connection, battery monitoring, and control of toys.

    Callbacks (the ones given here, and the ones given to :class:`ToyController` commands) run on the hub's background
    thread. From a callback you can use every :class:`ToyController` method and the ``*_callback`` methods of this
    class, but not its blocking methods, which raise ``RuntimeError`` there.

    Args:
        on_battery_update: Callback invoked with the battery levels of all connected toys (dict mapping toy_id to the
            level, or None if the toy has no battery) whenever one of them changes (checked every 2 minutes), and after
            toys were connected or reconnected.
        on_error: Callback invoked when an unexpected error occurs, including an exception raised by one of your
            callbacks and a failing discovery. Receives (exception, context_message, traceback_string). Without it,
            these errors are logged.
        on_disconnect: Callback invoked when a toy's connection is lost unexpectedly, or a command to it failed twice.
            Receives toy_id. ToyHub automatically attempts reconnection, with repeated attempts for up to one minute.
        on_reconnection_failure: Callback invoked when no reconnect attempt succeeded within that minute. Receives
            toy_id. The toy is disconnected; commands sent to it from then on are rejected right away (their callbacks
            receive None).
        on_reconnection_success: Callback invoked when automatic reconnection succeeds. Receives toy_id. The toy is
            stopped and its pattern paused at that point; resume it (e.g., ``set_paused(False)``) if it should continue.
            Commands issued while it was disconnected were rejected right away (their callbacks received None).
        on_power_off: Callback invoked when a toy is powered off via its physical button. Receives toy_id.
        logger_name: Name of the logger to use for logging messages.
        toy_cache_path: Path to a file for caching toy model names. Allows automatic model name assignment on later discoveries.
        default_model: Default model name to use if a toy isn't in the cache.
        bluetooth_scanner: BLE scanner class to use (defaults to BleakScanner). Can be overridden for testing.
        bluetooth_client: BLE client class to use (defaults to BleakClient). Can be overridden for testing.
        mock_toys: If True, also offer the fictional MockEstimToys brand. Not part of the public API. This parameter may be removed without notice.
    """

    def __init__(
        self,
        on_battery_update: Optional[Callable[[dict[str, int | None]], Any]] = None,
        on_error: Optional[Callable[[Exception, str, str], Any]] = None,
        on_disconnect: Optional[Callable[[str], Any]] = None,
        on_reconnection_failure: Optional[Callable[[str], Any]] = None,
        on_reconnection_success: Optional[Callable[[str], Any]] = None,
        on_power_off: Optional[Callable[[str], Any]] = None,
        logger_name: str = "toy",
        toy_cache_path: Path = Path(),
        default_model: str = "",
        bluetooth_scanner: Any = BleakScanner,
        bluetooth_client: Any = BleakClient,
        mock_toys: bool = False,
    ):
        self._battery_update_callback = on_battery_update
        self._error_callback = on_error
        self._disconnect_callback = on_disconnect
        self._reconnection_failure_callback = on_reconnection_failure
        self._reconnection_success_callback = on_reconnection_success
        self._power_off_callback = on_power_off
        self._log = getLogger(logger_name)

        self._runner = AsyncRunner()
        self._core = _ToyHub(
            on_status_change=self._on_status_change,
            on_battery_change=self._on_battery_change,
            toy_cache_path=toy_cache_path,
            default_model=default_model,
            log_name=logger_name,
            mock_toys=mock_toys,
            bluetooth_scanner=bluetooth_scanner,
            bluetooth_client=bluetooth_client,
        )
        self._runner.run_async(self._core.startup())
        # One controller per toy, so every method hands out the same object for a toy.
        self._controllers: dict[str, ToyController] = {}
        self._shut_down = False
        self._atexit_hook: Optional[Callable[[], None]] = self._register_atexit()

    def _register_atexit(self) -> Callable[[], None]:
        """
        Register a last-resort :meth:`shutdown` for interpreter exit and return the hook so it can be unregistered.

        :meth:`shutdown` is documented as mandatory, but nothing forces a caller to reach it: an uncaught exception,
        a ``KeyboardInterrupt``, or simply forgetting would otherwise leave every connected toy running at whatever
        intensity it was last given. The interpreter still runs ``atexit`` hooks in all of those cases, and the
        runner's event-loop thread is still alive at that point, so the toys can still be stopped and disconnected.

        The hook holds only a weak reference, so registering it does not keep the hub alive; a hub that is garbage
        collected simply makes the hook a no-op.

        Returns:
            The registered hook, to be passed to ``atexit.unregister`` by :meth:`shutdown`.
        """
        hub_ref = weakref.ref(self)

        def shutdown_at_exit() -> None:
            hub = hub_ref()
            if hub is None:
                return
            try:
                hub._log.warning(
                    "ToyHub.shutdown() was never called. Stopping and disconnecting all toys at exit."
                )
                hub.shutdown()
            except Exception:  # pragma: no cover
                pass  # Best effort: nothing useful can be raised during interpreter shutdown.

        atexit.register(shutdown_at_exit)
        return shutdown_at_exit

    # ------------------------------------------------------------------------------------------------------------------
    # Callback setters
    # ------------------------------------------------------------------------------------------------------------------

    def battery_update_callback(
        self, callback: Optional[Callable[[dict[str, int | None]], Any]]
    ) -> None:
        """
        Set or update the battery update callback.

        Args:
            callback: New callback function or None to disable.

        Example:
            ::

                def new_battery_handler(levels):
                    print(f"Battery update: {levels}")
                hub.battery_update_callback(new_battery_handler)
        """
        self._battery_update_callback = callback

    def error_callback(
        self, callback: Optional[Callable[[Exception, str, str], Any]]
    ) -> None:
        """
        Set or update the error callback.

        Args:
            callback: New callback function or None to disable.

        Example:
            ::

                def error_handler(exc, context, tb):
                    print(f"Hub error {exc} while {context}. Traceback: {tb}")
                hub.error_callback(error_handler)
        """
        self._error_callback = callback

    def disconnect_callback(self, callback: Optional[Callable[[str], Any]]) -> None:
        """
        Set or update the disconnect callback.

        Args:
            callback: New callback function or None to disable.
        """
        self._disconnect_callback = callback

    def reconnection_failure_callback(
        self, callback: Optional[Callable[[str], Any]]
    ) -> None:
        """
        Set or update the reconnection failure callback.

        Args:
            callback: New callback function or None to disable.
        """
        self._reconnection_failure_callback = callback

    def reconnection_success_callback(
        self, callback: Optional[Callable[[str], Any]]
    ) -> None:
        """
        Set or update the reconnection success callback.

        Args:
            callback: New callback function or None to disable.
        """
        self._reconnection_success_callback = callback

    def power_off_callback(self, callback: Optional[Callable[[str], Any]]) -> None:
        """
        Set or update the power-off callback.

        Args:
            callback: New callback function or None to disable.
        """
        self._power_off_callback = callback

    # ------------------------------------------------------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------------------------------------------------------

    def start_discovery(self, on_update: Callable[[list[ToyData]], Any]) -> None:
        """
        Start scanning for toys continuously, and report the available toys whenever they change.

        Args:
            on_update: Called with a list[ToyData] of ALL available toys whenever availability changes. 'ALL' includes
                toys that were previously discovered and are still available. Connected toys do not advertise and are
                not included. Model names are filled in from the ToyCache. Invoked with an empty list if the scan fails.

        Raises:
            DiscoveryStartError: The scan could not be started (e.g., Bluetooth is off).

        Note:
            A scan that fails after it started is reported to the error callback, and ends.
        """

        def forward(update: Exception | list[ToyData]) -> None:
            if isinstance(update, Exception):
                self._log.error(f"Discovery failed: {update!r}")
                self._report_error(
                    update, "Toy Discovery process failed. Is the Bluetooth still on?"
                )
                update = []  # Clear any now stale toy
            self._run_callback(on_update, update, "discovery update")

        self._runner.run_async(self._core.start_scan(forward))

    def stop_discovery(self) -> None:
        """Stop the scan started by :meth:`start_discovery`. The update callback is not invoked anymore afterward."""
        self._runner.run_async(self._core.stop_scan())

    def discover_toys_blocking(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Discover available toys synchronously (blocking call).

        Scans for nearby toys via Bluetooth and returns their discovery data.
        Model names are automatically filled from the cache if available.

        Args:
            timeout: Maximum scan duration in seconds. Longer timeouts may discover more devices but take longer.

        Returns:
            list[ToyData]: List of discovered toys. model_name set from cache if possible

        Raises:
            TimeoutError: If discovery exceeds timeout * 2. Should not occur with BleakScanner
            Exception: Any exception from the underlying BLE scanner.
            RuntimeError: If a continuous scan is in progress (see :meth:`start_discovery`), or if called from a
                callback.

        Example:
            ::

                toys = hub.discover_toys_blocking(timeout=10.0)

                for toy in toys:
                    print(f"Found: {toy.name}")
                    if toy.model_name:
                        print(f"Cached model: {toy.model_name}")
                    else:
                        print(f"Model unknown. Please set manually")
        """
        self._log.info("Starting toy discovery (blocking)...")
        toys = self._runner.run_async(self._core.discover(timeout), timeout * 2)
        self._log.info(f"Discovered {len(toys)} toy(s)")
        return toys

    def discover_toys_callback(
        self,
        on_discovered: Callable[[list[ToyData] | BaseException], Any],
        timeout: float = 10.0,
    ) -> None:
        """
        Discover available toys with a callback (non-blocking).

        Starts discovery in the background and returns immediately. The callback is invoked when discovery completes.

        Args:
            on_discovered: Callback invoked with either a list of discovered toys or an exception if discovery failed.
            timeout: Maximum scan duration in seconds.

        Example:
            ::

                def handle_discovery(result):
                    if isinstance(result, Exception):
                        print(f"Discovery failed: {result}")
                        return

                    print(f"Found {len(result)} toys")
                    for toy in result:
                        print(toy.name)

                hub.discover_toys_callback(handle_discovery, timeout=5.0)
        """
        self._log.info("Starting toy discovery (callback)...")
        self._submit_with_result(
            lambda: self._core.discover(timeout), on_discovered, timeout * 2
        )

    # ------------------------------------------------------------------------------------------------------------------
    # Connecting and disconnecting
    # ------------------------------------------------------------------------------------------------------------------

    def connect_toys_blocking(
        self, to_connect: list[ToyData], timeout: float = 30.0
    ) -> list[ToyController | BaseException]:
        """
        Connect to specified toys synchronously (blocking call).

        Attempts to connect to each toy in the list concurrently. Toys that connect successfully return ToyController
        instances; failed connections return exceptions. On success, the ToyCache remembers each toy's model name.

        Args:
            to_connect: List of ToyData objects with a valid model_name set. Must have been discovered first.
            timeout: Maximum time to wait for all connections in seconds.

        Returns:
            list[ToyController | BaseException]: Each element is either a connected ToyController or an exception
                (e.g., :class:`InvalidModelError`, :class:`BadModelError`, :class:`AddConnectionError`,
                :class:`ToyAlreadyAddedError`). Order matches the input list.

        Raises:
            TimeoutError: The connections took longer than *timeout*.
            RuntimeError: Called from a callback.

        Example:
            ::

                # Discover and connect
                toys = hub.discover_toys_blocking(5.0)

                # Set model names (required!)
                toys[0].model_name = "Nora"
                toys[1].model_name = "Lush"

                # Connect
                results = hub.connect_toys_blocking(toys, timeout=30.0)

                # Process results
                controllers = []
                for i, result in enumerate(results):
                    if isinstance(result, BaseException):
                        print(f"Failed to connect to {toys[i].name}: {result}")
                    else:
                        print(f"Connected: {result.model_name}")
                        controllers.append(result)
        """
        self._log.info(f"Connecting to {len(to_connect)} toy(s) (blocking)...")
        return self._runner.run_async(self._connect(to_connect), timeout)

    def connect_toys_callback(
        self,
        to_connect: list[ToyData],
        on_connected: Callable[
            [list[ToyController | BaseException] | BaseException], Any
        ],
        timeout: float = 30.0,
    ) -> None:
        """
        Connect to specified toys with a callback (non-blocking).

        Starts connections in the background and returns immediately. The callback is invoked when all connection
        attempts are complete.

        Args:
            to_connect: List of ToyData objects with a valid model_name set.
            on_connected: Callback invoked with a list of controllers or exceptions (order matches the input list), or
                with a TimeoutError if the connections took longer than *timeout*.
            timeout: Maximum time to wait for all connections in seconds.

        Example:
            ::

                def handle_connection(results):
                    for result in results:
                        if isinstance(result, BaseException):
                            print(f"Connection failed: {result}")
                        else:
                            print(f"Connected: {result.model_name}")

                hub.connect_toys_callback(toys, handle_connection, timeout=30.0)
        """
        self._log.info(f"Connecting to {len(to_connect)} toy(s) (callback)...")
        self._submit_with_result(
            lambda: self._connect(to_connect), on_connected, timeout
        )

    def disconnect_toys_blocking(
        self, to_disconnect: list[str], timeout: float = 10.0
    ) -> Sequence[BaseException | None]:
        """
        Disconnect specified toys synchronously (blocking call).

        Cleanly disconnects from the specified toys, stopping all actions and closing BLE connections.

        Args:
            to_disconnect: List of toy_ids (Bluetooth addresses) to disconnect.
            timeout: Maximum time to wait for all disconnections in seconds.

        Returns:
            list[BaseException | None]: List where each element is either None (successful disconnect) or an exception
            (:class:`UnknownToyError` for a toy that is not connected, :class:`ToyConnectionError` for a disconnect that
            failed). Order matches the input list. Toys are still disconnected even if an exception occurs.

        Raises:
            TimeoutError: The disconnections took longer than *timeout*.
            RuntimeError: Called from a callback.

        Example:
            ::

                toy_ids = [controller.toy_id for controller in controllers]
                results = hub.disconnect_toys_blocking(toy_ids, timeout=10.0)

                for toy_id, result in zip(toy_ids, results):
                    if result is None:
                        print(f"{toy_id} disconnected successfully")
                    else:
                        print(f"{toy_id} disconnect failed: {result}")
        """
        self._log.info(f"Disconnecting from {len(to_disconnect)} toy(s) (blocking)...")
        return self._runner.run_async(self._disconnect(to_disconnect), timeout)

    def disconnect_toys_callback(
        self,
        to_disconnect: list[str],
        on_disconnected: Callable[[list[BaseException | None] | BaseException], Any],
        timeout: float = 10.0,
    ) -> None:
        """
        Disconnect specified toys with a callback (non-blocking).

        Starts disconnections in the background and returns immediately.
        The callback is invoked when all disconnection attempts are complete.

        Args:
            to_disconnect: List of toy_ids to disconnect.
            on_disconnected: Callback invoked with a list of exceptions (or None for successful disconnects), see
                :meth:`disconnect_toys_blocking`, or with a TimeoutError if the disconnections took longer than
                *timeout*. Toys are still disconnected even if an exception occurs.
            timeout: Maximum time to wait for all disconnections in seconds.

        Example:
            ::

                def handle_disconnects(results):
                    success_count = sum(1 for r in results if r is None)
                    print(f"{success_count}/{len(results)} disconnected successfully")

                toy_ids = [c.toy_id for c in controllers]
                hub.disconnect_toys_callback(toy_ids, handle_disconnects)
        """
        self._log.info(f"Disconnecting from {len(to_disconnect)} toy(s) (callback)...")
        self._submit_with_result(
            lambda: self._disconnect(to_disconnect), on_disconnected, timeout
        )

    def update_model_name(
        self, toy_id: str, model_name: str
    ) -> ToyController | BaseException:
        """
        Update the model name for a connected toy.

        Changes the toy's model name, which affects which commands are available and how they're interpreted. On
        success, the ToyCache remembers the new model for this toy.

        Args:
            toy_id: Unique identifier of the toy to update.
            model_name: New model name (must be valid for the toy's brand).

        Returns:
            ToyController | BaseException: The updated controller if successful, or:

            - :class:`InvalidModelError`: model_name is not valid for this toy brand.
            - :class:`BadModelError`: the model_name is valid, but commands still fail.
            - :class:`UnknownToyError`: the toy is not connected to this hub.
            - :class:`ToyConnectionError`: the toy could not be reached, so its model was left unchanged.

        Raises:
            RuntimeError: Called from a callback (use :meth:`ToyController.set_model_name` there).

        Example:
            ::

                # Correct a wrong model assignment
                result = hub.update_model_name(toy_id, "Nora")
                if isinstance(result, BaseException):
                    print(f"Update failed: {result}")
                else:
                    print(f"Model updated to {result.model_name}")
        """
        try:
            self._runner.run_async(self._core.set_model(toy_id, model_name))
        except RuntimeError:
            raise
        except Exception as e:
            return e
        self._log.info(f"Updated model name for toy {toy_id} to {model_name}")
        controller = self._controller_for(toy_id)
        if controller is None:  # removed in the meantime
            return UnknownToyError(toy_id)
        return controller

    # ------------------------------------------------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------------------------------------------------

    def shutdown(self) -> None:
        """
        Stop scanning, disconnect all toys, and stop the background thread.

        This method should always be called before the program exits to ensure:

        - All toys are stopped and properly disconnected
        - The background thread is shut down cleanly

        Example:
            ::

                    hub = ToyHub()
                    # ... use hub ...
                    hub.shutdown()
        Note:
            After calling shutdown(), the ToyHub instance should not be reused.
            Create a new instance if you need to start working with toys again.
            The method is idempotent: calling it again after the first shutdown is a no-op.

        Note:
            As a safety net this also runs automatically at interpreter exit if you never call it (including after an
            uncaught exception or a ``KeyboardInterrupt``), so toys are not left running. Rely on it only as a
            backstop: it cannot run if the process is killed outright (``SIGKILL``, ``os._exit``, a power loss).
        """
        if self._shut_down:
            self._log.debug("shutdown() called again; already shut down.")
            return
        self._shut_down = True

        if self._atexit_hook is not None:
            atexit.unregister(self._atexit_hook)
            self._atexit_hook = None

        self._log.info("Shutting down ToyHub...")
        try:
            self._runner.run_async(self._core.shutdown())
        except Exception as e:
            self._log.error(f"Error while shutting down the toys: {e!r}")
        self._runner.shutdown()
        self._log.info("ToyHub shutdown complete")

    # ------------------------------------------------------------------------------------------------------------------
    # Private: coroutines run on the event loop
    # ------------------------------------------------------------------------------------------------------------------

    async def _connect(
        self, to_connect: list[ToyData]
    ) -> list[ToyController | BaseException]:
        """Connect every toy concurrently and hand out a controller for each one that connected."""
        results = await asyncio.gather(
            *(self._core.add_toy_data(data) for data in to_connect),
            return_exceptions=True,
        )
        connected: list[ToyController | BaseException] = []
        for data, result in zip(to_connect, results):
            if isinstance(result, BaseException):
                self._log.warning(f"Could not connect to {data.toy_id}: {result!r}")
                connected.append(result)
                continue
            controller = self._controller_for(data.toy_id, fresh=True)
            connected.append(controller if controller else UnknownToyError(data.toy_id))
        self._log.info("Connection process finished")
        if any(isinstance(c, ToyController) for c in connected):
            await self._report_batteries()
        return connected

    async def _disconnect(self, toy_ids: list[str]) -> list[BaseException | None]:
        """Remove every toy concurrently. Each result is None, or the exception its removal raised."""
        results = await asyncio.gather(
            *(self._core.remove(toy_id) for toy_id in toy_ids),
            return_exceptions=True,
        )
        return [
            result if isinstance(result, BaseException) else None for result in results
        ]

    async def _on_status_change(self, toy_id: str, status: ToyStatus) -> None:
        """Translate the core's connection status into the matching user callback."""
        if status == ToyStatus.RECONNECTING:
            self._log.warning(
                f"Lost the connection to {toy_id}. Will try to reconnect for up to {RECONNECT_WINDOW:.0f} s."
            )
            callback = self._disconnect_callback
        elif status == ToyStatus.CONNECTED:
            self._log.info(f"Reconnection successful for {toy_id}")
            callback = self._reconnection_success_callback
        elif status == ToyStatus.LOST:
            self._log.error(f"Unable to recover the connection to {toy_id}.")
            callback = self._reconnection_failure_callback
        else:
            self._log.warning(f"Powered off toy at {toy_id}")
            callback = self._power_off_callback
        self._run_callback(callback, toy_id, f"{status.value} callback")
        if status == ToyStatus.CONNECTED:
            await self._report_batteries()

    async def _on_battery_change(self, _changed: dict[str, int | None]) -> None:
        """A toy's battery level changed: report every toy's level (see ``on_battery_update``)."""
        await self._report_batteries()

    async def _report_batteries(self) -> None:
        """Hand the last known battery level of every connected toy to the battery callback."""
        if self._battery_update_callback is None:
            return
        levels: dict[str, int | None] = {}
        for toy_id in await self._core.get_toy_ids():
            toy = self._core.get_controller(toy_id)
            if toy is not None:
                levels[toy_id] = toy.battery
        self._run_callback(self._battery_update_callback, levels, "battery update")

    # ------------------------------------------------------------------------------------------------------------------
    # Private: running work on the event loop, and callbacks
    # ------------------------------------------------------------------------------------------------------------------

    def _controller_for(self, toy_id: str, fresh: bool = False) -> ToyController | None:
        """
        The ToyController for a toy the core knows, or None.

        Args:
            toy_id: Identifier of the toy.
            fresh: Hand out a new controller even if there is one already (the toy was just connected again).
        """
        toy = self._core.get_controller(toy_id)
        if toy is None:
            return None
        controller = self._controllers.get(toy_id)
        if controller is None or fresh:
            controller = ToyController(self, toy)
            self._controllers[toy_id] = controller
        return controller

    def _on_loop(self, fn: Callable[[], T]) -> T:
        """
        Run a quick function that does no I/O on the event loop's thread and return its result.

        State changes go through here, so they never race a command or playback tick. From the loop's own thread
        (a callback) the function simply runs; once the hub is shut down there is no loop left, and it runs here too.
        """
        if self._runner.in_loop_thread() or not self._runner.is_running:
            return fn()

        async def call() -> T:
            return fn()

        return self._runner.run_async(call(), _APPLY_TIMEOUT)

    def _submit(
        self,
        run: Callable[[], Awaitable[T]],
        callback: Optional[Callable[[T | None], Any]],
        command: str,
    ) -> None:
        """
        Run a toy command in the background and hand its result to *callback*, or None if it failed.

        Commands start in the order they are submitted, so those for one toy reach it in that order.
        """

        async def execute() -> None:
            result: T | None = None
            try:
                result = await run()
            except (ToyNotConnectedError, UnknownToyError) as e:
                self._log.info(f"'{command}' was not sent: {e!r}")
            except ToyConnectionError as e:
                self._log.warning(f"'{command}' failed: {e!r}")
            except Exception as e:
                self._log.error(f"'{command}' failed unexpectedly: {e!r}", exc_info=e)
            self._run_callback(callback, result, command)

        self._runner.submit(execute())

    def _submit_with_result(
        self,
        run: Callable[[], Awaitable[T]],
        callback: Callable[[T | BaseException], Any],
        timeout: float | None,
    ) -> None:
        """Run a hub operation in the background and hand *callback* its result, or the exception it raised."""

        async def execute() -> None:
            result: T | BaseException
            try:
                result = await asyncio.wait_for(run(), timeout)
            except Exception as e:
                e.add_note(traceback.format_exc())
                result = e
            self._run_callback(callback, result, "result")

        self._runner.submit(execute())

    def _run_callback(
        self, callback: Optional[Callable[[Any], Any]], result: Any, context: str
    ) -> None:
        """Invoke a user callback. An exception it raises goes to the error callback (or the log)."""
        if callback is None:
            return
        try:
            callback(result)
        except Exception as e:
            self._report_error(e, f"Your callback for {context} raised an exception")

    def _report_error(self, error: Exception, context: str) -> None:
        """Hand an error to the error callback, or log it if there is none."""
        tb = (
            error.tb
            if isinstance(error, DiscoveryError)
            else "".join(traceback.format_exception(error))
        )
        if self._error_callback is None:
            self._log.error(f"{context}: {error!r}\n{tb}")
            return
        try:
            self._error_callback(error, context, tb)
        except Exception:
            self._log.exception("The error callback raised an exception")
