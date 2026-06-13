"""
Private Module of the WebSocket API

Defines the _ToyController class, which extends the low-level Toy class with additional methods for pattern playback and toy control.
Comparable to the ToyController class of the tikal library, but offering async methods instead of sync.
Meant to be consumed by _ToyHub, which in turn is consumed by ToyServer. ToyServer defines a public API.
"""

from .._private import PatternHandler
from ..low_level import MIN_SEGMENT_LENGTH, Lovense, Toy


class _ToyController:
    """
    Parent class for high-level toy control.

    Extends the low-level Toy class with additional methods mostly related to pattern playback capabilities.

    Args:
        toy: Low-level toy object (Toy instance) for BLE communication.
        initial_battery: Initial battery level (0-100) or None if the toy has no battery.
    """

    def __init__(self, toy: Toy, initial_battery: int | None = None):
        self._toy = toy
        self._pattern_handler = PatternHandler()
        self._last_values: dict[str, int | None] = {
            "intensity1": None,
            "intensity2": None,
        }
        self._intensity_limits: list[int] = [
            self._toy.max_intensity,
            self._toy.max_intensity,
        ]
        self._accepted_pause = False
        self._is_blocked = False
        self._battery = initial_battery

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
        """
        Get a human-readable identifier for this toy.

        Returns:
             Human-readable identifier of the toy e.g., Bluetooth name.
        """
        return self._toy.name

    @property
    def brand(self) -> str:
        """
        Get the brand of the toy.

        Returns:
            Human-readable identifier of the toy brand e.g., 'Lovense'.
        """
        return self._toy.brand

    @property
    def max_intensity(self) -> int:
        """
        Get the maximum intensity value for this toy.

        Returns:
            int: Maximum intensity value (e.g., 20 for Lovense toys).
        """
        return self._toy.max_intensity

    @property
    def battery(self) -> int | None:
        """
        Get the current battery level of the toy (from memory, automatically updated by _ToyHub)

        Returns:
            Current battery level (0-100) or None if the toy has no battery.
        """
        return self._battery

    @property
    def is_paused(self):
        """
        Check if pattern playback is currently paused.

        When paused, the pattern timer stops advancing and toy intensities are set to zero. Manual commands can override the intensity levels

        Returns:
            bool: True if paused, False otherwise.
        """
        return self._pattern_handler.is_paused

    @property
    def is_blocked(self):
        """
        Check if the toy is currently blocked.

        When blocked, all intensity commands (manual and pattern-based) are rejected. Toy's intensities are forced to 0.

        Returns:
            bool: True if blocked, False otherwise.
        """
        return self._is_blocked

    async def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        This method validates and updates the toy's model name. The model name determines which commands are available and how they're interpreted.

        Args:
            model_name: New model name. Must be a valid model for this toy brand.

        Raises:
            - InvalidModelError: If model_name is not valid for this toy brand.
            - BadModelError: If the model_name is valid, but commands still fail. See BadModelError for details
        """
        await self._toy.set_model_name(model_name)

    async def toggle_pause(self) -> bool:
        """
        Toggle pattern playback pause state.

        When paused:
        - If a pattern is active, it stops advancing.
        - Toy intensities are set to zero, but manual commands can override this.
        - Block state is cleared if active (toy cannot be paused and blocked at the same time)

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: The command was sent to the toy, but the reply was not as excepted.

        Returns:
            bool: True if now paused, False if now unpaused.
        """
        if not self._pattern_handler.is_paused:
            self._pattern_handler.set_paused(True)
            await self.stop()
            self._is_blocked = False  # I don't want to pause and block at the same time
            return True
        else:
            self._pattern_handler.set_paused(False)
            return False

    async def toggle_block(self) -> bool:
        """
        Toggle block state.

        When blocked:
        - All intensity commands are rejected (return False via callback)
        - Toy intensities are forced to zero
        - Pattern continues advancing but doesn't control the toy
        - Pause state is cleared if active (toy cannot be paused and blocked at the same time)

        Raises:
            ConnectionError: The command could not be sent to the toy.
            ToyRefusedError (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            bool: True if now blocked, False if now unblocked.
        """
        if not self._is_blocked:
            self._is_blocked = True
            await self.stop()
            # I don't want to pause and block at the same time'
            self._pattern_handler.set_paused(False)
            return True
        else:
            self._is_blocked = False
            return False

    async def set_pattern(
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
        The maximum possible intensity can be looked up via :meth:`get_info`. An empty list clears the pattern.

        Args:
            pattern: List of (duration_ms, intensity1, intensity2) tuples
            wraparound: If True, the pattern loops indefinitely. If False, the pattern stops after one playthrough.
            reset_time: If True, restart the pattern from the beginning. If False, maintain the current position in the pattern.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            ToyRefusedError (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Note:
            Manual intensity commands automatically pause pattern playback to avoid conflicts. Call ``toggle_pause()`` to resume the pattern.
        """
        limited_pattern = []
        for duration_ms, intensity1, intensity2 in pattern:
            limited_pattern.append(
                (
                    duration_ms,
                    min(intensity1, self._intensity_limits[0]),
                    min(intensity2, self._intensity_limits[1]),
                )
            )
        self._pattern_handler.set_pattern(pattern, wraparound, reset_time)
        if not pattern:  # ensure that intensities are 0 if pattern is cleared
            await self.stop()

    async def set_intensity1_limit(self, level: int | None):
        """
        Set the upper limit for the primary intensity. All future intensity1 commands are clamped to this value.

        Args:
            level: Maximum allowed intensity1 value (0 – max_intensity). Clamped to max_intensity.
        """
        if level is None:
            self._intensity_limits[0] = self._toy.max_intensity
        else:
            self._intensity_limits[0] = min(level, self._toy.max_intensity)

    async def set_intensity2_limit(self, level: int | None):
        """
        Set the upper limit for the secondary intensity. All future intensity2 commands are clamped to this value.

        Args:
            level: Maximum allowed intensity2 value (0 – max_intensity). Clamped to max_intensity.
        """
        if level is None:
            self._intensity_limits[1] = self._toy.max_intensity
        else:
            self._intensity_limits[1] = min(level, self._toy.max_intensity)

    async def intensity1(self, level: int) -> bool:
        """
        Set the intensity of the primary capability.

        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts.

        Args:
            level: Intensity level. The Valid range depends on the toy type. Values outside the range are clamped.

        Raises:
            ConnectionError: The command could not be sent to the toy
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            True if the command was accepted, False if the toy is blocked. See :meth:`toggle_block`
        """
        if self._is_blocked:
            return False
        # avoid the pattern overriding the command
        self._pattern_handler.set_paused(True)
        level = min(level, self._intensity_limits[0])
        return await self._toy.strict_intensity1(level)

    async def intensity2(self, level: int) -> bool:
        """
        Set the intensity of the secondary capability

        Behavior is identical to :meth:`intensity1` but controls the secondary capability (e.g., rotation, air pump)
        Safe to call on toys without a secondary capability (will return false and do nothing).

        Args:
            level: Intensity level. The valid range depends on the toy type. Values outside the range are clamped.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            True if the command was accepted, False if the toy has no second capability or is blocked. See :meth:`toggle_block`
        """
        if self._is_blocked:
            return False
        # avoid the pattern overriding the command
        self._pattern_handler.set_paused(True)
        level = min(level, self._intensity_limits[1])
        return await self._toy.strict_intensity2(level)

    async def change_rotation_direction(self) -> bool:
        """
        Change rotation direction (if supported).

        This method toggles the rotation direction for toys with rotation capability.
        Safe to call on all toys. Does nothing and returns False if rotation is not supported.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
             True if the command was accepted, False if the toy does not support changing the rotation direction.
        """
        return await self._toy.strict_change_rotation_direction()

    async def stop(self) -> bool:
        """
        Stop all toy actions (set all intensities to zero).

        If a pattern is active and not paused, this method pauses the pattern.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            Always true
        """
        self._pattern_handler.set_paused(True)
        return await self._toy.strict_stop()

    async def get_battery_level(self) -> int | None:
        """
        Retrieve the toy's battery level.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            Battery level (0-100) or None if the toy has no battery.
        """
        return await self._toy.strict_get_battery_level()

    async def get_info(
        self, full: bool
    ) -> dict[str, str | list[str] | bool | int | None]:
        """
        Gather information about the toy.

        Info gathered (always):
        -  `toy_id` (str) unique identifier of the toy, e.g., Bluetooth address
        -  `name` (str) human-readable identifier of the toy, e.g., Bluetooth advertisement name
        -  `model_name` (str) model name of the toy. Typically, not retrieved from the toy itself but set by you when adding the toy. This returns this set name.
        -  `brand` (str) brand of the toy, e.g., Lovense
        -  `intensity_names` (list of str). Two human-readable strings. The second string is empty if the toy only has one intensity.
        -  `supports_rotation` (bool) whether the toy supports changing the rotation direction
        -  `max_intensity` (int) maximum intensity value possible for the toy. (Keep in mind that self._intensity_limits can apply stricter thresholds)
        -  `recommended_min_interval` (int) The recommended minimum interval between intensity commands (in ms). Especially useful for pattern playback.

        Args:
            full: If True, returns all available info (making requests to the toy). Otherwise, returns only the "cheap" info described above.
            Cheap in the sense that the info is retrieved solely from the software representation.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            dict: dictionary containing the gathered info.

        Note:
            Can only raise exceptions if full=True.
        """
        intensity1, intensity2 = self._toy.intensity_names
        if intensity2 is None:
            intensity2 = ""
        result = dict(
            toy_id=self._toy.toy_id,
            name=self._toy.name,
            model_name=self._toy.model_name,
            brand=self._toy.brand,
            intensity_names=[intensity1, intensity2],
            supports_rotation=self._toy.change_rotation_direction_available,
            max_intensity=self._toy.max_intensity,
            recommended_min_interval=MIN_SEGMENT_LENGTH[self._toy.model_name],
        )
        return result

    def get_state(self) -> dict:
        """
        Retrieve the current state of the toy (in-memory, no BLE communication).

        State information contains:
        -  `toy_id` (str) Unique identifier of the toy
        -  `current_intensity` (list[int, int]) Current intensity values. The second value is always zero if the toy only has one intensity.
        -  `intensity_limits` (list[int, int]) Current set intensity limits.
        -  `is_blocked` (bool) Whether the toy is currently blocked (toy's intensities are forced to zero)
        -  `pattern_version` (int) Each time the pattern state changes, the version number is incremented
        -  `pattern` (list[tuple[int, int, int]]) List of tuples (duration, intensity1, intensity2) defining the pattern segment
        -  `wraparound` (bool)  Whether the pattern repeats from the beginning after completing the last segment. If False, both Intensities are 0 after the last segment
        -  `is_paused` (bool) Whether the toy is currently paused (patterns do not advance)
        -  `elapsed` (float) Time elapsed since the start of the pattern or last wraparound in ms

        Returns:
            dict with keys as described above
        """
        pattern, wraparound, is_paused, elapsed = (
            self._pattern_handler.get_pattern_data()
        )
        result = dict(
            toy_id=self._toy.toy_id,
            current_intensities=list(self._toy.current_intensities),
            intensity_limits=self._intensity_limits.copy(),
            is_blocked=self._is_blocked,
            pattern_version=self._pattern_handler.pattern_version,
            pattern=pattern,
            wraparound=wraparound,
            is_paused=is_paused,
            elapsed=elapsed,
        )
        return result

    async def direct_command(self, command: str) -> str:
        """
        Send a raw command directly to the toy.

        Use this for accessing toy features not exposed by the library. Requires knowledge of the toy's protocol.

        Args:
            command: Command string in the toy's protocol format (e.g., "DeviceType").

        Raises:
            ConnectionError: The command could not be sent to the toy.

        Returns:
            toy response (str)
        """
        return str(await self._toy.strict_direct_command(command))

    async def process_communication(self) -> None:
        """
        Process pattern playback. This method is called periodically by the _ToyHub to execute pattern playback.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout
        """
        # Handle pattern playback
        if not self._pattern_handler.has_active_pattern:
            return

        # Handle a paused or blocked state
        if self._pattern_handler.is_paused or self._is_blocked:
            if not self._accepted_pause:
                # First time entering paused/blocked state - send stop command
                await self._toy.strict_stop()
                self._last_values["intensity1"] = None
                self._last_values["intensity2"] = None
                self._accepted_pause = True
            # If already paused/blocked, do nothing (no repeated stop commands)

        # Handle active state
        else:
            self._accepted_pause = False

            # Get current values and send commands if values have changed
            pattern_time = self._pattern_handler.get_pattern_time()
            intensity1_value, intensity2_value = (
                self._pattern_handler.get_pattern_values(pattern_time)
            )

            if intensity1_value != self._last_values["intensity1"]:
                await self._toy.strict_intensity1(intensity1_value)
                self._last_values["intensity1"] = intensity1_value

            if intensity2_value != self._last_values["intensity2"]:
                await self._toy.strict_intensity2(intensity2_value)
                self._last_values["intensity2"] = intensity2_value

    async def fetch_and_update_battery(self) -> int | None:
        """
        Fetch the current battery level from the toy.

        Updates internal _battery attribute if the value has changed and returns the new value.
        If the fetch fails (exception), return None and keep the old value.

        Returns:
            battery level (0-100) or None if the toy has no battery or the fetch failed.
        """
        try:
            new_battery = await self._toy.strict_get_battery_level()
        except Exception:
            return None
        if new_battery != self._battery:
            self._battery = new_battery
            return new_battery
        return None

    async def disconnect(self) -> None:
        """
        Disconnect from the device.

        Stops all toy actions, disables notifications, and closes the connection.
        This method should always be called before the toy object is destroyed to ensure proper cleanup.
        Any exception raised is only for logging. The toy is still disconnected in the error case.

        Raises:
            - ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
            - UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK"
        """
        self._is_blocked = True
        self._accepted_pause = (
            True  # prevent self._process_communication from sending any commands
        )
        await self._toy.strict_disconnect()

    async def reconnect(self) -> None:
        """
        Attempts to reconnect to the toy.

        Raises:
            - ConnectionError: The reconnection failed.
            - RuntimeError: The reconnection was attempted after intentionally disconnecting the toy
        """
        await self._toy.strict_reconnect()


class _LovenseController(_ToyController):
    """
    High-level controller for Lovense toys.

    Extends the low-level Lovense class with additional methods mostly related to pattern playback capabilities.

    Args:
        toy: Low-level Lovense instance.
        initial_battery: Initial battery level (0-100) or None if the toy has no battery.
    """

    def __init__(self, toy: Lovense, initial_battery: int | None = None):
        self._toy = toy
        super().__init__(toy, initial_battery)

    async def get_info(
        self, full: bool
    ) -> dict[str, str | list[str] | list[int] | bool | int | None]:
        """
        Gather information about the toy.

        Args:
            full: if false, only 'cheap' info is gathered (= info from the software representation, not the toy).
                If true, several requests are made to the toy, retrieving additional information

        Info gathered (always):
        -  `toy_id` (str) unique identifier of the toy, e.g., Bluetooth address
        -  `name` (str) human-readable identifier of the toy, e.g., Bluetooth advertisement name
        -  `model_name` (str) model name of the toy. Typically, not retrieved from the toy itself but set by you when adding the toy. This returns this set name.
        -  `brand` (str) brand of the toy, e.g., Lovense
        -  `intensity_names` (list of str). Two human-readable strings. The second string is empty if the toy only has one intensity.
        -  `supports_rotation` (bool) whether the toy supports changing the rotation direction
        -  `max_intensity` (int) maximum intensity value
        -  `recommended_min_interval` (int) The recommended minimum interval between intensity commands (in ms). Especially useful for pattern playback.
        Additional info if `full` is true:
        - 'status' (str): Status code ("2" for normal)
        - 'batch_number' (str): Manufacturing batch (e.g., "241015")
        - 'device_type' (str): Device info (e.g., "C:11:ADDRESS")

        Raises:
            - ConnectionError: The command could not be sent to the toy.
            - UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            dict: dictionary containing the gathered info.

        Note:
            can only raise exceptions if full=True.
        """
        info = await super().get_info(full)

        if full:
            info["status"] = await self._toy.strict_get_status()
            info["batch_number"] = await self._toy.strict_get_batch_number()
            info["device_type"] = await self._toy.strict_get_device_type()

        return info
