"""
Low-Level API: discovering and connecting toys.

- :class:`ConnectionBuilder`: the entry point. Discovers toys on every transport and connects them.
- :class:`BLEConnectionBuilder`: discovery and connection of BLE toys. Brand specifics come from the registered
  :class:`~tikal.low_level.BLEBrandHandler` subclasses.
- :class:`StaleDeviceError`: a toy discovered earlier is not available anymore.

Example::

    builder = ConnectionBuilder(
        on_disconnect=lambda toy_id: print(f"{toy_id} disconnected unexpectedly"),
        on_power_off=lambda toy_id: print(f"{toy_id} was powered off"),
        logger_name="tikal",
    )
    toys = await builder.discover_toys(timeout=5.0)
    toys[0].model_name = "Nora"
    toy = await builder.create_toy(toys[0])
"""

import asyncio
import time
from logging import getLogger
from typing import Any, Callable, Protocol, Type

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .brands import build_handlers
from .brands.mock_estim import MockConnectionBuilder
from .toy import Toy
from .toy_data import ToyData


class _TransportConnectionBuilder(Protocol):
    """
    Interface of a per-transport connection builder, as :class:`ConnectionBuilder` uses it.

    Implemented by :class:`BLEConnectionBuilder` and the mock brand's ``MockConnectionBuilder``; the methods behave
    like theirs. A brand on a new transport implements it and is appended to ``ConnectionBuilder._builders``.
    """

    async def discover_toys(self, timeout: float = ...) -> list[ToyData]: ...

    async def start_continuous(
        self, on_update: Callable[[list[ToyData] | Exception], Any] | None = ...
    ) -> None: ...

    async def stop_continuous(self) -> None: ...

    async def retrieve_continuous(self) -> list[ToyData]: ...

    def handles_toy(self, toy_data: ToyData) -> bool: ...

    async def create_toy(self, to_connect: ToyData) -> Toy | BaseException: ...


class StaleDeviceError(ConnectionError):
    """The toy was discovered earlier, but is not in the latest discovery result. Discover again."""

    pass


