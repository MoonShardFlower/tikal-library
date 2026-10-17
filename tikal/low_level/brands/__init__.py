"""
Low-Level API: the brand registry.

- :data:`BRANDS`: every supported brand and its model names.
- :data:`BRAND_REGISTRATIONS`: the BLE brands. :class:`~tikal.low_level.BLEConnectionBuilder` creates one handler per
  registration.

To add a BLE brand, create ``brands/<brand>/`` (a :class:`~tikal.low_level.BLEBrandHandler` subclass, its
:class:`~tikal.low_level.Toy` subclass, and its model data) and append a :class:`BrandRegistration` here.
"""

from dataclasses import dataclass
from typing import Any, Callable, Type

from bleak import BleakClient

from ..brand_handler import BLEBrandHandler
from .lovense import LOVENSE_TOY_NAMES, LovenseHandler
from .mock_estim import MOCK_ESTIM_TOY_NAMES


@dataclass(frozen=True)
class BrandRegistration:
    """
    One BLE brand.

    Attributes:
        name: Brand name, e.g., ``"Lovense"``.
        handler_cls: Its brand handler.
        model_names: Its model names.
    """

    name: str
    handler_cls: Type[BLEBrandHandler]
    model_names: list[str]


#: All registered BLE brands. Add a new brand by appending its registration here.
BRAND_REGISTRATIONS: list[BrandRegistration] = [
    BrandRegistration("Lovense", LovenseHandler, list(LOVENSE_TOY_NAMES.keys())),
]


#: Public mapping of brand name -> list of supported model names.
#:
#: BLE brands are derived from the registry above. Brands on other transports (which are not part of the BLE handler
#: registry) are added explicitly afterwards, so ``BRANDS`` stays the single source of truth for every supported brand.
#:
#: Type:
#:     dict[str, list[str]]
#:
#: Example:
#:     ::
#:
#:         print(BRANDS["Lovense"])  # ["Solace", "Lush", ...]
BRANDS: dict[str, list[str]] = {
    reg.name: reg.model_names for reg in BRAND_REGISTRATIONS
}

#: Non-BLE brands (e.g., the fictional MockEstimToys, on its own in-memory transport).
BRANDS["MockEstimToys"] = list(MOCK_ESTIM_TOY_NAMES.keys())


def build_handlers(
    on_disconnect: Callable[[str], Any],
    on_power_off: Callable[[str], Any],
    logger_name: str,
    client_class: Type[BleakClient] = BleakClient,
) -> list[BLEBrandHandler]:
    """
    Create one handler per registered BLE brand. The arguments go to every handler, see :class:`BLEBrandHandler`.

    Args:
        on_disconnect: Called with the toy id when a toy's connection drops unexpectedly.
        on_power_off: Called with the toy id when a toy reports a power-off.
        logger_name: Name of the logger.
        client_class: BleakClient (sub)class to connect with.

    Returns:
        The handlers, in registration order.
    """
    return [
        reg.handler_cls(on_disconnect, on_power_off, logger_name, client_class)
        for reg in BRAND_REGISTRATIONS
    ]
