import asyncio
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Callable, Any, Type
from bleak import BLEDevice, BleakScanner, BleakClient

from .toy_data import LOVENSE_TOY_NAMES, ToyData, LovenseData, ValidationError
from .toy_bled import ToyBLED, LovenseBLED


class ToyConnectionBuilder(ABC):

    def __init__(self, logger_name: str):
        """
        Abstract base class for discovering and connecting to toys. Each toy brand should implement
        its own connection builder that handles brand-specific discovery protocols and connection procedures.
        Args:
            logger_name: Name of the logger to use. Empty string for root logger.
        """
        self._log = getLogger(logger_name)

    @abstractmethod
    async def discover_toys(self, timeout: float) -> list[ToyData]:
        """Scans for available toys of this brand.
        Args:
            timeout: Maximum time to scan in seconds
        Returns:
            list[ToyData]: List of discovered toy data objects
        Raises:
            Exception: Any exception from the underlying discovery mechanism
        """
        raise NotImplementedError

    @abstractmethod
    async def create_toys(
        self, to_connect: list[ToyData]
    ) -> list[ToyBLED | BaseException]:
        """
        Create connected toy instances from discovery data.
        Args:
            to_connect: List of toy data objects to connect to. All ToyData must have valid model_names
        Returns:
            list[ToyBLED | BaseException]: contains connected ToyBLED instances and Exceptions for failed connections.
            Order matches the input list.
        Raises:
            ValidationError: If any ToyData has invalid model_name
        """
        raise NotImplementedError


class LovenseConnectionBuilder(ToyConnectionBuilder):

    def __init__(
        self,
        on_disconnect: Callable[[BleakClient], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str,
        scanner_class: Type[BleakScanner] = BleakScanner,
        client_class: Type[BleakClient] = BleakClient,
    ):
        """
        Handles discovery and connection for Lovense toys. Manages the Lovense-specific BLE discovery process and
        connection setup, including UUID discovery and notification configuration.
        Args:
            on_disconnect: Invoked when a toy disconnects unexpectedly. Receives the toy's BleakClient as an argument.
            on_power_off: Invoked when the user powers off a toy. Receives the toy's address as an argument.
            logger_name: Name of the logger to use. Empty string for root logger.
            scanner_class: Class to use for BLE scanning (defaults to BleakScanner)
            client_class: Class to use for BLE client connections (defaults to BleakClient)
        """
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
        Scans for available Lovense toys.
        Args:
            timeout: Maximum scan time in seconds
        Returns:
            list[LovenseData]: List of discovered Lovense toy data
        Raises:
            Exception: Any exception from BleakScanner.discover()
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
        Create connected Lovense toy instances.
        Args:
            to_connect: List of LovenseData objects with valid model_names
        Returns:
            list[LovenseBLED | BaseException]: List of LovenseBLED instances and exceptions for failed connections
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
        Find UUID by type (tx or rx) for the Lovense device.
        The TX UUID is needed for sending commands, the RX UUID for receiving responses.
        Args:
            client: BleakClient of the toy
            uuid_type: Either 'rx' or 'tx'
        Raises:
            ValueError: If uuid_type is not 'rx' or 'tx'
            ConnectionError: If unable to find the UUID
        Returns:
            UUID string for the specified type
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
        Connects to a single Lovense toy.
        Args:
            model_name: Model name of the toy (e.g. "Gush", "Nora")
            device: BLEDevice from discovery scan
        Raises:
            ValidationError: If model_name is not valid
            ConnectionError: If connection fails
            NotificationError: If notification setup fails
        Returns:
            LovenseBLED: Connected LovenseBLED instance
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
        """Wrapper that only calls on_disconnect for unexpected disconnects."""
        toy = self._toys_by_client.get(client)
        if toy and not toy.intentional_disconnect:
            # Only call the callback for unexpected disconnects
            self._on_disconnect(client)
        self._toys_by_client.pop(client, None)
