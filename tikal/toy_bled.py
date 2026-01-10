import asyncio
import traceback
from logging import getLogger
from abc import ABC, abstractmethod
from typing import Optional, Callable, Any
from bleak import BleakClient

from .toy_data import LOVENSE_TOY_NAMES, ROTATION_TOY_NAMES, ValidationError


class ToyBLED(ABC):

    def __init__(
        self,
        client: BleakClient,
        tx_uuid: str,
        rx_uuid: str,
        model_name: str,
        logger_name: str,
    ):
        """
        Abstract base class representing a low-level BLE toy connection. Responsible for handling the
        Bluetooth Low Energy communication with physical toys (command sending, response handling, and connection management)

        Args:
            client: BleakClient for BLE communication
            tx_uuid: UUID for sending commands to the toy (TX characteristic)
            rx_uuid: UUID for receiving responses from the toy (RX characteristic)
            model_name: Model name of the toy (e.g. "Gush", "Nora")
            logger_name: Name of the logger to use. Empty string for root logger.
        """
        self._model_name = model_name
        self._address = client.address
        self._name = client.name
        self._client = client
        self._tx_uuid = tx_uuid
        self._rx_uuid = rx_uuid
        self._log = getLogger(logger_name)
        self._notifications_started = False
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()
        self._command_lock = asyncio.Lock()  # Enforce sequential command execution
        self._intentional_disconnect = False  # Bleak has the annoying habit of calling on_disconnect regardless of whether the disconnect was intentional or not

    @property
    def model_name(self) -> str:
        """Gets the model name of the toy."""
        return self._model_name

    @property
    def address(self) -> str:
        """Gets the Bluetooth address of the toy."""
        return self._address

    @property
    def name(self) -> Optional[str]:
        """Gets the Bluetooth name of the toy."""
        return self._name

    @property
    def is_connected(self) -> bool:
        """True if the toy is connected, False otherwise."""
        return self._client is not None and self._client.is_connected

    @property
    def intentional_disconnect(self) -> bool:
        """True if the toy was intentionally disconnected, False otherwise."""
        return self._intentional_disconnect

    @abstractmethod
    def set_model_name(self, model_name: str) -> None:
        """
        Sets the model name of the toy.

        Args:
            model_name (str): New model name

        Raises:
             ValidationError: If model_name is not a valid model
        """
        raise NotImplementedError

    @abstractmethod
    async def start_notifications(self) -> None:
        """
        Start listening for messages from the toy. If successful, the notification callback will be invoked
        automatically whenever the toy sends a message.

        Raises:
            RuntimeError: If BleakClient is None or notifications cannot be started
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Disconnect from the device. Stops all actions, disables notifications, and closes the BLE connection.
        Should be called before the toy object is destroyed.
        """
        raise NotImplementedError

    @abstractmethod
    async def intensity1(self, level: int) -> bool:
        """
        Set the primary capability of the toy to a specified level.

        Args:
            level (int): Intensity level

        Returns:
            bool: True if the toy acknowledged the command, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    async def intensity2(self, level: int) -> bool:
        """
        Set the secondary capability of the toy to a specified level.
        Safe to call and will do nothing if the toy has no secondary capability.

        Args:
            level (int): Intensity level (typically 0-20, clamped to valid range)

        Returns:
            bool: True if successful or no secondary capability exists, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> bool:
        """
        Stop all toy actions (set all intensities to zero).

        Returns:
            bool: True if successful, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    async def rotate_change_direction(self) -> bool:
        """
        Change rotation direction. Returns True and does nothing if the toy does not support rotation.

        Returns:
            bool: True if successful, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    async def get_battery_level(self) -> Optional[int]:
        """
        Retrieve the battery level of the connected device.

        Returns:
            Optional[int]: Battery level in percentage (0-100), or None if an error occurred
        """
        raise NotImplementedError

    def _clear_response_queue(self) -> None:
        """Clear the response queue to prepare for a new command."""
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                # Apparently Queue.empty() is not 100% reliable. Just as a precaution, catch this exception
                pass

    async def _wait_for_response(self, timeout: float = 3.0) -> Optional[str]:
        """
        Wait for a response from the toy.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            str: Response string from the toy, or None if timeout occurred
        """
        try:
            response = await asyncio.wait_for(
                self._response_queue.get(), timeout=timeout
            )
            return response
        except asyncio.TimeoutError:
            return None


