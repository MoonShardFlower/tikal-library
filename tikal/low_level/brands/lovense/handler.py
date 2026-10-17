"""Low-Level API: :class:`LovenseHandler`, the BLE brand handler of Lovense toys."""

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from ...brand_handler import BLEBrandHandler
from ...toy import Toy
from ...toy_data import InvalidModelError, ToyData, ValidationError
from ...transport import BleTransport
from .data import LOVENSE_TOY_NAMES
from .toy import LovenseToy


class LovenseHandler(BLEBrandHandler):
    """
    Brand handler for Lovense BLE toys.

    Encapsulates all Lovense-specific logic: identification, UUID resolution, connection, and notification setup.
    Recognizes Lovense toys by their Bluetooth name (``LVS-...``).
    """

    _LOVENSE_SERVICE_PATTERN = "-4bd4-bbd5-a6920e4c5653"
    _UUID_REPLACEMENTS = {
        "tx": ("0001", "0002"),
        "rx": ("0001", "0003"),
    }

    @staticmethod
    def handles_device(device: BLEDevice) -> bool:
        """Return ``True`` if the BLE device is a lovense toy. Lovense toys advertise with a name prefixed 'LVS-'"""
        if device.name and device.name.startswith("LVS-"):
            return True
        return False

    @staticmethod
    def handles_toy(toy_data: ToyData) -> bool:
        """Return ``True`` if the given ``ToyData`` represents a Lovense toy."""
        return toy_data.brand == "Lovense"

    @staticmethod
    def create_toy_data(device: BLEDevice) -> ToyData:
        """Create a ``LovenseData`` instance (inherits from ``ToyData``) from a BLE device."""
        name = device.name if device.name else "unknown"
        return ToyData(name, device.address, "", "Lovense")

    async def create_toy(self, toy_data: ToyData, device: BLEDevice) -> Toy:
        """
        Connect a Lovense toy and start receiving its replies. The model's commands are not validated here.

        Args:
            toy_data: The toy, with its model name set (case-insensitive).
            device: Its BLE device.

        Raises:
            ~tikal.low_level.InvalidModelError: The model name is not a Lovense model.
            ConnectionError: Connecting, finding the characteristics, or subscribing to replies failed.

        Returns:
            The connected :class:`LovenseToy`.
        """
        if toy_data.model_name.title() not in LOVENSE_TOY_NAMES:
            raise InvalidModelError(
                f"Invalid model name '{toy_data.model_name}' for Lovense toy at address '{device.address}'."
            )

        transport = BleTransport(
            device,
            uuid_resolver=self._resolve_uuids,
            on_disconnect=self._on_disconnect,
            client_class=self._client_class,
        )
        try:
            await transport.connect()
            toy = LovenseToy(
                transport,
                toy_data.model_name.title(),
                self._on_power_off,
                self._log.name,
            )
            await toy.start_notifications()
        except ValidationError:
            await transport.disconnect()
            raise
        except Exception as e:
            await transport.disconnect()
            raise ConnectionError(
                f"Error connecting to {toy_data.model_name} at {device.address}: {e}."
            )
        return toy

    async def _resolve_uuids(self, client: BleakClient) -> tuple[str, str]:
        """
        Find the TX and RX characteristics of a connected Lovense toy.

        Args:
            client: The connected client.

        Raises:
            ConnectionError: A characteristic is missing.

        Returns:
            ``(tx_uuid, rx_uuid)``, upper case.
        """
        tx_uuid = await self._find_uuid_by_type(client, "tx")
        rx_uuid = await self._find_uuid_by_type(client, "rx")
        return tx_uuid, rx_uuid

    async def _find_uuid_by_type(self, client: BleakClient, uuid_type: str) -> str:
        """
        Find the TX or RX characteristic in the Lovense service.

        Args:
            client: The connected client.
            uuid_type: ``"tx"`` (commands) or ``"rx"`` (replies).

        Raises:
            ValueError: *uuid_type* is neither.
            ConnectionError: No matching characteristic, e.g., the device is not a Lovense toy.

        Returns:
            The UUID, upper case.
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
