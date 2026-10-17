"""
Part of the low-level API: Provides the transport layer for toy communication.

- :class:`Transport`: the interface.
- :class:`BleTransport`: Bluetooth Low Energy, via bleak.
- :class:`UsbTransport`: USB serial, via pyserial-asyncio-fast.
- :class:`MockTransport`: an in-memory device of the fictional MockEstimToys brand.

You are not meant to instantiate any of these classes yourself.
Objects of these classes are created by the ConnectionBuilder as needed.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Type

import serial_asyncio_fast
from bleak import BleakClient
from bleak.backends.device import BLEDevice


class Transport(ABC):
    """
    Byte-level link to one toy, so a :class:`~tikal.low_level.Toy` works without knowing the wire technology.

    Connected before a toy uses it.

    Args:
        toy_id: Unique identifier e.g., BLE: Bluetooth address string, USB: serial port path.
        name:  Device name e.g., BLE: advertised name.  USB: the literal string ``"usb-<serial port path>"``.
    """

    def __init__(self, toy_id: str, name: str):
        self._toy_id = toy_id
        self._name = name
        self._intentional_disconnect = False

    @property
    def toy_id(self) -> str:
        """Unique identifier, e.g., the Bluetooth address or the serial port."""
        return self._toy_id

    @property
    def name(self) -> str:
        """Human-readable name, e.g., the Bluetooth name."""
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
        Write bytes to the device.

        Args:
            data: Bytes to send (the ``Toy`` layer is responsible for framing).

        Raises:
            ConnectionError: The transport is not connected or the operation fails.
        """
        raise NotImplementedError

    @abstractmethod
    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """
        Start receiving inbound data and invoke *callback* for each one.

        Args:
            callback: callback invoked with each inbound ``bytes`` payload.

        Raises:
            ConnectionError: The transport is not connected or the operation fails.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Close the underlying connection and release all resources. After this call ``is_connected`` returns ``False``.

        Raises:
            ConnectionError: Closing failed. The link counts as closed anyway.
        """
        raise NotImplementedError


class BleTransport(Transport):
    """
    ``Transport`` implementation for Bluetooth Low Energy via bleak.

    Call :meth:`connect` after construction!
    It establishes the BLE connection and resolves TX / RX UUIDs, via the supplied *uuid_resolver*

    Args:
        device: The device to connect to.
        uuid_resolver: Called once by :meth:`connect` with the connected client.
            Returns ``(tx_uuid, rx_uuid)``: the characteristics to write commands to and to receive replies from.
        on_disconnect: Called with :attr:`toy_id` when the link drops unexpectedly.
            Not called after :meth:`disconnect` (= intentional disconnect).
        client_class: BleakClient (sub)class to connect with, e.g., a mock for tests.
    """

    def __init__(
        self,
        device: BLEDevice,
        uuid_resolver: Callable[[BleakClient], Awaitable[tuple[str, str]]],
        on_disconnect: Callable[[str], None] | None = None,
        client_class: Type[BleakClient] = BleakClient,
    ):
        super().__init__(toy_id=device.address, name=device.name or "")
        self._uuid_resolver = uuid_resolver
        self._on_disconnect = on_disconnect
        self._tx_uuid: str | None = None
        self._rx_uuid: str | None = None

        def _bleak_disconnect_cb(_: BleakClient) -> None:
            if not self._intentional_disconnect and on_disconnect:
                on_disconnect(self.toy_id)

        self._client: BleakClient | None = client_class(
            device, disconnected_callback=_bleak_disconnect_cb
        )

    async def connect(self) -> None:
        """
        Do not call. The ``ConnectionBuilder`` will call this for you.

        Opens the BLE connection and resolves TX / RX UUIDs.
        Must be called exactly once after construction and before any other method.

        Raises:
            ConnectionError: If the BLE connection fails or if UUID resolution raises.
        """
        assert self._client is not None
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
        assert self._client is not None
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
        assert self._client is not None and self._tx_uuid is not None
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
            ConnectionError: The transport is not connected or the operation fails.
        """

        # Bleak passes (characteristic, data: bytearray); we forward the data as bytes.
        def _bleak_cb(_: Any, data: bytearray) -> None:
            callback(bytes(data))

        assert self._client is not None and self._rx_uuid is not None
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
            ConnectionError: Closing failed. The link counts as closed anyway.
        """
        if not self._client:
            return
        self._intentional_disconnect = True
        try:
            await self._client.disconnect()
        except Exception as e:
            raise ConnectionError(
                f"Error disconnecting from toy at {self.toy_id}: {e!r}"
            ) from e
        finally:
            self._client = None


