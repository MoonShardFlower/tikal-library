"""Low-Level API: the Lovense brand, with :class:`LovenseToy`, :class:`LovenseHandler`, and the model data."""

from .data import (
    LOVENSE_TOY_NAMES,
    LOVENSE_TOY_SPECIFICATIONS,
    MIN_SEGMENT_LENGTH,
    ROTATION_TOY_NAMES,
)
from .handler import LovenseHandler
from .toy import LovenseToy

__all__ = [
    "LovenseToy",
    "LovenseHandler",
    "LOVENSE_TOY_SPECIFICATIONS",
    "LOVENSE_TOY_NAMES",
    "ROTATION_TOY_NAMES",
    "MIN_SEGMENT_LENGTH",
]
