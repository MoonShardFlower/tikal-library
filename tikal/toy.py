"""
Part of the Low Level API: Provides representations of toys

This module provides abstract and concrete implementations for communicating with toy devices
- :class:`Toy`: Abstract base class defining the toy communication interface
- :class:`LovenseToy`: Concrete implementation for Lovense brand toys
You are not meant to instantiate these classes directly. :class:`ConnectionBuilder` establishes connections to toys and
returns instances of :class:`Toy`

Example::

        # After connecting via BLEConnectionBuilder
        toy = connected_toys[0]

        # Control the toy
        await toy.intensity1(15)  # Set primary capability to level 15
        await toy.intensity2(10)  # Set secondary capability to level 10

        # Check battery
        battery = await toy.get_battery_level()
        print(f"Battery: {battery}%")

        # Disconnect when done
        await toy.disconnect()
"""

import asyncio
import traceback
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Any, Callable, Optional

from .toy_data import LOVENSE_TOY_NAMES, ROTATION_TOY_NAMES, ValidationError
from .utils import BleTransport, UsbTransport


class Toy(ABC):
    """
    Abstract base class representing a low-level toy.

    Responsible for handling the communication with physical toys.
    Provides functions for sending commands to the toy and handles its responses.
    Each toy brand implements this interface with brand-specific protocol details.
    You are not meant to instantiate these classes directly. :class:`ConnectionBuilder` establishes connections to toys
    and returns instances of :class:`Toy`

    Args:
        transport: Connected Transport instance.
        model_name: Model name of the toy (e.g., "Gush", "Nora").
        logger_name: Name of the logger to use. Use empty string for root logger.
    """

    def __init__(
        self,
        transport: BleTransport | UsbTransport,
        model_name: str,
        logger_name: str,
    ):
        self._model_name = model_name
        self._toy_id = transport.toy_id
        self._name = transport.name
        self._transport = transport
        self._log = getLogger(logger_name)
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()
        self._command_lock = asyncio.Lock()  # Enforce sequential command execution
        self._intensity1 = 0
        self._intensity2 = 0

    @property
    def model_name(self) -> str:
        """Returns the model name of the toy (e.g., "Nora", "Lush")."""
        return self._model_name

    @property
    def toy_id(self) -> str:
        """Returns a unique identifier of the toy e.g., Bluetooth address."""
        return self._toy_id

    @property
    def name(self) -> str:
        """Returns a human-readable identifier of the toy e.g., Bluetooth name."""
        return self._name

    @property
    @abstractmethod
    def brand(self) -> str:
        """Returns a human-readable identifier of the toy brand e.g., 'Lovense'."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """
        Check if the toy is currently connected.

        Returns:
            True if connected, False otherwise.
        """
        return self._transport.is_connected

    @property
    def change_rotation_direction_available(self) -> bool:
        """
        Check if the toy supports changing the rotation direction.

        Returns:
            bool: True if the rotation direction can be changed, False otherwise.

        Example::

                if toy.change_rotate_direction_available:
                    await toy.change_rotate_direction()
        """
        return self.model_name in ROTATION_TOY_NAMES

    @property
    @abstractmethod
    def intensity_names(self) -> tuple[str, str | None]:
        """
        Get the display names for the toy's capabilities.

        Returns:
            tuple[str, str | None]: A tuple of (primary_name, secondary_name). The secondary name is None if the toy has only one capability.

        Example::

                names = toy.intensity_names
                print(f"Primary: {names[0]}")  # example: Vibration
                if names[1]:
                    print(f"Secondary: {names[1]}")  # example: Rotation
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def max_intensity(self) -> int:
        """
        Get the maximum intensity value for this toy.

        Returns:
            int: Maximum intensity value (e.g., 20 for Lovense toys).

        Example::

                max_val = toy.intensity_max_value
                await toy.intensity1(max_val)  # Set to maximum
        """
        raise NotImplementedError

    @property
    def current_intensities(self) -> tuple[int, int]:
        """
        Get the current intensity values for the toy's capabilities.

        Returns:
            tuple[int, int]: A tuple of (primary_intensity, secondary_intensity). The secondary intensity is always 0 if the toy has only one capability.

        Example::
            intensity1, intensity2 = toy.current_intensities
            print(f"Primary intensity: {intensity1}, Secondary intensity: {intensity2}")")
        """
        return self._intensity1, self._intensity2

    @abstractmethod
    def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        This method validates and updates the toy's model name. The model name determines which commands are available
        and how they're interpreted.

        Args:
            model_name: New model name. Must be a valid model for this toy brand.

        Raises:
            ValidationError: If model_name is not valid for this toy brand.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Disconnect from the device.

        Stops all toy actions, disables notifications, and closes the BLE connection.
        This method should always be called before the toy object is destroyed to ensure proper cleanup.
        The method does not raise any exceptions.
        """
        raise NotImplementedError

    @abstractmethod
    async def reconnect(self) -> bool:
        """

        Does nothing if already connected. Use only after an unintended disconnect (ConnectionError).
        If you disconnected via disconnect, use the ConnectionBuilder instead.

        Returns:
            True if the reconnection was successful, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def intensity1(self, level: int) -> bool:
        """
        Set the primary capability of the toy to a specified level.

        The primary capability varies by toy model (e.g., vibration for Gush, thrusting for Solace).

        Args:
            level: Intensity level. The valid range is 0 - self.max_intensity. Values outside this range are clamped.

        Returns:
            True if the toy acknowledged the command, False otherwise.

        Example::

                # Set primary capability to medium intensity
                success = await toy.intensity1(10)
                if not success:
                    print("Command failed or timed out")
        """
        raise NotImplementedError

    @abstractmethod
    async def intensity2(self, level: int) -> bool:
        """
        Set the secondary capability of the toy to a specified level.

        The secondary capability varies by toy model (e.g., Depth for Solace, air pump for Max).
        Not all toys have a secondary capability. Returns true and does nothing if the toy has no secondary capability.

        Args:
            level: Intensity level. The valid range is 0 - self.max_intensity. Values outside this range are clamped.

        Returns:
            True if the toy acknowledged the command or the toy does not have a secondary capability, False otherwise.

        Example::

                # Set secondary capability to low intensity
                await toy.intensity2(5)
        """
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> bool:
        """
        Stop all toy actions by setting all intensities to zero.

        Returns:
            True if successful, False if either intensity command failed.

        Example::

                await toy.stop()
        """
        raise NotImplementedError

    @abstractmethod
    async def change_rotation_direction(self) -> bool:
        """
        Change rotation direction.

        For toys with rotation capability (e.g., Nora, Ridge), this toggles the rotation direction.
        Returns True and does nothing if the toy does not support rotation.

        Returns:
            True if the toy acknowledged the command or if rotation is not supported, False if the command failed.

        Example::

                # Reverse rotation direction
                await toy.rotate_change_direction()
        """
        raise NotImplementedError

    @abstractmethod
    async def get_battery_level(self) -> Optional[int]:
        """
        Retrieve the battery level of the connected device.

        Returns:
            Battery level as a percentage (0-100), or None if an error occurred, the command timed out, or the toy has no battery.

        Example::

                battery = await toy.get_battery_level()
                if battery is not None:
                    print(f"Battery: {battery}%")
                else:
                    print("Failed to read battery level")
        """
        raise NotImplementedError

    @abstractmethod
    async def direct_command(self, command: str, timeout: float = 3.0) -> str | None:
        """
        Send any command directly to the toy.

        This method allows sending commands that are not implemented by the library.

        Args:
            command: Command string in UTF-8 format
            timeout: Response timeout in seconds. Defaults to 3.0.

        Returns:
            Response string from the toy, or None if timeout or error occurred.
        """
        raise NotImplementedError

    def _clear_response_queue(self) -> None:
        """
        Clear the response queue to prepare for a new command.

        The response queue is cleared to ensure that old responses don't interfere with new command execution.
        Is called automatically before sending each command.
        """
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                # Apparently Queue.empty() is not 100% reliable. Just as a precaution, catch this exception
                pass

    async def _wait_for_response(self, timeout: float = 3.0) -> Optional[str]:
        """
        Wait for a response from the toy.

        This internal method waits for the toy to send a response via the notification callback. Responses are queued as they arrive.

        Args:
            timeout: Maximum time to wait in seconds. Defaults to 3.0.

        Returns:
            Response string from the toy, or None if timeout occurred.
        """
        try:
            response = await asyncio.wait_for(
                self._response_queue.get(), timeout=timeout
            )
            return response
        except asyncio.TimeoutError:
            return None


