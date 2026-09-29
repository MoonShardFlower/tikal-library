"""
Private package: the async toy-management core.

Holds the toy hub and toy controllers that manage connected toys (discovery, connection, state tracking, pattern
playback, reconnection), together with the ToyCache they use. The WebSocket server is built on it.
"""

from .toy_cache import ToyCache
from .toy_controller import (
    _CONTROLLER_BY_BRAND,
    _LovenseController,
    _MockEstimController,
    _ToyController,
)
from .toy_hub import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    SafetyHoldError,
    ToyAlreadyAddedError,
    ToyConnectionError,
    ToyNotConnectedError,
    ToyStatus,
    UnavailableToyError,
    UndiscoveredToyError,
    UnknownToyError,
    _ToyHub,
)

__all__ = [
    "ToyCache",
    "_CONTROLLER_BY_BRAND",
    "_LovenseController",
    "_MockEstimController",
    "_ToyController",
    "_ToyHub",
    "AddConnectionError",
    "BadModelError",
    "DiscoveryError",
    "DiscoveryStartError",
    "InvalidModelError",
    "SafetyHoldError",
    "ToyAlreadyAddedError",
    "ToyConnectionError",
    "ToyNotConnectedError",
    "ToyStatus",
    "UnavailableToyError",
    "UndiscoveredToyError",
    "UnknownToyError",
]
