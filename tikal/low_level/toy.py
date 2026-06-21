"""
Part of the Low Level API: the abstract :class:`Toy` interface.

This module defines the brand-agnostic base class for communicating with toy devices:
- :class:`Toy`: Abstract base class defining the toy communication interface
- :class:`UnexpectedToyResponse`: raised when a toy returns an unexpected reply

Concrete, brand-specific implementations live in ``tikal/low_level/brands/`` (e.g. :class:`tikal.low_level.brands.lovense.LovenseToy`).
You are not meant to instantiate these classes directly.
:class:`ConnectionBuilder` establishes connections to toys and returns instances of :class:`Toy`

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
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Optional

from .transport import BleTransport, UsbTransport


class UnexpectedToyResponse(ConnectionError):
    """Raised when the toy responds with an unexpected message."""


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
    @abstractmethod
    def change_rotation_direction_available(self) -> bool:
        """
        Check if the toy supports changing the rotation direction.

        Returns:
            bool: True if the rotation direction can be changed, False otherwise.

        Example::

                if toy.change_rotation_direction_available:
                    await toy.change_rotation_direction()
        """
        raise NotImplementedError

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
    @abstractmethod
    def recommended_min_interval(self) -> int:
        """
        Get the recommended minimum interval between intensity changes, in milliseconds.

        This is a per-model suggestion, primarily useful for pattern playback (the smallest sensible segment length).
        This is a recommendation, and you are free to ignore it.

        Returns:
            int: Recommended minimum interval between intensity changes in milliseconds.
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
    async def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        This method validates and updates the toy's model name.
        The model name determines which commands are available and how they're interpreted.

        Args:
            model_name: New model name. Must be a valid model for this toy brand.

        Raises:
            InvalidModelError: If model_name is not valid for this toy brand.
            BadModelError: If the model_name is valid, but commands still fail. See BadModelError for details.
        """
        raise NotImplementedError

    # ========================================================================
    # Public non-strict Methods
    # ========================================================================

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
        Attempts to reconnect to the toy to after an unintentional disconnect.

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

    # ========================================================================
    # Public Strict Methods
    # ========================================================================

    @abstractmethod
    async def strict_reconnect(self) -> bool:
        """
        Similar to :meth: reconnect, but this methode does raise an exception if the reconnection fails.

        Raises:
            ConnectionError: The reconnection failed.
            RuntimeError: The reconnection was attempted after intentionally disconnecting the toy.

        Returns:
            Always True.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_disconnect(self) -> None:
        """
        Similar to :meth: disconnect, but this methode does raise an exception if the disconnect fails.
        The exception is only for logging. The toy is still disconnected in the error case.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_intensity1(self, level: int) -> bool:
        """
        Similar to :meth: intensity1, but this method raises an exception if the intensity command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
        Returns:
            Always true
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_intensity2(self, level: int) -> bool:
        """
        Similar to :meth: intensity2, but this method raises an exception if the intensity command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".

        Returns:
            True if the toy supports a secondary capability, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_stop(self) -> bool:
        """
        Similar to :meth: stop, but this method raises an exception if the stop command fails.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.

        Returns:
            always True
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_change_rotation_direction(self) -> bool:
        """
        Similar to :meth: rotate_change_direction, but this method raises an exception if the command fails.
        Raises:
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
        Returns:
            True if the toy supports rotation, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_get_battery_level(self) -> Optional[int]:
        """
        Similar to :meth: get_battery_level, but this method raises an exception if the command fails.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected.
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.

        Returns:
            Battery level as a percentage (0-100), or None if the toy has no battery.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_direct_command(self, command: str, timeout: float = 3.0) -> str:
        """
        Similar to :meth: direct_command, but this method raises an exception if the command fails.

        Raises:
            ConnectionError: Command could not be sent, or the toy did not respond within the provided timeout.

        Returns:
            response string from the toy
        """
        raise NotImplementedError

    # ========================================================================
    # Private Methods
    # ========================================================================

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
            return await self._strict_wait_for_response(timeout)
        except ConnectionError:
            return None

    async def _strict_wait_for_response(self, timeout: float = 3.0) -> str:
        """
        Wait for a response from the toy.

        This internal method waits for the toy to send a response via the notification callback. Responses are queued as they arrive.

        Args:
            timeout: Maximum time to wait in seconds. Defaults to 3.0.

        Raises:
            ConnectionError: The toy did not respond within the provided timeout.

        Returns:
            Response string from the toy.
        """
        try:
            response = await asyncio.wait_for(
                self._response_queue.get(), timeout=timeout
            )
            self._log.debug(f"Received response from '{self._toy_id}': '{response}'")
            return response
        except asyncio.TimeoutError as e:
            self._log.warning(
                f"Timeout waiting for response from '{self._model_name}' at '{self._toy_id}'"
            )
            raise ConnectionError(
                f"Timeout waiting for response from '{self._model_name}' at '{self._toy_id}'"
            ) from e
