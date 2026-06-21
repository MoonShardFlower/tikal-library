from .brand_handler import BLEBrandHandler
from .brands import BRANDS
from .brands.lovense import (
    LOVENSE_TOY_NAMES,
    MIN_SEGMENT_LENGTH,
    ROTATION_TOY_NAMES,
    LovenseToy,
)
from .connection_builder import BLEConnectionBuilder, StaleDeviceError
from .toy import Toy, UnexpectedToyResponse
from .toy_data import (
    BadModelError,
    InvalidModelError,
    ToyData,
    ValidationError,
)
from .transport import BleTransport, Transport, UsbTransport

#: Backwards-compatible alias for :class:`LovenseToy` (the class was formerly named ``Lovense``).
Lovense = LovenseToy

__all__ = [
    "BLEConnectionBuilder",
    "StaleDeviceError",
    "BLEBrandHandler",
    "LovenseToy",
    "Lovense",
    "Toy",
    "UnexpectedToyResponse",
    "BRANDS",
    "LOVENSE_TOY_NAMES",
    "ROTATION_TOY_NAMES",
    "MIN_SEGMENT_LENGTH",
    "BadModelError",
    "InvalidModelError",
    "ToyData",
    "ValidationError",
    "Transport",
    "UsbTransport",
    "BleTransport",
]
