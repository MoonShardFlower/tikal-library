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
        """
        Central Interface for Toy Communication. Responsible for discovering and connecting to toys,
        managing battery updates, sending commands to the toys and providing a basic pattern handling.

        Args:
            on_battery_update: Invoked with battery levels (dict mapping toy_id to battery level or exception)
            on_error: invoked when a critical error occurs with (exception, context message, traceback)
            on_disconnect: Callback invoked when a toy disconnects unexpectedly.
                ToyHub automatically tries to reconnect (toy_id)
            on_reconnection_failure: Callback invoked when ToyHub fails to reconnect to a toy (toy_id)
            on_reconnection_success: Callback invoked when ToyHub successfully reconnects to a toy (toy_id) after
                the connection was lost unexpectedly.
            on_power_off: Callback invoked when a toy powers off (toy_id)
            logger_name: Name of the logger to use for logging.
            toy_cache_path: Path to a toy cache file for persisting model names
            default_model: Default model name to use if model name cannot be retrieved from cache
        """
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
        """Check if the communication loop is running."""
        return self._cancel_communication_loop is not None

    def battery_update_callback(self, callback: Optional[Callable[[dict[str, int | None]], Any]]) -> None:
        """Set a callback to be invoked when battery levels are updated."""
        self._battery_update_callback = callback

    def error_callback(self, callback: Optional[Callable[[Exception, str, str], Any]]) -> None:
        """Set a callback to be invoked when an error occurs."""
        self._error_callback = callback

    def disconnect_callback(self, callback: Optional[Callable[[str], Any]]) -> None:
        """Set a callback to be invoked when a toy disconnects."""
        self._disconnect_callback = callback

    def reconnection_failure_callback(self, callback: Optional[Callable[[str], Any]]) -> None:
        """Set a callback to be invoked when ToyHub fails to reconnect to a toy."""
        self._reconnection_failure_callback = callback

    def reconnection_success_callback(self, callback: Optional[Callable[[str], Any]]) -> None:
        """Set a callback to be invoked when ToyHub successfully reconnects to a toy."""
        self._reconnection_success_callback = callback

    def power_off_callback(self, callback: Optional[Callable[[str], Any]]) -> None:
        """Set a callback to be invoked when a toy powers off."""
        self._power_off_callback = callback

    def discover_toys_blocking(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Discover available toys synchronously (blocking call).

        Args:
            timeout: Maximum time to wait for discovery in seconds

        Returns:
            list[ToyData]: List of discovered toy data objects (with model names from cache if available)

        Raises:
            TimeoutError: If discovery exceeds the timeout by a factor of at least 2. This should never happen with Bleak Scanner
            Exception: Any exception from the underlying scanner
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
        Discover available toys with a callback. Returns immediately and invokes the callback when discovery completes.

        Args:
            on_discovered: Invoked with either a list of discovered toys or an exception if discovery failed
            timeout: Maximum time to wait for discovery in seconds
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

        Args:
            to_connect: List of toy data objects to connect to (must have valid model_name)
            timeout: Maximum time to wait for connections in seconds

        Returns:
            list[ToyController | BaseException]: List of ToyController objects or exceptions for each toy. Order matches input.
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
        Connect to specified toys with a callback. Returns immediately and invokes the callback when connections are complete.

        Args:
            to_connect: List of toy data objects to connect to
            on_connected: Callback invoked with a list of connected controllers or exceptions
            timeout: Maximum time to wait for connections in seconds
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

        Args:
            to_disconnect: List of toy_ids to disconnect
            timeout: Maximum time to wait for disconnections in seconds

        Returns:
             list[BaseException | None]: List of exceptions for each disconnected toy or None if successful. Order matches input.
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
        Disconnect specified toys. Returns immediately and invokes the callback when all toys are disconnected.

        Args:
            to_disconnect: List of toy_ids to disconnect
            on_disconnected: Callback invoked with a list of exceptions for each disconnected toy or None if successful. Order matches input.
            timeout: Maximum time to wait for disconnections in seconds
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
        Updates the model name for a toy. Returns the updated controller if successful, otherwise an Exception.

        Args:
            toy_id: Unique identifier for the toy
            model_name: New model name of the toy

        Returns:
            ToyController | BaseException: Updated controller if successful, otherwise an Exception.
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
        Register a toy controller for background communication.
        Starts the communication loop if this is the first controller.

        Args:
            toy_id: Unique identifier for the toy
            controller: Controller instance to register
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
        Stops the communication loop if no controllers remain.

        Args:
            toy_id: Unique identifier of the toy to unregister
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
            f"Unregistered toy {toy_id}. ({len(self._toy_controllers)} remaining)"
        )

    # ------------------------------------------------------------------------------------------------------------------
    # Background Communication Loop
    # ------------------------------------------------------------------------------------------------------------------

    def _start_communication_loop(self) -> None:
        """Start the communication loop if not already running."""
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
                    if time() - self._last_battery_update >= self.BATTERY_UPDATE_INTERVAL:
                        await self._update_battery_levels(controllers)
                # Process controller communication (pattern playback, etc.)
                await ToyHub._process_controller_communication(
                    controllers, sleep_time
                )
            except Exception as e:
                if self._error_callback:
                    self._error_callback(e, "Communication loop error", traceback.format_exc())
                else:
                    self._log.exception(f"Communication loop error: {e!r}", exc_info=True)

        self._cancel_communication_loop = self._runner.schedule_recurring(
            communication_iteration, sleep_time
        )
        self._log.debug("Communication loop started")

    def _stop_communication_loop(self) -> None:
        """Stop the communication loop gracefully."""
        if self._cancel_communication_loop is None:
            return
        self._cancel_communication_loop()
        self._cancel_communication_loop = None
        self._log.debug("Communication loop stopped")

    async def _update_battery_levels(self, controllers: list["ToyController"]) -> None:
        """
        Updates the battery levels of all controllers concurrently.

        Args:
            controllers: List of controllers to query
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
        Process communication for all controllers concurrently. This includes pattern playback, command execution, etc.

        Args:
            controllers: List of controllers to process
            sleep_time: Time to sleep between iterations in seconds
        """
        # Build list of coroutines for parallel execution
        coroutines = [controller.process_communication() for controller in controllers]
        # Add a sleep coroutine to control loop frequency
        coroutines.append(asyncio.sleep(sleep_time))
        # Execute all concurrently
        await asyncio.gather(*coroutines, return_exceptions=True)

    def _handle_disconnect(self, client: BleakClient) -> None:
        """
        Attempts to reconnect to a toy following an unexpected disconnect.

        Args:
            client: BleakClient instance of the disconnected toy
        """
        self._log.warning(
            f"Disconnected from {client.name} at {client.address}. Will attempt to reconnect once."
        )
        toy_controller = self._toy_controllers[client.address]
        self._unregister_controller(client.address)
        if self._disconnect_callback:
            self._disconnect_callback(client.address)

        async def reconnect_task():
            try:
                await asyncio.sleep(1.0)  # Give some time in hopes of the connection failure resolving itself
                if not client.is_connected:
                    await client.connect()  # Try to reconnect
            except Exception as e:
                controller = self._toy_controllers[client.address]
                await controller.toy.disconnect()
                raise e

        def on_reconnect_complete(result):
            if isinstance(result, Exception):
                self._log.error(
                    f"Unable to recover connection to toy at address {client.address} due to {result!r}"
                )
                if self._reconnection_failure_callback:
                    self._reconnection_failure_callback(client.address)
            elif result is None:
                self._log.info(
                    f"Reconnection successful for {client.name} at {client.address}"
                )
                self._register_controller(client.address, toy_controller)
                if self.reconnection_success_callback:
                    self.reconnection_success_callback()
            else:
                self._log.exception(f"Unexpected result type while trying to handle connection failure: {result!r}")
                if self._reconnection_failure_callback:
                    self._reconnection_failure_callback(client.address)

        self._runner.run_callback(reconnect_task(), on_reconnect_complete, 5.0)

    def _handle_power_off(self, address: str) -> None:
        """
        Disconnects and removes the powered off toy.

        Args:
            address: Bluetooth address of the powered off toy
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
                controller.toy.disconnect(),
                on_disconnect_complete,
                timeout=5.0
            )
        except Exception as e:
            print(f"Error scheduling disconnect: {e}")

    # ------------------------------------------------------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stops the communication loop, disconnects all toys, and cleans up resources."""
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
