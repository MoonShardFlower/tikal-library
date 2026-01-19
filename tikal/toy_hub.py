"""
Part of the High-Level API: Provides connection management for toy devices.

This module provides the ToyHub class, which serves as the entry point for all toy operations. The ToyHub manages:
- **Toy Discovery**: Scanning for available devices via Bluetooth
- **Connection Management**: Establishing and maintaining connections
- **Command Queueing**: Processing commands from multiple toys concurrently
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

import asyncio
from logging import getLogger
import traceback
from threading import Lock
from time import time
from typing import Callable, Optional, Any
from bleak import BleakClient, BleakScanner
from pathlib import Path

from .utils.async_runner import AsyncRunner
from .toy_cache import ToyCache
from .toy_data import ToyData, LovenseData
from .connection_builder import LovenseConnectionBuilder
from .toy_bled import LovenseBLED
from .toy_controller import ToyController, LovenseController


class ToyHub:
    """
    Central interface for toy communication and lifecycle management.

    Part of the High-Level API: Handles discovery, connection, battery monitoring, and control of toys.

    Args:
        on_battery_update: Callback invoked when battery levels are updated (regularly). Receives dict mapping toy_id to battery level (int) or None if unavailable.
        on_error: Callback invoked when critical errors occur. Receives (exception, context_message, traceback_string).
        on_disconnect: Callback invoked when a toy disconnects unexpectedly. Receives toy_id. ToyHub automatically attempts reconnection.
        on_reconnection_failure: Callback invoked when automatic reconnection fails. Receives toy_id.
        on_reconnection_success: Callback invoked when automatic reconnection succeeds. Receives toy_id.
        on_power_off: Callback invoked when a toy is powered off via its physical button. Receives toy_id.
        logger_name: Name of the logger to use for logging messages.
        toy_cache_path: Path to a file for caching toy model names. Allows automatic model name assignment on later discoveries.
        default_model: Default model name to use if a toy isn't in the cache.
        bluetooth_scanner: BLE scanner class to use (defaults to BleakScanner). Can be overridden for testing.
        bluetooth_client: BLE client class to use (defaults to BleakClient). Can be overridden for testing.

    Attributes:
        BATTERY_UPDATE_INTERVAL (float): Seconds between automatic battery updates (120.0).
        COMMUNICATION_FPS (int): Frames per second for communication loop (20).
    """

    BATTERY_UPDATE_INTERVAL = 120.0  # seconds
    COMMUNICATION_FPS = 20  # frames per second

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
    ):
        self._battery_update_callback = on_battery_update
        self._error_callback = on_error
        self._disconnect_callback = on_disconnect
        self._reconnection_failure_callback = on_reconnection_failure
        self._reconnection_success_callback = on_reconnection_success
        self._power_off_callback = on_power_off
        self._log = getLogger(logger_name)

        self._runner = AsyncRunner()
        self._toy_cache = ToyCache(toy_cache_path, default_model, logger_name)
        self._toy_controllers: dict[str, "ToyController"] = {}
        self._lock = Lock()
        self._last_battery_update = 0.0
        self._cancel_communication_loop: Optional[Callable[[], None]] = None
        # Connection builders for different toy brands
        self._lovense_builder = LovenseConnectionBuilder(
            self._handle_disconnect,
            self._handle_power_off,
            logger_name,
            bluetooth_scanner,
            bluetooth_client,
        )

    @property
    def is_running(self) -> bool:
        """
        Check if the communication loop is currently running.

        Returns:
            bool: True if the loop is active, False otherwise.

        Note:
            The loop starts automatically when toys are connected and stops when all toys are disconnected.
        """
        return self._cancel_communication_loop is not None

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
        toys: list[ToyData] = []
        # Discover Lovense toys
        lovense_toys = self._runner.run_async(
            self._lovense_builder.discover_toys(timeout), timeout * 2
        )
        for toy in lovense_toys:
            toy.model_name = self._toy_cache.get_model_name(toy.name)
            toys.append(toy)
        self._log.info(f"Discovered {len(toys)} toy(s)")
        return toys

    def discover_toys_callback(
        self,
        on_discovered: Callable[[list[ToyData] | BaseException], None],
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

        async def discovery_task():
            toys: list[ToyData] = []
            try:
                lovense_toys = await self._lovense_builder.discover_toys(timeout)
                for toy in lovense_toys:
                    toy.model_name = self._toy_cache.get_model_name(toy.name)
                    toys.append(toy)
                self._log.info(f"Discovered {len(toys)} toy(s)")
            except Exception as e:
                e.add_note(traceback.format_exc())
                return e
            return toys

        self._runner.run_callback(discovery_task(), on_discovered, timeout * 2)

    def connect_toys_blocking(
        self, to_connect: list[ToyData], timeout: float = 30.0
    ) -> list[ToyController | BaseException]:
        """
        Connect to specified toys synchronously (blocking call).

        Attempts to connect to each toy in the list concurrently. Toys that connect successfully return ToyController
        instances; failed connections return exceptions.

        Args:
            to_connect: List of ToyData objects with a valid model_name set. Must have been discovered first.
            timeout: Maximum time to wait for all connections in seconds.

        Returns:
            list[ToyController | BaseException]: Each element is either a connected ToyController or an exception.
                Order matches the input list.

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

        controllers: list["ToyController | BaseException"] = []
        cache_updates = {}

        # Separate by brand
        lovense_data = [data for data in to_connect if isinstance(data, LovenseData)]
        lovense_bleds = self._runner.run_async(
            self._lovense_builder.create_toys(lovense_data), timeout
        )
        for data, bled in zip(lovense_data, lovense_bleds):
            if isinstance(bled, LovenseBLED):
                controller = LovenseController(bled, bled.address, self._log.name)
                controllers.append(controller)
                self._register_controller(data.toy_id, controller)
                cache_updates[data.name] = bled.model_name
            else:
                # Connection failed, bled is an exception
                controllers.append(bled)

        # Update cache with newly connected toys
        if self._toy_cache and cache_updates:
            self._toy_cache.update(cache_updates)

        self._log.info(f"Connection process finished")
        return controllers

    def connect_toys_callback(
        self,
        to_connect: list[ToyData],
        on_connected: Callable[[list["ToyController | BaseException"]], None],
        timeout: float = 30.0,
    ) -> None:
        """
        Connect to specified toys with a callback (non-blocking).

        Starts connections in the background and returns immediately. The callback is invoked when all connection attempts are complete.

        Args:
            to_connect: List of ToyData objects with a valid model_name set.
            on_connected: Callback invoked with a list of controllers or exceptions. Order matches the input list.
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

        async def connection_task():
            controllers: list["ToyController | BaseException"] = []
            cache_updates = {}
            lovense_data = [
                data for data in to_connect if isinstance(data, LovenseData)
            ]
            lovense_bleds = await self._lovense_builder.create_toys(lovense_data)
            for data, bled in zip(lovense_data, lovense_bleds):
                if isinstance(bled, LovenseBLED):
                    controller = LovenseController(bled, bled.address, self._log.name)
                    controllers.append(controller)
                    self._register_controller(data.toy_id, controller)
                    cache_updates[data.name] = bled.model_name
                else:
                    controllers.append(bled)
            if self._toy_cache and cache_updates:
                self._toy_cache.update(cache_updates)
            self._log.info(f"Connection process finished")
            return controllers

        self._runner.run_callback(connection_task(), on_connected, timeout)

    def disconnect_toys_blocking(self, to_disconnect: list[str], timeout: float = 10.0):
        """
        Disconnect specified toys synchronously (blocking call).

        Cleanly disconnects from the specified toys, stopping all actions and closing BLE connections.

        Args:
            to_disconnect: List of toy_ids (Bluetooth addresses) to disconnect.
            timeout: Maximum time to wait for all disconnections in seconds.

        Returns:
            list[BaseException | None]: List where each element is either None (successful disconnect)
            or an exception (failed disconnect). Order matches input list.

        Example:
            ::

                # Disconnect specific toys
                toy_ids = [controller.toy_id for controller in controllers]
                results = hub.disconnect_toys_blocking(toy_ids, timeout=10.0)

                # Check results
                for toy_id, result in zip(toy_ids, results):
                    if result is None:
                        print(f"{toy_id} disconnected successfully")
                    else:
                        print(f"{toy_id} disconnect failed: {result}")
        """
        if not to_disconnect:
            return []
        self._log.info(f"Disconnecting from {len(to_disconnect)} toy(s) (blocking)...")
        coroutines = []
        for toy_id in to_disconnect:
            if toy_id not in self._toy_controllers:
                self._log.warning(f"Attempted to disconnect unknown toy {toy_id}")
                continue
            controller = self._toy_controllers[toy_id]
            self._unregister_controller(toy_id)
            coroutines.append(controller.toy.disconnect())
        self._log.info(f"Disconnected from {len(to_disconnect)} toy(s)")
        return self._runner.run_async_parallel(coroutines, timeout)

    def disconnect_toys_callback(
        self,
        to_disconnect: list[str],
        on_disconnected: Callable[[list[BaseException | None]], Any],
        timeout: float = 10.0,
    ) -> None:
        """
        Disconnect specified toys with a callback (non-blocking).

        Starts disconnections in the background and returns immediately.
        The callback is invoked when all disconnection attempts are complete.

        Args:
            to_disconnect: List of toy_ids to disconnect.
            on_disconnected: Callback invoked with list of exceptions (or None for successful disconnects).
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

        async def disconnect_task():
            coroutines = []
            for toy_id in to_disconnect:
                if toy_id not in self._toy_controllers:
                    self._log.warning(f"Attempted to disconnect unknown toy {toy_id}")
                    continue
                controller = self._toy_controllers[toy_id]
                self._unregister_controller(toy_id)
                coroutines.append(controller.toy.disconnect())
            result = await asyncio.gather(*coroutines, return_exceptions=True)
            self._log.info(f"Disconnected from {len(to_disconnect)} toy(s)")
            return result

        self._runner.run_callback(disconnect_task(), on_disconnected, timeout)

    def update_model_name(
        self, toy_id: str, model_name: str
    ) -> ToyController | BaseException:
        """
        Update the model name for a connected toy.

        Changes the toy's model name, which affects which commands are available and how they're interpreted

        Args:
            toy_id: Unique identifier of the toy to update.
            model_name: New model name (must be valid for the toy's brand).

        Returns:
            ToyController | BaseException: The updated controller if successful,
            or an exception if the toy_id is unknown or the model_name is invalid.

        Example:
            ::

                # Correct a wrong model assignment
                result = hub.update_model_name(toy_id, "Nora")
                if isinstance(result, BaseException):
                    print(f"Update failed: {result}")
                else:
                    print(f"Model updated to {result.model_name}")
        """
        with self._lock:
            if toy_id not in self._toy_controllers:
                return ValueError(
                    f"Attempted to update model name for unknown toy {toy_id}"
                )
            name = self._toy_controllers[toy_id].toy.name
            self._toy_cache.update({name: model_name})
            try:
                self._toy_controllers[toy_id].toy.set_model_name(model_name)
            except Exception as e:
                return e
            self._log.info(f"Updated model name for toy {toy_id} to {model_name}")
            return self._toy_controllers[toy_id]

    # ------------------------------------------------------------------------------------------------------------------
    # Controller Management
    # ------------------------------------------------------------------------------------------------------------------

    def _register_controller(self, toy_id: str, controller: ToyController) -> None:
        """
        Register a toy controller for background communication

        Adds the controller to the active controllers dict. Starts the communication loop if this is the first controller.

        Args:
            toy_id: Unique identifier for the toy.
            controller: Controller instance to register.
        """
        with self._lock:
            self._toy_controllers[toy_id] = controller
            controller.connected = True
            if len(self._toy_controllers) == 1:
                self._start_communication_loop()
        # Trigger immediate battery update for new device
        self._last_battery_update = time() - self.BATTERY_UPDATE_INTERVAL
        self._log.debug(
            f"Registered toy {toy_id}. ({len(self._toy_controllers)} total)"
        )

    def _unregister_controller(self, toy_id: str) -> None:
        """
        Unregister a toy controller from background communication.

        Removes the controller from active controllers. Stops the communication loop if no controllers remain.

        Args:
            toy_id: Unique identifier of the toy to unregister.
        """
        with self._lock:
            if toy_id not in self._toy_controllers:
                return
            toy_controller = self._toy_controllers[toy_id]
            toy_controller.connected = False
            del self._toy_controllers[toy_id]
            if len(self._toy_controllers) == 0:
                self._stop_communication_loop()
        self._log.debug(
            f"Unregister toy {toy_id}. ({len(self._toy_controllers)} remaining)"
        )

    # ------------------------------------------------------------------------------------------------------------------
    # Background Communication Loop
    # ------------------------------------------------------------------------------------------------------------------

    def _start_communication_loop(self) -> None:
        """
        Start the background communication loop

        The loop runs at COMMUNICATION_FPS (20 FPS / every 50ms) and handles:
        - Processing queued commands
        - Updating toy intensities based on patterns
        - Periodic battery level queries
        """
        if self._cancel_communication_loop is not None:
            return

        sleep_time = 1.0 / self.COMMUNICATION_FPS

        async def communication_iteration():
            try:
                # Get a snapshot of controllers (avoid holding lock during I/O)
                with self._lock:
                    if not self._toy_controllers:
                        return
                    controllers = list(self._toy_controllers.values())
                # Update battery levels periodically
                if self._battery_update_callback:
                    if (
                        time() - self._last_battery_update
                        >= self.BATTERY_UPDATE_INTERVAL
                    ):
                        await self._update_battery_levels(controllers)
                # Process controller communication (pattern playback, etc.)
                await ToyHub._process_controller_communication(controllers, sleep_time)
            except Exception as e:
                if self._error_callback:
                    self._error_callback(
                        e, "Communication loop error", traceback.format_exc()
                    )
                else:
                    self._log.exception(
                        f"Communication loop error: {e!r}", exc_info=True
                    )

        self._cancel_communication_loop = self._runner.schedule_recurring(
            communication_iteration, sleep_time
        )
        self._log.debug("Communication loop started")

    def _stop_communication_loop(self) -> None:
        """Stop the background communication loop"""
        if self._cancel_communication_loop is None:
            return
        self._cancel_communication_loop()
        self._cancel_communication_loop = None
        self._log.debug("Communication loop stopped")

    async def _update_battery_levels(self, controllers: list["ToyController"]) -> None:
        """
        Update battery levels for all controllers concurrently

        Args:
            controllers: List of controllers to query.
        """
        self._log.info(f"Updating battery levels...")
        self._last_battery_update = time()
        # Run all battery queries in parallel
        battery_coroutines = [
            controller.toy.get_battery_level() for controller in controllers
        ]
        battery_results = await asyncio.gather(
            *battery_coroutines, return_exceptions=True
        )
        # Map results to toy IDs
        batteries = dict(
            zip([controller.toy_id for controller in controllers], battery_results)
        )
        self._battery_update_callback(batteries)
        self._log.info(f"Battery levels updated")

    @staticmethod
    async def _process_controller_communication(
        controllers: list[ToyController], sleep_time: float
    ) -> None:
        """
        Process communication for all controllers concurrently.

        This includes pattern playback, command execution, and state management.

        Args:
            controllers: List of controllers to process.
            sleep_time: Time to sleep between iterations.
        """
        # Build list of coroutines for parallel execution
        coroutines = [controller.process_communication() for controller in controllers]
        # Add a sleep coroutine to control loop frequency
        coroutines.append(asyncio.sleep(sleep_time))
        # Execute all concurrently
        await asyncio.gather(*coroutines, return_exceptions=True)

    def _handle_disconnect(self, client: BleakClient) -> None:
        """
        Handle unexpected toy disconnection and attempt reconnection

        Args:
            client: BleakClient instance of the disconnected toy.
        """
        self._log.warning(
            f"Disconnected from {client.name} at {client.address}. Will attempt to reconnect once."
        )
        toy_controller = self._toy_controllers[client.address]
        self._unregister_controller(client.address)
        if self._disconnect_callback:
            self._disconnect_callback(client.address)

        async def reconnect_task():
            # Give some time in hopes of the connection failure resolving itself
            await asyncio.sleep(1.0)
            if not client.is_connected:
                await client.connect()  # Try to reconnect

        def on_reconnect_complete(result):
            if isinstance(result, Exception):
                self._log.error(
                    f"Unable to recover connection to toy at address {client.address} due to {result!r}"
                )
                if self._reconnection_failure_callback:
                    self._reconnection_failure_callback(client.address)
                try:
                    self._runner.run_async(toy_controller.toy.disconnect(), 4.0)
                except Exception as e:
                    pass
            elif result is None:
                self._log.info(
                    f"Reconnection successful for {client.name} at {client.address}"
                )
                self._register_controller(client.address, toy_controller)
                if self._reconnection_success_callback:
                    self._reconnection_success_callback(client.address)
            else:
                self._log.exception(
                    f"Unexpected result type while trying to handle connection failure: {result!r}"
                )
                if self._reconnection_failure_callback:
                    self._reconnection_failure_callback(client.address)
                try:
                    self._runner.run_async(toy_controller.toy.disconnect(), 4.0)
                except Exception as e:
                    pass

        self._runner.run_callback(reconnect_task(), on_reconnect_complete, 5.0)

    def _handle_power_off(self, address: str) -> None:
        """
        Handle toy power-off event and disconnect cleanly (internal).

        Args:
            address: Bluetooth address of the powered-off toy.

        Note:
            This is an internal callback. Do not call directly.
        """
        self._log.warning(f"Powered off toy at address {address}")
        controller = self._toy_controllers[address]
        self._unregister_controller(address)
        if self._power_off_callback:
            self._power_off_callback(address)

        def on_disconnect_complete(result):
            if isinstance(result, Exception):
                print(f"Unable to disconnect toy in time: {result}")
            if self._power_off_callback:
                self._power_off_callback(address)

        try:
            self._runner.run_callback(
                controller.toy.disconnect(), on_disconnect_complete, timeout=5.0
            )
        except Exception as e:
            print(f"Error scheduling disconnect: {e}")

    # ------------------------------------------------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------------------------------------------------

    def shutdown(self) -> None:
        """
        Stop the communication loop, disconnect all toys, and clean up resources.

        This method should always be called before the program exits to ensure:
        - All toys are properly disconnected
        - The communication loop is stopped and the async runner is shut down cleanly

        Example:
            ::

                    hub = ToyHub()
                    # ... use hub ...
                    hub.shutdown()
        Note:
            After calling shutdown(), the ToyHub instance should not be reused.
            Create a new instance if you need to start working with toys again.
        """
        self._log.info("Shutting down CommunicationHandler...")
        # Stop communication loop
        if self._cancel_communication_loop is not None:
            self._cancel_communication_loop()

        # Disconnect all toys
        controller_ids = list(self._toy_controllers.keys())
        if controller_ids:
            result = self.disconnect_toys_blocking(controller_ids)
            exceptions = [e for e in result if isinstance(e, BaseException)]
            for e in exceptions:
                self._log.error(f"Error while trying to disconnect a toy: {e}")

        self._runner.shutdown()
        self._log.info("CommunicationHandler shutdown complete")
