"""
High-Level API: synchronous control of toys, with callbacks for asynchronous results.

- :class:`ToyHub` discovers and connects toys, and hands out a :class:`ToyController` per toy.
- :class:`ToyCache` remembers each toy's model name between sessions.
- The exceptions are those of the async core, which the WebSocket server shares.
"""

from .._core import (
    AddConnectionError,
    BadModelError,
    DiscoveryError,
    DiscoveryStartError,
    InvalidModelError,
    ToyAlreadyAddedError,
    ToyCache,
    ToyConnectionError,
    ToyNotConnectedError,
    UnknownToyError,
)
from .toy_controller import ToyController
from .toy_hub import ToyHub

__all__ = [
    "ToyController",
    "ToyHub",
    "ToyCache",
    "AddConnectionError",
    "BadModelError",
    "DiscoveryError",
    "DiscoveryStartError",
    "InvalidModelError",
    "ToyAlreadyAddedError",
    "ToyConnectionError",
    "ToyNotConnectedError",
    "UnknownToyError",
]
