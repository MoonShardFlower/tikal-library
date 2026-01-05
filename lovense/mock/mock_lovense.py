import asyncio
from typing import Callable, Any, Optional
from enum import Enum


class MockBehavior(Enum):
    """Defines different toy simulation behaviors for testing"""

    NORMAL = "normal"
    CONNECTION_FAILURE = "connection_failure"
    POWER_OFF = "power_off"


class MockCharacteristic:
    """Mock GATT characteristic that matches bleak's BleakGATTCharacteristic interface"""

    def __init__(self, uuid: str):
        self.uuid = uuid

    def __str__(self):
        return self.uuid


class MockService:
    """Mock GATT service that matches bleak's BleakGATTService interface"""

    def __init__(self, uuid: str, characteristics: list[MockCharacteristic]):
        self.uuid = uuid
        self.characteristics = characteristics

    def __str__(self):
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

    @staticmethod
    async def discover(timeout: float) -> list[MockBLEDevice]:
        """
        Returns a list of mock devices with specific behaviors encoded in their names.
        Only returns devices that are not currently connected.

        Returns devices simulating:
        - Solace: Normal Solace toy (Thrusting and Depth commands)
        - Gush: Normal Gush toy (Vibrate command)
        - Gush connection_failure: Gush that stops responding 10 s after the first intensity command
        - Gush POWEROFF: Gush that sends POWEROFF 10 s after the first intensity command
        """
        await asyncio.sleep(
            timeout * 0.1
        )  # Simulate scan time (Actual scan time would be exactly timeout-seconds-long)

        all_devices = [
            MockBLEDevice("LVS-Solace", "00:00:00:00:00:01"),
            MockBLEDevice("LVS-Gush", "00:00:00:00:00:02"),
            MockBLEDevice("LVS-Gush connection_failure", "00:00:00:00:00:03"),
            MockBLEDevice("LVS-Gush POWEROFF", "00:00:00:00:00:04"),
        ]

        # Filter out devices that are currently connected
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
    - connection_failure: Stops responding 10s after the first intensity command
    - POWEROFF: Sends POWEROFF and disconnects 10s after the first intensity command
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
        await asyncio.sleep(0.01)
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
        return elapsed >= 10.0

    async def _trigger_connection_failure(self) -> None:
        """Simulate connection failure after 10 seconds"""
        await asyncio.sleep(10.0)
        if self._is_connected and not self._failure_triggered:
            self._failure_triggered = True
            # Simulate the device becoming unresponsive but not explicitly disconnecting

    async def _trigger_power_off(self) -> None:
        """Simulate POWEROFF notification after 10 seconds"""
        await asyncio.sleep(10.0)
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

        return b"OK;"  # Default: accept any intensity command

    async def _handle_power_off_command(self) -> None:
        """Handle PowerOff command"""
        await asyncio.sleep(0.05)
        self._is_connected = False
        self._notification_callback = None
        # Unregister the device, so appears in scans again
        MockBleakScanner.unregister_connection(self.address)
