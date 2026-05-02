"""
Part of the Low-Level API: Provides connection management for toy devices.

This module provides abstract and concrete implementations for discovering and connecting to toys. This module provides:

- :class:`ToyConnectionBuilder`: Abstract base class defining the connection interface
- :class:`LovenseConnectionBuilder`: Concrete implementation for Lovense brand toys

Example::

        def handle_disconnect(transport: Transport):
            print(f"Toy at {transport.toy_id} disconnected unexpectedly")

        def handle_power_off(address: str):
            print(f"Toy at {address} was powered off")

        builder = LovenseConnectionBuilder(
            on_disconnect=handle_disconnect,
            on_power_off=handle_power_off,
            logger_name="lovense"
        )
"""

import asyncio
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any, Callable, Type

from bleak import BleakClient, BleakScanner, BLEDevice

from .toy import Lovense, Toy
from .toy_data import LOVENSE_TOY_NAMES, LovenseData, ToyData, ValidationError
from .utils.transport import BleTransport


class ToyConnectionBuilder(ABC):
    """
    Abstract base class for discovering and connecting to toy devices.

    Each toy brand implements its own connection builder that handles brand-specific discovery protocols and connection
    procedures. This class defines the interface that all connection builders must implement.

    Args:
        logger_name: Name of the logger to use. Use empty string for root logger.

    """

    def __init__(self, logger_name: str):
        self._log = getLogger(logger_name)

    @abstractmethod
    async def discover_toys(self, timeout: float) -> list[ToyData]:
        """
        Scan for available toys of this brand.

        Scans the environment (e.g., Bluetooth, WiFi) for devices and returns their discovery data.

        Args:
            timeout: Maximum time to scan in seconds.

        Returns:
            List of discovered toy data objects.

        Raises:
            Exception: Any exception from the underlying discovery mechanism.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_toys(self, to_connect: list[ToyData]) -> list[Toy | BaseException]:
        """
        Create connected toy instances from discovery data.

        Attempts to connect to each toy in the provided list and returns either a connected toy instance or an
        exception for each connection attempt.

        Args:
            to_connect: List of toy data objects to connect to. All ToyData must have valid model_names set.

        Returns:
            List containing either connected ToyBLED instances or BaseException objects for failed connections.
            The order matches the input list. You can match results to input data by index.

        Raises:
            ValidationError: If any ToyData has an invalid or missing model_name.

        Example::

                results = await builder.create_toys(toys)
                for i, result in enumerate(results):
                    if isinstance(result, BaseException):
                        print(f"Failed to connect to {toys[i].name}: {result}")
                    else:
                        print(f"Connected to {result.model_name}")
        """
        raise NotImplementedError

    @abstractmethod
    async def create_toy(self, to_connect: ToyData) -> Toy | BaseException:
        """
        Create a connected Toy instance from discovery data.

        Attempts to connect to a single toy. See :meth:`create_toys` to connect to multiple toys concurrently.

        Args:
            to_connect: A ToyData object. Instances of ToyData are created with a prior call to :meth:`discover_toys`

        Returns:
            A connected Toy instance on success, or a BaseException on failure. Possible exceptions:
            - ``KeyError``: toy address was not found in the cache (i.e., :meth:`discover_toys` was not called first)
            - ``ValidationError``: model_name is invalid
            - ``ConnectionError``: connection failed, e.g., the toy may have become unavailable

        Example::

                toys = await builder.discover_toys(5.0)  # Discover toys
                toys[0].model_name = "Nora"  # Set model name (e.g., from user input)
                result = await builder.create_toy(toys[0])  # Connect
                if isinstance(result, Toy):
                    print("Connected!")
                else:
                    print(f"Failed: {result}")
        """
        raise NotImplementedError


class LovenseConnectionBuilder(ToyConnectionBuilder):
    """
    Connection builder for Lovense brand toys.

    Part of the Low-Level API: Handles discovery and connection for Lovense toys using Bluetooth Low Energy.
    Manages the Lovense-specific BLE discovery process, UUID discovery, and notification configuration.

    Args:
        on_disconnect: Callback invoked when a toy disconnects unexpectedly. Receives the toy's ``Transport`` instance. Not called for intentional disconnects.
        on_power_off: Callback invoked when the user powers off a toy via the physical power button. Receives the toy id.
        logger_name: Name of the logger to use. Use empty string for root logger.
        scanner_class: BLE scanner class to use. Defaults to BleakScanner. Can be overridden for testing.
        client_class: BLE client class to use. Defaults to BleakClient. Can be overridden for testing.

    Note:
        The Lovense discovery protocol identifies devices by the "LVS-" prefix in their Bluetooth name.
        Only devices matching this pattern will be discovered.
    """

    def __init__(
        self,
        on_disconnect: Callable[[BleTransport], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str,
        scanner_class: Type[BleakScanner] = BleakScanner,
        client_class: Type[BleakClient] = BleakClient,
    ):
        super().__init__(logger_name)
        self._on_disconnect = on_disconnect
        self._on_power_off = on_power_off
        self._scanner_class = scanner_class
        self._client_class = client_class
        self._cached_ble_devices: dict[str, BLEDevice] = {}
        self._LOVENSE_SERVICE_PATTERN = "-4bd4-bbd5-a6920e4c5653"
        self._UUID_REPLACEMENTS = {
            "tx": ("0001", "0002"),  # TX (write) characteristic
            "rx": ("0001", "0003"),  # RX (notify) characteristic
        }
        self._log.info("LovenseConnectionBuilder initialized.")

    async def discover_toys(self, timeout: float) -> list[LovenseData]:
        """
        Scan for available Lovense toys via Bluetooth LE.

        Discovers Lovense devices by scanning for BLE devices with names starting with "LVS-"

        Args:
            timeout: Maximum scan time in seconds. Longer timeouts increase the chance of discovering all nearby devices

        Returns:
            List of LovenseData objects containing the name and Bluetooth address of each discovered toy.
            The model_name is left empty and needs to be filled in by you.

        Raises:
            Exception: Any exception from BleakScanner.discover(), such as permission errors or Bluetooth adapter issues
            RuntimeError: If a continuous scan is already in progress. See meth: start_continuous_scan and meth: stop_continuous_scan.

        Example::

                toys = await builder.discover_toys(timeout=5.0)
                print(f"Found {len(toys)} Lovense devices")
                for toy in toys:
                    print(f"{toy.name} at {toy.toy_id}")

        Note:
            This method caches discovered BLE devices internally. You should call this method before calling :meth:`create_toys`.
        """
        self._log.info(f"Scanning for Lovense devices for {timeout} seconds")
        devices = await self._scanner_class.discover(timeout=timeout)
        self._cached_ble_devices = {}
        toys = []

        for device in devices:
            if device.name and device.name.startswith("LVS-"):
                self._cached_ble_devices[device.address] = device
                toys.append(LovenseData(device.name, device.address))
        self._log.debug(f"Discovered {len(toys)} Lovense devices")
        return toys

    async def create_toys(
        self, to_connect: list[LovenseData]
    ) -> list[Lovense | BaseException]:
        """
        Create connected Lovense toy instances from discovery data.

        Attempts to connect to the specified toys concurrently. For each toy, this method:
        1. Retrieves the internally cached BLE device and establishes a BLE connection
        2. Discovers the TX and RX UUIDs and starts notifications
        3. Creates a Lovense instance

        Args:
            to_connect: List of LovenseData objects with valid model_names. Valid model_names are in LOVENSE_TOY_NAMES.keys().
                Instances of LovenseData are created with a prior call to :meth:`discover_toys` and model_names must be set by you.

        Returns:
            List where each element is either a connected Lovense instance or a BaseException for failed connections. Possible exceptions per element:
            - ``KeyError``: toy address was not found in the cache (i.e., :meth:`discover_toys` was not called first)
            - ``ValidationError``: model_name is not a valid Lovense model name
            - ``ConnectionError``: BLE connection or notification setup failed, e.g., the toy may have become unavailable
            The order of results matches the order of the input list.

        Example::
                toys = await builder.discover_toys(5.0)  # Discover toys
                # Set model names (e.g., from user input)
                toys[0].model_name = "Nora"
                toys[1].model_name = "Lush"
                results = await builder.create_toys(toys)  # Connect
                # Process results
                connected_toys = [r for r in results if isinstance(r, Lovense)]
                failed = [r for r in results if isinstance(r, BaseException)]
        """
        self._log.info(f"Connecting to {len(to_connect)} Lovense devices")
        if not to_connect:
            return []
        coroutines = [self.create_toy(toy_data) for toy_data in to_connect]
        results = await asyncio.gather(*coroutines)
        count = len([toy for toy in results if isinstance(toy, Lovense)])
        self._log.debug(f"Connected successfully to {count} Lovense devices")
        return list(results)

    async def create_toy(self, to_connect: LovenseData) -> Lovense | BaseException:
        """
        Create a connected Lovense toy instance from discovery data.

        Attempts to connect to a single toy. See :meth:`create_toys` to connect to multiple toys concurrently.

        1. Retrieves the internally cached BLE device and establishes a BLE connection
        2. Discovers the TX and RX UUIDs and starts notifications
        3. Creates a Lovense instance

        Args:
            to_connect: LovenseData object with a valid model_name. Valid model_names are in LOVENSE_TOY_NAMES.keys().
                 Instances of LovenseData are created with a prior call to :meth:`discover_toys` and model_names must be set by you.

        Returns:
            A connected Lovense instance on success, or a BaseException on failure.
            Possible exceptions:

            - ``KeyError``: toy address was not found in the cache (i.e., :meth:`discover_toys` was not called first)
            - ``ValidationError``: model_name is not a valid Lovense model name
            - ``ConnectionError``: BLE connection or notification setup failed, e.g., the toy may have become unavailable

        Example::


                toys = await builder.discover_toys(5.0)  # Discover toys
                toys[0].model_name = "Nora"  # Set model name (e.g., from user input)
                result = await builder.create_toy(toys[0])  # Connect

                if isinstance(result, Lovense):
                    print("Connected!")
                else:
                    print(f"Failed: {result}")
        """
        self._log.info("Connecting to a Lovense device")
        try:
            ble_device = self._cached_ble_devices[to_connect.toy_id]
            result = await self._create_toy_helper(to_connect.model_name, ble_device)
        except Exception as e:
            return e
        self._log.debug("Connected successfully to a Lovense device")
        return result

    # ========================================================================
    # Private Methods
    # ========================================================================

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

        Searches through the device's GATT services to find the appropriate UUID based on the Lovense service pattern.

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
        services = client.services

        for service in services:
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

    async def _create_toy_helper(self, model_name: str, device: BLEDevice) -> Lovense:
        """
        Connect to a single Lovense toy and create a Lovense instance.

        This internal method handles the connection sequence:

        1. Validates the model name
        2. Constructs a ``BleTransport``, injecting ``_resolve_uuids`` as the UUID resolver and registering the disconnect callback.
        3. Calls ``transport.connect()``, which opens the BLE link and resolves the UUIDs.
        4. Creates a ``Lovense`` instance and starts notifications

        Args:
            model_name: Model name of the toy (e.g., "Gush", "Nora"). Must be a key in LOVENSE_TOY_NAMES.
            device: BLEDevice object from the discovery scan.

        Returns:
            Connected and notification-ready Lovense instance.

        Raises:
            ValidationError: If model_name is not a valid Lovense model name.
            ConnectionError: If the BLE connection, UUID resolution, or notification setup fails.

        Note:
            If any step fails after the transport is constructed, ``transport.disconnect()`` is called to release resources before re-raising.
        """
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
