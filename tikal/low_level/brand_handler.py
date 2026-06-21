"""
Part of the Low-Level API: the abstract contract every BLE toy brand implements.

A brand handler encapsulates everything brand-specific about BLE discovery and connection:
- Recognizing the brand from a BLE advertisement
- Creating the appropriate ``ToyData``
- Establishing a connection and returning a ready-to-use ``Toy`` instance

To add a new brand, create a subpackage under ``tikal/low_level/brands/`` with a ``BLEBrandHandler`` subclass and
register it in ``tikal/low_level/brands/__init__.py``. The ``BLEConnectionBuilder`` instantiates every registered
handler via the standard constructor defined here, so a new handler only needs to implement the four abstract methods.
"""

from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any, Callable, Type

from bleak import BleakClient, BLEDevice

from .toy import Toy
from .toy_data import ToyData


class BLEBrandHandler(ABC):
    """
    Abstract interface for handling a specific brand of BLE toy.

    Each concrete implementation provides the logic for:
    - Recognizing the brand from BLE advertisements
    - Creating the appropriate ``ToyData``
    - Establishing a connection and returning a ready-to-use ``Toy`` instance

    Subclasses are instantiated by the ``BLEConnectionBuilder`` through the constructor below.
    A subclass only needs to  implement the four abstract methods.

    Args:
        on_disconnect: Callback called when a toy disconnects unexpectedly. Receives the toy's toy_id.
        on_power_off: Callback called when the user powers off a toy via its physical button. Receives the toy id (address).
        logger_name: Name for the logger used by this handler. Defaults to 'tikal'.
        client_class: BLE client class to use. Defaults to BleakClient. Can be overridden for testing.
    """

    def __init__(
        self,
        on_disconnect: Callable[[str], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str = "tikal",
        client_class: Type[BleakClient] = BleakClient,
    ):
        self._on_disconnect = on_disconnect
        self._on_power_off = on_power_off
        self._log = getLogger(logger_name)
        self._client_class = client_class

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
        """Create a brand-specific ``ToyData`` object from a BLE device."""
        raise NotImplementedError

    @abstractmethod
    async def create_toy(self, toy_data: ToyData, device: BLEDevice) -> Toy:
        """
        Connect to the toy described by ``toy_data`` using the provided ``BLEDevice``.

        This method handles brand-specific connection steps (UUID resolution, notification setup, etc.) and returns a connected ``Toy`` instance.

        Raises:
            ValidationError: The ``toy_data`` contains an invalid model name.
            ConnectionError: The BLE connection, UUID resolution, or notification setup failed.
        """
        raise NotImplementedError
