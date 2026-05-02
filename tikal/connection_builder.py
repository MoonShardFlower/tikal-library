"""
Part of the Low-Level API: Provides connection management for toy devices.

This module provides:
- :class `BLEConnectionBuilder`: Entry point for all BLE toys manages BLE scanning (one‑shot and continuous) and creation of Toys  Delegates brand details to handlers.
- :class ``BLEBrandHandler``: Internal (Private) abstract interface for brand‑specific BLE logic
- :class ``LovenseHandler``: Internal (Private) implementation of ``BLEBrandHandler``

Example::

        def handle_disconnect(transport: Transport):
            print(f"Toy at {transport.toy_id} disconnected unexpectedly")

        def handle_power_off(address: str):
            print(f"Toy at {address} was powered off")

        builder = BLEConnectionBuilder(
            on_disconnect=handle_disconnect,
            on_power_off=handle_power_off,
            logger_name="tikal"
        )
"""

import asyncio
import time
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any, Callable, Type

from bleak import BleakClient, BleakScanner, BLEDevice

from .toy import Lovense, Toy
from .toy_data import LOVENSE_TOY_NAMES, LovenseData, ToyData, ValidationError
from .utils import BleTransport


class StaleDeviceError(ConnectionError):
    """Raised when attempting to connect to a device that was at one point discovered but is no longer available."""

    pass


class BLEBrandHandler(ABC):
    """
    Abstract interface for handling a specific brand of BLE toy.

    Each concrete implementation provides the logic for:
    - Recognizing the brand from BLE advertisements
    - Creating the appropriate ``ToyData``
    - Establishing a connection and returning a ready‑to‑use ``Toy`` instance
    """

    @staticmethod
    @abstractmethod
    def handles_device(device: BLEDevice) -> bool:
        """Return ``True`` if the BLE device belongs to this brand."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def handles_toy(toy_data: ToyData) -> bool:
        """Return ``True`` if the given ``ToyData`` belongs to this brand."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def create_toy_data(device: BLEDevice) -> ToyData:
        """Create a brand‑specific ``ToyData`` object from a BLE device."""
        raise NotImplementedError

    @abstractmethod
    async def create_toy(self, toy_data: ToyData, device: BLEDevice) -> Toy:
        """
        Connect to the toy described by ``toy_data`` using the provided ``BLEDevice``.

        This method handles brand‑specific connection steps (UUID resolution, notification setup, etc.) and returns a connected ``Toy`` instance.

        Raises:
            ValidationError: The ``toy_data`` contains an invalid model name.
            ConnectionError: The BLE connection, UUID resolution, or notification setup failed.
        """
        raise NotImplementedError


