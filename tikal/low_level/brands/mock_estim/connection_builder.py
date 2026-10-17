"""Low-Level API: :class:`MockConnectionBuilder`, the connection builder of the fictional MockEstimToys brand."""

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
    Discovers and connects the fake MockEstimToys devices. Same interface as
    :class:`~tikal.low_level.BLEConnectionBuilder`.

    - Discovery finds :data:`MOCK_ESTIM_DEVICES`, minus the connected ones, with their model names.
    - Connecting creates an in-memory :class:`~tikal.low_level.MockTransport`. No prior discovery is needed.

    Args:
        on_disconnect: Accepted for the common interface. The fake devices never drop.
        on_power_off: Called with the toy id when a device reports a power-off.
        logger_name: Name of the logger. Empty string for the root logger.
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
        # Connected devices. Like real toys, they are not discoverable until they disconnect.
        self._connected: set[str] = set()
        # The continuous scan's callback, to report connects and disconnects.
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
            timeout: Ignored: the mock does not scan.

        Returns:
            New :class:`~tikal.low_level.ToyData` objects.
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
        Connect a fake device and validate its model. Failures are returned, not raised:

        - ``KeyError``: *to_connect* is not one of :data:`MOCK_ESTIM_DEVICES`.
        - :class:`~tikal.low_level.InvalidModelError`: The model name is not a MockEstimToys model.
        - :class:`~tikal.low_level.BadModelError`: The device refused the model's commands.

        Args:
            to_connect: The device, with its model name set (case-insensitive).

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
        """Make a disconnected device discoverable again, and report the new snapshot."""
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
