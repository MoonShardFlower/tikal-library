"""
Part of the Low-Level API: Provides connection management for toy devices.

This module provides abstract and concrete implementations for discovering and connecting
to BLE toys. This module provides:

- :class:`ToyConnectionBuilder`: Abstract base class defining the connection interface
- :class:`LovenseConnectionBuilder`: Concrete implementation for Lovense brand toys

Example::

        def handle_disconnect(client: BleakClient):
            print(f"Toy at {client.address} disconnected unexpectedly")

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
from typing import Callable, Any, Type
from bleak import BLEDevice, BleakScanner, BleakClient

from .toy_data import LOVENSE_TOY_NAMES, ToyData, LovenseData, ValidationError
from .toy_bled import ToyBLED, LovenseBLED


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
    async def create_toys(
        self, to_connect: list[ToyData]
    ) -> list[ToyBLED | BaseException]:
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


class LovenseConnectionBuilder(ToyConnectionBuilder):
    """
    Connection builder for Lovense brand toys.

    Part of the Low-Level API: Handles discovery and connection for Lovense toys using Bluetooth Low Energy.
    Manages the Lovense-specific BLE discovery process, UUID discovery, and notification configuration.

    Args:
        on_disconnect: Callback invoked when a toy disconnects unexpectedly. Receives the toy's BleakClient as an argument. Not called for intentional disconnects.
        on_power_off: Callback invoked when the user powers off a toy via the physical power button. Receives the toy's Bluetooth address as a string.
        logger_name: Name of the logger to use. Use empty string for root logger.
        scanner_class: BLE scanner class to use. Defaults to BleakScanner. Can be overridden for testing.
        client_class: BLE client class to use. Defaults to BleakClient. Can be overridden for testing.

    Note:
        The Lovense discovery protocol identifies devices by the "LVS-" prefix in their Bluetooth name.
        Only devices matching this pattern will be discovered.
    """

    def __init__(
        self,
        on_disconnect: Callable[[BleakClient], Any],
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
        self._toys_by_client: dict[BleakClient, LovenseBLED] = {}
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
            List of LovenseData objects containing the name and Bluetooth address of each discovered toy. The
            model_name is left empty and needs to be filled in by you.

        Raises:
            Exception: Any exception from BleakScanner.discover(), such as permission errors or Bluetooth adapter issues

        Example::

                toys = await builder.discover_toys(timeout=5.0)
                print(f"Found {len(toys)} Lovense devices")
                for toy in toys:
                    print(f"{toy.name} at {toy.toy_id}")

        Note:
            This method caches discovered BLE devices internally.
            You should call this method before calling :meth:`create_toys`.
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
    ) -> list[LovenseBLED | BaseException]:
        """
        Create connected Lovense toy instances from discovery data.

        Attempts to the specified toys concurrently. For each toy, this method:

        1. Retrieves the internally cached BLE device and establishes a BLE connection
        2. Discovers the TX and RX UUIDs and starts notifications
        3. Creates a LovenseBLED instance

        Args:
            to_connect: List of LovenseData objects with valid model_names.
                Valid model_names are in LOVENSE_TOY_NAMES.keys(). Instances of LovenseData are created with a prior
                call to :meth:`discover_toys` and model_names must be set by you.

        Returns:
            List where each element is either a connected LovenseBLED instance or a BaseException
            (ConnectionError, ValidationError, etc.) for failed connections. The order matches the input list.

        Raises:
            KeyError: If a toy's address is not in the cached devices (i.e., the toy wasn't discovered earlier.

        Example::

                # Discover toys
                toys = await builder.discover_toys(5.0)

                # Set model names (e.g., from user input)
                toys[0].model_name = "Nora"
                toys[1].model_name = "Lush"

                # Connect
                results = await builder.create_toys(toys)

                # Process results
                connected_toys = [r for r in results if isinstance(r, LovenseBLED)]
                failed = [r for r in results if isinstance(r, BaseException)]
        """
        self._log.info(f"Connecting to {len(to_connect)} Lovense devices")
        if not to_connect:
            return []
        coroutines = []
        for toy_data in to_connect:
            ble_device = self._cached_ble_devices[toy_data.toy_id]
            coroutines.append(self._create_toy(toy_data.model_name, ble_device))
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        count = len([toy for toy in results if isinstance(toy, LovenseBLED)])
        self._log.debug(f"Connected successfully to {count} Lovense devices")
        return results

    # ========================================================================
    # Private Methods
    # ========================================================================

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

    async def _create_toy(self, model_name: str, device: BLEDevice) -> LovenseBLED:
        """
        Connect to a single Lovense toy and create a LovenseBLED instance.

        This internal method handles the connection process:
        1. Validates the model name
        2. Creates and connects a BleakClient and discovers TX and RX UUIDs
        3. Creates a LovenseBLED instance
        4. Starts notifications

        Args:
            model_name: Model name of the toy (e.g., "Gush", "Nora"). Must be a key in LOVENSE_TOY_NAMES.
            device: BLEDevice object from the discovery scan containing the device's Bluetooth address and metadata.

        Returns:
            Connected and notification-ready LovenseBLED instance.

        Raises:
            ValidationError: If model_name is not a valid Lovense model name.
            ConnectionError: If the BLE connection fails or if notification setup fails.

        Note:
            If any step fails after the initial connection, the BleakClient is disconnected before raising the exception
            to clean up resources.
        """
        if model_name not in LOVENSE_TOY_NAMES:
            raise ValidationError(
                f"Invalid model_name '{model_name}' for address {device.address}. "
                f"Valid model_names are: {list(LOVENSE_TOY_NAMES.keys())}"
            )
        # Attempt to connect
        try:
            client = self._client_class(device, self._filtered_on_disconnect)
            await client.connect()
        except Exception as e:
            raise ConnectionError(
                f"Error connecting to {model_name} at {device.address}: {e}."
            )

        # Setup notifications
        try:
            tx_uuid = await self._find_uuid_by_type(client, "tx")
            rx_uuid = await self._find_uuid_by_type(client, "rx")
            toy = LovenseBLED(
                client, tx_uuid, rx_uuid, model_name, self._on_power_off, self._log.name
            )
            await toy.start_notifications()
            self._toys_by_client[client] = toy
        except Exception as e:
            await client.disconnect()
            raise ConnectionError(
                f"Error setting up notifications for {model_name} at {device.address}: {e}."
            )
        return toy

    def _filtered_on_disconnect(self, client: BleakClient) -> None:
        """
        Internal disconnect handler that filters intentional vs. unexpected disconnects.

        This method is registered with BleakClient as the disconnect callback. It checks whether the disconnect was
        intentional (initiated by calling disconnect()) or unexpected (e.g., toy powered off, connection lost) and only
        invokes the user's on_disconnect callback for unexpected disconnects.

        Args:
            client: BleakClient instance that disconnected.
        """
        toy = self._toys_by_client.get(client)
        if toy and not toy.intentional_disconnect:
            # Only call the callback for unexpected disconnects
            self._on_disconnect(client)
        self._toys_by_client.pop(client, None)
