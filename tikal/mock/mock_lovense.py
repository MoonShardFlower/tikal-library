"""Private Mock: Used to simulate Lovense Devices for testing."""

import asyncio
from enum import Enum
from typing import Any, Callable, Optional


class MockBehavior(Enum):
    """Defines different toy simulation behaviors for testing"""

    NORMAL = "normal"
    CONNECTION_FAILURE = "connection_failure"
    POWER_OFF = "power_off"


class MockCharacteristic:
    """Mock GATT characteristic that matches bleak's BleakGATTCharacteristic interface"""

    def __init__(self, uuid: str):
        self.uuid = uuid

    def __str__(self) -> str:
        return self.uuid


class MockService:
    """Mock GATT service that matches bleak's BleakGATTService interface"""

    def __init__(self, uuid: str, characteristics: list[MockCharacteristic]):
        self.uuid = uuid
        self.characteristics = characteristics

    def __str__(self) -> str:
        return self.uuid


class MockBLEDevice:
    """Mock a BLE device that simulates discovered toys"""

    def __init__(self, name: str, address: str):
        self.name = name
        self.address = address


class MockBleakScanner:
    """
    Mock BLE scanner that returns predefined toy configurations.

    Tracks connected devices globally to simulate real Bluetooth behavior where
    connected devices stop advertising and don't appear in scan results.
    """

    _connected_addresses: set[str] = set()
    _SCAN_INTERVAL = 2.0  # seconds between simulated advertisement reports

    @classmethod
    def register_connection(cls, address: str) -> None:
        """Register a device as connected (will be excluded from scan results)"""
        cls._connected_addresses.add(address)

    @classmethod
    def unregister_connection(cls, address: str) -> None:
        """Unregister a device as connected (will appear in scan results again)"""
        cls._connected_addresses.discard(address)

    @classmethod
    def reset(cls) -> None:
        """Reset all connection tracking (useful for tests)"""
        cls._connected_addresses.clear()

    def __init__(
        self, detection_callback: Optional[Callable[[MockBLEDevice, Any], None]] = None
    ):
        """
        Initialize the scanner.

        Args:
            detection_callback: Optional callback that receives (device, advertisement_data)
                                when a device is detected during continuous scanning.
                                The second argument is None in this mock implementation.
        """
        self._detection_callback = detection_callback
        self._stop_event: Optional[asyncio.Event] = None
        self._scan_task: Optional[asyncio.Task[None]] = None
        self._is_started = False

    async def start(self) -> None:
        """
        Start continuous scanning.

        Raises:
            RuntimeError: If already scanning or no detection callback was provided.
        """
        if self._is_started:
            return
        if self._detection_callback is None:
            raise RuntimeError(
                "MockBleakScanner: detection_callback required for continuous scanning"
            )
        self._stop_event = asyncio.Event()
        self._scan_task = asyncio.create_task(self._scan_loop())
        self._is_started = True

    async def stop(self) -> None:
        """Stop continuous scanning."""
        if not self._is_started:
            return
        assert self._stop_event is not None
        self._stop_event.set()
        if self._scan_task:
            await self._scan_task
        self._scan_task = None
        self._stop_event = None
        self._is_started = False

    async def _scan_loop(self) -> None:
        """
        Background loop that periodically reports all non-connected devices.
        Simulates receiving advertisement packets at regular intervals.
        """
        stop_event = self._stop_event
        callback = self._detection_callback
        assert stop_event is not None and callback is not None
        while not stop_event.is_set():
            # Simulate scanning delay (real scanner would be continuous, but we batch reports)
            await asyncio.sleep(self._SCAN_INTERVAL)

            # Get the list of all possible devices (same as in discover())
            all_devices = self._get_all_devices()

            # Report each non-connected device to the callback
            for device in all_devices:
                if device.address not in self._connected_addresses:
                    # Simulate advertisement detection
                    task = callback(device, None)
                    if asyncio.iscoroutine(task):
                        await task

    @staticmethod
    def _get_all_devices() -> list[MockBLEDevice]:
        """Return the full list of mock devices (same as in discover())."""
        return [
            MockBLEDevice("LVS-Solace", "00:00:00:00:00:01"),
            MockBLEDevice("LVS-Gush", "00:00:00:00:00:02"),
            MockBLEDevice("LVS-Nora", "00:00:00:00:00:03"),
            MockBLEDevice("LVS-Gush connection_failure", "00:00:00:00:00:04"),
            MockBLEDevice("LVS-Gush POWEROFF", "00:00:00:00:00:05"),
        ]

    @staticmethod
    async def discover(timeout: float) -> list[MockBLEDevice]:
        """
        One-shot discovery: returns a list of mock devices that are not currently connected.

        Args:
            timeout: Simulated scan duration (ignored except for a small delay).

        Returns:
            List of MockBLEDevice objects for non-connected toys.
        """
        await asyncio.sleep(
            timeout * 0.1
        )  # Real scan time will be exactly timeout seconds, we speed it up.

        all_devices = MockBleakScanner._get_all_devices()
        return [
            device
            for device in all_devices
            if device.address not in MockBleakScanner._connected_addresses
        ]


