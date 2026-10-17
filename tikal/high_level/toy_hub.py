"""
High-Level API: :class:`ToyHub`, the entry point. It discovers and connects toys, and hands out a
:class:`~tikal.high_level.ToyController` per toy.

Example::

    hub = ToyHub(toy_cache_path=Path("./toys.json"))
    toys = hub.discover_toys_blocking(timeout=5.0)
    toys[0].model_name = "Lush"  # unless the toy cache knows it
    toy = hub.connect_toys_blocking(toys[:1])[0]  # a ToyController, or the exception that prevented it
    toy.intensity1(15)
    hub.shutdown()
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
    Discovers, connects, and manages toys. Synchronous: an event loop on a background thread does the work.

    - The ``*_blocking`` methods, :meth:`start_discovery`, :meth:`stop_discovery`, :meth:`update_model_name` and
      :meth:`shutdown` block until done.
    - ``*_callback`` methods return at once and pass the result to their callback, or the exception (with its traceback
      as a note).
    - Callbacks, including those of :class:`~tikal.high_level.ToyController` commands, run on the background thread.
      Keep them short.
    - Call :meth:`shutdown` when done.

    Args:
        on_battery_update: Called with ``{toy_id: level}`` for every connected toy (level in percent, None without
            battery) when a level changes (checked every 2 minutes), and after toys connected or reconnected.
        on_error: Called with ``(exception, context, traceback)`` when one of your callbacks raises, and when a scan
            started by :meth:`start_discovery` fails. Without it, these errors are logged.
        on_disconnect: Called with the toy id when a toy's connection is lost or a command to it failed twice. The hub
            reconnects it for up to 1 minute; its commands report None meanwhile.
        on_reconnection_failure: Called with the toy id when reconnecting failed for 1 minute. The toy is disconnected
            and its commands all report None.
        on_reconnection_success: Called with the toy id when reconnecting succeeded. The toy is stopped and its pattern
            paused. Resume it with ``set_paused(False)``.
        on_power_off: Called with the toy id when a toy was switched off with its button. The toy is disconnected.
        logger_name: Name of the logger.
        toy_cache_path: JSON file that remembers each toy's model name, so discovery can fill it in (see
            :class:`~tikal.high_level.ToyCache`). In-memory only if the empty path ``Path()`` is given.
        default_model: Model name of discovered toys that the toy cache does not know.
        bluetooth_scanner: BleakScanner (sub)class to scan with, e.g., ``tikal.mock.MockBleakScanner``.
        bluetooth_client: BleakClient (sub)class to connect with, e.g., ``tikal.mock.MockBleakClient``.
        mock_toys: Offer the fictional MockEstimToys brand. Not part of the public API and may be removed.

    Raises:
        RuntimeError: The background event loop did not start within 5 seconds.
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
        Register :meth:`shutdown` to run at interpreter exit, so an uncaught exception, a KeyboardInterrupt, or a
        forgotten :meth:`shutdown` does not leave toys running. The hook does not keep the hub alive.

        Returns:
            The hook, for :meth:`shutdown` to unregister.
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
        Start scanning for toys in the background, until :meth:`stop_discovery`. Blocks until the scan started.

        Args:
            on_update: Called with all available toys whenever they change, first with an empty list. Model names come
                from the toy cache. Already connected toys are not discovered. If the scan fails, it ends:
                *on_update* receives an empty list, and *on_error* the :class:`~tikal.high_level.DiscoveryError`.

        Raises:
            DiscoveryStartError: The scan could not start, e.g., Bluetooth is off.
            RuntimeError: Called from a callback.
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
        """
        Stop the scan started by :meth:`start_discovery`. Its *on_update* is not called anymore. No-op if no scan runs.

        Raises:
            RuntimeError: Called from a callback.
        """
        self._runner.run_async(self._core.stop_scan())

    def discover_toys_blocking(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Discover available toys (blocking).

        Args:
            timeout: Scan duration in seconds. Longer scans find more toys.

        Returns:
            The toys found, with model names from the toy cache (or *default_model*).

        Raises:
            RuntimeError: A scan started by :meth:`start_discovery` is running, or called from a callback.
            TimeoutError: The scan took more than twice *timeout*.
            Exception: Whatever the scanner raises, e.g., Bluetooth is off.
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
        Discover available toys with a callback. Returns immediately.

        Args:
            on_discovered: Called with the toys found (see :meth:`discover_toys_blocking`), or with the exception that
                ended the scan: RuntimeError if a scan started by :meth:`start_discovery` is running, TimeoutError after
                twice *timeout*, or whatever the scanner raised.
            timeout: Scan duration in seconds.
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
        Connect toys concurrently (blocking). The toy cache remembers the model of each connected toy.

        Args:
            to_connect: Discovered toys, each with its model name set (case-insensitive).
            timeout: Seconds to wait for all connections.

        Returns:
            One result per toy, in input order. Each is the toy's :class:`~tikal.high_level.ToyController`, or the
            exception that prevented connecting it:

            - :class:`~tikal.high_level.ToyAlreadyAddedError`: The toy is connected already, or being connected.
            - :class:`~tikal.high_level.InvalidModelError`: Not a model of the toy's brand.
            - :class:`~tikal.high_level.BadModelError`: The toy refused or did not answer the model's commands.
            - :class:`~tikal.high_level.AddConnectionError`: Connecting failed, e.g., the toy is off, connected
              elsewhere, or no longer found by the scan.
            - RuntimeError: Any other failure, e.g., the toy was never discovered.

        Raises:
            TimeoutError: Not all connections finished within *timeout*.
            RuntimeError: Called from a callback.
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
        Connect toys in the background. Returns at once. See :meth:`connect_toys_blocking`.

        Args:
            to_connect: Discovered toys, each with its model name set (case-insensitive).
            on_connected: Called with the results (see :meth:`connect_toys_blocking`), or with a TimeoutError if the
                connections took longer than *timeout*.
            timeout: Seconds to wait for all connections. Connections still running then are canceled.
        """
        self._log.info(f"Connecting to {len(to_connect)} toy(s) (callback)...")
        self._submit_with_result(
            lambda: self._connect(to_connect), on_connected, timeout
        )

    def disconnect_toys_blocking(
        self, to_disconnect: list[str], timeout: float = 10.0
    ) -> Sequence[BaseException | None]:
        """
        Stop and disconnect toys concurrently (blocking).
        After this their controllers keep reporting the last state and their commands report None.

        Args:
            to_disconnect: The toy ids.
            timeout: Seconds to wait for all disconnections.

        Returns:
            One result per toy, in input order. Each is None on success, or the exception that occurred:

            - :class:`~tikal.high_level.UnknownToyError`: The toy is not connected to this hub.
            - :class:`~tikal.high_level.ToyConnectionError`: Disconnecting cleanly failed. Treated as disconnected.

        Raises:
            TimeoutError: Not all disconnections finished within *timeout*. They keep going in the background.
            RuntimeError: Called from a callback.
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
        Stop and disconnect toys in the background. Returns at once. See :meth:`disconnect_toys_blocking`.

        Args:
            to_disconnect: The toy ids.
            on_disconnected: Called with the results (see :meth:`disconnect_toys_blocking`), or with a TimeoutError if
                the disconnections took longer than *timeout*.
            timeout: Seconds to wait for all disconnections. Disconnections still running then are canceled.
        """
        self._log.info(f"Disconnecting from {len(to_disconnect)} toy(s) (callback)...")
        self._submit_with_result(
            lambda: self._disconnect(to_disconnect), on_disconnected, timeout
        )

    def update_model_name(
        self, toy_id: str, model_name: str
    ) -> ToyController | BaseException:
        """
        Change the model of a connected toy. Capabilities both models share keep running. The others are switched off.
        On success, the toy cache remembers the model.

        In a callback, use :meth:`ToyController.set_model_name` instead.

        Args:
            toy_id: The toy.
            model_name: New model name, case-insensitive.

        Returns:
            The toy's :class:`~tikal.high_level.ToyController`, or the exception that prevented the change:

            - :class:`~tikal.high_level.InvalidModelError`: Not a model of the toy's brand.
            - :class:`~tikal.high_level.BadModelError`: The toy refused or did not answer the new model's commands.
            - :class:`~tikal.high_level.UnknownToyError`: The toy is not connected to this hub.
            - :class:`~tikal.high_level.ToyNotConnectedError`: The toy is reconnecting.
            - :class:`~tikal.high_level.ToyConnectionError`: The toy could not be reached. The hub reconnects it.
            - TimeoutError: No result within 30 seconds. The change may still happen.

        Raises:
            RuntimeError: Called from a callback.
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
        Stop scanning, stop and disconnect every toy, and stop the background thread. Idempotent.

        - Do not use the hub afterwards: create a new one.
        - If you never call it, it runs at interpreter exit, even after an uncaught exception or a KeyboardInterrupt.
          It cannot run if the process is killed outright (SIGKILL, ``os._exit``, power loss).

        Raises:
            RuntimeError: Called from a callback. Treat as shutdown anyway.
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
        """
        Connect toys concurrently. If any toy connected, report the batteries to *on_battery_update*.

        Args:
            to_connect: Discovered toys, each with its model name set.

        Returns:
            One result per toy, in input order. Each is its controller, or the exception that prevented the connection.
        """
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
        """
        Stop and disconnect toys concurrently.

        Args:
            toy_ids: The toys.

        Returns:
            One result per toy, in input order. Each is None on success, or the exception that occurred.
        """
        results = await asyncio.gather(
            *(self._core.remove(toy_id) for toy_id in toy_ids),
            return_exceptions=True,
        )
        return [
            result if isinstance(result, BaseException) else None for result in results
        ]

    async def _on_status_change(self, toy_id: str, status: ToyStatus) -> None:
        """
        Call the callback that matches a toy's new connection status. After a reconnect, also report the batteries.

        Args:
            toy_id: The toy.
            status: Its new status.
        """
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
        """
        Report every toy's battery level to *on_battery_update*, since one changed.

        Args:
            _changed: ``{toy_id: level}`` of the toys whose level changed. Unused: every level is reported.
        """
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
        The controller of a connected toy. Every call hands out the same one, until *fresh*.

        Args:
            toy_id: The toy.
            fresh: Replace the toy's controller with a new one, e.g., because the toy was just connected again.

        Returns:
            The controller, or None if the toy is not connected to this hub.
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
        Run a quick function without I/O on the loop's thread, so it never races a command or a playback tick. In a
        callback (already on the loop's thread) or after :meth:`shutdown`, it runs in the calling thread.

        Args:
            fn: The function.

        Returns:
            What *fn* returns.

        Raises:
            TimeoutError: The loop did not run *fn* within 5 seconds, e.g., because a callback blocks it.
            Exception: Whatever *fn* raises.
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
        Run a toy command in the background. Commands start in the order they are submitted. Failures are logged.

        Args:
            run: Starts the command.
            callback: Called with the command's result, or None if it failed. Optional.
            command: Name of the command, for the log.
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
        """
        Run a hub operation in the background and hand *callback* its result, or the exception it raised.

        Args:
            run: Starts the operation.
            callback: Called with the result, or with the exception raised (TimeoutError after *timeout*). The
                exception carries its traceback as a note.
            timeout: Seconds before the operation is canceled. None: no limit.
        """

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
        """
        Call a user callback. An exception it raises goes to *on_error* (or the log).

        Args:
            callback: The callback. None: do nothing.
            result: What the callback is called with.
            context: What the callback is for, for the error report, e.g., ``"battery update"``.
        """
        if callback is None:
            return
        try:
            callback(result)
        except Exception as e:
            self._report_error(e, f"Your callback for {context} raised an exception")

    def _report_error(self, error: Exception, context: str) -> None:
        """
        Pass an error with its traceback to *on_error*, or log it if there is none.
        An exception *on_error* raises is logged.

        Args:
            error: The error. For a :class:`~tikal.high_level.DiscoveryError`, the traceback of the failed scan is
                reported.
            context: What failed, for *on_error* and the log.
        """
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