class LovenseHandler(BLEBrandHandler):

    _LOVENSE_SERVICE_PATTERN = "-4bd4-bbd5-a6920e4c5653"
    _UUID_REPLACEMENTS = {
        "tx": ("0001", "0002"),
        "rx": ("0001", "0003"),
    }

    def __init__(
        self,
        on_disconnect: Callable[[BleTransport], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str = "tikal",
        client_class: Type[BleakClient] = BleakClient,
    ):
        """
        Brand handler for Lovense BLE toys.

        Encapsulates all Lovense‑specific logic: identification, UUID resolution, connection, and notification setup.

        Args:
            on_disconnect: Callback called when a toy disconnects unexpectedly. Receives the toy's ``BleTransport`` instance.
            on_power_off: Callback called when the user powers off a toy via its physical button. Receives the toy id (address).
            logger_name: Name for the logger used by this handler. Defaults to 'tikal'.
            client_class BLE client class to use. Defaults to BleakClient. Can be overridden for testing.
        """
        self._on_disconnect = on_disconnect
        self._on_power_off = on_power_off
        self._log = getLogger(logger_name)
        self._client_class = client_class

    @staticmethod
    def handles_device(device: BLEDevice) -> bool:
        """Return ``True`` if the BLE device is a lovense toy. Lovense toys advertise with a name prefixed 'LVS-'"""
        if device.name and device.name.startswith("LVS-"):
            return True
        return False

    @staticmethod
    def handles_toy(toy_data: ToyData) -> bool:
        """Return ``True`` if the given ``ToyData`` represents a Lovense toy."""
        return isinstance(toy_data, LovenseData)

    @staticmethod
    def create_toy_data(device: BLEDevice) -> LovenseData:
        """Create a ``LovenseData`` instance (inherits from ``ToyData``) from a BLE device representing a Lovense toy."""
        name = device.name if device.name else "unknown"
        return LovenseData(name, device.address, "")

    async def create_toy(self, toy_data: LovenseData, device: BLEDevice) -> Toy:
        """
        Connect to a single Lovense toy.

        Args:
            toy_data: ``LovenseData`` object representing the toy to connect to.
            device: ``BLEDevice`` object representing the toy to connect to.

        Returns:
            A connected, ready to use ``Lovense`` instance.

        Raises:
            ValidationError: The model_name in ``toy_data`` is invalid. Valid model_names are in LOVENSE_TOY_NAMES.keys() of module toy_data
            ConnectionError: The BLE connection, UUID resolution, or notification setup failed.
        """
        model_name = toy_data.model_name
        if model_name not in LOVENSE_TOY_NAMES:
            raise ValidationError(
                f"Invalid model_name '{model_name}' for address {device.address}. "
                f"Valid model_names are: {list(LOVENSE_TOY_NAMES.keys())}"
            )

        transport = BleTransport(
            device,
            uuid_resolver=self._resolve_uuids,
            on_disconnect=self._on_disconnect,
            client_class=self._client_class,
        )
        try:
            await transport.connect()
            toy = Lovense(transport, model_name, self._on_power_off, self._log.name)
            await toy.start_notifications()
        except Exception as e:
            await transport.disconnect()
            raise ConnectionError(
                f"Error connecting to {model_name} at {device.address}: {e}."
            )
        return toy

    async def _resolve_uuids(self, client: BleakClient) -> tuple[str, str]:
        """
        Resolve the TX and RX UUIDs for a Lovense device by inspecting its GATT services.

        Passed as the ``uuid_resolver`` to ``BleTransport``, where it is called once the BLE link is open.

        Args:
            client: The connected ``BleakClient`` to inspect.

        Returns:
            ``(tx_uuid, rx_uuid)`` as uppercase strings.

        Raises:
            ConnectionError: If either UUID cannot be found in the device's GATT table.
        """
        tx_uuid = await self._find_uuid_by_type(client, "tx")
        rx_uuid = await self._find_uuid_by_type(client, "rx")
        return tx_uuid, rx_uuid

    async def _find_uuid_by_type(self, client: BleakClient, uuid_type: str) -> str:
        """
        Find the TX or RX UUID for a Lovense device.

        Searches through the device's GATT services to find the appropriate UUID based on the Lovense service pattern
        and UUID replacement rules.

        Args:
            client: Connected BleakClient for the toy.
            uuid_type: Either 'rx' (for receiving notifications) or 'tx' (for sending commands).

        Returns:
            UUID string in uppercase format for the specified characteristic type.

        Raises:
            ValueError: If uuid_type is not 'rx' or 'tx'.
            ConnectionError: If unable to find the UUID matching the Lovense service pattern.
                This can happen if the device is not a valid Lovense toy or if the connection is incomplete.
        """
        if uuid_type not in self._UUID_REPLACEMENTS:
            raise ValueError(f"Invalid UUID type: {uuid_type}")
        old_pattern, new_pattern = self._UUID_REPLACEMENTS[uuid_type]
        for service in client.services:
            uuid_str = str(service.uuid).lower()
            if (
                uuid_str.endswith(self._LOVENSE_SERVICE_PATTERN)
                and uuid_str.startswith("4")
                and old_pattern in uuid_str
            ):
                target_uuid = uuid_str.replace(old_pattern, new_pattern).upper()
                if any(
                    str(char.uuid).upper() == target_uuid
                    for char in service.characteristics
                ):
                    return target_uuid
        raise ConnectionError(f"Unable to find {uuid_type}-UUID for {client.address}")


class BLEConnectionBuilder:

    def __init__(
        self,
        on_disconnect: Callable[[BleTransport], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str,
        scanner_class: Type[BleakScanner] = BleakScanner,
        client_class: Type[BleakClient] = BleakClient,
    ):
        """
        Connection builder for Bluetooth Low Energy (BLE) toys using the Bleak library.

        Part of the Low-Level API: Handles discovery and connection for Toys using Bluetooth Low Energy. Delegates
        brand-specific handling to instances of BLEBrandHandler

        on_disconnect: Callback invoked when a toy disconnects unexpectedly. Receives the toy's ``BleTransport`` instance. Not called for intentional disconnects.
        on_power_off: Callback invoked when the user powers off a toy via the physical power button. Receives the toy id.
        logger_name: Name of the logger to use. Use empty string for root logger.
        scanner_class: BLE scanner class to use. Defaults to BleakScanner. Can be overridden for testing.
        client_class: BLE client class to use. Defaults to BleakClient. Can be overridden for testing.

        Note:
            Lovense Toys:   toys of this brand are identified by their bluetooth name starting with "LVS-".
                            If the user configured the bluetooth name to be different, the builder will not find the toy.
        """
        self._handlers = [
            LovenseHandler(on_disconnect, on_power_off, logger_name, client_class)
        ]
        self._log = getLogger(logger_name)
        self._scanner_class = scanner_class

        # One‑shot and continuous scan caches
        self._ble_devices: dict[str, BLEDevice] = dict()
        self._discovered: dict[str, tuple[ToyData, float]] = (
            dict()
        )  # address -> (ToyData, last_seen)
        self._all_seen_addresses: set[str] = set()  # address

        # Continuous scan state
        self._continuous_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._continuous_exception: Exception | None = None
        self._on_update: Callable[[list[ToyData] | Exception], Any] | None = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def discover_toys(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Scan for all available BLE toys.

        This method caches discovered BLE devices internally. You should call this method before calling :meth:`create_toys`.

        Args:
            timeout: Scan time in seconds. Longer timeouts increase the chance of finding all nearby devices. Default is 10 seconds.

        Raises:
            Exception: Any exception from BleakScanner.discover(), such as permission errors or Bluetooth adapter issues
            RuntimeError: If a continuous scan is in progress. See meth: start_continuous_scan and meth: stop_continuous_scan.

        Returns:
            List of ``ToyData`` objects with brand‑appropriate types. This is a snapshot, not a continuous stream of updates.

        Examples:
            toys = await builder.discover_toys(timeout=5.0)
            print(f"Found {len(toys)} Lovense devices")
            for toy in toys:
                print(f"{toy.name} at {toy.toy_id}")
        """
        if self._continuous_task and not self._continuous_task.done():
            raise RuntimeError("Continuous scan in progress. Use meth: stop_continuous_scan")
        self._log.info(f"Starting toy discovery for {timeout}s")
        devices = await self._scanner_class.discover(timeout=timeout)
        if self._continuous_task and not self._continuous_task.done():
            raise RuntimeError("Continuous scan in progress. Use meth: stop_continuous_scan")
        self._ble_devices.clear()

        toy_data_list = []
        for dev in devices:
            self._ble_devices[dev.address] = dev
            self._all_seen_addresses.add(dev.address)
            for handler in self._handlers:
                if handler.handles_device(dev):
                    td = handler.create_toy_data(dev)
                    toy_data_list.append(td)
                    break  # first handler that claims the device wins
        self._log.info(f"Discovery complete: {len(toy_data_list)} toy(s) found")
        return toy_data_list

    async def start_continuous(self, on_update: Callable[[list[ToyData] | Exception], Any] | None = None) -> None:
        """
        Start continuous background discovery of Toys.

        Continuously scan for new toys. New toys are added to the internal cache and stale toys removed.
        Use meth: ``retrieve_continuous`` to get the current snapshot. Use meth: stop_continuous to stop the background scan.
        The method is idempotent. Calls to this method will be ignored if a continuous scan is already running.

        Args:
            on_update: Optional callback. Called with a snapshot of all discovered Toys whenever the internal cache changes.
                Called with an exception if the continuous scan encounters an error.
                Can be seen as a push version of :meth:`retrieve_continuous`.

        Raises:
            Exception: Any exception raised by the BLE scanner (e.g., permission errors).
        """
        self._on_update = on_update
        if self._continuous_task and not self._continuous_task.done():
            return
        # Reset internal state and start continuous scan
        self._continuous_exception = None
        self._stop_event = asyncio.Event()
        self._discovered.clear()
        self._continuous_task = asyncio.create_task(self._continuous_worker())
        self._log.info("Continuous discovery started")

    async def stop_continuous(self) -> None:
        """
        Stop the continuous background discovery.

        Use :meth: start_continuous to start continuous background discover.
        After stopping, :meth:`retrieve_continuous` will return an empty tuple.
        The method is idempotent. Does nothing if continuous discovery is not running.
        """
        if self._continuous_task is None or self._continuous_task.done():
            return
        # Stop the continuous scan and reset internal state
        self._stop_event.set()
        await self._continuous_task
        self._stop_event = None
        self._discovered.clear()
        self._continuous_task = None
        self._log.info("Continuous discovery stopped")

    async def retrieve_continuous(self) -> list[ToyData]:
        """
        Retrieve the current snapshot of discovered Toys. Needs a continuous scan to be running (else always return the empty list)
        Use :meth:`start_continuous` to start the continuous scan.

        If an error occurred during continuous scanning and is still relevant (=no call to :meth:`stop_continuous`
        was made afterward), the first call to this method raises that exception. Subsequent calls return an empty list.
        Note that such exceptions stop the continuous scan.

        Raises:
            Any exception that occurred during continuous scanning as described above.

        Returns:
            List of ``ToyData`` objects with brand‑appropriate types. List is empty if continuous discovery is not running.
            This is a snapshot, not a continuous stream of updates.
        """
        # If an exception is pending and the user hasn't stopped the scan, raise it once
        if self._continuous_exception is not None:
            exc = self._continuous_exception
            self._continuous_exception = None
            raise exc
        # Not running or already stopped -> empty snapshot
        if self._continuous_task is None or self._continuous_task.done():
            return []
        return [td for td, _ in self._discovered.values()]

    async def create_toys(self, to_connect: list[ToyData]) -> list[Toy | BaseException]:
        """
        Create Toy instances from discovery data.

        Args:
            to_connect: List of ToyData objects with valid model_names. For Lovense valid model names are in LOVENSE_TOY_NAMES.keys() of module toy_data
                Instances of ToyData are created by :meth:`discover_toys` or :meth:`retrieve_continuous` and model_names must be set by you.

        Returns:
            List where each element is either a connected Toy instance or a BaseException for failed connections. Possible exceptions per element:
            - ``KeyError``: toy address was not found in the cache (i.e., :meth:`discover_toys` was not called first)
            - ``StaleDeviceError``: Subclass of ConnectionError: Device was discovered, but has since become stale. Retrieve a new snapshot i.e., via :meth:`discover_toys`
            - ``ConnectionError``: BLE connection or notification setup failed, e.g., the toy may have become unavailable
            - ``ValidationError``: model_name is not a valid Lovense model name
            - ``RuntimeError``: Developer error. I did not specify a handler for this subclass of ToyData. Should never happen.
            The order of results matches the order of the input list.

        Example::
                toys = await builder.discover_toys(5.0)  # Discover toys
                # Set model names (e.g., from user input)
                toys[0].model_name = "Nora"
                toys[1].model_name = "Lush"
                results = await builder.create_toys(toys)  # Connect
                # Process results
                connected_toys = [r for r in results if isinstance(r, Toy)]
                failed = [r for r in results if isinstance(r, BaseException)]
        """
        if not to_connect:
            return []
        coroutines = [self.create_toy(td) for td in to_connect]
        return await asyncio.gather(*coroutines)

    async def create_toy(self, to_connect: ToyData) -> Toy | BaseException:
        """
        Create a Toy instance from discovery data.

        Args:
            to_connect: ToyData object with a valid model_name. For Lovense valid model names are in LOVENSE_TOY_NAMES.keys() of module toy_data
                Instances of ToyData are created with a prior call to :meth:`discover_toys` and the model_name must be set by you.

        Returns:
            connected Toy instance on success, or a BaseException on failure. Possible exceptions:
            - ``KeyError``: toy address was not found in the cache (i.e., :meth:`discover_toys` was not called first)
            - ``StaleDeviceError``: Subclass of ConnectionError: Device was discovered, but has since become stale. Retrieve a new snapshot i.e., via :meth:`discover_toys`
            - ``ConnectionError``: BLE connection or notification setup failed, e.g., the toy may have become unavailable
            - ``ValidationError``: model_name is not a valid Lovense model name
            - ``RuntimeError``: Developer error. I did not specify a handler for this subclass of ToyData. Should never happen.

        Example::
                toys = await builder.discover_toys(5.0)  # Discover toys
                # Set model names (e.g., from user input)
                toys[0].model_name = "Nora"
                result = await builder.create_toys(toys[0])  # Connect
                print(f"Connected: {isinstance(result, Toy)}")
        """
        handler = next((h for h in self._handlers if h.handles_toy(to_connect)), None)
        if handler is None:
            return RuntimeError(
                f"No handler for data type: {type(to_connect).__name__}"
            )
        if to_connect.toy_id not in self._all_seen_addresses:
            return KeyError(
                f"Device {to_connect.toy_id} was never discovered. Run meth: discover_toys first."
            )
        if to_connect.toy_id not in self._ble_devices:
            return StaleDeviceError(
                f"Device {to_connect.toy_id} is stale. Run meth: discover_toys first."
            )
        device = self._ble_devices[to_connect.toy_id]
        try:
            return await handler.create_toy(to_connect, device)
        except Exception as e:
            return e

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _continuous_worker(self):
        """
        Worker coroutine that runs the continuous scanner and the stale‑device cleanup.
        Any unhandled exception will be stored and re‑raised by :meth retrieve_continuous.
        """
        scanner = None
        cleanup_task = None
        try:
            scanner = self._scanner_class(detection_callback=self._on_device_detected)
            await scanner.start()
            cleanup_task = asyncio.create_task(self._cleanup_stale_devices())
            await self._stop_event.wait()
        except Exception as e:
            self._continuous_exception = e
            self._log.error(f"Continuous scan failed: {type(e)}", exc_info=True)
        finally:
            if cleanup_task:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
            if scanner:
                await scanner.stop()

    async def _on_device_detected(self, device: BLEDevice, _):
        """Callback invoked by BleakScanner when a BLE advertisement is received"""
        self._ble_devices[device.address] = device
        self._all_seen_addresses.add(device.address)
        for handler in self._handlers:
            if handler.handles_device(device):
                toy_data = handler.create_toy_data(device)
                self._discovered[device.address] = (toy_data, time.time())
                self._emit_snapshot()
                break

    async def _cleanup_stale_devices(self):
        """Remove devices that were not seen for more than 8 seconds."""
        while not self._stop_event.is_set():
            await asyncio.sleep(8.0)  # timeout for stale devices
            now = time.time()
            for addr, (_, ts) in self._discovered.items():
                if now - ts > 8.0:
                    del self._discovered[addr]
                    self._emit_snapshot()

    def _emit_snapshot(self):
        """Emit a snapshot of the current discovered devices to the user callback"""
        if not self._on_update:
            return
        snapshot = [td for td, _ in self._discovered.values()]
        self._on_update(snapshot)
