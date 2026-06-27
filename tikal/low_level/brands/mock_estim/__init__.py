"""
MockEstimToys brand subpackage: a fictional brand used to explore the architecture without real hardware.

Unlike the BLE brands (which plug into ``BLEConnectionBuilder`` via a ``BLEBrandHandler``), MockEstimToys speaks its own
in-memory transport, so it ships its own connection builder (:class:`MockConnectionBuilder`) that the composite
``ConnectionBuilder`` holds alongside ``BLEConnectionBuilder``.
"""

from .connection_builder import MockConnectionBuilder
from .data import (
    MAX_INTENSITY,
    MIN_SEGMENT_LENGTH,
    MOCK_ESTIM_DEVICES,
    MOCK_ESTIM_TOY_NAMES,
)
from .toy import MockEstimToy

__all__ = [
    "MockConnectionBuilder",
    "MockEstimToy",
    "MOCK_ESTIM_TOY_NAMES",
    "MOCK_ESTIM_DEVICES",
    "MIN_SEGMENT_LENGTH",
    "MAX_INTENSITY",
]
