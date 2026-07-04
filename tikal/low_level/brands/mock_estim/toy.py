"""
Part of the Low-Level API: the MockEstimToys concrete :class:`Toy` implementation.

``MockEstimToys`` is a fictional brand used to explore the architecture. :class:`MockEstimToy` implements the abstract
:class:`Toy` interface on top of :class:`tikal.low_level.transport.MockTransport`, using a tiny line protocol:

- ``Channel1:<level>`` / ``Channel2:<level>`` set the primary / secondary stimulation channels (response ``OK``)
- ``Battery`` returns an integer percentage

You are not meant to instantiate :class:`MockEstimToy` directly. The ``MockConnectionBuilder`` establishes connections
and returns ready-to-use instances.
"""

import asyncio
import traceback
from typing import Any, Callable, Optional

from ...toy import Toy, UnexpectedToyResponse
from ...toy_data import BadModelError, InvalidModelError
from ...transport import MockTransport
from .data import MAX_INTENSITY, MIN_SEGMENT_LENGTH, MOCK_ESTIM_TOY_NAMES


class MockEstimToy(Toy):
    """
    Low-level representation of a fictional MockEstimToys device.

    Implements the brand's simple channel-based protocol over :class:`MockTransport`. ``Thunder`` is a single-channel
    model; ``Lightning`` is a dual-channel model. You are not meant to instantiate this class directly.

    Args:
        transport: Connected ``MockTransport`` instance.
        model_name: Model name. Must be a key in ``MOCK_ESTIM_TOY_NAMES`` ("Thunder" or "Lightning").
        on_power_off: Callback invoked when the device reports a power-off. Receives the toy id.
        logger_name: Name of the logger to use. Use empty string for root logger.
    """

    _MAX_INTENSITY = MAX_INTENSITY

    def __init__(
        self,
        transport: MockTransport,
        model_name: str,
        on_power_off: Callable[[str], Any],
        logger_name: str,
    ):
        super().__init__(transport, model_name, logger_name)
        self._on_power_off = on_power_off

    @property
    def brand(self) -> str:
        """Returns the brand of the toy. Always "MockEstimToys" for this class."""
        return "MockEstimToys"

    @property
    def change_rotation_direction_available(self) -> bool:
        """MockEstimToys have no rotation capability."""
        return False

    @property
    def intensity_names(self) -> tuple[str, str | None]:
        """Get the display names for the device's channels. Secondary is None for single-channel models."""
        commands = MOCK_ESTIM_TOY_NAMES[self._model_name]
        return commands.intensity1_name, commands.intensity2_name

    @property
    def max_intensity(self) -> int:
        """Maximum intensity value for MockEstimToys (0 - MAX_INTENSITY)."""
        return self._MAX_INTENSITY

    @property
    def recommended_min_interval(self) -> int:
        """Recommended minimum interval between intensity changes (milliseconds)."""
        return MIN_SEGMENT_LENGTH[self._model_name]

    async def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        Args:
            model_name: New model name. Must be in ``MOCK_ESTIM_TOY_NAMES`` (case-insensitive).

        Raises:
            InvalidModelError: If model_name is not valid for this brand.
            BadModelError: If the model_name is valid, but commands still fail.
        """
        if model_name.title() not in MOCK_ESTIM_TOY_NAMES:
            self._log.error(
                f"Invalid model name '{model_name}' for MockEstimToys at '{self._toy_id}'."
            )
            raise InvalidModelError(
                f"Invalid model name '{model_name}' for MockEstimToys at '{self._toy_id}'. "
                f"Valid names are: '{list(MOCK_ESTIM_TOY_NAMES.keys())}'"
            )

        # Verify the model's commands work by replaying the current intensities.
        try:
            i1, i2 = self.current_intensities
            await self.strict_intensity1(i1)
            await self.strict_intensity2(i2)
        except Exception as e:
            self._log.exception(
                f"Failed to set model name '{model_name}' at '{self._toy_id}': {type(e)}"
            )
            raise BadModelError(
                f"Model name '{model_name}' is valid, but commands failed. "
                f"Details: '{type(e)}' with '{traceback.format_exc()}'"
            ) from e

        self._model_name = model_name.title()

    # ========================================================================
    # Public non-strict Methods
    # ========================================================================

    async def reconnect(self) -> bool:
        """
        Attempts to reconnect after an unintentional disconnect. Does nothing if already connected.

        Returns:
            True if the reconnection was successful, False otherwise.
        """
        self._log.info(f"Reconnecting to '{self._model_name}' at '{self._toy_id}'")
        await asyncio.sleep(0)
        if self._transport.is_connected:
            return True
        try:
            await self._transport.reconnect()
            return True
        except Exception as e:
            self._log.error(
                f"Failed to reconnect to '{self._model_name}' at '{self._toy_id}': {type(e)}"
            )
            return False

    async def disconnect(self) -> None:
        """Stop all actions and close the simulated connection. Does not raise."""

        def log_disconnect_error(exception: Exception) -> None:
            self._log.warning(
                f"Disconnect error for '{self._model_name}' at '{self._toy_id}': '{exception}'"
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
        """Set the primary channel. Returns True if the device acknowledged the command."""
        level = max(0, min(self._MAX_INTENSITY, level))
        self._intensity1 = level
        command = MOCK_ESTIM_TOY_NAMES[self._model_name].intensity1_command
        return await self._execute_level_command(command, level)

    async def intensity2(self, level: int) -> bool:
        """Set the secondary channel. Returns True (no-op) for single-channel models."""
        command = MOCK_ESTIM_TOY_NAMES[self._model_name].intensity2_command
        if not command:
            return True
        level = max(0, min(self._MAX_INTENSITY, level))
        self._intensity2 = level
        return await self._execute_level_command(command, level)

    async def stop(self) -> bool:
        """Set all channels to zero."""
        result1 = await self.intensity1(0)
        result2 = await self.intensity2(0)
        return result1 and result2

    async def change_rotation_direction(self) -> bool:
        """No-op: MockEstimToys do not support rotation. Returns True."""
        return True

    async def get_battery_level(self) -> Optional[int]:
        """Retrieve the battery level as a percentage, or None on failure."""
        response = await self._execute_command("Battery")
        if not response:
            return None
        try:
            return int(response)
        except ValueError:
            self._log.warning(
                f"Invalid battery response for '{self._model_name}' at '{self._toy_id}': '{response}'"
            )
            return None

    async def direct_command(self, command: str, timeout: float = 3.0) -> str | None:
        """Send any command directly to the device. Returns the response, or None on failure."""
        return await self._execute_command(command, timeout)

    # ========================================================================
    # Public Strict Methods
    # ========================================================================

    async def strict_reconnect(self) -> bool:
        """Like :meth:`reconnect`, but raises on failure."""
        self._log.info(f"Reconnecting to '{self._model_name}' at '{self._toy_id}'")
        await asyncio.sleep(0)
        if self._transport.is_connected:
            return True
        await self._transport.reconnect()
        return True

    async def strict_disconnect(self) -> None:
        """Like :meth:`disconnect`, but raises if the disconnect fails. The toy is still disconnected."""
        exc = None
        try:
            await self.strict_stop()
        except Exception as e:
            exc = e
        try:
            await self._transport.disconnect()
        except Exception as e:
            exc = e
        if isinstance(exc, Exception):
            raise ConnectionError(
                f"Failed to cleanly disconnect from '{self._model_name}' at '{self._toy_id}'."
            ) from exc

    async def strict_intensity1(self, level: int) -> bool:
        """Like :meth:`intensity1`, but raises on failure. Always returns True."""
        level = max(0, min(self._MAX_INTENSITY, level))
        command = MOCK_ESTIM_TOY_NAMES[self._model_name].intensity1_command
        await self._strict_execute_level_command(command, level)
        self._intensity1 = level
        return True

    async def strict_intensity2(self, level: int) -> bool:
        """Like :meth:`intensity2`, but raises on failure. Returns False for single-channel models."""
        command = MOCK_ESTIM_TOY_NAMES[self._model_name].intensity2_command
        if not command:
            return False
        level = max(0, min(self._MAX_INTENSITY, level))
        await self._strict_execute_level_command(command, level)
        self._intensity2 = level
        return True

    async def strict_stop(self) -> bool:
        """Like :meth:`stop`, but raises on failure. Always returns True."""
        await self.strict_intensity1(0)
        await self.strict_intensity2(0)
        return True

    async def strict_change_rotation_direction(self) -> bool:
        """No-op: MockEstimToys do not support rotation. Returns False."""
        return False

    async def strict_get_battery_level(self) -> Optional[int]:
        """Like :meth:`get_battery_level`, but raises on failure."""
        response = await self._strict_execute_command("Battery")
        try:
            return int(response)
        except ValueError:
            self._log.error(
                f"Unexpected battery response for '{self._model_name}' at '{self._toy_id}': '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command 'Battery' from '{self._toy_id}'."
            )

    async def strict_direct_command(self, command: str, timeout: float = 3.0) -> str:
        """Like :meth:`direct_command`, but raises on failure."""
        return await self._strict_execute_command(command, timeout)

    # ========================================================================
    # Private Methods
    # ========================================================================

    async def start_notifications(self) -> None:
        """
        Start listening for messages from the device. Called by the ``MockConnectionBuilder`` during connection setup.

        Raises:
            ConnectionError: Failed to start notifications.
        """
        await self._transport.start_notify(self._notification_callback)

    def _notification_callback(self, data: bytes) -> None:
        """Decode an inbound message, queue it, and handle power-off notifications."""
        try:
            msg = data.decode("utf-8").rstrip(";")
            asyncio.get_event_loop().call_soon_threadsafe(
                self._response_queue.put_nowait, msg
            )
            if msg.strip().upper() == "POWEROFF":
                self._on_power_off(self._toy_id)
        except Exception as e:
            self._log.exception(
                f"Failed to decode a notification for '{self._model_name}' at '{self._toy_id}': {type(e)}"
            )

    async def _send_command(self, command: str) -> None:
        """Encode and send a command, adding the ``;`` terminator if needed."""
        if not self._transport.is_connected:
            raise ConnectionError(
                f"Failed to send '{command}' to '{self._toy_id}': Currently disconnected."
            )
        cmd = command if command.endswith(";") else command + ";"
        await self._transport.send(cmd.encode("utf-8"))

    async def _execute_command(
        self, command: str, timeout: float = 3.0
    ) -> Optional[str]:
        """Execute a command and wait for the response, returning None on connection failure."""
        try:
            return await self._strict_execute_command(command, timeout)
        except ConnectionError:
            return None

    async def _strict_execute_command(self, command: str, timeout: float = 3.0) -> str:
        """Execute a command and wait for the response, raising on connection failure or timeout."""
        async with self._command_lock:
            self._clear_response_queue()
            await self._send_command(command)
            return await self._strict_wait_for_response(timeout)

    async def _execute_level_command(self, channel: str, level: int) -> bool:
        """Execute a ``<channel>:<level>`` command; returns True if acknowledged with "OK"."""
        response = await self._execute_command(f"{channel}:{level}")
        return response == "OK"

    async def _strict_execute_level_command(self, channel: str, level: int) -> None:
        """Execute a ``<channel>:<level>`` command, raising if the response is not "OK"."""
        command = f"{channel}:{level}"
        response = await self._strict_execute_command(command)
        if response != "OK":
            self._log.error(
                f"Failed to execute '{command}' for '{self._model_name}' at '{self._toy_id}': '{response}'"
            )
            raise UnexpectedToyResponse(
                f"Unexpected response '{response}' for command '{channel}' from '{self._toy_id}'."
            )
