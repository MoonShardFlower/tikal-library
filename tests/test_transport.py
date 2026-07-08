"""
Tests for the transport layer (:mod:`tikal.low_level.transport`).

Covers the three concrete transports with lightweight fakes instead of real hardware:
- ``BleTransport`` against a fake ``BleakClient`` (connect / send / notify / disconnect / reconnect + the
  unexpected-vs-intentional disconnect callback).
- ``UsbTransport`` against fake serial reader/writer streams.
- ``MockTransport`` (the in-memory transport used by the fictional MockEstimToys brand).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

import tikal.low_level.transport as transport_mod
from tikal.low_level.transport import BleTransport, MockTransport, UsbTransport

# ---------------------------------------------------------------------------
# BleTransport
# ---------------------------------------------------------------------------


class _Device:
    """Minimal stand-in for bleak's BLEDevice (only ``.address`` / ``.name`` are used)."""

    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="LVS-Test"):
        self.address = address
        self.name = name


class _FakeBleakClient:
    """A controllable fake of the subset of BleakClient that BleTransport touches."""

    def __init__(self, device, disconnected_callback=None):
        self.device = device
        self.address = device.address
        self._disconnected_callback = disconnected_callback
        self.is_connected = False
        self.sent: list[bytes] = []
        self.notify_cb = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        # Failure toggles for individual operations
        self.connect_should_fail = False
        self.write_should_fail = False
        self.notify_should_fail = False
        self.disconnect_should_fail = False

    async def connect(self):
        self.connect_calls += 1
        if self.connect_should_fail:
            raise RuntimeError("connect failed")
        self.is_connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_should_fail:
            raise RuntimeError("disconnect failed")
        self.is_connected = False

    async def write_gatt_char(self, uuid, data, response=True):
        if self.write_should_fail:
            raise RuntimeError("write failed")
        self.sent.append((uuid, bytes(data), response))

    async def start_notify(self, uuid, cb):
        if self.notify_should_fail:
            raise RuntimeError("notify failed")
        self.notify_cb = cb

    def fire_unexpected_disconnect(self):
        """Invoke the disconnected_callback BleTransport registered (as bleak would on a drop)."""
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)


def _make_ble_transport(on_disconnect=None, device=None):
    """Build a BleTransport wired to a captured _FakeBleakClient. Returns (transport, holder)."""
    device = device or _Device()
    holder: dict = {}

    def client_class(dev, disconnected_callback=None):
        client = _FakeBleakClient(dev, disconnected_callback)
        holder["client"] = client
        return client

    async def uuid_resolver(client):
        return "TX-UUID", "RX-UUID"

    transport = BleTransport(
        device,
        uuid_resolver=uuid_resolver,
        on_disconnect=on_disconnect,
        client_class=client_class,
    )
    return transport, holder


@pytest.mark.asyncio
async def test_ble_connect_success_sets_uuids_and_connected():
    transport, holder = _make_ble_transport()
    assert transport.is_connected is False

    await transport.connect()

    assert transport.is_connected is True
    assert holder["client"].connect_calls == 1
    # tx UUID resolved: proven by send() targeting it below
    await transport.send(b"Vibrate:5;")
    assert holder["client"].sent == [("TX-UUID", b"Vibrate:5;", False)]


@pytest.mark.asyncio
async def test_ble_connect_failure_raises_connection_error():
    transport, holder = _make_ble_transport()
    holder["client"].connect_should_fail = True

    with pytest.raises(ConnectionError):
        await transport.connect()
    assert transport.is_connected is False


@pytest.mark.asyncio
async def test_ble_send_failure_raises_connection_error():
    transport, holder = _make_ble_transport()
    await transport.connect()
    holder["client"].write_should_fail = True

    with pytest.raises(ConnectionError):
        await transport.send(b"Vibrate:5;")


@pytest.mark.asyncio
async def test_ble_start_notify_forwards_bytes_to_callback():
    transport, holder = _make_ble_transport()
    await transport.connect()

    received: list[bytes] = []
    await transport.start_notify(received.append)

    # Simulate an inbound notification the way bleak does: (characteristic, bytearray)
    holder["client"].notify_cb("char", bytearray(b"OK;"))
    assert received == [b"OK;"]