class BLEConnectionBuilder:
    """
    Discovers and connects BLE toys, via bleak. Brand specifics come from the registered brand handlers.

    Prefer :class:`ConnectionBuilder`, which includes it. Lovense toys are recognized by a Bluetooth name starting with
    ``LVS-``: a renamed toy is not found.

    Args:
        on_disconnect: Called with the toy id when a toy's connection drops unexpectedly. Not called after a deliberate
            disconnect.
        on_power_off: Called with the toy id when a toy reports a power-off (its button was pressed).
        logger_name: Name of the logger. Empty string for the root logger.
        scanner_class: BleakScanner (sub)class to scan with, e.g., a mock for tests.
        client_class: BleakClient (sub)class to connect with, e.g., a mock for tests.
    """

    def __init__(
        self,
        on_disconnect: Callable[[str], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str,
        scanner_class: Type[BleakScanner] = BleakScanner,
        client_class: Type[BleakClient] = BleakClient,
    ):
        self._handlers = build_handlers(
            on_disconnect, on_power_off, logger_name, client_class
        )
        self._log = getLogger(logger_name)
        self._scanner_class = scanner_class

        # Address -> device, from the latest one-shot scan and from the continuous scan. Only these can connect.
        self._ble_devices: dict[str, BLEDevice] = dict()
        # The continuous scan's snapshot: address -> (ToyData, time last seen).
        self._discovered: dict[str, tuple[ToyData, float]] = dict()
        # Every address ever seen, to tell a stale toy from an undiscovered one.
        self._all_seen_addresses: set[str] = set()

        # Continuous scan state
        self._continuous_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._continuous_exception: Exception | None = None
        self._on_update: Callable[[list[ToyData] | Exception], Any] | None = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def handles_toy(self, toy_data: ToyData) -> bool:
        """Whether a brand handler recognizes *toy_data*, i.e., whether this builder can connect it."""
        return any(h.handles_toy(toy_data) for h in self._handlers)

    async def discover_toys(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Scan for BLE toys once.

        Args:
            timeout: Scan duration in seconds. Longer scans find more toys.

        Raises:
            RuntimeError: A continuous scan is running (see :meth:`start_continuous`).
            Exception: Whatever the scanner raises, e.g., Bluetooth is off or the permission is missing.

        Returns:
            List of ``ToyData`` objects with brand‑appropriate types. This is a snapshot and not updated.

        Examples::

            toys = await builder.discover_toys(timeout=5.0)
            print(f"Found {len(toys)} Lovense devices")
            for toy in toys:
                print(f"{toy.name} at {toy.toy_id}")
        """
        if self._continuous_task and not self._continuous_task.done():
            raise RuntimeError(
                "Continuous scan in progress. Use meth: stop_continuous_scan"
            )
        self._log.info(f"Starting toy discovery for {timeout}s")
        devices = await self._scanner_class.discover(timeout=timeout)
        if self._continuous_task and not self._continuous_task.done():
            raise RuntimeError(
                "Continuous scan in progress. Use meth: stop_continuous_scan"
            )
        self._ble_devices.clear()

        toy_data_list = []
        for dev in devices:
            self._ble_devices[dev.address] = dev
            self._all_seen_addresses.add(dev.address)
            for handler in self._handlers:
                if handler.handles_device(dev):
                    td = handler.create_toy_data(dev)
                    toy_data_list.append(td)
                    break  # first handler that claims the device wins
        self._log.info(f"Discovery complete: {len(toy_data_list)} toy(s) found")
        return toy_data_list

    async def start_continuous(
        self, on_update: Callable[[list[ToyData] | Exception], Any] | None = None
    ) -> None:
        """
        Start continuous background discovery of Toys.

        Continuously scan for new toys and remove stale toys. The method is idempotent.
        Use meth: ``retrieve_continuous`` to get the current snapshot.
        Use meth: stop_continuous to stop the background scan.

        Args:
            on_update: **Optional** callback. Called with a snapshot of all discovered Toys whenever a change occurs.
                Called with an exception if the continuous scan encounters an error.
                Can be seen as a push version of :meth:`retrieve_continuous`.

        Raises:
            Exception: Any exception raised by the BLE scanner (e.g., permission errors).
        """
        if self._continuous_task and not self._continuous_task.done():
            return
        self._on_update = on_update
        if self._on_update:
            self._on_update(
                []
            )  # ensure any user cache is cleared. We'll clear our cache below

        # Reset internal state and start continuous scan
        self._continuous_exception = None
        self._stop_event = asyncio.Event()
        self._discovered.clear()
        self._continuous_task = asyncio.create_task(self._continuous_worker())
        self._log.info("Continuous discovery started")

    async def stop_continuous(self) -> None:
        """
        Stop the continuous background discovery.

        Use :meth: start_continuous to start continuous background discover. After stopping,
        :meth:`retrieve_continuous` will return an empty tuple. The method is idempotent.
        """
        if self._continuous_task is None or self._continuous_task.done():
            return
        # Stop the continuous scan and reset internal state
        assert self._stop_event is not None
        self._stop_event.set()
        await self._continuous_task
        self._stop_event = None
        self._discovered.clear()
        self._continuous_task = None
        self._log.info("Continuous discovery stopped")

    async def retrieve_continuous(self) -> list[ToyData]:
        """
        Retrieve the current snapshot of discovered Toys. Needs a continuous scan (else returns the empty list).
        Use :meth:`start_continuous` to start the continuous scan.

        If an error occurred during continuous scanning and is still relevant (=no call to :meth:`stop_continuous`
        was made afterward), the first call to this method raises that exception. Subsequent calls return an empty list.
        Note that such exceptions stop the continuous scan.

        Raises:
            Exception: The scan failed: its exception, raised once (until the next :meth:`start_continuous`.

        Returns:
            The toys currently found. Empty while no scan runs.
        """
        self._log.debug("Retrieving snapshot of discovered Toys.")
        # A pending exception is raised once
        if self._continuous_exception is not None:
            exc = self._continuous_exception
            self._continuous_exception = None
            raise exc
        # Not running or already stopped -> empty snapshot
        if self._continuous_task is None or self._continuous_task.done():
            return []
        return [td for td, _ in self._discovered.values()]

    async def create_toys(self, to_connect: list[ToyData]) -> list[Toy | BaseException]:
        """
        Connect several BLE toys concurrently. See :meth:`create_toy`.

        Args:
            to_connect: The toys, from discovery, each with its model name set.

        Returns:
            One result per toy, in input order. Each is the connected toy, or an exception (See  :meth:`create_toy`).
        """
        if not to_connect:
            return []
        coroutines = [self.create_toy(td) for td in to_connect]
        return await asyncio.gather(*coroutines)

    async def create_toy(self, to_connect: ToyData) -> Toy | BaseException:
        """
        Connect a discovered BLE toy and validate its model. A toy whose model is rejected is disconnected again.

        Failures are returned, not raised:

        - ``KeyError``: The toy was never discovered.
        - :class:`StaleDeviceError`: The toy is not in the latest discovery result.
        - ``ConnectionError``: Connecting failed, e.g., the toy is off or connected elsewhere.
        - :class:`~tikal.low_level.InvalidModelError`: The model name is not a model of the toy's brand.
        - :class:`~tikal.low_level.BadModelError`: The toy refused or did not answer the model's commands.
        - ``RuntimeError``: No brand handler handles the toy (a bug).

        Args:
            to_connect: The toy, with its model name set (case-insensitive, see :data:`~tikal.low_level.BRANDS`). It
                must be in the latest result of :meth:`discover_toys` or of the background scan.

        Returns:
            The connected toy, ready for commands, or the exception that prevented it (see above).
        """
        handler = next((h for h in self._handlers if h.handles_toy(to_connect)), None)
        if handler is None:
            self._log.error("No handler for data type: %s", type(to_connect).__name__)
            return RuntimeError(
                f"No handler for data type: {type(to_connect).__name__}"
            )
        if to_connect.toy_id not in self._all_seen_addresses:
            self._log.error(
                "Device %s was never discovered. Run meth: discover_toys first.",
                to_connect.toy_id,
            )
            return KeyError(
                f"Device {to_connect.toy_id} was never discovered. Run meth: discover_toys first."
            )
        if to_connect.toy_id not in self._ble_devices:
            self._log.error(
                "Device %s is stale. Run meth: discover_toys first.", to_connect.toy_id
            )
            return StaleDeviceError(
                f"Device {to_connect.toy_id} is stale. Run meth: discover_toys first."
            )
        device = self._ble_devices[to_connect.toy_id]
        try:
            toy = await handler.create_toy(to_connect, device)
        except Exception as e:
            return e

        # From here the toy is connected, so any failure has to close the link again.
        try:
            await toy.set_model_name(to_connect.model_name)
        except Exception as e:
            self._log.warning(
                f"Rejected model '{to_connect.model_name}' for {to_connect.toy_id}; disconnecting again."
            )
            await toy.disconnect()
            return e
        self._log.info(f"Connected to {to_connect.toy_id} as {to_connect.model_name}")
        return toy

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _continuous_worker(self) -> None:
        """
        Worker coroutine that runs the continuous scanner and the stale‑device cleanup.
        Any unhandled exception will be stored and re‑raised by :meth retrieve_continuous.
        """
        scanner = None
        cleanup_task = None
        try:
            scanner = self._scanner_class(detection_callback=self._on_device_detected)
            await scanner.start()
            cleanup_task = asyncio.create_task(self._cleanup_stale_devices())
            assert self._stop_event is not None
            await self._stop_event.wait()
        except Exception as e:
            self._continuous_exception = e
            self._log.error(f"Continuous scan failed: {type(e)}", exc_info=True)
        finally:
            if cleanup_task:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
            if scanner:
                await scanner.stop()

    async def _on_device_detected(self, device: BLEDevice, _: Any) -> None:
        """Callback invoked by BleakScanner when a BLE advertisement is received"""
        addr = device.address
        now = time.time()
        self._ble_devices[addr] = device

        # already discovered, so just refresh timestamp
        existing = self._discovered.get(addr)
        if existing:
            td, _ = existing
            self._discovered[addr] = (td, now)
            return

        # new device: identify + emit
        self._all_seen_addresses.add(addr)
        for handler in self._handlers:
            if handler.handles_device(device):
                td = handler.create_toy_data(device)
                self._discovered[addr] = (td, now)
                self._emit_snapshot()
                break

    async def _cleanup_stale_devices(self) -> None:
        """Remove devices that were not seen for more than 5 seconds."""
        stop_event = self._stop_event
        assert stop_event is not None
        while not stop_event.is_set():
            await asyncio.sleep(5.0)  # timeout for stale devices
            now = time.time()
            for addr, (_, ts) in list(self._discovered.items()):
                if now - ts > 5.0:
                    del self._discovered[addr]
                    self._log.debug(f"Removed stale device {addr}")
                    self._emit_snapshot()

    def _emit_snapshot(self) -> None:
        """Emit a snapshot of the current discovered devices to the user callback"""
        if not self._on_update:
            return
        snapshot = [td for td, _ in self._discovered.values()]
        self._on_update(snapshot)


class ConnectionBuilder:
    """
    Entry point of the Low-Level API: discovers toys on every transport and connects them.

    Combines one builder per transport: :class:`BLEConnectionBuilder`, plus the mock brand's builder with
    *mock_toys*. Discovery merges their results; connecting goes to the builder that handles the toy.

    Args:
        on_disconnect: Called with the toy id when a toy's connection drops unexpectedly. Not called after a deliberate
            disconnect.
        on_power_off: Called with the toy id when a toy reports a power-off (its button was pressed).
        logger_name: Name of the logger. Empty string for the root logger.
        bluetooth_scanner: BleakScanner (sub)class to scan with, e.g., a mock for tests.
        bluetooth_client: BleakClient (sub)class to connect with, e.g., a mock for tests.
        mock_toys: Also offer the fictional MockEstimToys brand. Not part of the public API; may be removed.
    """

    def __init__(
        self,
        on_disconnect: Callable[[str], Any],
        on_power_off: Callable[[str], Any],
        logger_name: str,
        bluetooth_scanner: Type[BleakScanner] = BleakScanner,
        bluetooth_client: Type[BleakClient] = BleakClient,
        mock_toys: bool = False,
    ):
        self._log = getLogger(logger_name)

        # One connection builder per supported transport. Append new transports here.
        self._builders: list[_TransportConnectionBuilder] = [
            BLEConnectionBuilder(
                on_disconnect,
                on_power_off,
                logger_name,
                bluetooth_scanner,
                bluetooth_client,
            ),
        ]
        if mock_toys:
            self._builders.append(
                MockConnectionBuilder(on_disconnect, on_power_off, logger_name)
            )

        # The latest snapshot of each builder's continuous scan, merged for on_update.
        self._on_update: Callable[[list[ToyData] | Exception], Any] | None = None
        self._snapshots: list[list[ToyData]] = [[] for _ in self._builders]

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def discover_toys(self, timeout: float = 10.0) -> list[ToyData]:
        """
        Scan every transport once.

        Args:
            timeout: Scan duration in seconds. Longer scans find more toys.

        Raises:
            RuntimeError: A continuous scan is running (see :meth:`start_continuous`).
            Exception: Whatever a scanner raises, e.g., Bluetooth is off or the permission is missing.

        Returns:
            The toys found. Lovense toys come without a model name.
        """
        results = await asyncio.gather(
            *(builder.discover_toys(timeout) for builder in self._builders)
        )
        return [toy_data for result in results for toy_data in result]

    async def start_continuous(
        self, on_update: Callable[[list[ToyData] | Exception], Any] | None = None
    ) -> None:
        """
        Start a background scan. Does nothing if already scanning.

        - Toys are added to the snapshot when found and removed when gone (BLE: not seen for 5 seconds).
        - Read the snapshot with :meth:`retrieve_continuous`, or get it pushed to *on_update*.
        - A failing BLE scanner, e.g., Bluetooth off, ends the BLE scan. :meth:`retrieve_continuous` raises its
          exception once.

        Args:
            on_update: Called with the merged snapshot whenever a transport's snapshot changes, first with an empty
                list. Called with the exception if a transport reports one.
        """
        self._on_update = on_update
        self._snapshots = [[] for _ in self._builders]
        for index, builder in enumerate(self._builders):
            await builder.start_continuous(self._make_continuous_callback(index))
        self._log.debug(
            "Continuous discovery started across %d transport(s)", len(self._builders)
        )

    async def stop_continuous(self) -> None:
        """Stop the background scan. Does nothing if not scanning."""
        for builder in self._builders:
            await builder.stop_continuous()
        self._snapshots = [[] for _ in self._builders]
        self._on_update = None

    async def retrieve_continuous(self) -> list[ToyData]:
        """
        The merged snapshot of the background scan.

        Raises:
            Exception: A transport's scan failed: its exception, raised once.

        Returns:
            The toys currently found. Empty while no scan runs.
        """
        result: list[ToyData] = []
        for builder in self._builders:
            result.extend(await builder.retrieve_continuous())
        return result

    async def create_toys(self, to_connect: list[ToyData]) -> list[Toy | BaseException]:
        """
        Create Toy instances from discovery data, routing each to the transport that owns it.

        See :meth:`BLEConnectionBuilder.create_toys` for arguments and the per-element result/exception types.

        Returns:
            List where each element is a connected ``Toy`` or a ``BaseException``. Order matches the input list.
        """
        if not to_connect:
            return []
        return await asyncio.gather(*(self.create_toy(td) for td in to_connect))

    async def create_toy(self, to_connect: ToyData) -> Toy | BaseException:
        """
        Connect a discovered toy and validate its model. A toy whose model is rejected is disconnected again.

        Failures are returned, not raised:

        - ``KeyError``: The toy was never discovered.
        - :class:`StaleDeviceError`: The toy is not in the latest discovery result.
        - ``ConnectionError``: Connecting failed, e.g., the toy is off or connected elsewhere.
        - :class:`~tikal.low_level.InvalidModelError`: The model name is not a model of the toy's brand.
        - :class:`~tikal.low_level.BadModelError`: The toy refused or did not answer the model's commands.
        - ``RuntimeError``: No builder handles the toy (a bug).

        Args:
            to_connect: The toy, with its model name set (case-insensitive, see :data:`~tikal.low_level.BRANDS`). A BLE
                toy must be in the latest result of :meth:`discover_toys` or of the background scan.

        Returns:
            The connected toy, ready for commands, or the exception that prevented it (see above).
        """
        builder = next((b for b in self._builders if b.handles_toy(to_connect)), None)
        if builder is None:
            self._log.error(
                "No connection builder for data type: %s", type(to_connect).__name__
            )
            return RuntimeError(
                f"No connection builder for data type: {type(to_connect).__name__}"
            )
        return await builder.create_toy(to_connect)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _make_continuous_callback(
        self, index: int
    ) -> Callable[[list[ToyData] | Exception], None]:
        """The callback for builder *index*: records its snapshot and passes the merged one to *on_update*."""

        def callback(update: list[ToyData] | Exception) -> None:
            if self._on_update is None:
                return
            if isinstance(update, Exception):
                self._on_update(update)
                return
            self._snapshots[index] = update
            merged = [toy_data for snapshot in self._snapshots for toy_data in snapshot]
            self._on_update(merged)

        return callback
