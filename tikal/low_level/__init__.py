"""
Low-Level API: async, direct control of toys.

- :class:`ConnectionBuilder` discovers toys (:class:`ToyData`) and connects them.
- A connected toy is a :class:`Toy`, e.g., a :class:`LovenseToy`.
- :data:`BRANDS` and :data:`LOVENSE_TOY_NAMES` list the valid model names. A toy needs one to connect.
"""

from .brand_handler import BLEBrandHandler
from .brands import BRANDS
from .brands.lovense import (
    LOVENSE_TOY_NAMES,
    LOVENSE_TOY_SPECIFICATIONS,
    MIN_SEGMENT_LENGTH,
    ROTATION_TOY_NAMES,
    LovenseToy,
)
from .brands.mock_estim import (
    MOCK_ESTIM_TOY_NAMES,
    MockConnectionBuilder,
    MockEstimToy,
)
from .connection_builder import (
    BLEConnectionBuilder,
    ConnectionBuilder,
    StaleDeviceError,
)
from .toy import Toy, UnexpectedToyResponse
from .toy_data import (
    BadModelError,
    InvalidModelError,
    ToyCommands,
    ToyData,
    ToySpecification,
    ValidationError,
    carried_over_capabilities,
)
from .transport import BleTransport, MockTransport, Transport, UsbTransport

#: Backwards-compatible alias for :class:`LovenseToy` (the class was formerly named ``Lovense``).
Lovense = LovenseToy

__all__ = [
    "ConnectionBuilder",
    "BLEConnectionBuilder",
    "StaleDeviceError",
    "BLEBrandHandler",
    "LovenseToy",
    "Lovense",
    "MockConnectionBuilder",
    "MockEstimToy",
    "MOCK_ESTIM_TOY_NAMES",
    "Toy",
    "UnexpectedToyResponse",
    "BRANDS",
    "LOVENSE_TOY_SPECIFICATIONS",
    "LOVENSE_TOY_NAMES",
    "ROTATION_TOY_NAMES",
    "MIN_SEGMENT_LENGTH",
    "BadModelError",
    "InvalidModelError",
    "ToyCommands",
    "ToyData",
    "ToySpecification",
    "ValidationError",
    "carried_over_capabilities",
    "Transport",
    "UsbTransport",
    "BleTransport",
    "MockTransport",
]
