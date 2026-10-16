"""
Private package: the async core that the High-Level API and the WebSocket server are built on.

- ``_ToyHub`` manages every toy: discovery, connection, state, pattern playback, reconnection.
- ``_ToyController`` (one per toy, per brand) holds a toy's state and plays its pattern.
- ``ToyCache`` remembers each toy's model name.
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
    ToyAlreadyAddedError,
    ToyBlockedError,
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
    "ToyAlreadyAddedError",
    "ToyBlockedError",
    "ToyConnectionError",
    "ToyNotConnectedError",
    "ToyStatus",
    "UnavailableToyError",
    "UndiscoveredToyError",
    "UnknownToyError",
]