@pytest.mark.asyncio
async def test_ble_start_notify_failure_raises_connection_error():
    transport, holder = _make_ble_transport()
    await transport.connect()
    holder["client"].notify_should_fail = True

    with pytest.raises(ConnectionError):
        await transport.start_notify(lambda _: None)


@pytest.mark.asyncio
async def test_ble_disconnect_closes_and_marks_disconnected():
    transport, holder = _make_ble_transport()
    await transport.connect()

    await transport.disconnect()
    assert holder["client"].disconnect_calls == 1
    assert transport.is_connected is False


@pytest.mark.asyncio
async def test_ble_disconnect_error_still_marks_disconnected():
    transport, holder = _make_ble_transport()
    await transport.connect()
    holder["client"].disconnect_should_fail = True

    with pytest.raises(ConnectionError):
        await transport.disconnect()
    # Even on error the transport is considered closed (client is dropped).
    assert transport.is_connected is False


@pytest.mark.asyncio
async def test_ble_reconnect_noop_when_connected():
    transport, holder = _make_ble_transport()
    await transport.connect()
    assert holder["client"].connect_calls == 1

    await transport.reconnect()  # already connected -> no extra connect
    assert holder["client"].connect_calls == 1


@pytest.mark.asyncio
async def test_ble_reconnect_reopens_after_unexpected_drop():
    transport, holder = _make_ble_transport()
    await transport.connect()
    holder["client"].is_connected = False  # simulate an unexpected drop

    await transport.reconnect()
    assert holder["client"].connect_calls == 2
    assert transport.is_connected is True


@pytest.mark.asyncio
async def test_ble_reconnect_after_intentional_disconnect_raises_runtime_error():
    transport, holder = _make_ble_transport()
    await transport.connect()
    await transport.disconnect()

    with pytest.raises(RuntimeError):
        await transport.reconnect()


@pytest.mark.asyncio
async def test_ble_unexpected_disconnect_invokes_on_disconnect():
    seen: list[str] = []
    transport, holder = _make_ble_transport(on_disconnect=seen.append)
    await transport.connect()

    holder["client"].fire_unexpected_disconnect()
    assert seen == [transport.toy_id]


@pytest.mark.asyncio
async def test_ble_intentional_disconnect_does_not_invoke_on_disconnect():
    seen: list[str] = []
    transport, holder = _make_ble_transport(on_disconnect=seen.append)
    await transport.connect()
    client = holder["client"]

    await transport.disconnect()
    client.fire_unexpected_disconnect()  # a drop after an intentional disconnect
    assert seen == []


# ---------------------------------------------------------------------------
# UsbTransport
# ---------------------------------------------------------------------------


class _FakeReader:
    """Fake asyncio.StreamReader backed by a queue; ``readline`` blocks when empty."""

    def __init__(self):
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, line: bytes) -> None:
        self._q.put_nowait(line)

    async def readline(self) -> bytes:
        return await self._q.get()


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []
        self.closed = False
        self.drain = AsyncMock()
        self.wait_closed = AsyncMock()

    def write(self, data) -> None:
        self.written.append(bytes(data))

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_usb_send_writes_and_drains():
    reader, writer = _FakeReader(), _FakeWriter()
    transport = UsbTransport("COM3", 115200, reader, writer)

    await transport.send(b"hello\n")
    assert writer.written == [b"hello\n"]
    writer.drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_usb_send_when_disconnected_raises():
    reader, writer = _FakeReader(), _FakeWriter()
    transport = UsbTransport("COM3", 115200, reader, writer)
    await transport.disconnect()

    with pytest.raises(ConnectionError):
        await transport.send(b"hello\n")


