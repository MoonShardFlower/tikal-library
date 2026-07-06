"""
Part of the Low-Level API: the MockEstimToys connection builder.

``MockConnectionBuilder`` is the per-transport connection builder for the fictional ``MockEstimToys`` brand. It plays
the same role for mock toys that ``BLEConnectionBuilder`` plays for BLE toys, and exposes the same interface so that the
composite :class:`tikal.low_level.connection_builder.ConnectionBuilder` can hold both side by side.

Because the toys are fake, "discovery" simply returns a fixed set of devices (see :data:`MOCK_ESTIM_DEVICES`) and
"connecting" wires up a :class:`MockTransport` / :class:`MockEstimToy` pair in memory.
"""

import asyncio
from logging import getLogger
from typing import Any, Callable

from ...toy import Toy
from ...toy_data import InvalidModelError, ToyData
from ...transport import MockTransport
from .data import MOCK_ESTIM_DEVICES, MOCK_ESTIM_TOY_NAMES
from .toy import MockEstimToy

_BRAND = "MockEstimToys"


class MockConnectionBuilder:
    """
    Connection builder for the fictional ``MockEstimToys`` brand.

    Discovers a fixed set of fake devices and connects them via in-memory transports. Implements the same surface the
    composite ``ConnectionBuilder`` requires of every per-transport builder: ``discover_toys``, ``start_continuous``,
    ``stop_continuous``, ``retrieve_continuous``, ``handles_toy``, and ``create_toy``.

    Args:
        on_disconnect: Callback invoked when a toy disconnects unexpectedly. Receives the toy id. Unused by the mock
            (the fake devices never drop on their own) but accepted for interface symmetry.
        on_power_off: Callback invoked when a toy reports a power-off. Receives the toy id.
        logger_name: Name of the logger to use. Use empty string for root logger.
    """

    def __init__(
        self,
        on_disconnect: Callable[[str], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str,
    ):
        self._on_disconnect = on_disconnect
        self._on_power_off = on_power_off
        self._log = getLogger(logger_name)
        self._scanning = False
        # toy_ids of currently connected toys. A connected device stops "advertising" and is excluded from discovery until it disconnects again.
        self._connected: set[str] = set()
        # The active continuous-scan callback, kept so we can re-emit when connection state changes.
        self._on_update: Callable[[list[ToyData] | Exception], Any] | None = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def handles_toy(self, toy_data: ToyData) -> bool:
        """Return ``True`` if the given ``ToyData`` belongs to the MockEstimToys brand."""
        return toy_data.brand == _BRAND

    async def discover_toys(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Return the fixed set of fake MockEstimToys devices.

        Args:
            timeout: Ignored; there is no real scan. Present for interface compatibility.

        Returns:
            A fresh list of ``ToyData`` (model name pre-filled, since the fake devices self-identify).
        """
        await asyncio.sleep(0)  # yield, mirroring a real async scan
        self._log.info(
            "MockEstimToys discovery: returning %d fake toy(s)", len(MOCK_ESTIM_DEVICES)
        )
        return self._discovered_toy_data()

    async def start_continuous(
        self, on_update: Callable[[list[ToyData] | Exception], Any] | None = None
    ) -> None:
        """
        Start "continuous" discovery. The fake devices are reported immediately and then never change.

        Args:
            on_update: Optional callback. Called once with an empty list (to clear any cache) and then with the fake
                devices, mirroring the contract of the real builders.
        """
        self._scanning = True
        self._on_update = on_update
        if on_update:
            on_update([])  # mirror real builders clearing their cache on start
            on_update(self._discovered_toy_data())

    async def stop_continuous(self) -> None:
        """Stop "continuous" discovery. Idempotent."""
        self._scanning = False
        self._on_update = None

    async def retrieve_continuous(self) -> list[ToyData]:
        """Return the current snapshot: the fake devices while scanning, otherwise an empty list."""
        return self._discovered_toy_data() if self._scanning else []

    async def create_toy(self, to_connect: ToyData) -> Toy | BaseException:
        """
        Connect to a fake MockEstimToys device.

        Returns:
            A connected ``MockEstimToy`` on success, or a ``BaseException`` on failure:
            - ``KeyError``: the toy id is not one of the known fake devices.
            - ``InvalidModelError``: the model name is not valid for this brand.
            - ``ConnectionError``: connection or notification setup failed.
        """
        known = any(d.toy_id == to_connect.toy_id for d in MOCK_ESTIM_DEVICES)
        if not known:
            self._log.error("Unknown MockEstimToys device: %s", to_connect.toy_id)
            return KeyError(
                f"Device {to_connect.toy_id} is not a known MockEstimToys device."
            )
        if to_connect.model_name.title() not in MOCK_ESTIM_TOY_NAMES:
            self._log.error(
                "Invalid model '%s' for MockEstimToys device %s",
                to_connect.model_name,
                to_connect.toy_id,
            )
            return InvalidModelError(
                f"Invalid model name '{to_connect.model_name}' for MockEstimToys at '{to_connect.toy_id}'."
            )

        transport = MockTransport(
            to_connect.toy_id, to_connect.name, on_disconnect=self._on_toy_disconnect
        )
        try:
            toy = MockEstimToy(
                transport,
                to_connect.model_name.title(),
                self._on_power_off,
                self._log.name,
            )
            await toy.start_notifications()
            await toy.set_model_name(to_connect.model_name)
            # Hide the now-connected toy from discovery and re-emit so any active scan updates.
            self._connected.add(to_connect.toy_id)
            self._emit()
            self._log.info(
                "Connected to %s as %s", to_connect.toy_id, to_connect.model_name
            )
            return toy
        except Exception as e:
            await transport.disconnect()
            return e

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _on_toy_disconnect(self, toy_id: str) -> None:
        """A connected toy disconnected: make it discoverable again and re-emit the snapshot."""
        self._connected.discard(toy_id)
        self._emit()

    def _emit(self) -> None:
        """Push the current connection-filtered snapshot to the continuous-scan callback, if scanning."""
        if self._scanning and self._on_update is not None:
            self._on_update(self._discovered_toy_data())

    def _discovered_toy_data(self) -> list[ToyData]:
        """Build a fresh ``ToyData`` list for every fake device that is not currently connected."""
        return [
            ToyData(device.name, device.toy_id, device.model_name, _BRAND)
            for device in MOCK_ESTIM_DEVICES
            if device.toy_id not in self._connected
        ]