class MockBleakClient:
    """
    Mock BLE client that simulates Lovense toy communication.

    Supports different behaviors based on device name:
    - Normal operation: Responds to all commands
    - connection_failure: Stops responding 5s after the first intensity command
    - POWEROFF: Sends POWEROFF and disconnects 5s after the first intensity command
    """

    def __init__(
        self, device: MockBLEDevice, disconnected_callback: Callable[[Any], Any]
    ):
        self.address = device.address
        self.name = device.name
        self._device = device
        self._disconnected_callback = disconnected_callback
        self._is_connected = False
        self._notification_callback: Optional[Callable[[int, bytes], None]] = None
        self._services_cache: Optional[list[MockService]] = None
        self._battery_level = 85
        self._first_intensity_time: Optional[float] = None
        self._behavior = MockBehavior.NORMAL
        if "connection_failure" in device.name:
            self._behavior = MockBehavior.CONNECTION_FAILURE
        elif "POWEROFF" in device.name:
            self._behavior = MockBehavior.POWER_OFF
        self._model_name = device.name.split()[0].replace("LVS-", "")
        self._failure_triggered = False

    async def connect(self) -> None:
        """Simulate connection to the toy"""
        await asyncio.sleep(1.0)

        if self._failure_triggered:
            raise RuntimeError("Connection failed")

        self._is_connected = True

        # Register this device as connected so it won't appear in future scans
        MockBleakScanner.register_connection(self.address)

        # Create a mock service structure for UUID discovery
        base_uuid = "40300001-0023-4bd4-bbd5-a6920e4c5653"
        tx_uuid = "40300002-0023-4bd4-bbd5-a6920e4c5653"
        rx_uuid = "40300003-0023-4bd4-bbd5-a6920e4c5653"

        self._services_cache = [
            MockService(
                uuid=base_uuid,
                characteristics=[
                    MockCharacteristic(tx_uuid),
                    MockCharacteristic(rx_uuid),
                ],
            )
        ]

    async def disconnect(self) -> None:
        """Simulate disconnection"""
        await asyncio.sleep(0.01)
        self._is_connected = False
        self._notification_callback = None

        # Unregister this device so it will appear in future scans
        MockBleakScanner.unregister_connection(self.address)

    @property
    def is_connected(self) -> bool:
        """Check connection status"""
        return self._is_connected

    @property
    def services(self) -> list[MockService]:
        """Return mock GATT services"""
        if self._services_cache is None:
            return []
        return self._services_cache

    async def start_notify(
        self, _: str, callback: Callable[[int, bytes], None]
    ) -> None:
        """Start notifications on a characteristic"""
        await asyncio.sleep(0.01)
        if not self._is_connected:
            raise RuntimeError("Not connected")
        self._notification_callback = callback

    async def stop_notify(self, _: str) -> None:
        """Stop notifications on a characteristic"""
        await asyncio.sleep(0.01)
        self._notification_callback = None

    async def write_gatt_char(self, _: str, data: bytes, response: bool = True) -> None:
        """
        Simulate writing to a GATT characteristic (sending commands to the toy).
        Processes commands and triggers appropriate responses via notification callback.
        """
        if not self._is_connected:
            raise RuntimeError("Not connected")

        await asyncio.sleep(0.01)

        # Decode the command
        command = data.decode("utf-8").strip(";")

        # Check if this is an intensity command and we should start the failure timer
        if (
            MockBleakClient.is_intensity_command(command)
            and self._first_intensity_time is None
        ):
            self._first_intensity_time = asyncio.get_event_loop().time()

            # Schedule behavior-specific actions
            if self._behavior == MockBehavior.CONNECTION_FAILURE:
                asyncio.create_task(self._trigger_connection_failure())
            elif self._behavior == MockBehavior.POWER_OFF:
                asyncio.create_task(self._trigger_power_off())

        # Check if we should stop responding (connection failure scenario)
        if self._should_stop_responding():
            return  # Silently ignore command

        # Process the command and send a response
        response_data = await self._process_command(command)

        if response_data and self._notification_callback:
            self._notification_callback(0, response_data)

    @staticmethod
    def is_intensity_command(command: str) -> bool:
        """Check if the command is an intensity command"""
        intensity_prefixes = [
            "Vibrate:",
            "Rotate:",
            "Thrusting:",
            "Depth:",
            "Air:Level:",
        ]
        return any(command.startswith(prefix) for prefix in intensity_prefixes)

    def _should_stop_responding(self) -> bool:
        """Check if the toy should stop responding (connection_failure behavior)"""
        if self._behavior != MockBehavior.CONNECTION_FAILURE:
            return False

        if self._first_intensity_time is None:
            return False

        elapsed = asyncio.get_event_loop().time() - self._first_intensity_time
        return elapsed >= 5.0

    async def _trigger_connection_failure(self) -> None:
        """Simulate connection failure after 5 seconds"""
        await asyncio.sleep(5.0)

        if self._is_connected and not self._failure_triggered:
            self._failure_triggered = True
            self._is_connected = False
            self._notification_callback = None

            # Notify the client of disconnection
            if self._disconnected_callback is not None:
                self._disconnected_callback(self)

    async def _trigger_power_off(self) -> None:
        """Simulate POWEROFF notification after 10 seconds"""
        await asyncio.sleep(5.0)
        if self._is_connected and not self._failure_triggered:
            self._failure_triggered = True

            # Send POWEROFF notification
            if self._notification_callback:
                poweroff_data = b"POWEROFF;"
                self._notification_callback(0, poweroff_data)

            # Simulate disconnection
            await asyncio.sleep(0.05)
            self._is_connected = False
            self._notification_callback = None

            # Unregister the device, so appears in scans again
            MockBleakScanner.unregister_connection(self.address)

    async def _process_command(self, command: str) -> Optional[bytes]:
        """
        Process a command and return the appropriate response.
        Simulates toy-specific command handling.
        """
        # Battery command
        if command == "Battery":
            return f"{self._battery_level};".encode("utf-8")

        # Device type command
        if command == "DeviceType":
            # Format: ModelCode:FirmwareVersion:Address
            model_code = "C" if self._model_name == "Gush" else "P"
            return f"{model_code}:11:{self.address.replace(':', '')};".encode("utf-8")

        # Status command
        if command.startswith("Status:"):
            return b"2;"  # 2 = Normal status

        # Batch number
        if command == "GetBatch":
            return b"241225;"  # YYMMDD format

        # PowerOff command
        if command == "PowerOff":
            asyncio.create_task(self._handle_power_off_command())
            return b"OK;"

        # Rotation direction change (for toys that support it)
        if command == "RotateChange":
            if self._model_name in ["Nora", "Ridge"]:
                return b"OK;"
            return b"err;"

        # Intensity commands - validate based on the toy model
        if MockBleakClient.is_intensity_command(command):
            return self._handle_intensity_command(command)

        # Unknown command
        return b"err"

    def _handle_intensity_command(self, command: str) -> Optional[bytes]:
        """Handle intensity commands based on a toy model"""

        # Solace supports Thrusting and Depth
        if self._model_name == "Solace":
            if command.startswith("Thrusting:") or command.startswith("Depth:"):
                return b"OK;"
            return b"err;"  # Unsupported command for this toy

        # Gush supports Vibrate
        if self._model_name == "Gush":
            if command.startswith("Vibrate:"):
                return b"OK;"
            return b"err;"

        # Nora supports Rotate
        if self._model_name == "Nora":
            if command.startswith("Rotate:") or command.startswith("Vibrate:"):
                return b"OK;"
            return b"err;"

        return b"OK;"  # Default: accept any intensity command

    async def _handle_power_off_command(self) -> None:
        """Handle PowerOff command"""
        await asyncio.sleep(0.05)
        self._is_connected = False
        self._notification_callback = None
        # Unregister the device, so appears in scans again
        MockBleakScanner.unregister_connection(self.address)
