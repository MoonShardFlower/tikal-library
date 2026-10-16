"""
TIKAL ('Toys I Know And Love'): control Lovense toys over Bluetooth, without the Lovense apps.

- :mod:`tikal.high_level`: synchronous API. :class:`~tikal.high_level.ToyHub` connects toys, a
  :class:`~tikal.high_level.ToyController` controls one.
- :mod:`tikal.low_level`: async API. :class:`~tikal.low_level.ConnectionBuilder` connects toys, a
  :class:`~tikal.low_level.Toy` controls one.
- :mod:`tikal.websocket`: a WebSocket server, for clients in any language.
- :mod:`tikal.funscript`: converts funscripts into patterns.
"""

__version__ = "2.0.0"
