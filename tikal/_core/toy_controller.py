"""
Async core: :class:`_ToyController`, the state and pattern playback of one connected toy, and its brand subclasses.

The hub (``_ToyHub``) owns the controllers and runs every command through them.
"""

import asyncio
import math
from typing import Any, Awaitable, Callable, TypeVar

from .._private import PatternHandler
from ..funscript import Funscript, FunscriptMode, funscript_to_pattern
from ..low_level import LovenseToy, MockEstimToy, Toy, UnexpectedToyResponse

T = TypeVar("T")


class _ToyController:
    """
    State and pattern playback of one connected toy: pattern, pause, block, intensity limits, and the battery level.

    There are two types of state changes:

    - ``apply_*`` and :meth:`accept_manual_intensity` change the state without I/O and tell whether the toy needs a
      command.
    - :meth:`stop_output`, :meth:`send_intensity` and :meth:`enforce_limit` send a  command.

    A toy is never both paused and blocked, except after ``apply_blocked(True, keep_pause=True)``.
    Brands can subclass it, see :data:`_CONTROLLER_BY_BRAND`.

    Args:
        toy: The connected toy.
        initial_battery: Its battery level in percent. None if it has no battery.
    """

    def __init__(self, toy: Toy, initial_battery: int | None = None):
        self._toy = toy
        self._cmd_lock = asyncio.Lock()
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

    @property
    def cmd_lock(self) -> asyncio.Lock:
        """
        The toy's command lock.
        Prevents races (e.g., clients racing on set_paused) and serializes concurrent BLE commands to the same toy.
        """
        return self._cmd_lock

    # ------------------------------------------------------------------
    # Read-only passthroughs to the underlying toy
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        """Model name of the toy (e.g., "Nora", "Lush")."""
        return self._toy.model_name

    @property
    def toy_id(self) -> str:
        """Unique identifier of the toy, e.g., the Bluetooth address."""
        return self._toy.toy_id

    @property
    def name(self) -> str:
        """Human-readable name of the toy, e.g., the Bluetooth name."""
        return self._toy.name

    @property
    def brand(self) -> str:
        """Human-readable identifier of the toy brand, e.g., 'Lovense'."""
        return self._toy.brand

    @property
    def max_intensity(self) -> int:
        """Highest intensity level for this toy, e.g., 20 for Lovense toys. The lowest is 0."""
        return self._toy.max_intensity

    @property
    def current_intensities(self) -> tuple[int, int]:
        """
        ``(intensity1, intensity2)``: the levels the toy last acknowledged.
        intensity2 is always 0 if the toy does not have a secondary capability.
        """
        return self._toy.current_intensities

    @property
    def intensity_names(self) -> tuple[str, str | None]:
        """Display names ``(primary, secondary)``. The secondary name is None without a secondary capability."""
        return self._toy.intensity_names

    @property
    def change_rotation_direction_available(self) -> bool:
        """Whether the toy can change its rotation direction."""
        return self._toy.change_rotation_direction_available

    # ------------------------------------------------------------------
    # Pattern / pause / block / limit state (read)
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Whether pattern playback is paused. The pattern time stands still while paused."""
        return self._pattern_handler.is_paused

    @property
    def is_blocked(self) -> bool:
        """Whether the toy is blocked: it stays at 0, and manual intensity commands are refused."""
        return self._is_blocked

    @property
    def intensity_limits(self) -> tuple[int, int]:
        """``(limit1, limit2)``: manual levels and pattern values are capped at these."""
        return self._intensity_limits[0], self._intensity_limits[1]

    @property
    def pattern_version(self) -> int:
        """Increases whenever the pattern or pause state changes."""
        return self._pattern_handler.pattern_version

    def get_pattern_time(self) -> float:
        """Elapsed time in the current pattern in milliseconds (paused time does not count). 0.0 if no pattern."""
        return self._pattern_handler.get_pattern_time()

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """The ``(intensity1, intensity2)`` the pattern plays at *pattern_time* (ms), before limits."""
        return self._pattern_handler.get_pattern_values(pattern_time)

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """``(pattern, wraparound, is_paused, pattern_time)``, as in :meth:`get_state`."""
        return self._pattern_handler.get_pattern_data()

    async def stop_output(self) -> bool:
        """
        Set both intensities to 0. Pause and block stay as they are (unlike :meth:`stop`).

        Raises:
            ConnectionError: The command could not be sent to the toy.
            UnexpectedToyResponse: (subclass of ConnectionError) The toy replied unexpectedly, refusing the command.

        Returns:
            Always True.
        """
        return await self._toy.strict_stop()

    def _output_suppressed(self) -> bool:
        """Whether playback keeps the toy at 0: while it is blocked."""
        return self._is_blocked

    @property
    def battery(self) -> int | None:
        """The last known battery level in percent, kept up to date by the hub. None if the toy has no battery."""
        return self._battery

    async def set_model_name(self, model_name: str) -> None:
        """
        Change the model name, see :meth:`tikal.low_level.Toy.set_model_name`.
        The model name determines which commands are available and how they're interpreted.

        Args:
            model_name: New model name, case-insensitive.

        Raises:
            tikal.low_level.InvalidModelError: Not a model of the toy's brand. Nothing changed.
            tikal.low_level.BadModelError: The toy refused or did not answer the new model's commands. Nothing changed.
            ConnectionError: A command switching off a capability the new model replaces could not be delivered.
                Nothing changed.
        """
        await self._toy.set_model_name(model_name)
        # A model switch can change the toy's levels behind playback's back.
        self._invalidate_last_values()

    def apply_paused(self, pause: bool) -> bool:
        """
        Pause or resume pattern playback. Pausing unblocks the toy. Manual intensity commands still work while paused.

        Args:
            pause: True to pause, False to resume.

        Returns:
            Whether the toy has to be stopped now (see :meth:`stop_output`), which is when pausing.
        """
        self._pattern_handler.set_paused(pause)
        if pause:
            self._is_blocked = False  # A toy is never paused and blocked at once
        return pause

    def apply_blocked(self, block: bool, keep_pause: bool = False) -> bool:
        """
        Block or unblock the toy. Blocking unpauses the pattern, unless *keep_pause*.

        While blocked:

        - The toy stays at 0. Manual intensity commands are refused, and playback stops.
        - The pattern keeps advancing.

        Args:
            block: True to block, False to unblock.
            keep_pause: Keep the pause, so the toy can be blocked and paused at once.

        Returns:
            Whether the toy has to be stopped now (see :meth:`stop_output`), which is when blocking.
        """
        self._is_blocked = block
        if block and not keep_pause:
            self._pattern_handler.set_paused(False)
        return block

    def apply_pattern(
        self,
        pattern: list[tuple[int, int, int]],
        wraparound: bool = True,
        reset_time: bool = True,
    ) -> bool:
        """
        Set a pattern. An empty list clears it. The pause stays as it is.

        Args:
            pattern: List of (duration_ms, intensity1, intensity2) tuples. Limits are applied later during playback,
                so a changed limit affects a running pattern at once.
            wraparound: Loop the pattern. If False, it plays once; then the intensities are 0.
            reset_time: Start at the beginning. If False, keep the current pattern time.

        Returns:
            Whether the toy has to be stopped now (see :meth:`stop_output`), which is when the pattern was cleared.
        """
        self._pattern_handler.set_pattern(pattern, wraparound, reset_time)
        if not pattern:  # ensure that intensities are 0 if pattern is cleared
            # The stop happens behind playback's back: a pattern set later has to send its values again.
            self._invalidate_last_values()
            return True
        return False

    def apply_seek(self, pattern_time: float) -> bool:
        """
        Move playback to a position, e.g., to follow a video that jumped. A paused pattern stays paused there.

        Args:
            pattern_time: The new pattern time in ms. Negative values count as 0.

        Raises:
            ValueError: *pattern_time* is not a finite number. Nothing changed.

        Returns:
            Always False = No action is ever needed. The next playback tick plays the new position automatically.
        """
        self._pattern_handler.seek(pattern_time)
        return False

    def convert_funscript(
        self,
        script: Funscript,
        *,
        mode: FunscriptMode = "speed",
        max_speed: float = 400.0,
        secondary: Funscript | None = None,
    ) -> list[tuple[int, int, int]]:
        """
        Convert a funscript into a pattern for this toy, to be set with :meth:`apply_funscript`.

        Uses the toy's :attr:`max_intensity` and recommended minimum interval, see
        :func:`tikal.funscript.funscript_to_pattern`. Changes nothing, so it may run outside the event loop.

        Args:
            script: The script for the primary capability.
            mode: ``"speed"`` (the intensity follows the stroke speed) or ``"position"``.
            max_speed: Stroke speed in positions per second that maps to the maximum intensity in ``"speed"`` mode.
            secondary: Optional script for the secondary capability.

        Raises:
            ValueError: *max_speed* is not positive, or *mode* is unknown.

        Returns:
            The pattern.
        """
        return funscript_to_pattern(
            script,
            max_intensity=self._toy.max_intensity,
            min_segment_ms=self._toy.recommended_min_interval,
            mode=mode,
            max_speed=max_speed,
            secondary=secondary,
        )

    def apply_funscript(
        self, pattern: list[tuple[int, int, int]], start_ms: float = 0.0
    ) -> bool:
        """
        Set a pattern from :meth:`convert_funscript`, played once from *start_ms*. The pause stays as it is.

        Pattern and position change together: no playback tick plays the script's start in between.

        Args:
            pattern: The converted script.
            start_ms: Pattern time to start at in ms, e.g., the video's position. Negative values count as 0.

        Raises:
            ValueError: *start_ms* is not a finite number. Nothing changed.

        Returns:
            Whether the toy has to be stopped now (see :meth:`stop_output`), which is when the pattern is empty (no
            action after 0 ms) and so clears the pattern.
        """
        if not math.isfinite(start_ms):
            raise ValueError(f"start_ms must be a finite number, got {start_ms}")
        must_stop = self.apply_pattern(pattern, wraparound=False)
        self._pattern_handler.seek(start_ms)
        return must_stop

    def apply_stop(self) -> bool:
        """
        Pause the pattern, so playback does not restart the toy after a stop. The block stays as it is.

        Returns:
            Always True, as the toy has to be stopped now (see :meth:`stop_output`).
        """
        self._pattern_handler.set_paused(True)
        return True

    def accept_manual_intensity(self) -> bool:
        """
        Prepare a manual intensity command (refuses it while blocked)

        Pauses pattern playback if active, so it does not override the command.

        Returns:
            True if the command may be sent (see :meth:`send_intensity`), False while blocked.
        """
        if self._is_blocked:
            return False
        self._pattern_handler.set_paused(True)
        self._accepted_pause = True
        # The toy runs at the manual level from now on. A resumed pattern has to send its values again.
        self._invalidate_last_values()
        return True

    def _limited(self, channel: int, level: int) -> int:
        """*level*, capped at the limit of *channel*."""
        return min(level, self._intensity_limits[channel])

    async def _strict_intensity(self, channel: int, level: int) -> bool:
        """
        Send *level* to *channel* (0 or 1) through the toy's strict intensity method.

        Raises:
            ConnectionError: The command could not be delivered.
            UnexpectedToyResponse: (a ConnectionError) The toy refused it.

        Returns:
            True, or False without that capability (nothing is sent).
        """
        if channel == 0:
            return await self._toy.strict_intensity1(level)
        return await self._toy.strict_intensity2(level)

    def apply_limit(self, channel: int, level: int | None) -> bool:
        """
        Set the limit of one channel. Its manual levels and pattern values are capped at it.

        Args:
            channel: 0 for intensity1, 1 for intensity2.
            level: The limit, clamped to 0 to :attr:`max_intensity`. None removes it.

        Returns:
            True if the toy runs above the new limit and has to be brought down now (see :meth:`enforce_limit`).
        """
        self._intensity_limits[channel] = self._normalize_limit(level)
        return self._toy.current_intensities[channel] > self._intensity_limits[channel]

    def _normalize_limit(self, level: int | None) -> int:
        """Turn *level* into a usable limit: ``None`` means "no limit", anything else is clamped to range."""
        if level is None:
            return self._toy.max_intensity
        return max(0, min(level, self._toy.max_intensity))

    async def enforce_limit(self, channel: int) -> None:
        """
        Bring a channel down to its limit if it runs above, else does nothing.

        Args:
            channel: 0 for intensity1, 1 for intensity2.

        Raises:
            ConnectionError: The command could not be delivered. The limit stays in force.
            UnexpectedToyResponse: (a ConnectionError) The toy refused it.
        """
        limit = self._intensity_limits[channel]
        if self._toy.current_intensities[channel] <= limit:
            return
        await self._strict_intensity(channel, limit)
        # The toy now sits at the ceiling. Record it so the next playback tick does not repeat the command.
        self._last_values[channel] = limit

    async def intensity(self, channel: int, level: int) -> bool:
        """
        Set one capability manually: :meth:`accept_manual_intensity`, then :meth:`send_intensity`. Pauses the pattern.

        Args:
            channel: 0 for the primary capability, 1 for the secondary one.
            level: The level, capped at the channel's limit. Values outside 0 to :attr:`max_intensity` are clamped.

        Raises:
            ConnectionError: The command could not be delivered.
            UnexpectedToyResponse: (a ConnectionError) The toy refused it.

        Returns:
            True if the toy took the level. False while blocked (nothing changes), or without that capability (nothing
            is sent, but the pattern is paused).
        """
        if not self.accept_manual_intensity():
            return False
        return await self.send_intensity(channel, level)

    async def send_intensity(self, channel: int, level: int) -> bool:
        """
        Send a manual level to one capability, capped at its limit. Leaves the pattern alone.

        The second half of a manual intensity command (see :meth:`accept_manual_intensity`).
        Checks the block again, as it can have come in between.

        Args:
            channel: 0 for the primary capability, 1 for the secondary one.
            level: The level. Values outside 0 to :attr:`max_intensity` are clamped.

        Raises:
            ConnectionError: The command could not be delivered.
            UnexpectedToyResponse: (a ConnectionError) The toy refused it.

        Returns:
            True if the toy took the level. False while blocked or without that capability (nothing is sent).
        """
        if self._output_suppressed():
            return False
        return await self._strict_intensity(channel, self._limited(channel, level))

    async def change_rotation_direction(self) -> bool:
        """
        Reverse the rotation direction.

        Raises:
            ConnectionError: The command could not be delivered.
            UnexpectedToyResponse: (a ConnectionError) The toy refused it.

        Returns:
            True, or False without rotation support (nothing is sent).
        """
        return await self._toy.strict_change_rotation_direction()

    async def stop(self) -> bool:
        """
        Set both intensities to 0 and pause the pattern: :meth:`apply_stop`, then :meth:`stop_output`.

        Raises:
            ConnectionError: The command could not be delivered.
            UnexpectedToyResponse: (a ConnectionError) The toy refused it.

        Returns:
            Always True.
        """
        self.apply_stop()
        return await self.stop_output()

    async def refresh_battery(self) -> int | None:
        """
        Ask the toy for its battery level and remember it (see :attr:`battery`).

        Raises:
            ConnectionError: The query could not be delivered. The last known level stays.
            UnexpectedToyResponse: (a ConnectionError) The reply is not a battery level.

        Returns:
            The level in percent, or None if the toy has no battery.
        """
        self._battery = await self._toy.strict_get_battery_level()
        return self._battery

    async def get_info(
        self, full: bool
    ) -> dict[str, str | list[str] | bool | int | None]:
        """
        Information about the toy (in-memory, no BLE communication):

        - `toy_id` (str) unique identifier of the toy, e.g., Bluetooth address.
        - `name` (str) human-readable identifier of the toy, e.g., Bluetooth advertisement name.
        - `model_name` (str) model name of the toy.
        - `brand` (str) brand of the toy, e.g., Lovense.
        - `intensity_names` (list of str). Two human-readable strings.
            The second string is empty if the toy only has one intensity.
        - `supports_rotation` (bool) whether the toy supports changing the rotation direction.
        - `max_intensity` (int) maximum intensity value possible for the toy.
            (self._intensity_limits can apply stricter thresholds).
        - `recommended_min_interval` (int) The recommended minimum interval between intensity commands (in ms).

        Args:
            full: Ignored. Brand subclasses ask the toy for more information if True.

        Returns:
            The information.
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

        - `toy_id` (str) Unique identifier of the toy.
        - `current_intensity` (list[int, int]) Current intensity values.
            The second value is always 0 if the toy only has one intensity.
        - `intensity_limits` (list[int, int]) Current set intensity limits.
        - `is_blocked` (bool) Whether the toy is currently blocked (toy's intensities are forced to zero).
        - `pattern_version` (int) Each time the pattern state changes, the version number is incremented.
        - `pattern` (list[tuple[int, int, int]]) List of tuples (duration, intensity1, intensity2) defining the pattern.
        - `wraparound` (bool) Whether the pattern repeats from the beginning after completing the last segment.
            If False, both Intensities are 0 after the last segment.
        - `is_paused` (bool) Whether the toy is currently paused (patterns do not advance).
        - `elapsed` (float) Time elapsed since the start of the pattern or last wraparound in ms

        Returns:
            The state.
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
        Send a raw command.

        Use this for accessing toy features not exposed by the library. Requires knowledge of the toy's protocol.

        Args:
            command: The command in the toy's protocol, e.g., ``"DeviceType"``.

        Raises:
            ConnectionError: The command could not be delivered, or no reply came in time.

        Returns:
            The toy's reply.
        """
        return str(await self._toy.strict_direct_command(command))

    async def process_communication(self) -> None:
        """
        Advance pattern playback by one tick. The hub calls it periodically.

        - Paused or blocked: stop the toy once, on the first tick in that state.
        - Playing: send each channel's pattern value, capped at its limit, if it differs from the last value sent.

        A send that fails changes nothing. The next tick tries again.
        Does nothing without a pattern, or after :meth:`disconnect` has been called.

        Raises:
            ConnectionError: A command could not be delivered.
            UnexpectedToyResponse: (a ConnectionError) The toy refused a command.
        """
        if self._closed or not self._pattern_handler.has_active_pattern:
            return

        if self._pattern_handler.is_paused or self._output_suppressed():
            if not self._accepted_pause:
                # First tick in the paused/blocked state: bring the toy to rest once.
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
        Forget the levels playback last sent, so it resends them on the next tick.
        Use when the toy's level changed behind playback's back, e.g., a model switch or a manual command.
        """
        self._last_values = [None, None]

    async def fetch_and_update_battery(self) -> int | None:
        """
        Fetch the current battery level from the toy.

        Updates internal _battery attribute (see :meth:`refresh_battery`). Returns the new value if it changed.
        If the fetch fails, returns None and keeps the old value.

        Returns:
            The new level if it changed, else None (unchanged, no battery, or the query failed).
        """
        old_battery = self._battery
        try:
            new_battery = await self.refresh_battery()
        except Exception:
            return None
        return new_battery if new_battery != old_battery else None

    async def disconnect(self) -> None:
        """
        Stop the toy and close the connection. Playback sends nothing afterward.
        The toy is disconnected even if this raises.

        Raises:
            ConnectionError: Stopping the toy or closing the connection failed.
        """
        # A playback tick scheduled before the hub dropped this toy can still run; it must not send anything any more.
        self._closed = True
        await self._toy.strict_disconnect()

    async def reconnect(self) -> None:
        """
        Reopen the connection after an unexpected disconnect.

        Raises:
            ConnectionError: Reconnecting failed.
            RuntimeError: Called after :meth:`disconnect`.
        """
        await self._toy.strict_reconnect()


class _LovenseController(_ToyController):
    """
    Controller of Lovense toys. :meth:`get_info` adds Lovense information.

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
        Information about the toy, see :meth:`_ToyController.get_info`. With *full*, the toy is additionally asked for:

        - ``status`` (int or None): 2 means normal. None if the toy does not report it.
        - ``batch_number`` (str or None): e.g., ``"241015"``. None if the toy does not report it.
        - ``device_type`` (str): e.g., ``"C:11:0082059AD3BD"``.

        Args:
            full: Ask the toy for the information above in addition to the information asked by _ToyController.get_info

        Raises:
            ConnectionError: With *full*: a query could not be delivered, or no reply came in time.

        Returns:
            The information.
        """
        info = await super().get_info(full)

        if full:
            info["status"] = await self._if_reported(self._toy.strict_get_status)
            info["batch_number"] = await self._if_reported(
                self._toy.strict_get_batch_number
            )
            info["device_type"] = await self._toy.strict_get_device_type()

        return info

    @staticmethod
    async def _if_reported(query: Callable[[], Awaitable[T]]) -> T | None:
        """
        Ask the toy for a value that not every toy reports (a Gush answers the status request with "unkown").

        Args:
            query: The strict request to send (e.g., ``strict_get_status``).

        Raises:
            ConnectionError: The query could not be delivered, or no reply came in time.

        Returns:
            The value, or None if the toy answered with something else.
        """
        try:
            return await query()
        except UnexpectedToyResponse:
            return None


class _MockEstimController(_ToyController):
    """
    High-level controller for the fictional MockEstimToys brand.

    Args:
        toy: Low-level ``MockEstimToy`` instance.
        initial_battery: Initial battery level (0-100) or None if the toy has no battery.
    """

    def __init__(self, toy: MockEstimToy, initial_battery: int | None = None):
        self._toy: MockEstimToy = toy
        super().__init__(toy, initial_battery)


#: Brand (``Toy.brand``) -> its controller class. Register a new brand's controller here.
_CONTROLLER_BY_BRAND: dict[str, type[_ToyController]] = {
    "Lovense": _LovenseController,
    "MockEstimToys": _MockEstimController,
}
