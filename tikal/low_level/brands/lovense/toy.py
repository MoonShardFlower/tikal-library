"""
Part of the Low-Level API: the Lovense concrete :class:`Toy` implementation.

Implements the Lovense-specific BLE protocol: command formatting, response parsing, and Lovense-specific notifications
(like power-off events). You are not meant to instantiate :class:`LovenseToy` directly.
:class: ``BLEConnectionBuilder`` establishes connections to toys and returns ready-to-use instances.
"""

import asyncio
import traceback
from typing import Any, Callable, Optional

from ...toy import Toy, UnexpectedToyResponse
from ...toy_data import BadModelError, InvalidModelError
from ...transport import BleTransport
from .data import LOVENSE_TOY_NAMES, MIN_SEGMENT_LENGTH, ROTATION_TOY_NAMES


class LovenseToy(Toy):
    """
    Low-level representation of a Lovense BLE toy.

    Implements the Lovense-specific protocol for communication with Lovense toys over Bluetooth Low Energy.
    Handles command formatting, response parsing, and Lovense-specific notifications (like power-off events).
    You are not meant to instantiate these classes directly. :class:`BLEConnectionBuilder` establishes connections
    to toys and returns instances of :class:`LovenseToy`

    Args:
        transport: Transport layer handling the BLE communication.
        model_name: Model name (e.g., "Nora", "Lush"). Must be a key in LOVENSE_TOY_NAMES.
        on_power_off: Callback invoked when the user powers off the toy via the physical power button. Receives the toy's Bluetooth address as a string argument.
        logger_name: Name of the logger to use. Use empty string for root logger.

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
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def brand(self) -> str:
        """Returns the brand of the toy. Always "Lovense" for this class"""
        return "Lovense"

    @property
    def change_rotation_direction_available(self) -> bool:
        """
        Check if the toy supports changing the rotation direction.

        Returns:
            bool: True if the rotation direction can be changed, False otherwise.

        Example::

                if toy.change_rotation_direction_available:
                    await toy.change_rotation_direction()
        """
        return self.model_name in ROTATION_TOY_NAMES

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

    @property
    def recommended_min_interval(self) -> int:
        """
        Get the recommended minimum interval between intensity changes (milliseconds).

        Returns:
            int: The Lovense per-model suggested minimum segment length.
        """
        return MIN_SEGMENT_LENGTH[self._model_name]

    async def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        Args:
            model_name: New model name. Must be in LOVENSE_TOY_NAMES.keys() of module ToyData. Case Insensitive. Case Insensitive.

        Raises:
            InvalidModelError: If model_name is not valid for this toy brand.
            BadModelError: If the model_name is valid, but commands still fail. See BadModelError for details.

        Example::

                # Update model name in case it was set incorrectly while building the connection via the ConnectionBuilder
                toy.set_model_name("Nora")
        """
        normalized_name = model_name.title()
        if normalized_name not in LOVENSE_TOY_NAMES:
            self._log.error(
                f"Invalid model name '{model_name}' for lovense toy at address '{self._toy_id}'."
            )
            raise InvalidModelError(
                f"Invalid model name '{model_name}' for lovense toy at address '{self._toy_id}'. "
                f"Valid names are: '{list(LOVENSE_TOY_NAMES.keys())}'"
            )

        # Validate against the new model's commands.
        previous_name = self._model_name
        self._model_name = normalized_name
        try:
            i1, i2 = self.current_intensities
            await self.strict_intensity1(i1)
            await self.strict_intensity2(i2)
        except Exception as e:
            self._model_name = previous_name
            self._log.exception(
                f"Failed to set model name for toy '{model_name}' at '{self._toy_id}': '{type(e)}' with details '{traceback.format_exc()}'."
            )
            raise BadModelError(
                f"Model name '{model_name}' is valid, but commands failed. This exception can mean two things:"
                f"\n1) A valid, but wrong model_name is being set."
                f"\n2) the commands being incorrect -> the Library does not handle this model correctly. Please contact the library maintainer in this case."
                f"Details: '{type(e)}' with details '{traceback.format_exc()}'"
            ) from e

    # ========================================================================
    # Public non-strict Methods
    # ========================================================================

    async def reconnect(self) -> bool:
        """
        Attempts to reconnect to the toy to after an unintentional disconnect.

        Does nothing if already connected. Use only after an unintended disconnect (ConnectionError).
        If you disconnected via disconnect, use the ConnectionBuilder instead.

        Returns:
            True if the reconnection was successful, False otherwise.
        """
        self._log.info(f"Reconnecting to '{self._model_name}' at '{self._toy_id}'")

        # Give some time in hopes of the connection failure resolving itself
        await asyncio.sleep(1.0)
        if self._transport.is_connected:
            return True

        try:
            await self._transport.reconnect()
            self._log.info(f"Connected to '{self._model_name}' at '{self._toy_id}'")
            return True
        except Exception as e:
            self._log.error(
                f"Failed to reconnect to '{self._model_name}' at '{self._toy_id}': '{type(e)}' with details '{traceback.format_exc()}'"
            )
            return False

    async def disconnect(self) -> None:
        """
        Disconnect from the device.

        Stops all toy actions, disables notifications, and closes the BLE connection. This is regarded as intentional
        disconnect and does not trigger an on_disconnect callback even if an on_disconnect callback was set
        (either during initialization or via set_on_disconnect). The method does not raise any exceptions.

        Note:
            After calling this method, the toy object is unusable. To connect again, you will need to re-scan and
            connect using the ConnectionBuilder. Use the newly provided Lovense object by the ConnectionBuilder.
        """

        def log_disconnect_error(exception: Exception) -> None:
            self._log.warning(
                f"Disconnect error for '{self._model_name}' at '{self._toy_id}': '{exception}' with details '{traceback.format_exc()}'"
            )

        try:
            await self.stop()
        except Exception as e:
            log_disconnect_error(e)
        try:
            await self._transport.disconnect()
            self._log.info(
                f"Disconnected from '{self._model_name}' at '{self._toy_id}'"
            )
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
        adjusted_level = level
        if intensity2_cmd == "Air:Level":
            adjusted_level = int(level / 4)

        self._intensity2 = level
        return await self._execute_level_command(intensity2_cmd, adjusted_level)

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
        if self.model_name not in ROTATION_TOY_NAMES:
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
                f"Failed to retrieve battery for '{self._model_name}' at '{self._toy_id}'"
            )
            return None
        try:
            # Quirk: If reconnected after disconnect, the battery reports as "s<value>"
            response = response.strip("s")
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid battery response for '{self._model_name}' at '{self._toy_id}': '{response}'"
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
                f"Failed to retrieve status for '{self._model_name}' at '{self._toy_id}'"
            )
            return None
        try:
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid status response for '{self._model_name}' at '{self._toy_id}': '{response}'"
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
    # Public Strict Methods
    # ========================================================================

    async def strict_reconnect(self) -> bool:
        """
        Similar to :meth: reconnect, but this methode does raise an exception if the reconnection fails.

        Raises:
            ConnectionError: The reconnection failed.
            RuntimeError: The reconnection was attempted after intentionally disconnecting the toy.

        Returns:
            Always True.
        """
        try:
            self._log.info(f"Reconnecting to '{self._model_name}' at '{self._toy_id}'")

            # Give some time in hopes of the connection failure resolving itself
            await asyncio.sleep(1.0)
            if self._transport.is_connected:
                return True

            await self._transport.reconnect()
            self._log.info(f"Connected to '{self._model_name}' at '{self._toy_id}'")
            return True
        except Exception as e:
            self._log.exception(
                f"Failed to reconnect to '{self._model_name}' at '{self._toy_id}': {type(e)} with details {traceback.format_exc()}"
            )
            raise e

    async def strict_disconnect(self) -> None:
        """
        Similar to :meth: disconnect, but this methode does raise an exception if the disconnect fails.
        The exception is only for logging. The toy is still disconnected in the error case.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
        """
        exc = None
        try:
            await self.strict_stop()
        except Exception as e:
            exc = e
        try:
            await self._transport.disconnect()
            self._log.info(
                f"Disconnected from '{self._model_name}' at '{self._toy_id}'."
            )
        except Exception as e:
            self._log.exception(
                f"Failed to disconnect from '{self._model_name}' at '{self._toy_id}': {type(e)} with details {traceback.format_exc()}"
            )
            exc = e
        if isinstance(exc, Exception):
            raise ConnectionError(
                f"Failed to cleanly disconnect from '{self._model_name}' at '{self._toy_id}'. Toy is still disconnected."
            ) from exc

    async def strict_intensity1(self, level: int) -> bool:
        """
        Similar to :meth: intensity1, but this method raises an exception if the intensity command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".

        Returns:
            Always true
        """
        level = max(0, min(self._MAX_INTENSITY, level))
        intensity1_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity1_command
        await self._strict_execute_level_command(intensity1_cmd, level)
        self._intensity1 = level
        return True

    async def strict_intensity2(self, level: int) -> bool:
        """
        Similar to :meth: intensity2, but this method raises an exception if the intensity command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".

        Returns:
            True if the toy supports a secondary capability, False otherwise.
        """
        level = max(0, min(self._MAX_INTENSITY, level))
        intensity2_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity2_command
        if not intensity2_cmd:
            return False

        # Special case: Air:Level takes values 0-5 instead of 0-20
        adjusted_level = level
        if intensity2_cmd == "Air:Level":
            adjusted_level = int(level / 4)

        await self._strict_execute_level_command(intensity2_cmd, adjusted_level)
        self._intensity2 = level
        return True

    async def strict_stop(self) -> bool:
        """
        Similar to :meth: stop, but this method raises an exception if the stop command fails.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.

        Returns:
            Always True.
        """
        await self.strict_intensity1(0)
        await self.strict_intensity2(0)
        return True

    async def strict_change_rotation_direction(self) -> bool:
        """
        Similar to :meth: rotate_change_direction, but this method raises an exception if the command fails.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.

        Returns:
            True if the toy supports rotation, False otherwise. Does not raise if the toy does not support rotation.
        """
        if self.model_name not in ROTATION_TOY_NAMES:
            return False
        response = await self._strict_execute_command("RotateChange")
        if not response == "OK":
            self._log.error(
                f"Failed to change rotation direction of '{self._model_name}' at '{self._toy_id}': Unexpected response: '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command 'RotateChange' from toy '{self._model_name}' at {self._toy_id}'."
            )
        return True

    async def strict_get_battery_level(self) -> int:
        """
        Similar to :meth: get_battery_level, but this method raises an exception if the command fails.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected.
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.

        Returns:
            Battery level as a percentage (0-100).
        """
        response = await self._strict_execute_command("Battery")
        try:
            # Quirk: If reconnected after disconnect, the battery reports as "s<value>"
            response = response.strip("s")
            return int(response)
        except ValueError:
            self._log.error(
                f"Failed to retrieve battery level of '{self._model_name}' at '{self._toy_id}': Unexpected response: '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command 'Battery' from toy '{self._model_name}' at '{self._toy_id}'."
            )

    async def strict_direct_command(self, command: str, timeout: float = 3.0) -> str:
        """
        Similar to :meth: direct_command, but this method raises an exception if the command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within the provided timeout.

        Returns:
            response string from the toy
        """
        return await self._strict_execute_command(command, timeout)

    async def strict_get_device_type(self) -> str:
        """
        Similar to :meth: get_device_type, but this method raises an exception if the command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within the provided timeout.

        Returns:
            String in format "DeviceType:FirmwareVersion:Address" (e.g., "C:11:0082059AD3BD")

        Note:
            does not raise UnexpectedToyResponse as the response is not verified.
        """
        return await self._strict_execute_command("DeviceType")

    async def strict_get_status(self) -> int:
        """
        Similar to :meth: get_status, but this method raises an exception if the command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within the provided timeout.
            UnexpectedToyResponse: The toys' response was unexpected, in this case any string containing non-digit characters.

        Returns:
            Status code (2 = Normal operation)
        """
        response = await self._strict_execute_command("Status:1")
        try:
            return int(response)
        except ValueError:
            self._log.error(
                f"Failed to retrieve status of '{self._model_name}' at '{self._toy_id}': Unexpected response: '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command 'Status' from toy '{self._model_name}' at '{self._toy_id}'"
            )

    async def strict_get_batch_number(self) -> str:
        """
        Similar to :meth: get_batch_number, but this method raises an exception if the command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            UnexpectedToyResponse: The toys' response was unexpected.

        Returns:
            Batch number string (e.g., "241015" for October 15, 2024)
        """
        response = await self._strict_execute_command("GetBatch")
        if not response.isdigit():
            self._log.error(
                f"Failed to retrieve batch number of '{self._model_name}' at '{self._toy_id}': Unexpected response: '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command 'GetBatch' from toy '{self._model_name}' at '{self._toy_id}'"
            )
        return response

    async def strict_power_off(self) -> bool:
        """
        Similar to :meth: power_off, but this method raises an exception if the command fails.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected.
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.

        Returns:
            Always True.
        """
        response = await self._strict_execute_command("PowerOff")
        if not response == "OK":
            self._log.error(
                f"Failed to power off '{self._model_name}' at '{self._toy_id}': Unexpected response: '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command 'PowerOff' from toy '{self._model_name}' at '{self._toy_id}'"
            )
        return True

    # ========================================================================
    # Private Methods
    # ========================================================================

    async def start_notifications(self) -> None:
        """
        Start listening for messages from the Lovense toy.

        This method is called by the ConnectionBuilder during connection setup and should not be called manually.

        Raises:
            ConnectionError: Failed to start notifications.
        """
        self._loop = asyncio.get_running_loop()
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
            # Hand the message to the event loop thread-safely. The loop is captured in start_notifications()
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._response_queue.put_nowait, msg)
            else:
                self._response_queue.put_nowait(msg)

            # Handle power-off notification
            if msg.strip().upper() == "POWEROFF":
                self._on_power_off(self._toy_id)

        except Exception as e:
            self._log.exception(
                f"Failed to decode a notification for '{self._model_name}' at '{self._toy_id}': {type(e)} with details {traceback.format_exc()}"
            )

    async def _send_command(self, command: str) -> None:
        """
        Encode and send a command to the toy via the TX characteristic.

        Args:
            command: Command string in UTF-8 format. A semicolon terminator will be added if not already present.

        Raises:
            ConnectionError: If the command could not be sent.
        """
        if not self._transport.is_connected:
            self._log.error(
                f"Failed to send command '{command}' to '{self._model_name}' at '{self._toy_id}': Currently disconnected."
            )
            raise ConnectionError(
                f"Failed to send command {command} to toy '{self._model_name}' at '{self._toy_id}': Currently disconnected. Use Toy.reconnect() if the problem persists."
            )
        try:
            cmd_bytes = (
                (command + ";").encode("utf-8")
                if not command.endswith(";")
                else command.encode("utf-8")
            )
            await self._transport.send(cmd_bytes)
            self._log.debug(
                f"Sent command to {self._model_name} {self._toy_id}: {command.strip(';')}"
            )
        except Exception as e:
            self._log.exception(
                f"Failed to send command '{command}' to toy '{self._model_name}' at '{self._toy_id}': {type(e)} with details {traceback.format_exc()}"
            )
            raise ConnectionError(
                f"Error sending command '{command}' to toy '{self._toy_id}'"
            ) from e

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
        try:
            return await self._strict_execute_command(command, timeout)
        except ConnectionError:
            return None

    async def _strict_execute_command(self, command: str, timeout: float = 3.0) -> str:
        """
        Execute a command and wait for the response.

        This internal method handles the command execution.
        It ensures sequential execution, clears the response queue, sends the command, and waits for the response with a timeout.

        Args:
            command: Command string (UTF-8). Semicolon added automatically.
            timeout: Response timeout in seconds. Defaults to 3.0.

        Raises:
            ConnectionError: If the command could not be sent, or the response timed out.

        Returns:
            Response string from toy (with semicolon stripped)
        """
        async with self._command_lock:  # Prevent response mixing
            self._clear_response_queue()
            await self._send_command(command)
            return await self._strict_wait_for_response(timeout)

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

    async def _strict_execute_level_command(
        self, command_name: str, level: int
    ) -> None:
        """
        Execute a command with a level parameter.

        This internal helper method formats and executes level-based commands (e.g., "Vibrate:15", "Rotate:10").

        Args:
            command_name: Command string without the level parameter (e.g., "Vibrate", "Rotate").
            level: Intensity level.

        Raises:
            ConnectionError: If the command could not be sent, or the response timed out.
            UnexpectedToyResponse: If the response is not 'OK' e.g. 'ERROR'.
        """
        command = f"{command_name}:{level}"
        response = await self._strict_execute_command(command)
        if not response == "OK":
            self._log.error(
                f"Failed to execute command '{command}' for {self._model_name} at {self._toy_id}: Unexpected response: {response}"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command '{command_name}' from toy '{self._model_name}' at '{self._toy_id}'"
            )
