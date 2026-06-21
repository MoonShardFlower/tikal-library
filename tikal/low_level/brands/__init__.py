"""
Brand registry for the Low-Level API.

Each supported BLE brand lives in its own subpackage (e.g. ``brands/lovense``) and is registered here. ``BLEConnectionBuilder``
builds its handler list from this registry, and the public ``BRANDS`` mapping (brand -> supported model names) is derived from it.

Adding a new BLE brand:
    1. Create a subpackage ``brands/<brand>/`` with a ``BLEBrandHandler`` subclass (and its toy class + model data).
    2. Append one ``BrandRegistration`` entry to ``BRAND_REGISTRATIONS`` below.
"""

from dataclasses import dataclass
from typing import Any, Callable, Type

from bleak import BleakClient

from ..brand_handler import BLEBrandHandler
from .lovense import LOVENSE_TOY_NAMES, LovenseHandler


@dataclass(frozen=True)
class BrandRegistration:
    """One registered BLE brand: its display name, handler class, and supported model names."""

    name: str
    handler_cls: Type[BLEBrandHandler]
    model_names: list[str]


#: All registered BLE brands. Add a new brand by appending its registration here.
BRAND_REGISTRATIONS: list[BrandRegistration] = [
    BrandRegistration("Lovense", LovenseHandler, list(LOVENSE_TOY_NAMES.keys())),
]


#: Mapping of brand name -> list of supported model names, derived from the registry.
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


def build_handlers(
    on_disconnect: Callable[[str], Any],
    on_power_off: Callable[[str], Any],
    logger_name: str,
    client_class: Type[BleakClient] = BleakClient,
) -> list[BLEBrandHandler]:
    """
    Instantiate one handler per registered brand.

    Called by ``BLEConnectionBuilder`` to populate its handler list.
    Every registered handler is constructed with the same signature (see :class:`BLEBrandHandler`).
    """
    return [
        reg.handler_cls(on_disconnect, on_power_off, logger_name, client_class)
        for reg in BRAND_REGISTRATIONS
    ]
