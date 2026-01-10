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

    def __init__(self, toy: ToyBLED, toy_id: str, logger_name: str):
        """
        Extends the low-level ToyBLED class, providing convenient synchronized methods (instead of async) and
        pattern-related functionalities. You are not meant to instantiate this class yourself. The ToyHub will produce
        and return instances of this class for you.

        Args:
            toy: Toy object representing the physical lovense toy.
            toy_id: Unique ID of the toy.
            logger_name: Name of the logger to use. Empty string for root logger.
        """
        self._toy = toy
        self._toy_id = toy_id
        self._model_name = toy.model_name
        self._log = getLogger(logger_name)

        # Command queue: stores tuples of (coroutine, callback)
        self._command_queue: deque[tuple[Callable, Optional[Callable[[Any], None]]]] = (
            deque()
        )

        # Pattern state
        self._pattern: list[tuple[int, int, int]] = []
        self._pattern_wraparound = True

        # Playback state - now duration-based
        self._is_paused = False
        self._is_blocked = False
        self._pattern_elapsed_time: float = 0.0  # Time elapsed in the pattern (excluding pauses)
        self._segment_start_time: Optional[float] = None  # Real time when the current segment started
        self._pause_segment_elapsed: float = 0.0  # Time elapsed in the segment before a pause

        self._last_values: dict[str, int | None] = {
            "intensity1": None,
            "intensity2": None,
        }
        self._accepted_pause = False
        self._log.info(f"ToyController initialized for {toy_id}")
        self._connected = False

    @property
    def is_paused(self):
        """Returns True if the toy is currently paused, False otherwise."""
        return self._is_paused

    @property
    def is_blocked(self):
        """Returns True if the toy is currently blocked, False otherwise."""
        return self._is_blocked

    @property
    def toy_id(self):
        """Returns the unique ID of the toy. If the toy is connected via bluetooth: toy_id == bluetooth address."""
        return self._toy_id

    @property
    def model_name(self):
        """Returns the model name of the toy'"""
        return self._model_name

    @property
    def connected(self):
        """
        Returns True if the toy is currently connected False otherwise. If False commands aren't set to the toy.
        Commands aren't lost. Upon re-establishment of the connection, they are sent to the toy
        """
        return self._connected

    @connected.setter
    def connected(self, value: bool):
        """Called by the ToyHub when the connection is lost/restored. You are not supposed to call this!"""
        self._connected = value

    @property
    @abstractmethod
    def intensity_names(self) -> tuple[str, str | None]:
        """Returns the names of intensity1 and intensity2 for this toy type."""
        raise NotImplementedError

    @property
    @abstractmethod
    def intensity_max_value(self) -> int:
        """Returns the maximum intensity value for this toy type."""
        raise NotImplementedError

    def change_rotate_direction_available(self) -> bool:
        """Returns true if the toy supports changing the rotation direction, false otherwise."""
        return self._model_name in ROTATION_TOY_NAMES

    def toggle_pause(self) -> bool:
        """
        Toggle pause state. Returns true if paused, False if unpaused. If paused and a pattern is set, the pattern
        progresses, but toy intensities are forced to remain 0

        Returns:
            bool: True if paused, False if unpaused
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
        Toggle block state. Returns true if blocked, False if unblocked. If a pattern is set
        If blocked, the toys intensities are forced to remain 0 (even when manual commands are used)

        Returns:
            bool: True if blocked, False if unblocked
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
        Sets a new pattern.
        Patterns are defined as a list of tuples (duration_ms, intensity1, intensity2). Each tuple represents a segment
        with a duration in milliseconds.
        Example: [(100, 5, 5), (200, 0, 0)] sets intensity1=intensity2=5 for 100ms, then intensity1=intensity2=0 for 200ms.
        An empty list means that no pattern is set and allows the toy to be controlled manually.

        Args:
            pattern: Pattern to be set
            wraparound: Whether the pattern should wrap around and be repeated indefinitely
            reset_time: Whether to reset the pattern playback position.
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
        Get time elapsed in the current pattern (excluding pauses). Returns 0.0 if no pattern is set

        Returns:
            float: Time elapsed since pattern start or last wraparound (in ms)
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

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """
        Get pattern values at the specific time.

        Args:
            pattern_time: Time (in ms) elapsed in the pattern.

        Returns:
            tuple[int, int]: (intensity1, intensity2) values at the specified time
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

    @abstractmethod
    def intensity1(
            self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Sets the intensity of the toy's primary capability to the specified level.
        Values outside the valid range (0-20) are clamped. Does not work if the toy is currently blocked.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            level: level to set the toy's primary intensity to.
            callback: Optional callback called with True if successful, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    def intensity2(
            self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Sets the intensity of the toy's secondary capability to the specified level.
        Does not work if the toy is currently blocked.
        Always succeeds (but does nothing) if the toy has no secondary intensity.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            level: level to set the toy's secondary intensity to.
            callback: Optional callback called with True if successful, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    def change_rotate_direction(
            self, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Changes the rotation direction of the toy (if available, else does nothing and returns True)
        See self.change_rotate_direction_available() to find out if the toy supports this.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Optional callback called with True if successful, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self, callback: Optional[Callable[[bool], None]] = None) -> None:
        """
        Stops all toy actions (set all intensities to zero).
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Optional callback called with True if successful, False otherwise
        """
        raise NotImplementedError

    @abstractmethod
    def get_battery_level(self, callback: Callable[[Optional[int]], None]) -> None:
        """
        Retrieves the battery level of the toy.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Callback called with the battery level (or None if unavailable)
        """
        raise NotImplementedError

    @abstractmethod
    def get_information(self, callback: Callable[[dict[str, str]], None]) -> None:
        """
        Gathers information about the toy. Needs to send several commands to the Toy to find out the information.
        Does not send commands directly but schedules them to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Callback called with a dictionary containing information about the toy.
                Contains the keys: 'Battery Level', 'Status', 'Batch number', 'Bluetooth name', and 'Device type'
        """
        raise NotImplementedError

    @abstractmethod
    def direct_command(self, command: str, callback: Callable[[str], None]) -> None:
        """
        Sends the specified command to the toy.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            command: String containing the command to send.
            callback: Callback called with the response from the toy (always a string).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------------------------------------------------------
    # Private Methods
    # ------------------------------------------------------------------------------------------------------------------

    @property
    def toy(self):
        """
        Gets the underlying low-level toy object. Called by the ToyHub if needed.
        You are not supposed to call this!
        """
        return self._toy

    @toy.setter
    def toy(self, toy: ToyBLED):
        """
        Sets the underlying low-level toy object. Called by the ToyHub if needed.
        You are not supposed to call this!

        Args:
            toy (ToyBLED): New toy object
        """
        self._toy = toy

    async def process_communication(self) -> None:
        """
        Periodically called by the ToyHub for pattern control and command execution.
        You are not supposed to call this!
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
        """Process all queued commands."""
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
        """Add a command to the execution queue."""
        self._command_queue.append((command, callback))

    def _restart_pattern(self) -> None:
        """Restart pattern timing from the beginning."""
        self._pattern_elapsed_time = 0.0
        self._segment_start_time = time.time() * 1000
        self._pause_segment_elapsed = 0.0

    def _set_paused(self, paused: bool) -> None:
        """
        Sets whether the toy is currently paused. Patterns of paused toys do not advance.

        Args:
            paused: Whether the toy is currently paused.
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


class LovenseController(ToyController):

    def __init__(self, toy: LovenseBLED, toy_id: str, logger_name: str):
        """
        Extends the low-level LovenseBLED class, providing convenient synchronized methods (instead of async) and
        pattern-related functionalities. You are not meant to instantiate this class yourself. The ToyHub will produce
        and return instances of this class for you.

        Args:
            toy: Toy object representing the physical lovense toy.
            toy_id: Unique ID of the toy.
            logger_name: Name of the logger to use. Empty string for root logger.
        """
        self._toy: LovenseBLED = toy
        super().__init__(toy, toy_id, logger_name)

    @property
    def intensity_names(self) -> tuple[str, str | None]:
        """
        Returns the names of intensity1 and intensity2 for Lovense toys.
        The second value is None if the toy does not have a secondary capability
        """
        intensity1_name = LOVENSE_TOY_NAMES[self._model_name].intensity1_name
        intensity2_name = LOVENSE_TOY_NAMES[self._model_name].intensity2_name
        return intensity1_name, intensity2_name

    @property
    def intensity_max_value(self) -> int:
        """Returns the maximum intensity value for Lovense toys. For Lovense toys the value is always 20"""
        return 20

    def intensity1(
        self, level: int, callback: Optional[Callable[[bool], None]] = None
    ) -> None:
        """
        Sets the intensity of the toy's primary capability to the specified level.
        Values outside the valid range (0-20) are clamped. Does not work if the toy is currently blocked.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            level: level to set the toy's primary intensity to.
            callback: Optional callback called with True if successful, False otherwise
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
        Sets the intensity of the toy's secondary capability to the specified level.
        Does not work if the toy is currently blocked.
        Always succeeds (but does nothing) if the toy has no secondary intensity.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            level: level to set the toy's secondary intensity to.
            callback: Optional callback called with True if successful, False otherwise
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
        Stops all toy actions (set all intensities to zero).
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Optional callback called with True if successful, False otherwise
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
        Changes the rotation direction of the toy (if available, else does nothing and returns True)
        See self.change_rotate_direction_available() to find out if the toy supports this.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Optional callback called with True if successful, False otherwise
        """
        async def _execute():
            return await self._toy.rotate_change_direction()

        self._schedule_command(_execute, callback)

    def get_battery_level(self, callback: Callable[[Optional[int]], None]) -> None:
        """
        Retrieves the battery level of the toy.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Callback called with the battery level (or None if unavailable)
        """
        async def _execute():
            return await self._toy.get_battery_level()

        self._schedule_command(_execute, callback)

    def get_information(self, callback: Callable[[dict[str, str]], None]) -> None:
        """
        Gathers information about the toy. Needs to send several commands to the Toy to find out the information.
        Does not send commands directly but schedules them to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            callback: Callback called with a dictionary containing information about the toy.
                Contains the keys: 'Battery Level', 'Status', 'Batch number', 'Bluetooth name', and 'Device type'
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
        Sends the specified command to the toy.
        Does not send the command directly but schedules it to be sent by the ToyHub.
        The ToyHub processes new commands every 50ms so this does not introduce a noticeable delay.
        If the Toy is unconnected, the command is not lost but processed upon the re-establishment of connection

        Args:
            command: String containing the command to send.
            callback: Callback called with the response from the toy (always a string).
        """

        async def _execute():
            return await self._toy.direct_command(command)

        self._schedule_command(_execute, callback)
