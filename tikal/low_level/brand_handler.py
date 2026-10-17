"""
Low-Level API: :class:`BLEBrandHandler`, what a BLE brand implements.

A handler recognizes its brand's devices, creates their :class:`~tikal.low_level.ToyData`, and connects them. To add a
BLE brand, subclass it in ``tikal/low_level/brands/<brand>/`` and register it in ``tikal/low_level/brands/__init__.py``.
:class:`~tikal.low_level.BLEConnectionBuilder` then uses it.
"""

from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any, Callable, Type

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .toy import Toy
from .toy_data import ToyData


class BLEBrandHandler(ABC):
    """
    The brand-specific part of BLE discovery and connection. Subclasses implement the four abstract methods.

    The constructor signature is fixed: :class:`~tikal.low_level.BLEConnectionBuilder` creates every registered
    handler with it.

    Args:
        on_disconnect: Called with the toy id when a toy's connection drops unexpectedly.
        on_power_off: Called with the toy id when a toy reports a power-off (its button was pressed).
        logger_name: Name of the logger.
        client_class: BleakClient (sub)class to connect with, e.g., a mock for tests.
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
        """The :class:`~tikal.low_level.ToyData` of a device for which :meth:`handles_device` is true."""
        raise NotImplementedError

    @abstractmethod
    async def create_toy(self, toy_data: ToyData, device: BLEDevice) -> Toy:
        """
        Connect to the toy described by ``toy_data`` using the provided ``BLEDevice``.

        The model's commands are not validated here: the connection builder does that afterward.

        Args:
            toy_data: The toy, with its model name set.
            device: Its BLE device.

        Raises:
            ~tikal.low_level.InvalidModelError: The model name is not a model of the brand.
            ConnectionError: Connecting failed.

        Returns:
            The connected toy.
        """
        raise NotImplementedError
