"""
Low-Level API: :class:`Toy`, the command interface of a connected toy.

Brands implement it, e.g., :class:`~tikal.low_level.LovenseToy`. :class:`~tikal.low_level.ConnectionBuilder` connects
toys and returns them as :class:`Toy` instances.

- Methods report a failed command through their return value.
- Their ``strict_*`` variants raise :class:`ConnectionError` or :class:`UnexpectedToyResponse` instead.

Example::

    toy = await builder.create_toy(toy_data)  # or the exception that prevented connecting
    await toy.intensity1(15)
    print(await toy.get_battery_level())
    await toy.disconnect()
"""

import asyncio
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Optional

from .transport import Transport


class UnexpectedToyResponse(ConnectionError):
    """The toy replied, but not as expected, e.g., ``ERROR`` instead of ``OK``."""


class Toy(ABC):
    """
    A connected toy. Brands implement this interface; :class:`~tikal.low_level.ConnectionBuilder` creates instances.

    A toy has a primary capability (:meth:`intensity1`) and may have a secondary one (:meth:`intensity2`), depending
    on its model (see :attr:`intensity_names`).

    Args:
        transport: The connected transport.
        model_name: Model name, e.g., ``"Nora"``.
        logger_name: Name of the logger. Empty string for the root logger.
    """

    def __init__(
        self,
        transport: Transport,
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
        """Whether the connection to the toy is open."""
        return self._transport.is_connected

    @property
    @abstractmethod
    def change_rotation_direction_available(self) -> bool:
        """Whether the model can change its rotation direction (see :meth:`change_rotation_direction`)."""
        raise NotImplementedError

    @property
    @abstractmethod
    def intensity_names(self) -> tuple[str, str | None]:
        """
        Display names of the capabilities as ``(primary, secondary)``, e.g., ``("Vibration", "Rotation")``.

        The secondary name is None without a secondary capability.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def max_intensity(self) -> int:
        """Highest intensity level of both capabilities, e.g., 20 for Lovense toys. The lowest is 0."""
        raise NotImplementedError

    @property
    @abstractmethod
    def recommended_min_interval(self) -> int:
        """Recommended minimum interval between intensity changes in ms. Only a suggestion."""
        raise NotImplementedError

    @property
    def current_intensities(self) -> tuple[int, int]:
        """
        ``(intensity1, intensity2)``: the levels the toy last acknowledged.

        intensity2 is always 0 without a secondary capability.
        """
        return self._intensity1, self._intensity2

    @abstractmethod
    async def set_model_name(self, model_name: str) -> None:
        """
        Change the model name. The toy has to accept the new model's commands first.

        - Capabilities both models drive with the same command keep running (e.g., Solace to Sex Machine).
        - The others are switched off through the old command (see
          :func:`~tikal.low_level.carried_over_capabilities`). If the toy refuses that, the change still happens.

        Args:
            model_name: New model name, case-insensitive. Must be a model of the toy's brand (see
                :data:`~tikal.low_level.BRANDS`).

        Raises:
            ~tikal.low_level.InvalidModelError: *model_name* is not a model of the brand. Nothing changed.
            ~tikal.low_level.BadModelError: The toy refused or did not answer the new model's commands. Nothing changed.
            ConnectionError: A command switching off a replaced capability could not be delivered. Nothing changed.
        """
        raise NotImplementedError

    # ========================================================================
    # Public non-strict Methods
    # ========================================================================

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Stop the toy and close the connection. Never raises: errors are logged.

        A deliberate disconnect: *on_disconnect* is not called, and :meth:`reconnect` refuses afterwards. To use the
        toy again, connect it through the connection builder.
        """
        raise NotImplementedError

    @abstractmethod
    async def reconnect(self) -> bool:
        """
        Reopen the connection after an unexpected disconnect. Does nothing while connected.

        After :meth:`disconnect`, connect through the connection builder instead.

        Returns:
            True if the reconnection was successful, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def intensity1(self, level: int) -> bool:
        """
        Set the primary capability, e.g., Gush: vibration, Solace: thrust.

        :attr:`current_intensities` changes once the toy acknowledges.

        Args:
            level: 0 to :attr:`max_intensity`. Values outside are clamped.

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
        Set the secondary capability, e.g., Solace: depth, Max: air pump.

        - Without a secondary capability, nothing is sent.
        - :attr:`current_intensities` changes once the toy acknowledges.

        Args:
            level: 0 to :attr:`max_intensity`. Values outside are clamped.

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
        Set both capabilities to 0.

        Returns:
            True if successful, False if either intensity command failed.

        Example::

                await toy.stop()
        """
        raise NotImplementedError

    @abstractmethod
    async def change_rotation_direction(self) -> bool:
        """
        Reverse the rotation direction. Without rotation support (:attr:`change_rotation_direction_available`),
        nothing is sent.

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
        Ask the toy for its battery level.

        Returns:
            Battery level as a percentage (0-100). None if an error or timeout occurred, or the toy has no battery.

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
        Send a raw command, e.g., one the library does not implement. The tracked state (e.g.,
        :attr:`current_intensities`) does not change.

        Args:
            command: The command in the toy's protocol, e.g., ``"DeviceType"``.
            timeout: Seconds to wait for the reply.

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
        Like :meth:`reconnect`, but raises on failure.

        Raises:
            ConnectionError: Reconnecting failed.
            RuntimeError: Called after :meth:`disconnect`.

        Returns:
            Always True.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_disconnect(self) -> None:
        """
        Like :meth:`disconnect`, but raises if stopping the toy or closing the connection failed. The toy is
        disconnected either way.

        Raises:
            ConnectionError: Stopping the toy or closing the connection failed.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_intensity1(self, level: int) -> bool:
        """
        Like :meth:`intensity1`, but raises on failure.

        Args:
            level: 0 to :attr:`max_intensity`. Values outside are clamped.

        Raises:
            ConnectionError: The command could not be sent, or no reply came in time.
            UnexpectedToyResponse: The toy refused the command, e.g., ``ERROR`` instead of ``OK``.

        Returns:
            Always True.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_intensity2(self, level: int) -> bool:
        """
        Like :meth:`intensity2`, but raises on failure.

        Args:
            level: 0 to :attr:`max_intensity`. Values outside are clamped.

        Raises:
            ConnectionError: The command could not be sent, or no reply came in time.
            UnexpectedToyResponse: The toy refused the command, e.g., ``ERROR`` instead of ``OK``.

        Returns:
            True if the toy supports a secondary capability, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_stop(self) -> bool:
        """
        Like :meth:`stop`, but raises on failure.

        Raises:
            ConnectionError: A command could not be sent, or no reply came in time.
            UnexpectedToyResponse: The toy refused a command, e.g., ``ERROR`` instead of ``OK``.

        Returns:
            Always True.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_change_rotation_direction(self) -> bool:
        """
        Like :meth:`change_rotation_direction`, but raises on failure.

        Raises:
            ConnectionError: The command could not be sent, or no reply came in time.
            UnexpectedToyResponse: The toy refused the command, e.g., ``ERROR`` instead of ``OK``.

        Returns:
            True if the toy supports rotation, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_get_battery_level(self) -> Optional[int]:
        """
        Like :meth:`get_battery_level`, but raises on failure.

        Raises:
            ConnectionError: The query could not be sent, or no reply came in time.
            UnexpectedToyResponse: The reply is not a battery level.

        Returns:
            Battery level as a percentage (0-100), or None if the toy has no battery.
        """
        raise NotImplementedError

    @abstractmethod
    async def strict_direct_command(self, command: str, timeout: float = 3.0) -> str:
        """
        Like :meth:`direct_command`, but raises on failure.

        Args:
            command: The command in the toy's protocol, e.g., ``"DeviceType"``.
            timeout: Seconds to wait for the reply.

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
        Wait for the toy's next reply.

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
        Wait for the toy's next reply.

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
