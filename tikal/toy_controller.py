"""
Part of the High-level API: Provides representations of toys.

This module wraps the low-level ToyBLED in synchronous methods and adds advanced features:

- **Synchronous API**: All methods are synchronous (non-async), making them easy to use from regular Python code. Commands are queued and executed asynchronously by ToyHub.
- **Pattern Playback**: Set time-based patterns that automatically control toy intensities.
- **Pause/Block States**: Temporarily halt toy actions while maintaining pattern state.
- **Callback Support**: Optional callbacks provide feedback when commands complete.

This module provides:

- :class:`ToyController`: Abstract base class defining the controller interface
- :class:`LovenseController`: Concrete implementation for Lovense brand toys

Note:
    You should not instantiate controllers. They are created for you by :class: ToyHub
    The ToyHub manages the background communication loop that processes queued commands and handles pattern playback.
"""

import time
import traceback
from typing import Optional, Callable, Any
from abc import ABC, abstractmethod
from logging import getLogger
from collections import deque

from . import ROTATION_TOY_NAMES
from .toy_bled import ToyBLED, LovenseBLED
from .toy_data import LOVENSE_TOY_NAMES


class ToyController(ABC):
    """
    Abstract base class for high-level toy control.

    Extends the low-level ToyBLED interface with synchronous methods, command queueing,
    and pattern playback capabilities.

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
        toy: Low-level toy object (ToyBLED instance) for BLE communication.
        toy_id: Unique identifier for the toy (typically the Bluetooth address).
        logger_name: Name of the logger to use. Use empty string for root logger.

    Note:
        This class should not be instantiated directly. Use ToyHub's connection methods to get controller instances.
    """

    def __init__(self, toy: ToyBLED, toy_id: str, logger_name: str):
        self._toy = toy
        self._toy_id = toy_id
        self._log = getLogger(logger_name)

        # Command queue
        self._command_queue: deque[tuple[Callable, Optional[Callable[[Any], None]]]] = (
            deque()
        )

        # Pattern state
        self._pattern: list[tuple[int, int, int]] = []
        self._pattern_wraparound = True
        self._is_paused = False
        self._is_blocked = False
        self._pattern_elapsed_time: float = (
            0.0  # Time elapsed in the pattern (excluding pauses)
        )
        self._segment_start_time: Optional[float] = (
            None  # Real time when the current segment started
        )
        self._pause_segment_elapsed: float = (
            0.0  # Time elapsed in the segment before a pause
        )

        self._last_values: dict[str, int | None] = {
            "intensity1": None,
            "intensity2": None,
        }
        self._accepted_pause = False
        self._connected = False
        self._log.info(f"ToyController initialized for {toy_id}")

    @property
    def is_paused(self):
        """
        Check if pattern playback is currently paused.

        When paused, the pattern timer stops advancing and toy intensities are set to zero.
        Manual commands can override the intensity levels

        Returns:
            bool: True if paused, False otherwise.
        """
        return self._is_paused

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
    def toy_id(self):
        """
        Get the unique identifier for this toy.

        Returns:
            str: Toy ID (typically the Bluetooth address).
        """
        return self._toy_id

    @property
    def model_name(self):
        """
        Get the model name of the toy.

        Returns:
            str: Model name (e.g., "Nora", "Lush").
        """
        return self._toy.model_name

    @property
    def connected(self):
        """
        Check if the toy is currently connected.

        When disconnected, commands are queued but not sent. Upon reconnection, queued commands are processed.

        Returns:
            bool: True if connected, False otherwise.
        """
        return self._connected

    @connected.setter
    def connected(self, value: bool):
        """
        Set the connection state (internal use only).

        This setter is called by ToyHub when the connection state changes. You should not call this

        Args:
            value: New connection state.
        """
        self._connected = value

    @property
    @abstractmethod
    def intensity_names(self) -> tuple[str, str | None]:
        """
        Get the display names for the toy's capabilities.

        Returns:
            tuple[str, str | None]: A tuple of (primary_name, secondary_name).
            The secondary name is None if the toy has only one capability.

        Example::

                names = toy.intensity_names
                print(f"Primary: {names[0]}")  # example: Vibration
                if names[1]:
                    print(f"Secondary: {names[1]}")  # example: Rotation
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def intensity_max_value(self) -> int:
        """
        Get the maximum intensity value for this toy.

        Returns:
            int: Maximum intensity value (e.g., 20 for Lovense toys).

        Example::

                max_val = toy.intensity_max_value
                toy.intensity1(max_val)  # Set to maximum
        """
        raise NotImplementedError

    def change_rotate_direction_available(self) -> bool:
        """
        Check if the toy supports changing the rotation direction.

        Returns:
            bool: True if the rotation direction can be changed, False otherwise.

        Example::

                if toy.change_rotate_direction_available():
                    toy.change_rotate_direction()
        """
        return self._toy.model_name in ROTATION_TOY_NAMES

    def toggle_pause(self) -> bool:
        """
        Toggle pattern playback pause state.

        When paused:
        - if a pattern is active, it stops advancing.
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
        if not self._is_paused:
            self._set_paused(True)
            self.stop()
            self._is_blocked = False  # I don't want to pause and block at the same time
            return True
        else:
            self._set_paused(False)
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
            self._set_paused(False)  # I don't want to pause and block at the same time'
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
        -intensity1: Primary capability intensity (0-max)
        -intensity2: Secondary capability intensity (0-max)
        The maximum possible intensity can be looked up via :meth:`intensity_max_value`.
        An empty list clears the pattern.

        Args:
            pattern: List of (duration_ms, intensity1, intensity2) tuples
            wraparound: If True, the pattern loops indefinitely. If False, the pattern stops after one playthrough.
            reset_time: If True, restart pattern from beginning. If False, maintain current position in pattern.

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
        self._pattern = pattern
        self._pattern_wraparound = wraparound
        if reset_time:
            self._restart_pattern()
        # If not resetting, keep the current elapsed time to maintain position
        if not pattern:  # ensure that intensities are at 0
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
        if self._is_paused:
            # When paused, return the frozen elapsed time
            return self._pattern_elapsed_time

        if self._segment_start_time is None:
            return 0.0

        # Calculate time elapsed in current segment
        current_time = time.time() * 1000
        segment_elapsed = current_time - self._segment_start_time

        # Total elapsed = pattern elapsed at segment start + current segment elapsed
        return self._pattern_elapsed_time + segment_elapsed

    @abstractmethod
    def intensity1(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Set the intensity of the primary capability

        Commands are queued and executed asynchronously by ToyHub (every 50ms).
        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts

        Args:
            level: Intensity level. The Valid range depends on the toy type. Values outside the range are clamped.
            callback: Optional callback is invoked when the command completes. Receives True if successful, False if blocked or failed.

        Example::

                # Simple command
                toy.intensity1(15)

                # With callback
                def on_complete(success):
                    print("Succeeded:", success)

                toy.intensity1(15, callback=on_complete)

        Note:
            If the toy is blocked, the callback receives False immediately and no command is sent.
            If disconnected, the command is queued and sent upon reconnection.
        """
        raise NotImplementedError

    @abstractmethod
    def intensity2(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Set the intensity of the secondary capability

        Behavior is identical to :meth:`intensity1` but controls the secondary capability (e.g., rotation, air pump)
        Safe to call on toys without a secondary capability (will return true but do nothing).

        Args:
            level: Intensity level. The valid range depends on the toy type. Values outside the range are clamped.
            callback: Optional callback is invoked when the command completes.

        Example::

                # Set secondary capability intensity to medium
                toy.intensity2(10)
        """
        raise NotImplementedError

    @abstractmethod
    def change_rotate_direction(
        self, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Change rotation direction (if supported).

        This method toggles the rotation direction for toys with rotation capability.
        Safe to call on all toys. Does nothing and returns True via callback if rotation is not supported.

        Args:
            callback: Optional callback invoked when command completes. Receives True if successful or not supported, False if failed.

        Example::

                toy.change_rotate_direction(callback=lambda ok: print("Direction changed" if ok else "Failed"))

        Note:
            You can use :meth:`change_rotate_direction_available` to check support before calling.
        """
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

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

    @abstractmethod
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
        raise NotImplementedError

    # ------------------------------------------------------------------------------------------------------------------
    # Private Methods
    # ------------------------------------------------------------------------------------------------------------------

    @property
    def toy(self):
        """
        Get the underlying low-level toy object (internal use only)

        Returns:
            ToyBLED: The low-level toy object.

        Warning:
            This is an internal method used by ToyHub and not meant to be used by you.
        """
        return self._toy

    @toy.setter
    def toy(self, toy: ToyBLED):
        """
        Set the underlying low-level toy object (internal use only)

        Args:
            toy: New ToyBLED instance.

        Warning:
            This is an internal method used by ToyHub and not meant to be used by you.
        """
        self._toy = toy

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
        if not self._pattern:
            return

        # Handle a paused or blocked state
        if self._is_paused or self._is_blocked:
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
            pattern_time = self.get_pattern_time()
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

    def _restart_pattern(self) -> None:
        """Restart pattern playback from the beginning"""
        self._pattern_elapsed_time = 0.0
        self._segment_start_time = time.time() * 1000
        self._pause_segment_elapsed = 0.0

    def _set_paused(self, paused: bool) -> None:
        """
        Update the pause state and adjust timing.

        When entering pause, freezes the pattern timer. When exiting pause, resumes timing from the frozen position.

        Args:
            paused: New pause state.
        """
        if paused == self._is_paused:
            return

        self._is_paused = paused
        if paused:
            # Entering pause: update elapsed time up to now
            if self._segment_start_time is not None:
                current_time = time.time() * 1000
                segment_elapsed = current_time - self._segment_start_time
                self._pattern_elapsed_time += segment_elapsed
                self._pause_segment_elapsed = segment_elapsed
                self._segment_start_time = None
        else:
            # Exiting pause: reset segment start time now
            self._segment_start_time = time.time() * 1000

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
        pattern = self._pattern
        if not pattern:
            return 0, 0

        # Calculate total pattern duration
        total_duration = sum(duration for duration, _, _ in pattern)

        if total_duration == 0:
            return 0, 0

        # Handle wraparound
        if self._pattern_wraparound:
            pattern_time = pattern_time % total_duration
        elif pattern_time >= total_duration:
            return 0, 0

        # Find the segment we're in
        elapsed = 0.0
        for duration, intensity1, intensity2 in pattern:
            if pattern_time < elapsed + duration:
                return intensity1, intensity2
            elapsed += duration

        # Should not reach here, but return last segment values
        return pattern[-1][1], pattern[-1][2]


class LovenseController(ToyController):
    """
    High-level controller for Lovense toys.

    Extends the low-level LovenseBLED class with synchronous methods, command queueing,
    and pattern playback capabilities.

    Args:
        toy: Low-level LovenseBLED instance.
        toy_id: Unique identifier (Bluetooth address).
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

    def __init__(self, toy: LovenseBLED, toy_id: str, logger_name: str):
        self._toy: LovenseBLED = toy
        super().__init__(toy, toy_id, logger_name)

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
        intensity1_name = LOVENSE_TOY_NAMES[self._toy.model_name].intensity1_name
        intensity2_name = LOVENSE_TOY_NAMES[self._toy.model_name].intensity2_name
        return intensity1_name, intensity2_name

    @property
    def intensity_max_value(self) -> int:
        """
        Get maximum intensity value for Lovense toys.

        Returns:
            int: Always 20 for Lovense toys.

        Note:
            Some capabilities (like Max's air pump) use different ranges. Those are automatically scaled for you.
        """
        return 20

    def intensity1(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Set the intensity of the primary capability

        Commands are queued and executed asynchronously by ToyHub (every 50ms).
        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts

        Args:
            level: Intensity level (0-20). Values outside the range are clamped.
            callback: Optional callback is invoked when the command completes. Receives True if successful, False if blocked or failed.

        Example::

                # Simple command
                toy.intensity1(15)

                # With callback
                def on_complete(success):
                    print("Succeeded:", success)

                toy.intensity1(15, callback=on_complete)

        Note:
            If the toy is blocked, the callback receives False immediately and no command is sent.
            If disconnected, the command is queued and sent upon reconnection.
        """
        if self._is_blocked:
            if callback:
                callback(False)
            return
        if self._pattern and not self._is_paused:
            self._set_paused(True)  # avoid the pattern overriding the command

        async def _execute():
            return await self._toy.intensity1(level)

        self._schedule_command(_execute, callback)

    def intensity2(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Set the intensity of the secondary capability

        Behavior is identical to :meth:`intensity1` but controls the secondary capability (e.g., rotation, air pump)
        Safe to call on toys without a secondary capability (will return true but do nothing).

        Args:
            level: Intensity level (0-20). Values outside the range are clamped.
            callback: Optional callback is invoked when the command completes.

        Example::

                # Set secondary capability intensity to medium
                toy.intensity2(10)
        """
        if self._is_blocked:
            if callback:
                callback(False)
            return

        if self._pattern and not self._is_paused:
            self._set_paused(True)  # avoid the pattern overriding the command

        async def _execute():
            return await self._toy.intensity2(level)

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

        if self._pattern and not self._is_paused:
            self._set_paused(True)

        self._schedule_command(_execute, callback)

    def change_rotate_direction(
        self, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Change rotation direction (if supported).

        This method toggles the rotation direction for toys with rotation capability.
        Safe to call on all toys. Does nothing and returns True via callback if rotation is not supported.

        Args:
            callback: Optional callback invoked when command completes. Receives True if successful or not supported, False if failed.

        Example::

                toy.change_rotate_direction(callback=lambda ok: print("Direction changed" if ok else "Failed"))

        Note:
            You can use :meth:`change_rotate_direction_available` to check support before calling.
        """

        async def _execute():
            return await self._toy.rotate_change_direction()

        self._schedule_command(_execute, callback)

    def get_battery_level(self, callback: Callable[[Optional[int]], None]) -> None:
        """
        Retrieve the toy's battery level.

        Args:
            callback: Callback invoked with battery level (0-100%) or None if an error occurred.
                Unlike most methods, here providing a callback is required (not optional).

        Example::

                def show_battery(level):
                    if level is not None:
                        print(f"Battery: {level}%")
                    else:
                        print("Error retrieving battery level")
                toy.get_battery_level(show_battery)

        Note:
            You can provide a callback to ToyHub as well. If you do so, ToyHub queries battery levels regularly and
            invokes the hub's battery callback. This method serves as an alternative to querying the battery level
        """

        async def _execute():
            return await self._toy.get_battery_level()

        self._schedule_command(_execute, callback)

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
