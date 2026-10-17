"""
Low-Level API: :class:`LovenseToy`, the :class:`~tikal.low_level.Toy` of Lovense toys.

Implements the Lovense-specific BLE protocol: command formatting, response parsing, and Lovense-specific notifications
(like power-off events). You are not meant to instantiate :class:`LovenseToy` directly.
:class: ``BLEConnectionBuilder`` establishes connections to toys and returns ready-to-use instances.
"""

import asyncio
import traceback
from typing import Any, Callable, Optional

from ...toy import Toy, UnexpectedToyResponse
from ...toy_data import BadModelError, InvalidModelError, carried_over_capabilities
from ...transport import BleTransport
from .data import LOVENSE_TOY_NAMES, MIN_SEGMENT_LENGTH, ROTATION_TOY_NAMES


class LovenseToy(Toy):
    """
    A connected Lovense toy. Created by :class:`~tikal.low_level.ConnectionBuilder`.

    - Levels run from 0 to 20. The Max's air pump takes 0 to 5: its levels are divided by 4.
    - Adds the Lovense queries :meth:`get_device_type`, :meth:`get_status`, :meth:`get_batch_number`, and
        :meth:`power_off`.

    Args:
        transport: The connected transport.
        model_name: A key of :data:`~tikal.low_level.LOVENSE_TOY_NAMES`.
        on_power_off: Called with :attr:`toy_id` when the toy reports a power-off (its button was pressed). May run
            on a thread other than the event loop's.
        logger_name: Name of the logger. Empty string for the root logger.
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
            model_name: New model name. Must be in LOVENSE_TOY_NAMES.keys() of module ToyData. Case Insensitive.

        Raises:
            ~tikal.low_level.InvalidModelError: The model_name is not a lovense model.
            ~tikal.low_level.BadModelError: The model_name is valid, but commands fail. See BadModelError for details.
            ConnectionError: The new model was accepted, but a required command could not be delivered.
                 The model is left unchanged. The caller can retry.

        Example::

                # Update model name in case it was set incorrectly while building the connection
                toy.set_model_name("Nora")

        Note:
            A running toy is interrupted only as far as needed:

            - Capabilities both models drive with the same command keep their level. Switching a Solace to a Sex
              Machine (identical commands throughout) interrupts nothing.
            - A capability whose command the new model replaces or drops restarts at zero and is switched off through
              the old command as part of the change. Otherwise, it would keep running with nothing able to stop it.
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

        previous_name = self._model_name
        old_commands = LOVENSE_TOY_NAMES[previous_name]
        new_commands = LOVENSE_TOY_NAMES[normalized_name]
        keeps_intensity1, keeps_intensity2 = carried_over_capabilities(
            old_commands, new_commands
        )
        previous_intensity1, previous_intensity2 = self.current_intensities

        self._model_name = normalized_name
        try:
            await self.strict_intensity1(previous_intensity1 if keeps_intensity1 else 0)
            await self.strict_intensity2(previous_intensity2 if keeps_intensity2 else 0)
        except Exception as e:
            self._restore_model(previous_name, previous_intensity1, previous_intensity2)
            self._log.exception(
                f"Failed to set model name for toy '{model_name}' at '{self._toy_id}': '{type(e)}' with details '{traceback.format_exc()}'."
            )
            raise BadModelError(
                f"Model name '{model_name}' is valid, but commands failed. This exception can mean two things:"
                f"\n1) A valid, but wrong model_name is being set."
                f"\n2) the commands being incorrect -> the Library does not handle this model correctly. Please contact the library maintainer in this case."
                f"Details: '{type(e)}' with details '{traceback.format_exc()}'"
            ) from e

        try:
            for command, carried_over, previous_level in (
                (
                    old_commands.intensity1_command,
                    keeps_intensity1,
                    previous_intensity1,
                ),
                (
                    old_commands.intensity2_command,
                    keeps_intensity2,
                    previous_intensity2,
                ),
            ):
                if carried_over or previous_level == 0:
                    continue
                assert command is not None
                await self._strict_execute_level_command(command, 0)
        except UnexpectedToyResponse:
            self._log.warning(
                f"'{previous_name}' at '{self._toy_id}' refused to release the capabilities replaced by "
                f"'{normalized_name}'. Switching anyway: the old model could not have switched them off either."
            )
        except Exception as e:
            # The command never reached the toy. While the old model is in force ``stop`` still addresses the
            # capability, so keeping it is better than committing to a model that cannot reach it.
            self._restore_model(previous_name, previous_intensity1, previous_intensity2)
            self._log.exception(
                f"Failed to release the capabilities replaced by '{normalized_name}' on '{self._toy_id}': "
                f"'{type(e)}' with details '{traceback.format_exc()}'."
            )
            raise ConnectionError(
                f"Cannot switch '{self._toy_id}' from '{previous_name}' to '{normalized_name}': the capabilities the "
                f"new model replaces could not be reached, so the model was left unchanged."
            ) from e

        if not keeps_intensity1:
            self._intensity1 = 0
        if not keeps_intensity2:
            self._intensity2 = 0

    def _restore_model(self, model_name: str, intensity1: int, intensity2: int) -> None:
        """
        Roll back to the model name and tracked levels the toy had before a failed model change.

        The levels are restored too: a partial change may have recorded a level sent with the new model's commands.
        """
        self._model_name = model_name
        self._intensity1 = intensity1
        self._intensity2 = intensity2

    # ========================================================================
    # Public non-strict Methods
    # ========================================================================

    async def reconnect(self) -> bool:
        """
        Reopen the connection after an unexpected disconnect.

        Does nothing if already connected. Use only after an unintended disconnect (ConnectionError).
        If you disconnected via disconnect, use the ConnectionBuilder instead.

        Returns:
            True if the reconnection was successful, False otherwise.
        """
        self._log.info(f"Reconnecting to '{self._model_name}' at '{self._toy_id}'")

        # The connection may recover by itself.
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

        This is regarded as an intentional disconnect and does not trigger an on_disconnect callback!
        The method does not raise any exceptions.

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

        Note:
            :attr:`current_intensities` is updated only once the toy acknowledges the command.
            It always reports the level the toy is actually at.
        """
        level = max(0, min(self._MAX_INTENSITY, level))
        intensity1_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity1_command
        acknowledged = await self._execute_level_command(intensity1_cmd, level)
        if acknowledged:
            self._intensity1 = level
        return acknowledged

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
            :attr:`current_intensities` is updated only once the toy acknowledges the command.
            It always reports the level the toy is actually at.
        """
        intensity2_cmd = LOVENSE_TOY_NAMES[self._model_name].intensity2_command
        level = max(0, min(self._MAX_INTENSITY, level))

        if not intensity2_cmd:
            return True  # No secondary capability, return success

        # Special case: Air:Level takes values 0-5 instead of 0-20
        adjusted_level = level
        if intensity2_cmd == "Air:Level":
            adjusted_level = int(level / 4)

        acknowledged = await self._execute_level_command(intensity2_cmd, adjusted_level)
        if acknowledged:
            self._intensity2 = level
        return acknowledged

    async def stop(self) -> bool:
        """
        Stop all toy actions by setting all intensities to zero. Shortcut for intensity1(0) and intensity2(0)

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
            This method strips the 's' when present.
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
        Send any command directly to the toy. Use to send commands that are not implemented by the library.

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
        Ask the toy for its production batch indicating the manufacturing date (YYMMDD).

        Returns:
            Batch number string (e.g., "241015" for October 15, 2024) or None if the query failed.
        """
        return await self._execute_command("GetBatch")

    async def power_off(self) -> bool:
        """
        Switch the toy off. Only its power button switches it on again.

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
        Similar to :meth: reconnect, but raises an exception if the reconnection fails.

        Raises:
            ConnectionError: The reconnection failed.
            RuntimeError: The reconnection was attempted after intentionally disconnecting the toy.

        Returns:
            Always True.
        """
        try:
            self._log.info(f"Reconnecting to '{self._model_name}' at '{self._toy_id}'")

            # The connection may recover by itself.
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
            ConnectionError: Command could not be sent, or the toy did not respond.
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
            ConnectionError: Command could not be sent, or the toy did not respond.
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
            ConnectionError: Command could not be sent, or the toy did not respond.
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
            ConnectionError: Command could not be sent, or the toy did not respond.

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
            ConnectionError: Command could not be sent, or the toy did not respond.

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
        Like :meth:`get_battery_level`, but raises on failure.

        Raises:
            ConnectionError: The query could not be sent, or no reply came in time.
            UnexpectedToyResponse: The reply is not a battery level.

        Returns:
            The level in percent.
        """
        response = await self._strict_execute_command("Battery")
        try:
            # Quirk: after a reconnect, the toy reports the battery as "s<value>"
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
        Like :meth:`direct_command`, but raises on failure.

        Args:
            command: The command, e.g., ``"DeviceType"``. The ``;`` terminator is optional.
            timeout: Seconds to wait for the reply.

        Raises:
            ConnectionError: The command could not be sent, or no reply came within *timeout*.

        Returns:
            The reply without its ``;``.
        """
        return await self._strict_execute_command(command, timeout)

    async def strict_get_device_type(self) -> str:
        """
        Like :meth:`get_device_type`, but raises on failure. The reply is not checked.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond.

        Returns:
            The reply, e.g., ``"C:11:0082059AD3BD"`` (type C, firmware 11).
        """
        return await self._strict_execute_command("DeviceType")

    async def strict_get_status(self) -> int:
        """
        Like :meth:`get_status`, but raises on failure.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond.
            UnexpectedToyResponse: The toys' response was not an integer.

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
        Like :meth:`get_batch_number`, but raises on failure.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond.
            UnexpectedToyResponse: The toys' response was not a valid batch number.

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
        Like :meth:`power_off`, but raises on failure.

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
        Queue a reply from the toy, and report a power-off to *on_power_off*. May run outside the event loop.

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
            ConnectionError: The command could not be sent.
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
        Send a command and wait for its reply, one command at a time.

        Args:
            command: The command. The ``;`` terminator is optional.
            timeout: Seconds to wait for the reply.

        Returns:
            Response string from toy (with semicolon stripped). None if notifications aren't started, send failed,
            or timeout occurred.
        """
        try:
            return await self._strict_execute_command(command, timeout)
        except ConnectionError:
            return None

    async def _strict_execute_command(self, command: str, timeout: float = 3.0) -> str:
        """
        Send a command and wait for its reply, one command at a time.

        Args:
            command: The command. The ``;`` terminator is optional.
            timeout: Seconds to wait for the reply.

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

        Args:
            command_name: Command string without the level parameter (e.g., "Vibrate", "Rotate").
            level: Intensity level.

        Returns:
            True if the toy replied ``OK``, else False.
        """
        command = f"{command_name}:{level}"
        response = await self._execute_command(command)
        return response == "OK"

    async def _strict_execute_level_command(
        self, command_name: str, level: int
    ) -> None:
        """
        Execute a command with a level parameter.

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