class MockTransport(Transport):
    """
    In-memory :class:`Transport` of the fictional MockEstimToys brand. No hardware.

    Simulates a device with a line protocol: commands and replies are UTF-8 text ending in ``;``. Every :meth:`send`
    is answered at once through the notification callback:

    - ``Channel1:<level>``, ``Channel2:<level>``: ``OK``
    - ``Battery``: the battery level
    - ``DeviceType``: ``MockEstim``
    - anything else: ``OK``

    Args:
        toy_id: Unique identifier, e.g., ``"Thunder_ID"``.
        name: Human-readable name, e.g., ``"Thunder1"``.
        battery: Battery level the device reports, in percent.
        on_disconnect: Called with *toy_id* on every :meth:`disconnect` (unlike with :class:`BleTransport`). The mock
            connection builder uses it to make the device discoverable again.
    """

    def __init__(
        self,
        toy_id: str,
        name: str,
        battery: int = 77,
        on_disconnect: Callable[[str], None] | None = None,
    ):
        super().__init__(toy_id, name)
        self._connected = True
        self._battery = battery
        self._notify_callback: Callable[[bytes], None] | None = None
        self._on_disconnect = on_disconnect

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def reconnect(self) -> None:
        """
        Re-open the simulated link. Does nothing if already connected.

        Raises:
            RuntimeError: Called after :meth:`disconnect`.
        """
        if self._connected:
            return
        if self._intentional_disconnect:
            raise RuntimeError("Cannot reconnect after intentional disconnect")
        self._connected = True

    async def send(self, data: bytes) -> None:
        """
        Answer a command at once through the notification callback.

        Args:
            data: One UTF-8 command, with or without its ``;``.

        Raises:
            ConnectionError: The transport is not connected.
        """
        if not self._connected:
            raise ConnectionError(f"Mock transport {self._toy_id} is not connected")
        response = self._respond(data.decode("utf-8").rstrip(";"))
        if response is not None and self._notify_callback:
            self._notify_callback((response + ";").encode("utf-8"))

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        """
        Register the callback that receives the replies.

        Args:
            callback: Called with each reply, ``;`` included.

        Raises:
            ConnectionError: The transport is not connected.
        """
        if not self._connected:
            raise ConnectionError(f"Mock transport {self._toy_id} is not connected")
        self._notify_callback = callback

    async def disconnect(self) -> None:
        """Close the simulated link. After this call ``is_connected`` returns ``False``."""
        self._intentional_disconnect = True
        self._connected = False
        self._notify_callback = None
        if self._on_disconnect is not None:
            self._on_disconnect(self._toy_id)

    def _respond(self, command: str) -> str | None:
        """The device's reply to *command*, without the ``;``."""
        if command.startswith("Channel1:") or command.startswith("Channel2:"):
            return "OK"
        if command == "Battery":
            return str(self._battery)
        if command == "DeviceType":
            return "MockEstim"
        # Lenient default: acknowledge anything else so direct commands succeed.
        return "OK"


class UsbTransport(Transport):
    """
    :class:`Transport` over USB serial, via pyserial-asyncio-fast. Create it with :meth:`connect`.

    :attr:`toy_id` is the port, :attr:`name` is ``"usb"``.

    Args:
        port: Serial port, e.g., ``"/dev/ttyUSB0"`` or ``"COM3"``.
        baudrate: Baud rate.
        reader: Stream reader of the open port.
        writer: Stream writer of the open port.
    """

    def __init__(
        self,
        port: str,
        baudrate: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        super().__init__(toy_id=port, name="usb")
        self._reader: asyncio.StreamReader | None = reader
        self._writer: asyncio.StreamWriter | None = writer
        self._port = port
        self._baudrate = baudrate
        self._connected = True
        self._notify_callback: Callable[[bytes], None] | None = None
        self._read_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, port: str, baudrate: int) -> "UsbTransport":
        """
        Open a serial port.

        Args:
            port: Serial port, e.g., ``"/dev/ttyUSB0"`` or ``"COM3"``.
            baudrate: Baud rate.

        Raises:
            OSError: The port could not be opened (serial.SerialException is one).

        Returns:
            The connected transport.
        """
        reader, writer = await serial_asyncio_fast.open_serial_connection(
            url=port, baudrate=baudrate
        )
        return cls(port, baudrate, reader, writer)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send(self, data: bytes) -> None:
        """
        Write bytes to the port.

        Args:
            data: The encoded command, framing included.

        Raises:
            ConnectionError: Not connected.
            OSError: Writing failed.
        """
        if not self._connected or self._writer is None:
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
            self._connected = True
            await self.start_notify(self._notify_callback)
        except Exception as e:
            raise ConnectionError(
                f"Error connecting to toy at {self.toy_id}: {e!r}"
            ) from e

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
        async def _read_loop() -> None:
            reader = self._reader
            if reader is None:
                return
            try:
                while self._connected:
                    data = await reader.readline()
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
            ConnectionError: If the operation fails. The connection is still regarded as closed.
        """
        self._connected = False
        try:
            if self._read_task:
                self._read_task.cancel()
                await asyncio.gather(self._read_task, return_exceptions=True)
                self._read_task = None
            if self._writer is not None:
                self._writer.close()
                await self._writer.wait_closed()
        except Exception as e:
            raise ConnectionError(
                f"Error disconnecting from toy at {self.toy_id}: {e!r}"
            ) from e
        finally:
            self._reader = None
            self._writer = None
