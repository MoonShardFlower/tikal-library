"""
Part of the Low-Level API: the Lovense :class:`BLEBrandHandler` implementation.

Encapsulates all Lovense-specific BLE logic: identification (``LVS-`` name prefix), UUID resolution, connection, and
notification setup. Instantiated by ``BLEConnectionBuilder`` via the brand registry; you are not meant to use it directly.
"""

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
    The common construction signature (callbacks, logger, client class) is inherited from :class:`BLEBrandHandler`.
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
        """Create a ``LovenseData`` instance (inherits from ``ToyData``) from a BLE device representing a Lovense toy."""
        name = device.name if device.name else "unknown"
        return ToyData(name, device.address, "", "Lovense")

    async def create_toy(self, toy_data: ToyData, device: BLEDevice) -> Toy:
        """
        Connect to a single Lovense toy.

        Args:
            toy_data: ``LovenseData`` object representing the toy to connect to.
            device: ``BLEDevice`` object representing the toy to connect to.

        Returns:
            A connected, ready to use ``LovenseToy`` instance.

        Raises:
            ValidationError: The ``toy_data`` contains an invalid model name.
            ConnectionError: The BLE connection, UUID resolution, or notification setup failed.
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
                transport, toy_data.model_name, self._on_power_off, self._log.name
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
            ValueError:         If uuid_type is not 'rx' or 'tx'.
            ConnectionError:    If unable to find the UUID matching the Lovense service pattern.
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
