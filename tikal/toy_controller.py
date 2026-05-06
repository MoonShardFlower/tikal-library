"""
Part of the High-level API: Provides representations of toys.

This module wraps the low-level Toy in synchronous methods and adds advanced features:
- **Synchronous API**: All methods are synchronous (non-async), making them easy to use from regular Python code. Commands are queued and executed asynchronously by ToyHub.
- **Pattern Playback**: Set time-based patterns that automatically control toy intensities.
- **Pause/Block States**: Temporarily halt toy actions while maintaining the pattern state.
- **Callback Support**: Optional callbacks provide feedback when a command completes.

This module provides:

- :class:`ToyController`: Abstract base class defining the controller interface
- :class:`LovenseController`: Concrete implementation for Lovense brand toys

Note:
    You should not instantiate controllers. They are created for you by :class: ToyHub
    The ToyHub manages the background communication loop that processes queued commands and handles pattern playback.
"""

import traceback
from abc import ABC, abstractmethod
from collections import deque
from logging import getLogger
from typing import Any, Callable, Optional

from .toy import Lovense, Toy
from .utils import PatternHandler


class ToyController(ABC):
    """
    Abstract base class for high-level toy control.

    Extends the low-level Toy interface with synchronous methods, command queueing, and pattern playback capabilities.

    Example::

            # Get controller from ToyHub (see :class: ToyHub for that)
            controllers = hub.connect_toys_blocking(discovered_toys)
            toy = controllers[0]

            # Manually control the toy
            toy.intensity1(15)  # Set primary capability to level 15
            toy.intensity2(10)  # Set secondary capability to level 10

            # Set a pattern (duration_ms, intensity1, intensity2)
            pattern = [
                (1000, 10, 5),   # 1 second at intensity 10/5
                (500, 0, 0),     # 0.5 seconds off
                (1000, 20, 20),  # 1 second at max intensity
            ]
            toy.set_pattern(pattern, wraparound=True)

            # Pause/resume pattern
            toy.toggle_pause()  # Pauses pattern, sets toys intensity levels to  0
            toy.toggle_pause()  # Resumes pattern

    Args:
        toy: Low-level toy object (Toy instance) for BLE communication.
        logger_name: Name of the logger to use. Use empty string for root logger.

    Note:
        This class should not be instantiated directly. Use ToyHub's connection methods to get controller instances.
    """

    def __init__(self, toy: Toy, logger_name: str):
        self._toy = toy
        self._log = getLogger(logger_name)

        # State
        self._command_queue: deque[tuple[Callable, Optional[Callable[[Any], None]]]] = (
            deque()
        )
        self._pattern_handler = PatternHandler()
        self._last_values: dict[str, int | None] = {
            "intensity1": None,
            "intensity2": None,
        }
        self._accepted_pause = False
        self._connected = False
        self._is_blocked = False

        self._log.info(f"ToyController initialized for {toy.toy_id}")

    @property
    def model_name(self):
        """
        Get the model name of the toy.

        Returns:
            str: Model name (e.g., "Nora", "Lush").
        """
        return self._toy.model_name

    @property
    def toy_id(self):
        """
        Get the unique identifier for this toy.

        Returns:
            str: Toy ID (typically the Bluetooth address).
        """
        return self._toy.toy_id

    @property
    def name(self) -> str:
        """Returns a human-readable identifier of the toy e.g., Bluetooth name."""
        return self._toy.name

    @property
    def brand(self) -> str:
        """Returns a human-readable identifier of the toy brand e.g., 'Lovense'."""
        return self._toy.brand

    @property
    def is_connected(self):
        """
        Check if the toy is currently connected.

        When disconnected, commands are queued but not sent. Upon reconnection, queued commands are processed.

        Returns:
            bool: True if connected, False otherwise.
        """
        return self._connected

    @property
    def change_rotate_direction_available(self) -> bool:
        """
        Check if the toy supports changing the rotation direction.

        Returns:
            bool: True if the rotation direction can be changed, False otherwise.

        Example::

                if toy.change_rotate_direction_available:
                    toy.change_rotate_direction()
        """
        return self._toy.change_rotation_direction_available

    @property
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
        return self._toy.intensity_names

    @property
    def max_intensity(self) -> int:
        """
        Get the maximum intensity value for this toy.

        Returns:
            int: Maximum intensity value (e.g., 20 for Lovense toys).

        Example::

                max_val = toy.max_intensity
                await toy.intensity1(max_val)  # Set to maximum
        """
        return self._toy.max_intensity

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
        return self._toy.current_intensities

    @property
    def is_paused(self):
        """
        Check if pattern playback is currently paused.

        When paused, the pattern timer stops advancing and toy intensities are set to zero.
        Manual commands can override the intensity levels

        Returns:
            bool: True if paused, False otherwise.
        """
        return self._pattern_handler.is_paused

    @property
    def is_blocked(self):
        """
        Check if the toy is currently blocked.

        When blocked, all intensity commands (manual and pattern-based) are rejected.
        Toy's intensities are forced to 0.

        Returns:
            bool: True if blocked, False otherwise.
        """
        return self._is_blocked

    @property
    def pattern_version(self) -> int:
        """
        Each time the pattern state changes, the version number is incremented.

        Returns:
            current version number.
        """
        return self._pattern_handler.pattern_version

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

    def toggle_pause(self) -> bool:
        """
        Toggle pattern playback pause state.

        When paused:
        - If a pattern is active, it stops advancing.
        - Toy intensities are set to zero, but manual commands can override this.
        - Block state is cleared if active (toy cannot be paused and blocked at the same time)

        Returns:
            bool: True if now paused, False if now unpaused.

        Example::

                # Pause pattern playback
                is_paused = toy.toggle_pause()
                print(f"Paused: {is_paused}")

                # Resume
                is_paused = toy.toggle_pause()
                print(f"Paused: {is_paused}")
        """
        self._log.info(f"ToyController toggle pause: {self.toy_id}")
        if not self._pattern_handler.is_paused:
            self._pattern_handler.set_paused(True)
            self.stop()
            self._is_blocked = False  # I don't want to pause and block at the same time
            return True
        else:
            self._pattern_handler.set_paused(False)
            return False

    def toggle_block(self) -> bool:
        """
        Toggle block state.

        When blocked:
        - All intensity commands are rejected (return False via callback)
        - Toy intensities are forced to zero
        - Pattern continues advancing but doesn't control the toy
        - Pause state is cleared if active (toy cannot be paused and blocked at the same time)

        Returns:
            bool: True if now blocked, False if now unblocked.

        Example::

                # Block all toy commands
                is_blocked = toy.toggle_block()

                # Try to control (will fail)
                toy.intensity1(10, callback=lambda success: print(success))  # False

                # Unblock
                is_blocked = toy.toggle_block()
        """
        self._log.info(f"ToyController toggle block: {self.toy_id}")
        if not self._is_blocked:
            self._is_blocked = True
            self.stop()
            self._pattern_handler.set_paused(
                False
            )  # I don't want to pause and block at the same time'
            return True
        else:
            self._is_blocked = False
            return False

    def set_pattern(
        self,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> None:
        """
        Set a time-based pattern for automatic toy control.

        Patterns are lists of segments. Each segment is a tuple of (duration_ms, intensity1, intensity2) where:
        - duration_ms: How long this segment lasts (milliseconds)
        - intensity1: Primary capability intensity (0-max)
        - intensity2: Secondary capability intensity (0-max)
        The maximum possible intensity can be looked up via :meth:`intensity_max_value`. An empty list clears the pattern.

        Args:
            pattern: List of (duration_ms, intensity1, intensity2) tuples
            wraparound: If True, the pattern loops indefinitely. If False, the pattern stops after one playthrough.
            reset_time: If True, restart the pattern from the beginning. If False, maintain the current position in the pattern.

        Example::

                # Simple pulse pattern
                pattern = [
                    (500, 10, 0),   # 0.5s at intensity 10
                    (500, 0, 0),    # 0.5s off
                ]
                toy.set_pattern(pattern, wraparound=True)

                # Clear pattern
                toy.set_pattern([])

        Note:
            Manual intensity commands automatically pause pattern playback to avoid conflicts.
            Call ``toggle_pause()`` to resume the pattern.
        """
        self._log.info(f"ToyController sets pattern for {self.toy_id}: {pattern}")
        self._pattern_handler.set_pattern(pattern, wraparound, reset_time)
        if not pattern:  # ensure that intensities are 0 if pattern is cleared
            self.stop()

    def get_pattern_time(self) -> float:
        """
        Get elapsed time in the current pattern (Time spent paused does not count toward elapsed time).

        Returns:
            float: Time elapsed in milliseconds since the pattern start or last wraparound. Returns 0.0 if no pattern is set

        Example::

                elapsed = toy.get_pattern_time()
                print(f"Pattern position: {elapsed}ms")
        """
        return self._pattern_handler.get_pattern_time()

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """
        Get intensity values at a specific time in the pattern.

        Args:
            pattern_time: Time position in the pattern (milliseconds).

        Returns:
            tuple[int, int]: (intensity1, intensity2) values at that time.

        Note:
            For wraparound patterns, time is taken modulo the total pattern duration.
            For non-wraparound patterns, returns (0, 0) after the pattern completes.
        """
        return self._pattern_handler.get_pattern_values(pattern_time)

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """Gets the complete pattern state for visualization.

        Pattern state consists of:
        - pattern: List of tuples (duration, intensity1, intensity2) defining the pattern segments.
        - wraparound: Whether the pattern repeats from the beginning after completing the last segment. If False, both
        Intensities are 0 after the last segment.
        - is_paused: Whether the pattern is currently paused. Paused patterns do not advance
        - elapsed_time: Time elapsed since the start of the pattern or last wraparound in ms

        Returns:
            tuple: (pattern, wraparound, is_paused, elapsed_time)
        """
        return self._pattern_handler.get_pattern_data()

    def intensity1(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Set the intensity of the primary capability.

        Commands are queued and executed asynchronously by ToyHub (every 50ms).
        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts.

        Args:
            level: Intensity level. The Valid range is [0, self.max_intensity]. Values outside the range are clamped.
            callback: Optional callback is invoked when the command completes. Receives True if successful, False if blocked or failed.

        Example::

                toy.intensity1(15)  # Simple command

                def on_complete(success):
                    print("Succeeded:", success)

                toy.intensity1(toy.max_intensity, callback=on_complete)  # With callback

        Note:
            If the toy is blocked, the callback receives False immediately and no command is sent.
            If disconnected, the command is queued and sent upon reconnection.
        """

        async def _execute():
            return await self._toy.intensity1(level)

        if self._is_blocked:
            if callback:
                callback(False)
            return

        # avoid the pattern overriding the command
        self._pattern_handler.set_paused(True)
        self._schedule_command(_execute, callback)

    def intensity2(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Set the intensity of the secondary capability.

        Behavior is identical to :meth:`intensity1` but controls the secondary capability (e.g., rotation, air pump)
        Safe to call on toys without a secondary capability (will return true but do nothing).

        Args:
            level: Intensity level. The valid range is [0, self.max_intensity]. Values outside the range are clamped.
            callback: Optional callback is invoked when the command completes. Receives True if successful, False if blocked or failed.

        Example::

                toy.intensity2(toy.max_intensity // 2)  # Set secondary capability intensity to medium
        """

        async def _execute():
            return await self._toy.intensity2(level)

        if self._is_blocked:
            if callback:
                callback(False)
            return

        # avoid the pattern overriding the command
        self._pattern_handler.set_paused(True)
        self._schedule_command(_execute, callback)

    def change_rotation_direction(
        self, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Change rotation direction (if supported).

        This method toggles the rotation direction for toys with rotation capability.
        Safe to call on all toys. Does nothing and returns True via callback if rotation is not supported.

        Args:
            callback: Optional callback invoked when command completes. Receives True if successful or not supported, False if failed.

        Example::

                toy.change_rotation_direction(callback=lambda ok: print("Direction changed" if ok else "Failed"))

        Note:
            You can use property:`change_rotation_direction_available` to check support before calling.
        """

        async def _execute():
            return await self._toy.change_rotation_direction()

        self._schedule_command(_execute, callback)

    def stop(self, callback: Optional[Callable[[bool], None]] = None) -> None:
        """
        Stop all toy actions (set all intensities to zero).

        If a pattern is active and not paused, this method pauses the pattern.

        Args:
            callback: Optional callback invoked when command completes. Receives True if successful, False otherwise.

        Example::

                toy.stop()
                # With confirmation
                toy.stop(callback=lambda ok: print("Stopped" if ok else "Failed"))
        """

        async def _execute():
            return await self._toy.stop()

        # avoid the pattern overriding the command
        self._pattern_handler.set_paused(True)
        self._schedule_command(_execute, callback)

    def get_battery_level(self, callback: Callable[[Optional[int]], None]) -> None:
        """
        Retrieve the toy's battery level.

        Args:
            callback: Callback invoked with battery level (0-100%) or None if unavailable. Unlike most methods, here providing a callback is required (not optional).

        Example::

                def show_battery(level):
                    if level is not None:
                        print(f"Battery: {level}%")
                    else:
                        print("Battery unavailable")
                toy.get_battery_level(show_battery)

        Note:
            You can provide a callback to ToyHub as well. If you do so, ToyHub queries battery levels regularly and
            invokes the hub's battery callback. This method serves as an alternative to querying the battery level
        """

        async def _execute():
            return await self._toy.get_battery_level()

        self._schedule_command(_execute, callback)

    @abstractmethod
    def get_information(self, callback: Callable[[dict[str, str]], None]) -> None:
        """
        Gather detailed information about the toy.

        The Information gathered depends on the toy, but might include the following dictionary keys:
            - 'Battery Level': Battery percentage (e.g., "75%")
            - 'Status': Status code(e.g., "2" for normal)
            - 'Batch number': Manufacturing batch (e.g., "241015")
            - 'Bluetooth Name': BLE device name (e.g., "LVS-Z36D")
            - 'Device type': Device info (e.g., "C:11:ADDRESS")

        Args:
            callback: Callback invoked with a dictionary containing toy information. Keys describe the Information type, values contain the information.

        Example::

                def show_info(info):
                    print("Toy Information:")
                    for key, value in info.items():
                        print(f"{key}:{value}")
                toy.get_information(show_info)
        """
        raise NotImplementedError

    def direct_command(self, command: str, callback: Callable[[str], None]) -> None:
        """
        Send a raw command directly to the toy.

        Use this for accessing toy features not exposed by the library. Requires knowledge of the toy's protocol.

        Args:
            command: Command string in the toy's protocol format (e.g., "DeviceType").
            callback: Callback invoked with the toy's response string. This callback is required (not optional).

        Example::

                def handle_response(response):
                    print(f"Device type response: {response}")
                    # Example: "C:11:0082059AD3BD"
                toy.direct_command("DeviceType", callback=handle_response)
        """

        async def _execute():
            return await self._toy.direct_command(command)

        self._schedule_command(_execute, callback)

    # ------------------------------------------------------------------------------------------------------------------
    # Private Methods
    # ------------------------------------------------------------------------------------------------------------------

    @is_connected.setter
    def is_connected(self, value: bool):
        """
        Set the connection state (internal use only).

        This setter is called by ToyHub when the connection state changes. You should not call this

        Args:
            value: New connection state.
        """
        self._connected = value

    @property
    def toy(self):
        """
        Get the underlying low-level toy object (internal use only)

        Returns:
            Toy: The low-level toy object.

        Warning:
            This is an internal method used by ToyHub and not meant to be used by you.
        """
        return self._toy

    async def process_communication(self) -> None:
        """
        Process queued commands and pattern playback (internal use only)

        This method is called periodically by the ToyHub to execute queued commands and maintain pattern playback.

        Warning:
            This is an internal method used by ToyHub and not meant to be used by you.
        """
        if not self._toy or not self._connected:
            return

        # Process queued commands first
        await self._process_command_queue()

        # Then handle pattern playback
        if not self._pattern_handler.has_active_pattern:
            return

        # Handle a paused or blocked state
        if self._pattern_handler.is_paused or self._is_blocked:
            if not self._accepted_pause:
                # First time entering paused/blocked state - send stop command
                await self._toy.stop()
                self._last_values["intensity1"] = None
                self._last_values["intensity2"] = None
                self._accepted_pause = True
            # If already paused/blocked, do nothing (no repeated stop commands)

        # Handle active state
        else:
            self._accepted_pause = False

            # Get current values and send commands if values have changed
            pattern_time = self._pattern_handler.get_pattern_time()
            intensity1_value, intensity2_value = self.get_pattern_values(pattern_time)

            if intensity1_value != self._last_values["intensity1"]:
                await self._toy.intensity1(intensity1_value)
                self._last_values["intensity1"] = intensity1_value

            if intensity2_value != self._last_values["intensity2"]:
                await self._toy.intensity2(intensity2_value)
                self._last_values["intensity2"] = intensity2_value

    async def _process_command_queue(self) -> None:
        """
        Execute all queued commands in order.

        Commands are executed sequentially, with callbacks invoked after each command completes.
        """
        while self._command_queue:
            command, callback = self._command_queue.popleft()
            try:
                result = await command()
                if callback:
                    callback(result)
            except Exception as e:
                self._log.error(
                    f"Error executing command {command}: {e} with details {traceback.format_exc()}"
                )
                if callback:
                    callback(None)

    def _schedule_command(
        self, command: Callable, callback: Optional[Callable[[Any], None]] = None
    ) -> None:
        """
        Add a command to the execution queue.

        Args:
            command: Async callable that executes the command.
            callback: Optional callback to invoke with the result.
        """
        self._command_queue.append((command, callback))


class LovenseController(ToyController):
    """
    High-level controller for Lovense toys.

    Extends the low-level Lovense class with synchronous methods, command queueing, and pattern playback capabilities.

    Args:
        toy: Low-level Lovense instance.
        logger_name: Name of the logger to use. Use empty string for root logger.

    Example::

            # Connection (see :class ToyHub)
            controllers = hub.connect_toys_blocking(discovered_toys)
            toy = controllers[0]  # LovenseController instance

            # Manual control
            toy.intensity1(15)  # Primary capability
            toy.intensity2(10)  # Secondary capability

            # Pattern control
            pattern = [
                (1000, 10, 5),
                (500, 0, 0),
                (1000, 20, 10),
            ]
            toy.set_pattern(pattern, wraparound=True)

            # Pause/resume
            toy.toggle_pause()

            # Get battery level
            toy.get_battery_level(lambda lvl: print(f"Battery: {lvl}%"))

            # Advanced features
            if toy.change_rotate_direction_available():
                toy.change_rotate_direction()

    Note:
        This class should not be instantiated directly. Use ToyHub's connection methods to get controller instances.
    """

    def __init__(self, toy: Lovense, logger_name: str):
        self._toy: Lovense = toy
        super().__init__(toy, logger_name)

    def get_information(self, callback: Callable[[dict[str, str]], None]) -> None:
        """
        Gather detailed information about the toy.

        The following information is gathered (Key, Value pairs of the dictionary):
            - 'Battery Level': Battery percentage (e.g., "75%")
            - 'Status': Status code ("2" for normal)
            - 'Batch number': Manufacturing batch (e.g., "241015")
            - 'Bluetooth Name': BLE device name (e.g., "LVS-Z36D")
            - 'Device type': Device info (e.g., "C:11:ADDRESS")

        Args:
            callback: Callback invoked with a dictionary containing toy information.
                Keys describe the Information type, values contain the information.

        Example::

                def show_info(info):
                    print("Toy Information:")
                    for key, value in info.items():
                        print(f"{key}:{value}")
                toy.get_information(show_info)
        """

        async def _execute():
            info = dict()
            battery = await self._toy.get_battery_level()
            status = await self._toy.get_status()
            batch = await self._toy.get_batch_number()
            device_type = await self._toy.get_device_type()
            bluetooth_name = self._toy.name

            info["Battery level"] = f"{battery}%" if battery is not None else "Unknown"
            for name, value in [
                ("Status", status),
                ("Batch number", batch),
                ("Bluetooth Name", bluetooth_name),
                ("Device type", device_type),
            ]:
                info[name] = str(value) if value is not None else "Unknown"
            return info

        self._schedule_command(_execute, callback)

    def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        Args:
            model_name: New model name. Must be in LOVENSE_TOY_NAMES.keys() of module ToyData. Case Insensitive.

        Raises:
            ValidationError: If model_name is not a valid Lovense model.

        Example::

                # Update model name in case it was set incorrectly while building the connection via the ConnectionBuilder
                toy.set_model_name("Nora")
        """
        self._toy.set_model_name(model_name)
