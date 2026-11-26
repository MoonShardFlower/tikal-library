"""Low-Level API: :class:`LovenseHandler`, the BLE brand handler of Lovense toys."""

import asyncio
import contextlib

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ...brand_handler import BLEBrandHandler
from ...toy import Toy
from ...toy_data import InvalidModelError, ToyData, ValidationError
from ...transport import BleTransport
from .data import LOVENSE_TOY_NAMES, LOVENSE_UUID_PREFIXES
from .toy import LovenseToy

#: The end of the Lovense service UUID. The first group differs per model: its first four hex digits tell the model
#: (see :data:`~tikal.low_level.brands.lovense.data.LOVENSE_UUID_PREFIXES`).
_LOVENSE_SERVICE_SUFFIX = "-4bd4-bbd5-a6920e4c5653"

#: The generic serial services some older toys use instead -> their ``(tx, rx)`` characteristics.
_GENERIC_SERVICES: dict[str, tuple[str, str]] = {
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e": (
        "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
        "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
    ),
    "0000fff0-0000-1000-8000-00805f9b34fb": (
        "0000fff2-0000-1000-8000-00805f9b34fb",
        "0000fff1-0000-1000-8000-00805f9b34fb",
    ),
}


def _is_lovense_service(uuid: str) -> bool:
    """Whether *uuid* (lower case) has the form of a Lovense service UUID."""
    return len(uuid) == 36 and uuid.endswith(_LOVENSE_SERVICE_SUFFIX)


def _characteristics(service_uuid: str) -> tuple[str, str] | None:
    """
    The ``(tx, rx)`` characteristic UUIDs a toy's service should have, lower case.

    A Lovense service ``xxxxxxx1-...`` has its tx characteristic at ``xxxxxxx2-...`` and its rx at ``xxxxxxx3-...``.

    Args:
        service_uuid: Any service UUID.

    Returns:
        ``(tx, rx)``, or None if *service_uuid* is neither a Lovense service nor one of the generic ones.
    """
    uuid = service_uuid.lower()
    if uuid in _GENERIC_SERVICES:
        return _GENERIC_SERVICES[uuid]
    if not _is_lovense_service(uuid):
        return None
    try:
        first_group = int(uuid[:8], 16)
    except ValueError:
        return None
    rest = uuid[8:]
    return f"{first_group + 1:08x}{rest}", f"{first_group + 2:08x}{rest}"


def _advertised_model(advertisement: AdvertisementData | None) -> str:
    """The model of the first advertised Lovense service UUID whose start is known (see LOVENSE_UUID_PREFIXES), or ""."""
    if advertisement is None:
        return ""
    for uuid in advertisement.service_uuids:
        uuid = uuid.lower()
        if _is_lovense_service(uuid) and uuid[:4] in LOVENSE_UUID_PREFIXES:
            return LOVENSE_UUID_PREFIXES[uuid[:4]]
    return ""


class LovenseHandler(BLEBrandHandler):
    """
    Brand handler for Lovense BLE toys.

    Encapsulates all Lovense-specific logic: identification, UUID resolution, connection, and notification setup.
    Recognizes Lovense toys by their Bluetooth name (``LVS-...``) or by an advertised Lovense service UUID.
    """

    @staticmethod
    def handles_device(
        device: BLEDevice, advertisement: AdvertisementData | None = None
    ) -> bool:
        """Whether the device is a Lovense toy: its name starts with ``LVS-``, or it advertises a Lovense service."""
        if device.name and device.name.startswith("LVS-"):
            return True
        return advertisement is not None and any(
            _is_lovense_service(uuid.lower()) for uuid in advertisement.service_uuids
        )

    @staticmethod
    def handles_toy(toy_data: ToyData) -> bool:
        """Return ``True`` if the given ``ToyData`` represents a Lovense toy."""
        return toy_data.brand == "Lovense"

    @staticmethod
    def create_toy_data(
        device: BLEDevice, advertisement: AdvertisementData | None = None
    ) -> ToyData:
        """
        The :class:`~tikal.low_level.ToyData` of a Lovense toy.

        The model name is filled out if the UUID is known, else empty.
        (see :data:`~tikal.low_level.brands.lovense.data.LOVENSE_UUID_PREFIXES`)
        """
        name = device.name or (advertisement and advertisement.local_name) or "unknown"
        return ToyData(
            name, device.address, _advertised_model(advertisement), "Lovense"
        )

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
        except BaseException:
            # Canceled: the link may be open already. Close it, and pass the cancellation on.
            with contextlib.suppress(Exception):
                await asyncio.shield(transport.disconnect())
            raise
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

    @staticmethod
    async def _find_uuid_by_type(client: BleakClient, uuid_type: str) -> str:
        """
        Find the TX or RX characteristic in the Lovense service, or else in one of the generic serial services.

        Args:
            client: The connected client.
            uuid_type: ``"tx"`` (commands) or ``"rx"`` (replies).

        Raises:
            ValueError: *uuid_type* is neither.
            ConnectionError: No matching characteristic, e.g., the device is not a Lovense toy.

        Returns:
            The UUID, upper case.
        """
        if uuid_type not in ("tx", "rx"):
            raise ValueError(f"Invalid UUID type: {uuid_type}")
        index = 0 if uuid_type == "tx" else 1
        # A Lovense service wins over a generic one. sorted() keeps the order otherwise.
        services = sorted(
            client.services, key=lambda s: str(s.uuid).lower() in _GENERIC_SERVICES
        )
        for service in services:
            characteristics = _characteristics(str(service.uuid))
            if characteristics is None:
                continue
            target_uuid = characteristics[index]
            if any(str(c.uuid).lower() == target_uuid for c in service.characteristics):
                return target_uuid.upper()
        raise ConnectionError(f"Unable to find {uuid_type}-UUID for {client.address}")
