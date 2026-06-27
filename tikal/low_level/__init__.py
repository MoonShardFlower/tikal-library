from .brand_handler import BLEBrandHandler
from .brands import BRANDS
from .brands.lovense import (
    LOVENSE_TOY_NAMES,
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
    ToyData,
    ValidationError,
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
    "MockTransport",
]