class LovenseBLED(ToyBLED):

    def __init__(
        self,
        client: BleakClient,
        tx_uuid: str,
        rx_uuid: str,
        model_name: str,
        on_power_off: Callable[[str], Any],
        logger_name: str,
    ):
        """
        Low-level representation of a Lovense BLE toy.
        Implements the Lovense-specific protocol for communication with Lovense toys over Bluetooth Low Energy.

        Args:
            client: BleakClient for BLE communication
            tx_uuid: UUID for sending commands to the toy
            rx_uuid: UUID for receiving responses from the toy
            model_name: Model name (must be a key in LOVENSE_TOY_NAMES)
            on_power_off: Invoked when the user powers off a toy. Receives the toy's address as an argument.
            logger_name: Name of the logger to use. Empty string for root logger.

        Raises:
            ValueError: If model_name is not a valid Lovense model
        """
        super().__init__(client, tx_uuid, rx_uuid, model_name, logger_name)
        self._on_power_off = on_power_off
        self.set_model_name(model_name)

    def set_model_name(self, model_name: str) -> None:
        """
        Sets the model name of the toy.

        Args:
            model_name: New model name (must be a key in LOVENSE_TOY_NAMES)

        Raises:
            ValidationError: If model_name is not a valid Lovense model
        """
        if model_name not in LOVENSE_TOY_NAMES:
            raise ValidationError(
                f"LovenseBLED at address {self._address} has an invalid model_name '{model_name}'. "
                f"Valid names are: {list(LOVENSE_TOY_NAMES.keys())}"
            )
        self._model_name = model_name

    async def start_notifications(self) -> None:
        """Start listening for messages from the toy."""
        if self._notifications_started:
            return
        if not self._client:
            raise RuntimeError(
                "Notifications couldn't be started, because client is None"
            )
        await self._client.start_notify(self._rx_uuid, self._notification_callback)
        self._notifications_started = True

    async def disconnect(self) -> None:
        """Disconnect from the device. Should be called before the toy object is destroyed."""
        try:
            self._intentional_disconnect = True
            await self.stop()
            if self._notifications_started:
                await self._client.stop_notify(self._rx_uuid)
                self._notifications_started = False
            await self._client.disconnect()
            self._log.info(f"Disconnected from {self._model_name} at {self._address}")
        except Exception as e:
            self._log.warning(
                f"Disconnect error for {self._model_name} at {self._address}: {e} with details {traceback.format_exc()}"
            )

    async def intensity1(self, level: int) -> bool:
        """
        Sets the primary capability (e.g., vibration, thrust) to the specified level (0-20).

        Args:
            level: Intensity level (0-20)

        Returns:
            bool: True if the toy acknowledged the command, False otherwise
        """
        intensity1_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity1_command
        return await self._execute_level_command(intensity1_cmd, level)

    async def intensity2(self, level: int) -> bool:
        """
        Set the secondary capability (e.g., rotation, oscillation) to the specified level (0-20).

        Args:
            level: Intensity level (0-20)

        Returns:
            bool: True if successful or no secondary capability exists, False otherwise
        """
        intensity2_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity2_command

        if not intensity2_cmd:
            return True  # No secondary capability, return success

        # Special case: Air:Level takes values 0-5 instead of 0-20
        if intensity2_cmd == "Air:Level":
            return await self._execute_level_command(intensity2_cmd, int(level / 4))

        return await self._execute_level_command(intensity2_cmd, level)

    async def stop(self) -> bool:
        """
        Stop all toy actions by setting both intensities to zero.

        Returns:
            bool: True if successful, False otherwise
        """
        result1 = await self.intensity1(0)
        result2 = await self.intensity2(0)
        return result1 and result2

    async def rotate_change_direction(self) -> bool:
        """
        Change rotation direction. Returns True and does nothing if the toy does not support rotation.

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.model_name in ROTATION_TOY_NAMES:
            return True
        response = await self._execute_command("RotateChange")
        return response == "OK"

    async def get_battery_level(self) -> Optional[int]:
        """
        Retrieve the battery level (0-100%).

        Returns:
            int: Battery level in %, or None if an error occurred
        """
        response = await self._execute_command("Battery")
        if not response:
            self._log.warning(
                f"Failed to retrieve battery for {self._model_name} at {self._address}"
            )
            return None
        try:
            # Quirk: If reconnected after disconnect, the battery reports as "s<value>"
            response = response.strip("s")
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid battery response for {self._model_name} at {self._address}: {response}"
            )
            return None

    async def direct_command(self, command: str, timeout: float = 3.0) -> str | None:
        """
        Send any command directly to the toy. Useful for commands not explicitly provided by other methods.

        Args:
            command: Command string (UTF-8)
            timeout: Response timeout in seconds

        Returns:
            Optional[str]: Response string from toy, or None if timeout/error occurred
        """
        return await self._execute_command(command, timeout)

    async def get_device_type(self) -> str | None:
        """
        Retrieves device information.

        Returns:
            Optional[str]: String in format "DeviceType:FirmwareVersion:Address" (e.g. "C:11:0082059AD3BD") or None if
                an error occurred
        """
        return await self._execute_command("DeviceType")

    async def get_status(self) -> int | None:
        """
        Retrieves the status code of the toy.

        Returns:
            Optional[int]: Status code (2 = Normal) or None if error occurred
        """
        response = await self._execute_command("Status:1")
        if not response:
            self._log.warning(
                f"Failed to retrieve status for {self._model_name} at {self._address}"
            )
            return None
        try:
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid status response for {self._model_name} at {self._address}: {response}"
            )
            return None

    async def get_batch_number(self) -> Optional[str]:
        """
        Retrieve production batch number (appears to be YYMMDD format).

        Returns:
            Optional[str]: Batch number string or None if an error occurred
        """
        return await self._execute_command("GetBatch")

    async def power_off(self) -> bool:
        """
        Turn off power to the toy.

        Returns:
            bool: True if successful, False otherwise
        """
        response = await self._execute_command("PowerOff")
        return response == "OK"

    # ========================================================================
    # Private Methods
    # ========================================================================

    def _notification_callback(self, _: int, data: bytes) -> None:
        """
        Callback invoked automatically when the toy sends a message.

        Args:
            _: Sender handle (unused)
            data: Raw response bytes from the toy
        """
        try:
            msg = data.decode("utf-8").rstrip(";")
            # Use call_soon_threadsafe for thread-safe queue access
            asyncio.get_event_loop().call_soon_threadsafe(
                self._response_queue.put_nowait, msg
            )

            # Handle power-off notification
            if msg.strip().upper() == "POWEROFF":
                self._on_power_off(self._address)

        except Exception as e:
            self._log.warning(
                f"Error decoding notification for {self._model_name} at {self._address}: {e} with details {traceback.format_exc()}"
            )

    async def _send_command(self, command: str) -> bool:
        """
        Encode and send a command to the toy.

        Args:
            command: Command string (UTF-8)

        Returns:
            bool: True if successfully sent, False otherwise
        """
        if not self._client or not self.is_connected:
            return False
        try:
            cmd_bytes = (
                (command + ";").encode("utf-8")
                if not command.endswith(";")
                else command.encode("utf-8")
            )
            await self._client.write_gatt_char(self._tx_uuid, cmd_bytes, response=False)
            self._log.info(
                f"Sent command to {self._model_name} {self._address}: {command.strip(';')}"
            )
            return True
        except Exception as e:
            self._log.warning(
                f"Error sending command to {self._model_name} at {self._address}: {e} with details {traceback.format_exc()}"
            )
            return False

    async def _execute_command(
            self, command: str, timeout: float = 3.0
    ) -> Optional[str]:
        """
        Execute a command and wait for the response.

        Args:
            command: Command string (UTF-8)
            timeout: Response timeout in seconds

        Returns:
            Optional[str]: Response string from toy, or None if a timeout/error occurred
        """
        self._log.debug(
            f"Sending command {command} to {self._model_name} at {self._address}"
        )
        async with self._command_lock:  # Prevent response mixing
            if not self._notifications_started:
                self._log.warning(
                    f"Notifications not started for {self._model_name} at {self._address}"
                )
                return None

            self._clear_response_queue()

            if not await self._send_command(command):
                self._log.warning(
                    f"Failed to send command {command} to {self._model_name} at {self._address}"
                )
                return None

            response = await self._wait_for_response(timeout)
            if response is None:
                self._log.warning(
                    f"Timeout waiting for response from {self._model_name} at {self._address}"
                )
                return None

            self._log.debug(
                f"Received response from {self._model_name} at {self._address}: {response}"
            )
            return response

    async def _execute_level_command(
        self, command_name: str, level: int, max_level: int = 20
    ) -> bool:
        """
        Execute a command with a level parameter (0-max_level).

        Args:
            command_name: Command string
            level: Intensity level (will be clamped to 0-max_level)
            max_level: Maximum allowed level

        Returns:
            bool: True if the toy acknowledged the command with "OK", False otherwise
        """
        level = max(0, min(max_level, level))
        command = f"{command_name}:{level}"
        response = await self._execute_command(command)
        return response == "OK"