class Lovense(Toy):
    """
    Low-level representation of a Lovense BLE toy.

    Implements the Lovense-specific protocol for communication with Lovense toys over Bluetooth Low Energy.
    Handles command formatting, response parsing, and Lovense-specific notifications (like power-off events).
    You are not meant to instantiate these classes directly. :class:`LovenseConnectionBuilder` establishes connections
    to toys and returns instances of :class:`Lovense`

    Args:
        transport: Transport layer handling the BLE communication.
        model_name: Model name (e.g., "Nora", "Lush"). Must be a key in LOVENSE_TOY_NAMES.
        on_power_off: Callback invoked when the user powers off the toy via the physical power button. Receives the toy's Bluetooth address as a string argument.
        logger_name: Name of the logger to use. Use empty string for root logger.

    Raises:
        ValidationError: If model_name is not a valid Lovense model.

    Example::

            # Set intensity of the primary capability to maximum
            await toy.intensity1(20)

            # Send a custom command
            response = await toy.direct_command("DeviceType")
            print(f"Device info: {response}")
    """

    _MAX_INTENSITY = 20

    def __init__(
        self,
        transport: BleTransport,
        model_name: str,
        on_power_off: Callable[[str], Any],
        logger_name: str,
    ):
        super().__init__(transport, model_name, logger_name)
        self._on_power_off = on_power_off
        self.set_model_name(model_name)

    @property
    def brand(self) -> str:
        """Returns the brand of the toy. Always "Lovense" for this class"""
        return "Lovense"

    @property
    def intensity_names(self) -> tuple[str, str | None]:
        """
        Get display names for Lovense toy capabilities.

        Returns:
            tuple[str, str | None]: (primary_name, secondary_name).
            Secondary name is None if the toy has only one capability.

        Example::

                names = toy.intensity_names
                print(f"{names[0]}: intensity1")  # "Vibration: intensity1"
                if names[1]:
                    print(f"{names[1]}: intensity2")  # "Rotation: intensity2"
        """
        intensity1_name = LOVENSE_TOY_NAMES[self._model_name].intensity1_name
        intensity2_name = LOVENSE_TOY_NAMES[self._model_name].intensity2_name
        return intensity1_name, intensity2_name

    @property
    def max_intensity(self) -> int:
        """
        Get maximum intensity value for Lovense toys.

        Returns:
            int: Always 20 for Lovense toys.

        Note:
            Some capabilities (like Max's air pump) use different ranges. Those are automatically scaled for you.
        """
        return self._MAX_INTENSITY

    def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        Args:
            model_name: New model name. Must be in LOVENSE_TOY_NAMES.keys() of module ToyData. Case Insensitive. Case Insensitive.

        Raises:
            ValidationError: If model_name is not a valid Lovense model.

        Example::

                # Update model name in case it was set incorrectly while building the connection via the ConnectionBuilder
                toy.set_model_name("Nora")
        """
        if model_name.title() not in LOVENSE_TOY_NAMES:
            raise ValidationError(
                f"Invalid model name '{model_name}' for lovense toy at address {self._toy_id}. "
                f"Valid names are: {list(LOVENSE_TOY_NAMES.keys())}"
            )
        self._model_name = model_name.title()


    async def reconnect(self) -> bool:
        """
        Attempts to reconnect to the toy to after an unintentional disconnect.

        Does nothing if already connected. Use only after an unintended disconnect (ConnectionError).
        If you disconnected via disconnect, use the ConnectionBuilder instead.

        Returns:
            True if the reconnection was successful, False otherwise.
        """
        self._log.info(f"Reconnecting to {self._model_name} at {self._toy_id}")

        # Give some time in hopes of the connection failure resolving itself
        await asyncio.sleep(1.0)
        if self._transport.is_connected:
            return True

        try:
            await self._transport.reconnect()
            self._log.info(f"Connected to {self._model_name} at {self._toy_id}")
            return True
        except Exception as e:
            self._log.error(
                f"Failed to reconnect to {self._model_name} at {self._toy_id}: {e} with details {traceback.format_exc()}"
            )
            return False

    async def disconnect(self) -> None:
        """
        Disconnect from the device.

        Stops all toy actions, disables notifications, and closes the BLE connection. This is regarded as intentional
        disconnect and does not trigger an on_disconnect callback even if an on_disconnect callback was set
        (either during initialization or via set_on_disconnect).
        The method does not raise any exceptions.

        Note:
            After calling this method, the toy object is unusable. To connect again, you will need to re-scan and
            connect using the ConnectionBuilder. Use the newly provided Lovense object by the ConnectionBuilder.
        """

        def log_disconnect_error(exception):
            self._log.warning(
                f"Disconnect error for {self._model_name} at {self._toy_id}: {exception} with details {traceback.format_exc()}"
            )

        try:
            await self.stop()
        except Exception as e:
            log_disconnect_error(e)
        try:
            await self._transport.disconnect()
            self._log.info(f"Disconnected from {self._model_name} at {self._toy_id}")
        except Exception as e:
            log_disconnect_error(e)

    async def intensity1(self, level: int) -> bool:
        """
        Set the primary capability to the specified level.

        The primary capability depends on the toy model (e.g., vibration for Gush, thrusting for Solace)

        Args:
            level: Intensity level (0-20). Values outside this range are clamped.

        Returns:
            True if the toy acknowledged the command, False otherwise.

        Example::

                await toy.intensity1(20)  # Set primary capability to maximum
        """
        level = max(0, min(self._MAX_INTENSITY, level))
        self._intensity1 = level
        intensity1_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity1_command
        return await self._execute_level_command(intensity1_cmd, level)

    async def intensity2(self, level: int) -> bool:
        """
        Set the secondary capability to the specified level.

        The secondary capability depends on the toy model (e.g., Rotation for Nora, depth control for Solace).
        Not all toys have a secondary capability.
        Returns true immediately and does not send a command if the toy has no secondary capability.

        Args:
            level: Intensity level (0-20). Values outside the valid range are clamped.

        Returns:
            True if the toy acknowledged the command or if no secondary capability exists, False otherwise

        Example::

                await toy.intensity2(10)  # Set secondary capability to medium intensity

        Note:
            For Max's air pump, the level is automatically divided by 4 to convert from 0-20 scale to 0-5 scale.
        """
        intensity2_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity2_command
        level = max(0, min(self._MAX_INTENSITY, level))

        if not intensity2_cmd:
            return True  # No secondary capability, return success

        # Special case: Air:Level takes values 0-5 instead of 0-20
        if intensity2_cmd == "Air:Level":
            level = int(level / 4)

        self._intensity2 = level
        return await self._execute_level_command(intensity2_cmd, level)

    async def stop(self) -> bool:
        """
        Stop all toy actions by setting all intensities to zero.

        This method is a shortcut for intensity1(0) and intensity2(0)

        Returns:
            True if both commands succeeded, False if either failed.

        Example::

                await toy.stop()
        """
        result1 = await self.intensity1(0)
        result2 = await self.intensity2(0)
        return result1 and result2

    async def change_rotation_direction(self) -> bool:
        """
        Change rotation direction for toys with rotation capability.

        This method only affects toys that support rotation (Nora and Ridge).
        For other toys, it returns True immediately without sending any command.

        Returns:
            True if the toy acknowledged the command or if rotation is not supported, False if the command failed.

        Example::

                await toy.rotate_change_direction()  # Toggle rotation direction
        """
        if not self.model_name in ROTATION_TOY_NAMES:
            return True
        response = await self._execute_command("RotateChange")
        return response == "OK"

    async def get_battery_level(self) -> Optional[int]:
        """
        Retrieve the battery level as a percentage.

        Returns:
            Battery level (0-100%), or None if the command failed or timed out.

        Example::

                battery = await toy.get_battery_level()
                if battery is not None:
                    if battery < 20:
                        print(f"Low battery: {battery}%")
                    else:
                        print(f"Battery: {battery}%")

        Note:
            Lovense toys have a quirk where they prefix 's' before the value if the toy was recently reconnected.
            This method strips the 's' when present
        """
        response = await self._execute_command("Battery")
        if not response:
            self._log.warning(
                f"Failed to retrieve battery for {self._model_name} at {self._toy_id}"
            )
            return None
        try:
            # Quirk: If reconnected after disconnect, the battery reports as "s<value>"
            response = response.strip("s")
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid battery response for {self._model_name} at {self._toy_id}: {response}"
            )
            return None

    async def direct_command(self, command: str, timeout: float = 3.0) -> str | None:
        """
        Send any command directly to the toy.

        This method allows sending commands that are not implemented by the library.

        Args:
            command: Command string in UTF-8 format. A semicolon terminator will be added if not present.
            timeout: Response timeout in seconds. Defaults to 3.0.

        Returns:
            Response string from the toy (with semicolon stripped), or None if timeout or error occurred.

        Example::

                # Query firmware version
                response = await toy.direct_command("DeviceType")
                # Response format: "C:11:0082059AD3BD"
                # (C = device type, 11 = firmware version, address)
        """
        return await self._execute_command(command, timeout)

    async def get_device_type(self) -> str | None:
        """
        Retrieve device type and firmware information.

        Returns:
            String in format "DeviceType:FirmwareVersion:Address" (e.g., "C:11:0082059AD3BD"), or None if an error occurred.

        Example::

                info = await toy.get_device_type()
                if info:
                    parts = info.split(":")
                    device_type = parts[0]
                    firmware = parts[1]
                    print(f"Device type: {device_type}, Firmware: {firmware}")
        """
        return await self._execute_command("DeviceType")

    async def get_status(self) -> int | None:
        """
        Retrieve the status code of the toy.

        Returns:
            Status code (2 = Normal operation), or None if an error occurred.

        Example::

                status = await toy.get_status()
                if status == 2:
                    print("Toy is operating normally")
                else
                    print(f"Unusual status code: {status}")
        """
        response = await self._execute_command("Status:1")
        if not response:
            self._log.warning(
                f"Failed to retrieve status for {self._model_name} at {self._toy_id}"
            )
            return None
        try:
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid status response for {self._model_name} at {self._toy_id}: {response}"
            )
            return None

    async def get_batch_number(self) -> Optional[str]:
        """
        Retrieve the production batch number.

        The batch number appears to be in YYMMDD format, indicating the manufacturing date.

        Returns:
            Batch number string (e.g., "241015" for October 15, 2024), or None if an error occurred.

        Example::

                batch = await toy.get_batch_number()
                if batch:
                    print(f"Manufactured: 20{batch[:2]}/{batch[2:4]}/{batch[4:6]}")
        """
        return await self._execute_command("GetBatch")

    async def power_off(self) -> bool:
        """
        Turn off power to the toy.

        This sends the PowerOff command which turns off the toy. The toy can then only be turned back on via the physical power button.

        Returns:
            True if successful, False otherwise.

        Example::

                await toy.power_off()
        """
        response = await self._execute_command("PowerOff")
        return response == "OK"

    # ========================================================================
    # Private Methods
    # ========================================================================

    async def start_notifications(self) -> None:
        """
        Start listening for messages from the Lovense toy.

        This method is called by the ConnectionBuilder during connection setup and should not be called manually.

        Raises:
            RuntimeError: See :meth `start_notifications` of :class: `BleTransport` for details.
        """
        await self._transport.start_notify(self._notification_callback)

    def _notification_callback(self, data: bytes) -> None:
        """
        Callback invoked automatically when the toy sends a message.

        This method is registered with self.transport.start_notify and is called whenever data arrives.
        It decodes the message, strips the semicolon terminator, and queues the response for further processing.

        Args:
            data: Raw response bytes from the toy.

        """
        try:
            msg = data.decode("utf-8").rstrip(";")
            # Use call_soon_threadsafe for thread-safe queue access
            asyncio.get_event_loop().call_soon_threadsafe(
                self._response_queue.put_nowait, msg
            )

            # Handle power-off notification
            if msg.strip().upper() == "POWEROFF":
                self._on_power_off(self._toy_id)

        except Exception as e:
            self._log.warning(
                f"Error decoding notification for {self._model_name} at {self._toy_id}: {e} with details {traceback.format_exc()}"
            )

    async def _send_command(self, command: str) -> bool:
        """
        Encode and send a command to the toy via the TX characteristic.

        Args:
            command: Command string in UTF-8 format. A semicolon terminator will be added if not already present.

        Returns:
            True if successfully sent, False if the client is not connected or an error occurred.
        """
        if not self._transport.is_connected:
            return False
        try:
            cmd_bytes = (
                (command + ";").encode("utf-8")
                if not command.endswith(";")
                else command.encode("utf-8")
            )
            await self._transport.send(cmd_bytes)
            self._log.info(
                f"Sent command to {self._model_name} {self._toy_id}: {command.strip(';')}"
            )
            return True
        except Exception as e:
            self._log.warning(
                f"Error sending command to {self._model_name} at {self._toy_id}: {e} with details {traceback.format_exc()}"
            )
            return False

    async def _execute_command(
        self, command: str, timeout: float = 3.0
    ) -> Optional[str]:
        """
        Execute a command and wait for the response.

        This internal method handles the command execution.
        It ensures sequential execution, clears the response queue, sends the command, and waits for the response with a timeout.

        Args:
            command: Command string (UTF-8). Semicolon added automatically.
            timeout: Response timeout in seconds. Defaults to 3.0.

        Returns:
            Response string from toy (with semicolon stripped), or None if notifications aren't started, send failed,
            or timeout occurred.

        """
        self._log.debug(
            f"Sending command {command} to {self._model_name} at {self._toy_id}"
        )
        async with self._command_lock:  # Prevent response mixing
            self._clear_response_queue()

            if not await self._send_command(command):
                self._log.warning(
                    f"Failed to send command {command} to {self._model_name} at {self._toy_id}"
                )
                return None

            response = await self._wait_for_response(timeout)
            if response is None:
                self._log.warning(
                    f"Timeout waiting for response from {self._model_name} at {self._toy_id}"
                )
                return None

            self._log.debug(
                f"Received response from {self._model_name} at {self._toy_id}: {response}"
            )
            return response

    async def _execute_level_command(self, command_name: str, level: int) -> bool:
        """
        Execute a command with a level parameter.

        This internal helper method formats and executes level-based commands (e.g., "Vibrate:15", "Rotate:10").

        Args:
            command_name: Command string without the level parameter (e.g., "Vibrate", "Rotate").
            level: Intensity level.

        Returns:
            True if the toy acknowledged the command with "OK", False otherwise.
        """
        command = f"{command_name}:{level}"
        response = await self._execute_command(command)
        return response == "OK"
