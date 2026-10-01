"""
Private Module of the async core

Defines the _ToyController class, which extends the low-level Toy class with additional methods for pattern playback and toy control.
Meant to be consumed by _ToyHub, which in turn is consumed by ToyServer and the High-Level ToyHub. Both define a public API.
"""

from typing import Any

from .._private import PatternHandler
from ..low_level import LovenseToy, MockEstimToy, Toy


class _ToyController:
    """
    Parent class for high-level toy control.

    Wraps a low-level toy and adds pattern playback, pause/block, intensity limits, the safety hold, and a battery cache.

    Args:
        toy: Low-level toy object (Toy instance) for BLE communication.
        initial_battery: Initial battery level (0-100) or None if the toy has no battery.
    """

    def __init__(self, toy: Toy, initial_battery: int | None = None):
        self._toy = toy
        self._pattern_handler = PatternHandler()
        # What playback last sent on each channel, so it only sends a value when it changes.
        self._last_values: list[int | None] = [None, None]
        # Set once the toy is at rest (or under a manual command) for the current pause/block, so playback sends no
        # further stops. See process_communication.
        self._accepted_pause = False
        self._is_blocked = False
        # Set by disconnect(): from then on, playback sends nothing to the toy.
        self._closed = False
        self._intensity_limits: list[int] = [
            self._toy.max_intensity,
            self._toy.max_intensity,
        ]
        self._battery = initial_battery
        # Safety hold of the heartbeat watchdog, owned by _ToyHub. Independent of block and pause.
        self._held = False

    # ------------------------------------------------------------------
    # Read-only passthroughs to the underlying toy
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        """Model name of the toy (e.g., "Nora", "Lush")."""
        return self._toy.model_name

    @property
    def toy_id(self) -> str:
        """Unique identifier for the toy (typically its Bluetooth address)."""
        return self._toy.toy_id

    @property
    def name(self) -> str:
        """Human-readable identifier of the toy (e.g., its Bluetooth name)."""
        return self._toy.name

    @property
    def brand(self) -> str:
        """Human-readable identifier of the toy brand (e.g., 'Lovense')."""
        return self._toy.brand

    @property
    def max_intensity(self) -> int:
        """Maximum intensity value for this toy (e.g., 20 for Lovense toys)."""
        return self._toy.max_intensity

    @property
    def current_intensities(self) -> tuple[int, int]:
        """Current (primary, secondary) intensity values. Secondary is always 0 for single-capability toys."""
        return self._toy.current_intensities

    @property
    def intensity_names(self) -> tuple[str, str | None]:
        """Display names of the (primary, secondary) capability. The secondary name is None for single-capability toys."""
        return self._toy.intensity_names

    @property
    def change_rotation_direction_available(self) -> bool:
        """Whether the toy supports changing its rotation direction."""
        return self._toy.change_rotation_direction_available

    # ------------------------------------------------------------------
    # Pattern / pause / block / limit state (read)
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Whether pattern playback is currently paused (the pattern timer stops advancing)."""
        return self._pattern_handler.is_paused

    @property
    def is_blocked(self) -> bool:
        """Whether the toy is currently blocked (all intensities forced to zero)."""
        return self._is_blocked

    @property
    def intensity_limits(self) -> tuple[int, int]:
        """The current (intensity1, intensity2) limits. Every command and pattern value is clamped to them."""
        return self._intensity_limits[0], self._intensity_limits[1]

    @property
    def pattern_version(self) -> int:
        """Incremented each time the pattern state changes."""
        return self._pattern_handler.pattern_version

    def get_pattern_time(self) -> float:
        """Elapsed time in the current pattern in milliseconds (paused time does not count). 0.0 if no pattern."""
        return self._pattern_handler.get_pattern_time()

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """The ``(intensity1, intensity2)`` values at ``pattern_time`` (milliseconds) in the current pattern."""
        return self._pattern_handler.get_pattern_values(pattern_time)

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """The full pattern state: ``(pattern, wraparound, is_paused, elapsed_ms)``."""
        return self._pattern_handler.get_pattern_data()

    @property
    def is_held(self) -> bool:
        """Whether the toy is under the safety hold (intensities forced to zero, manual intensity commands rejected)."""
        return self._held

    def set_held(self, held: bool) -> None:
        """
        Put the toy under the safety hold, or take it off.

        The hold is independent of block and pause and leaves both untouched. While held, manual intensity commands
        are rejected and a pattern keeps advancing without driving the toy. Once the hold is off, the toy follows its
        own state again: the next playback tick re-drives a running pattern, manual intensity levels are not replayed.

        Only records the state, so ``_ToyHub`` can hold every toy at once under its lock. Bringing a running toy
        down to zero is a separate step: see :meth:`stop_output`.

        Args:
            held: True to hold the toy, False to release it.
        """
        self._held = held

    async def stop_output(self) -> bool:
        """
        Set both intensities to zero without touching pause or block (unlike :meth:`stop`, which pauses the pattern).

        Used by the safety hold, which must not change the state the toy returns to once the hold is lifted.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError) The toy replied unexpectedly.

        Returns:
            Always True
        """
        return await self._toy.strict_stop()

    def _output_suppressed(self) -> bool:
        """Playback keeps the toy at zero while it is blocked *or* held."""
        return self._is_blocked or self._held

    @property
    def battery(self) -> int | None:
        """
        Get the current battery level of the toy (from memory, automatically updated by _ToyHub)

        Returns:
            Current battery level (0-100) or None if the toy has no battery.
        """
        return self._battery

    async def set_model_name(self, model_name: str) -> None:
        """
        Set the model name of the toy.

        This method validates and updates the toy's model name. The model name determines which commands are available and how they're interpreted.

        Args:
            model_name: New model name. Must be a valid model for this toy brand.

        Raises:
            - InvalidModelError: If model_name is not valid for this toy brand.
            - BadModelError: If the model_name is valid, but commands still fail. See BadModelError for details
            - ConnectionError: The toy could not be stopped on its old command set, so the model was left unchanged.
        """
        await self._toy.set_model_name(model_name)
        # Switching models stops the toy on the old command set, so whatever playback last sent no longer holds.
        self._invalidate_last_values()

    def apply_paused(self, pause: bool) -> bool:
        """
        Pause or resume pattern playback.

        When paused:

        - If a pattern is active, it stops advancing.
        - Toy intensities are to be set to zero, but manual commands can override this.
        - Block state is cleared if active (toy cannot be paused and blocked at the same time)

        Args:
            pause: True to pause, False to resume.

        Returns:
            True if the toy has to be stopped now (see :meth:`stop_output`), which is whenever it is paused.
        """
        self._pattern_handler.set_paused(pause)
        if pause:
            self._is_blocked = False  # I don't want to pause and block at the same time
        return pause

    def apply_blocked(self, block: bool) -> bool:
        """
        Block or unblock the toy.

        When blocked:

        - All intensity commands are rejected (return False)
        - Toy intensities are forced to zero
        - Pattern continues advancing but doesn't control the toy
        - Pause state is cleared if active (toy cannot be paused and blocked at the same time)

        Args:
            block: True to block, False to unblock.

        Returns:
            True if the toy has to be stopped now (see :meth:`stop_output`), which is whenever it is blocked.
        """
        self._is_blocked = block
        if block:
            # I don't want to pause and block at the same time
            self._pattern_handler.set_paused(False)
        return block

    def apply_pattern(
        self,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> bool:
        """
        Set a time-based pattern for automatic toy control.

        Patterns are lists of segments. Each segment is a tuple of (duration_ms, intensity1, intensity2) where:

        - duration_ms: How long this segment lasts (milliseconds)
        - intensity1: Primary capability intensity (0-max)
        - intensity2: Secondary capability intensity (0-max)

        The maximum possible intensity can be looked up via :meth:`get_info`. An empty list clears the pattern, which
        stops the toy like :meth:`apply_stop` does.

        Args:
            pattern: List of (duration_ms, intensity1, intensity2) tuples
            wraparound: If True, the pattern loops indefinitely. If False, the pattern stops after one playthrough.
            reset_time: If True, restart the pattern from the beginning. If False, maintain the current position in the pattern.

        Returns:
            True if the toy has to be stopped now (see :meth:`stop_output`), which is whenever the pattern was cleared.

        Note:
            Manual intensity commands automatically pause pattern playback to avoid conflicts. Resume with
            :meth:`apply_paused`.

        Note:
            The pattern is stored exactly as given. Intensity limits are applied on every playback tick instead, so a
            limit lowered while this pattern is running takes effect immediately, and one raised again restores the
            pattern's own values.
        """
        self._pattern_handler.set_pattern(pattern, wraparound, reset_time)
        if not pattern:  # ensure that intensities are 0 if pattern is cleared
            return self.apply_stop()
        return False

    def apply_stop(self) -> bool:
        """
        Pause the pattern, as every stop does, so playback does not start the toy again.

        Unlike :meth:`apply_paused`, this leaves the block alone.

        Returns:
            Always True: the toy has to be stopped now (see :meth:`stop_output`).
        """
        self._pattern_handler.set_paused(True)
        return True

    def accept_manual_intensity(self) -> bool:
        """
        Decide whether a manual intensity command may be sent, and make way for it.

        Refused while the toy is blocked or held. Accepting pauses the pattern, so playback does not override the
        command, and tells playback that the toy is taken care of: without that, the next tick would send the one stop
        it sends when a pattern gets paused, undoing the manual level right after it was set.

        Returns:
            True if the command may be sent (see :meth:`send_intensity`), False if the toy is blocked or held.
        """
        if self._is_blocked or self._held:
            return False
        self._pattern_handler.set_paused(True)
        self._accepted_pause = True
        # The toy runs at the manual level from now on, so a resumed pattern has to send its values again.
        self._invalidate_last_values()
        return True

    def _limited(self, channel: int, level: int) -> int:
        """Clamp a level to the limit currently configured for *channel*."""
        return min(level, self._intensity_limits[channel])

    async def _strict_intensity(self, channel: int, level: int) -> bool:
        """Send a level to one capability of the toy via the strict toy method (raises on failure)."""
        if channel == 0:
            return await self._toy.strict_intensity1(level)
        return await self._toy.strict_intensity2(level)

    def apply_limit(self, channel: int, level: int | None) -> bool:
        """
        Set the upper limit for one intensity. All commands and pattern values for that channel are clamped to it.

        A limit is a safety ceiling, so a toy already running above it has to come down right away rather than only
        being clamped from the next command onwards: see :meth:`enforce_limit`.

        Args:
            channel: 0 for the primary intensity, 1 for the secondary one.
            level: Maximum allowed value (0 – max_intensity). Clamped to that range. None removes the limit.

        Returns:
            True if the toy runs above the new limit and has to be brought down now (see :meth:`enforce_limit`).
        """
        self._intensity_limits[channel] = self._normalize_limit(level)
        return self._toy.current_intensities[channel] > self._intensity_limits[channel]

    def _normalize_limit(self, level: int | None) -> int:
        """Turn a requested limit into a usable ceiling: ``None`` means "no limit", anything else is clamped to range."""
        if level is None:
            return self._toy.max_intensity
        return max(0, min(level, self._toy.max_intensity))

    async def enforce_limit(self, channel: int) -> None:
        """
        Bring the toy down now if one of its capabilities is running above its current limit.

        Pattern playback would pick the change up on its own next tick; this also covers the cases where nothing else
        is about to send an intensity (no active pattern, or a segment that lasts minutes). Does nothing if the toy is
        not above the limit, so it is safe to retry.

        Args:
            channel: 0 for the primary intensity, 1 for the secondary one.

        Raises:
            ConnectionError: The corrective intensity command could not be delivered. The limit itself stays in force,
                so every later command and playback tick respects it.
            UnexpectedToyResponse: (subclass of ConnectionError) The toy replied unexpectedly to the corrective command.
        """
        limit = self._intensity_limits[channel]
        if self._toy.current_intensities[channel] <= limit:
            return
        await self._strict_intensity(channel, limit)
        # The toy now sits at the ceiling. Record it so the next playback tick does not repeat the command.
        self._last_values[channel] = limit

    async def intensity(self, channel: int, level: int) -> bool:
        """
        Set the intensity of one capability: :meth:`accept_manual_intensity`, then :meth:`send_intensity`.

        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts.
        Safe to call for the secondary capability of a toy that has none (will return false and do nothing).

        Args:
            channel: 0 for the primary capability, 1 for the secondary one (e.g., rotation, air pump).
            level: Intensity level. The Valid range depends on the toy type. Values outside the range are clamped.

        Raises:
            ConnectionError: The command could not be sent to the toy
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            True if the command was accepted, False if the toy has no such capability or is blocked or held. See :meth:`apply_blocked` and :meth:`set_held`
        """
        if not self.accept_manual_intensity():
            return False
        return await self.send_intensity(channel, level)

    async def send_intensity(self, channel: int, level: int) -> bool:
        """
        Send a level to one capability, clamped to its limit, without touching the pattern.

        The second half of a manual intensity command (see :meth:`accept_manual_intensity`). Blocked and held are
        checked again, because either can have come in between the two halves.

        Args:
            channel: 0 for the primary capability, 1 for the secondary one.
            level: Intensity level. Values outside the valid range are clamped.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            True if the toy took the command, False if it is blocked or held (nothing is sent) or has no such capability.
        """
        if self._output_suppressed():
            return False
        return await self._strict_intensity(channel, self._limited(channel, level))

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

        If a pattern is active and not paused, this method pauses the pattern: :meth:`apply_stop`, then :meth:`stop_output`.

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            Always true
        """
        self.apply_stop()
        return await self.stop_output()

    async def refresh_battery(self) -> int | None:
        """
        Ask the toy for its battery level and remember it (see :attr:`battery`).

        Raises:
            ConnectionError: The command could not be sent to the toy. The last known level is kept.
            UnexpectedToyResponse: (subclass of ConnectionError): The command was sent to the toy, but the reply was not as excepted.

        Returns:
            Battery level (0-100) or None if the toy has no battery.
        """
        self._battery = await self._toy.strict_get_battery_level()
        return self._battery

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
        result: dict[str, str | list[str] | bool | int | None] = dict(
            toy_id=self._toy.toy_id,
            name=self._toy.name,
            model_name=self._toy.model_name,
            brand=self._toy.brand,
            intensity_names=[intensity1, intensity2],
            supports_rotation=self._toy.change_rotation_direction_available,
            max_intensity=self._toy.max_intensity,
            recommended_min_interval=self._toy.recommended_min_interval,
        )
        return result

    def get_state(self) -> dict[str, Any]:
        """
        Retrieve the current state of the toy (in-memory, no BLE communication).

        State information contains:

        -  `toy_id` (str) Unique identifier of the toy
        -  `current_intensity` (list[int, int]) Current intensity values. The second value is always zero if the toy only has one intensity.
        -  `intensity_limits` (list[int, int]) Current set intensity limits.
        -  `is_blocked` (bool) Whether the toy is currently blocked (toy's intensities are forced to zero)
        -  `is_held` (bool) Whether the toy is under the safety hold (toy's intensities are forced to zero)
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
            is_held=self._held,
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
        Advance pattern playback by one tick. Called periodically by the _ToyHub.

        On the first tick after entering a paused, blocked, or held state, sends a single stop and latches it (no
        repeated stops). While active, sends an intensity only when its *limited* target value changed since the last
        successful send. Tracking state is updated only after each send's ``await`` returns, so a send that raises
        leaves the state unchanged and the command is retried next tick.

        Raises:
            UnexpectedToyResponse: The toys' response was unexpected, e.g. "ERROR" instead of "OK".
            ConnectionError: Command could not be sent, or the toy did not respond within an appropriate timeout.
                ``_ToyHub`` retries and reconnects.
        """
        if self._closed or not self._pattern_handler.has_active_pattern:
            return

        if self._pattern_handler.is_paused or self._output_suppressed():
            if not self._accepted_pause:
                # First tick in the paused/blocked/held state: bring the toy to rest once.
                await self._toy.strict_stop()
                self._invalidate_last_values()
                self._accepted_pause = True
            return

        self._accepted_pause = False
        pattern_time = self._pattern_handler.get_pattern_time()
        levels = self._pattern_handler.get_pattern_values(pattern_time)
        for channel, level in enumerate(levels):
            # A limit lowered/increased mid-playback has to take effect on an already running pattern.
            level = self._limited(channel, level)
            if level != self._last_values[channel]:
                await self._strict_intensity(channel, level)
                self._last_values[channel] = level

    def _invalidate_last_values(self) -> None:
        """
        Forget which intensities were last sent, so the next playback tick re-sends both.

        Call this after anything that changes the toy's actual level behind the playback engine's back
        (e.g., a model switch stops the toy, a manual command sets its own level).
        """
        self._last_values = [None, None]

    async def fetch_and_update_battery(self) -> int | None:
        """
        Fetch the current battery level from the toy.

        Updates internal _battery attribute (see :meth:`refresh_battery`) and returns the new value if it changed.
        If the fetch fails (exception), return None and keep the old value.

        Returns:
            battery level (0-100) if it changed, else None (unchanged, no battery, or the fetch failed).
        """
        old_battery = self._battery
        try:
            new_battery = await self.refresh_battery()
        except Exception:
            return None
        return new_battery if new_battery != old_battery else None

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
        # A playback tick scheduled before the hub dropped this toy can still run; it must not send anything any more.
        self._closed = True
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

    def __init__(self, toy: LovenseToy, initial_battery: int | None = None):
        self._toy: LovenseToy = toy
        super().__init__(toy, initial_battery)

    async def get_info(
        self, full: bool
    ) -> dict[str, str | list[str] | bool | int | None]:
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


class _MockEstimController(_ToyController):
    """
    High-level controller for the fictional MockEstimToys brand (WebSocket variant).

    The MockEstimToys brand exposes no extra ``full`` info beyond the generic fields, so the base ``get_info`` is reused
    as-is. This subclass exists only to type the wrapped toy and to register the brand.

    Args:
        toy: Low-level ``MockEstimToy`` instance.
        initial_battery: Initial battery level (0-100) or None if the toy has no battery.
    """

    def __init__(self, toy: MockEstimToy, initial_battery: int | None = None):
        self._toy: MockEstimToy = toy
        super().__init__(toy, initial_battery)


#: Maps a toy's brand (``toy.brand``) to its websocket controller class.
#: Register a brand's controller here when adding support for a new brand.
_CONTROLLER_BY_BRAND: dict[str, type[_ToyController]] = {
    "Lovense": _LovenseController,
    "MockEstimToys": _MockEstimController,
}
