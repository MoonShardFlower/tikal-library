"""
MockEstimToys: a fictional brand on an in-memory transport, for working without hardware. Not part of the public API.

Brings its own connection builder, :class:`MockConnectionBuilder`, which
:class:`~tikal.low_level.ConnectionBuilder` includes when created with ``mock_toys=True``.
"""

from .connection_builder import MockConnectionBuilder
from .data import (
    MAX_INTENSITY,
    MIN_SEGMENT_LENGTH,
    MOCK_ESTIM_DEVICES,
    MOCK_ESTIM_TOY_NAMES,
    MOCK_ESTIM_TOY_SPECIFICATIONS,
)
from .toy import MockEstimToy

__all__ = [
    "MockConnectionBuilder",
    "MockEstimToy",
    "MOCK_ESTIM_TOY_SPECIFICATIONS",
    "MOCK_ESTIM_TOY_NAMES",
    "MOCK_ESTIM_DEVICES",
    "MIN_SEGMENT_LENGTH",
    "MAX_INTENSITY",
]
