"""
Provides the transport layer for toy communication. You are not meant to instantiate any of these classes yourself.
Objects of these classes are created by the ConnectionBuilder as needed.

Decouples toy protocol logic from the underlying wire technology (BLE, USB, etc.).
Concrete implementations wrap bleak or pyserial-asyncio-fast.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Type

import serial_asyncio_fast
from bleak import BleakClient, BLEDevice


class Transport(ABC):
    """
    Abstract transport layer for a connected toy.

    Wraps the raw I/O operations so that ``Toy`` subclasses can send commands and receive notifications without knowing
    whether the underlying link is BLE, USB, or anything else.

    A ``Transport`` is always created in a *connected* state. Connection setup is the responsibility of the ``ConnectionBuilder``.

    Args:
        toy_id: Unique identifier e.g., BLE: Bluetooth address string, USB: serial port path.
        name:  Human-readable device name e.g., BLE: advertised name.  USB: the literal string ``"usb-<serial port path>"``.
    """

    def __init__(self, toy_id: str, name: str):
        self._toy_id = toy_id
        self._name = name
        self._intentional_disconnect = False

    @property
    def toy_id(self) -> str:
        """returns a unique identifier (BLE address or USB port)."""
        return self._toy_id

    @property
    def name(self) -> str:
        """Returns a Human-readable device name."""
        return self._name

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """``True`` while the underlying link is open, else ``False``."""
        raise NotImplementedError

    @abstractmethod
    async def reconnect(self) -> None:
        """Attempts to reconnect to the toy. Does nothing if already connected."""
        raise NotImplementedError

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """
        Write raw bytes to the device.

        Args:
            data: Bytes to send (the ``Toy`` layer is responsible for framing).

        Raises:
            ConnectionError: If the transport is not connected or the operation fails.
        """
        raise NotImplementedError

    @abstractmethod
    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """
        Start receiving inbound data and invoke *callback* for each one.

        Args:
            callback: callback invoked with each inbound ``bytes`` payload.

        Raises:
            ConnectionError: If the transport is not connected or the operation fails.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Close the underlying connection and release all resources. After this call ``is_connected`` must return ``False``.

        Raises:
            ConnectionError: If the operation fails.
        """
        raise NotImplementedError


class BleTransport(Transport):
    """
    ``Transport`` implementation for Bluetooth Low Energy via bleak.

    Call :meth:`connect` after construction!
    It establishes the BLE connection and resolves TX / RX UUIDs, via the supplied *uuid_resolver*

    Args:
        device: The ``BLEDevice`` to connect to.
        uuid_resolver: Async callable that receives the connected ``BleakClient`` and returns ``(tx_uuid, rx_uuid)``.
            Invoked once inside :meth:`connect` while the link is already open, so it can inspect the GATT services.
        on_disconnect: Optional callback is invoked with this ``BleTransport`` instance when the underlying BLE
            connection drops unexpectedly. Not invoked for intentional disconnects via :meth:`disconnect`.
        client_class: ``BleakClient`` subclass to use. Defaults to ``BleakClient``; override for testing.
    """

    def __init__(
        self,
        device: BLEDevice,
        uuid_resolver: Callable[[BleakClient], Awaitable[tuple[str, str]]],
        on_disconnect: Callable[["BleTransport"], None] | None = None,
        client_class: Type[BleakClient] = BleakClient,
    ):
        super().__init__(toy_id=device.address, name=device.name or "")
        self._uuid_resolver = uuid_resolver
        self._on_disconnect = on_disconnect
        self._tx_uuid: str | None = None
        self._rx_uuid: str | None = None

        def _bleak_disconnect_cb(_: BleakClient) -> None:
            if not self._intentional_disconnect and on_disconnect:
                on_disconnect(self)

        self._client = client_class(device, disconnected_callback=_bleak_disconnect_cb)

    async def connect(self) -> None:
        """
        Do not call. The ``ConnectionBuilder`` will call this for you.

        Opens the BLE connection and resolves TX / RX UUIDs.
        Must be called exactly once after construction and before any other method.

        Raises:
            ConnectionError: If the BLE connection fails or if UUID resolution raises.
        """
        try:
            await self._client.connect()
            self._tx_uuid, self._rx_uuid = await self._uuid_resolver(self._client)
        except Exception as e:
            raise ConnectionError(
                f"Error connecting to toy at {self.toy_id}: {e!r}"
            ) from e

    @property
    def is_connected(self) -> bool:
        """``True`` while the underlying link is open, else ``False``."""
        return self._client is not None and self._client.is_connected

    async def reconnect(self) -> None:
        """
        Attempts to reconnect to the toy. Does nothing if already connected.
        Use only after an unintended disconnect (``ConnectionError``).
        If you disconnected via :meth:`disconnect`, use the ``ConnectionBuilder`` instead.

        Raises:
            RuntimeError: If called after an intentional disconnect.
            ConnectionError: If the reconnection attempt fails.
        """
        if self.is_connected:
            return
        if self._intentional_disconnect:
            raise RuntimeError("Cannot reconnect after intentional disconnect")
        try:
            await self._client.connect()
        except Exception as e:
            raise ConnectionError(
                f"Error connecting to toy at {self.toy_id}: {e!r}"
            ) from e

    async def send(self, data: bytes) -> None:
        """
        Send encoded data to the toy.

        Args:
            data: Encoded data to send to the toy.

        Raises:
            ConnectionError: If the transport is not connected or the operation fails.
        """
        try:
            await self._client.write_gatt_char(self._tx_uuid, data, response=False)
        except Exception as e:
            raise ConnectionError(
                f"Error sending data to toy at {self.toy_id}: {e!r}"
            ) from e

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """
        Start receiving inbound data and invoke *callback* for each one.

        Args:
            callback: callback invoked with each inbound ``bytes`` payload.

        Raises:
            ConnectionError: If the transport is not connected or the operation fails.
        """

        # Bleak passes (handle, data: bytes); we only forward data.
        def _bleak_cb(_, data: bytes) -> None:
            callback(data)

        try:
            await self._client.start_notify(self._rx_uuid, _bleak_cb)
        except Exception as e:
            raise ConnectionError(
                f"Error starting notifications for toy at {self.toy_id}: {e!r}"
            ) from e

    async def disconnect(self) -> None:
        """
        Close the BLE connection and release all resources. After this call ``is_connected`` returns ``False``.

        Raises:
            ConnectionError: If the operation fails. Connection is still regarded as closed if this exception is raised.
        """
        if not self._client:
            return
        self._intentional_disconnect = True
        try:
            await self._client.disconnect()
        except Exception as e:
            raise ConnectionError(
                f"Error disconnecting from toy at {self.toy_id}: {e!r}"
            )
        finally:
            self._client = None


class UsbTransport(Transport):
    """
    ``Transport`` implementation for USB serial via pyserial-asyncio-fast.

    Args:
        port: Serial port path, e.g. ``"/dev/ttyUSB0"`` or ``"COM3"``.
        baudrate: Baud rate for the serial connection.
        reader: ``asyncio.StreamReader`` from ``serial_asyncio_fast``.
        writer: ``asyncio.StreamWriter`` from ``serial_asyncio_fast``.
    """

    def __init__(
        self,
        port: str,
        baudrate: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        super().__init__(toy_id=port, name="usb")
        self._reader = reader
        self._writer = writer
        self._port = port
        self._baudrate = baudrate
        self._connected = True
        self._notify_callback: Callable | None = None
        self._read_task: asyncio.Task | None = None

    @classmethod
    async def connect(cls, port: str, baudrate: int) -> "UsbTransport":
        """Open the serial port and return a connected ``UsbTransport``."""
        reader, writer = await serial_asyncio_fast.open_serial_connection(
            url=port, baudrate=baudrate
        )
        return cls(port, baudrate, reader, writer)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionError(f"USB transport {self._toy_id} is not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def reconnect(self) -> None:
        """
        Attempts to reconnect to the toy. Does nothing if already connected.
        Use only after an unintended disconnect (``ConnectionError``).
        If you disconnected via :meth:`disconnect`, use the ``ConnectionBuilder`` instead.

        Raises:
            RuntimeError: If called after an intentional disconnect.
            ConnectionError: If the reconnection attempt fails.
        """
        if self.is_connected:
            return
        if self._intentional_disconnect:
            raise RuntimeError("Cannot reconnect after intentional disconnect")
        try:
            if not self._notify_callback:
                raise RuntimeError("Cannot reconnect without a notification callback")
            self._reader, self._writer = (
                await serial_asyncio_fast.open_serial_connection(
                    url=self._port, baudrate=self._baudrate
                )
            )
            await self.start_notify(self._notify_callback)
        except Exception as e:
            raise ConnectionError(f"Error connecting to toy at {self.toy_id}: {e!r}")

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """
        Spawns a background task that reads lines from the serial port and invokes *callback* for each one.

        Args:
            callback: callback invoked with each inbound ``bytes`` payload.

        Raises:
            ConnectionError: If the transport is not connected or the operation fails.
        """

        # TODO: Assumes readline strategy. Other read strategies (fixed-size ``read``) might be possible.
        #  Might need multiple USB Transport layer classes to handle different USB toy protocols.
        async def _read_loop():
            try:
                while self._connected:
                    data = await self._reader.readline()
                    if data:
                        callback(data)
            except asyncio.CancelledError:
                pass

        self._notify_callback = callback
        self._read_task = asyncio.create_task(_read_loop())

    async def disconnect(self) -> None:
        """
        Close the serial connection and release all resources. After this call ``is_connected`` returns ``False``.

        Raises:
            ConnectionError: If the operation fails. The connection is still regarded as closed if this exception is raised.
        """
        self._connected = False
        try:
            if self._read_task:
                self._read_task.cancel()
                await asyncio.gather(self._read_task, return_exceptions=True)
                self._read_task = None
            self._writer.close()
            await self._writer.wait_closed()
        except Exception as e:
            raise ConnectionError(
                f"Error disconnecting from toy at {self.toy_id}: {e!r}"
            )
        finally:
            self._reader = None
            self._writer = None