@pytest.mark.asyncio
async def test_usb_start_notify_delivers_each_line():
    reader, writer = _FakeReader(), _FakeWriter()
    transport = UsbTransport("COM3", 115200, reader, writer)

    received: list[bytes] = []
    reader.feed(b"resp1;\n")
    reader.feed(b"resp2;\n")
    await transport.start_notify(received.append)
    await asyncio.sleep(0.02)  # let the read loop drain the queued lines

    assert received == [b"resp1;\n", b"resp2;\n"]
    await transport.disconnect()  # cancels the read loop cleanly


@pytest.mark.asyncio
async def test_usb_disconnect_closes_writer_and_marks_disconnected():
    reader, writer = _FakeReader(), _FakeWriter()
    transport = UsbTransport("COM3", 115200, reader, writer)
    await transport.start_notify(lambda _: None)

    await transport.disconnect()
    assert transport.is_connected is False
    assert writer.closed is True
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_usb_connect_classmethod_opens_serial(monkeypatch):
    reader, writer = _FakeReader(), _FakeWriter()
    open_mock = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(
        transport_mod.serial_asyncio_fast, "open_serial_connection", open_mock
    )

    transport = await UsbTransport.connect("COM7", 9600)
    assert transport.is_connected is True
    assert transport.toy_id == "COM7"
    open_mock.assert_awaited_once_with(url="COM7", baudrate=9600)


@pytest.mark.asyncio
async def test_usb_reconnect_noop_when_connected():
    reader, writer = _FakeReader(), _FakeWriter()
    transport = UsbTransport("COM3", 115200, reader, writer)

    await transport.reconnect()  # already connected -> nothing happens
    assert transport.is_connected is True


# ---------------------------------------------------------------------------
# MockTransport
# ---------------------------------------------------------------------------


async def _mock_roundtrip(transport: MockTransport, command: bytes) -> bytes | None:
    """Send ``command`` and return the single response the mock feeds back (or None)."""
    received: list[bytes] = []
    await transport.start_notify(received.append)
    await transport.send(command)
    return received[0] if received else None


@pytest.mark.asyncio
async def test_mock_channel_command_acknowledged():
    transport = MockTransport("Thunder_ID", "Thunder1")
    assert await _mock_roundtrip(transport, b"Channel1:5;") == b"OK;"


@pytest.mark.asyncio
async def test_mock_battery_reports_configured_value():
    transport = MockTransport("Thunder_ID", "Thunder1", battery=42)
    assert await _mock_roundtrip(transport, b"Battery;") == b"42;"


@pytest.mark.asyncio
async def test_mock_device_type_and_lenient_default():
    transport = MockTransport("Thunder_ID", "Thunder1")
    assert await _mock_roundtrip(transport, b"DeviceType;") == b"MockEstim;"
    # Anything else is leniently acknowledged so direct commands succeed.
    assert await _mock_roundtrip(transport, b"Whatever;") == b"OK;"


@pytest.mark.asyncio
async def test_mock_send_when_disconnected_raises():
    transport = MockTransport("Thunder_ID", "Thunder1")
    await transport.disconnect()
    with pytest.raises(ConnectionError):
        await transport.send(b"Channel1:5;")


@pytest.mark.asyncio
async def test_mock_start_notify_when_disconnected_raises():
    transport = MockTransport("Thunder_ID", "Thunder1")
    await transport.disconnect()
    with pytest.raises(ConnectionError):
        await transport.start_notify(lambda _: None)


@pytest.mark.asyncio
async def test_mock_disconnect_invokes_on_disconnect_once():
    seen: list[str] = []
    transport = MockTransport("Thunder_ID", "Thunder1", on_disconnect=seen.append)

    await transport.disconnect()
    assert transport.is_connected is False
    assert seen == ["Thunder_ID"]


@pytest.mark.asyncio
async def test_mock_reconnect_noop_when_connected():
    transport = MockTransport("Thunder_ID", "Thunder1")
    await transport.reconnect()
    assert transport.is_connected is True


@pytest.mark.asyncio
async def test_mock_reconnect_after_intentional_disconnect_raises():
    transport = MockTransport("Thunder_ID", "Thunder1")
    await transport.disconnect()
    with pytest.raises(RuntimeError):
        await transport.reconnect()
