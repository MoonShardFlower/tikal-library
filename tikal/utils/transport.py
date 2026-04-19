"""
Provides the transport layer for toy communication. You are not meant to instantiate any of these classes yourself.
Objects of these classes are created by the ConnectionBuilder as needed.

Decouples toy protocol logic from the underlying wire technology (BLE, USB, etc.).
Concrete implementations wrap bleak or pyserial-asyncio-fast.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Callable

import serial_asyncio_fast
from bleak import BleakClient


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

    Args:
        client: An already-connected ``BleakClient``.
        tx_uuid: GATT characteristic UUID for outbound data (write).
        rx_uuid: GATT characteristic UUID for inbound data (notify).
    """

    def __init__(self, client: BleakClient, tx_uuid: str, rx_uuid: str):
        super().__init__(toy_id=client.address, name=client.name or "")
        self._client = client
        self._tx_uuid = tx_uuid
        self._rx_uuid = rx_uuid

    @property
    def is_connected(self) -> bool:
        """``True`` while the underlying link is open, else ``False``."""
        return self._client is not None and self._client.is_connected

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
            raise ConnectionError(f"Error sending data to toy at {self.toy_id}: {e!r}")

    async def disconnect(self) -> None:
        """
        Close the BLE Connection and release all resources. After this call ``is_connected`` returns ``False``.

        Raises:
            ConnectionError: If the operation fails.
        """
        if not self._client:
            return
        try:
            await self._client.disconnect()
        except Exception as e:
            raise ConnectionError(
                f"Error disconnecting from toy at {self.toy_id}: {e!r}"
            )
        self._client = None

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """
        Start receiving inbound data and invoke *callback* for each one.

        Args:
            callback: callback invoked with each inbound ``bytes`` payload.

        Raises:
            ConnectionError: If the transport is not connected or the operation fails.
        """

        # Bleak passes (handle, data: bytes); we only forward data.
        async def _bleak_cb(_, data: bytes) -> None:
            callback(data)

        try:
            await self._client.start_notify(self._rx_uuid, _bleak_cb)
        except Exception as e:
            raise ConnectionError(
                f"Error starting notifications for toy at {self.toy_id}: {e!r}"
            )


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
        self._baudrate = baudrate
        self._connected = True
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

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """Spawns a background task that reads lines from the serial port and invokes *callback* for each one."""

        # TODO: Assumes readline strategy. Other read strategy (fixed-size ``read``) might be possible.
        #  Might need multiple USB Transport layer classes to handle different USB toy protocols.
        async def _read_loop():
            try:
                while self._connected:
                    data = await self._reader.readline()
                    if data:
                        callback(data)
            except asyncio.CancelledError:
                pass

        self._read_task = asyncio.create_task(_read_loop())

    async def disconnect(self) -> None:
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)
            self._read_task = None
        self._writer.close()
        await self._writer.wait_closed()
