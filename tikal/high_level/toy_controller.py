"""
Part of the High-level API: Provides representations of toys.

This module provides :class:`ToyController`, the synchronous handle for one connected toy:

- **Synchronous API**: All methods are synchronous, making them easy to use. State changes
  (pause, block, pattern, limits) take effect at once. Commands to the toy are sent in the background.
- **Pattern Playback**: Set time-based patterns that automatically control toy intensities.
- **Pause/Block States**: Temporarily halt toy actions while maintaining the pattern state.
- **Intensity Limits**: Cap what any command or pattern can make the toy do.
- **Callback Support**: Optional callbacks provide feedback when a command completes.

Note:
    You should not instantiate controllers. They are created for you by :class:`ToyHub`, which runs the toys and the
    pattern playback in the background.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, TypeVar

from .._core import UnknownToyError, _ToyController

if TYPE_CHECKING:
    from .toy_hub import ToyHub

T = TypeVar("T")


class ToyController:
    """
    Synchronous control of one connected toy.

    State changes (:meth:`set_paused`, :meth:`set_blocked`, :meth:`set_pattern`, the limits) take effect at once, so
    reading the state right afterward reflects them. Commands needing the toy are sent in the background. Commands
    that take a callback report their result there: None means the command could not be delivered (e.g., the toy is not
    connected, or the connection failed).

    Callbacks run on the hub's background thread. Keep them short. From a callback, you can call every method of a
    ToyController, but not the blocking methods of :class:`ToyHub` (they raise ``RuntimeError`` there).

    Example::

            # Get controller from ToyHub (see :class:`ToyHub` for that)
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
            toy.toggle_pause()  # Pauses pattern, sets the toy's intensity levels to 0
            toy.toggle_pause()  # Resumes pattern

    Args:
        hub: The ToyHub that manages the toy.
        toy: The toy's controller in the hub's core.

    Note:
        This class should not be instantiated directly. Use ToyHub's connection methods to get controller instances.
    """

    def __init__(self, hub: "ToyHub", toy: _ToyController):
        self._hub = hub
        self._core = hub._core
        self._toy_id = toy.toy_id
        self._last_known = toy

    # ------------------------------------------------------------------------------------------------------------------
    # State (read)
    # ------------------------------------------------------------------------------------------------------------------

    @property
    def toy_id(self) -> str:
        """Unique identifier for the toy (typically its Bluetooth address)."""
        return self._toy_id

    @property
    def model_name(self) -> str:
        """Model name of the toy (e.g., "Nora", "Lush"). Change it with :meth:`set_model_name`."""
        return self._toy().model_name

    @property
    def name(self) -> str:
        """Human-readable identifier of the toy (e.g., its Bluetooth name)."""
        return self._toy().name

    @property
    def brand(self) -> str:
        """Human-readable identifier of the toy brand (e.g., 'Lovense')."""
        return self._toy().brand

    @property
    def max_intensity(self) -> int:
        """Maximum intensity value for this toy (e.g., 20 for Lovense toys)."""
        return self._toy().max_intensity

    @property
    def current_intensities(self) -> tuple[int, int]:
        """Current (primary, secondary) intensity values. Secondary is always 0 for single-capability toys."""
        return self._toy().current_intensities

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
        return self._toy().intensity_names

    @property
    def change_rotation_direction_available(self) -> bool:
        """
        Check if the toy supports changing the rotation direction.

        Example::

                if toy.change_rotation_direction_available:
                    toy.change_rotation_direction()
        """
        return self._toy().change_rotation_direction_available

    @property
    def is_connected(self) -> bool:
        """
        Check if the toy is currently connected.

        While it is not (e.g., while the hub reconnects to it), nothing is sent to the toy: commands with a callback
        report None right away. State changes such as a pause or block still take effect. Upon reconnection, the toy is
        stopped and its pattern paused (it does not resume on its own).
        """
        return self._core.is_connected(self._toy_id)

    @property
    def is_paused(self) -> bool:
        """Whether pattern playback is currently paused (the pattern timer stops advancing)."""
        return self._toy().is_paused

    @property
    def is_blocked(self) -> bool:
        """Whether the toy is currently blocked (all intensities forced to zero)."""
        return self._toy().is_blocked

    @property
    def intensity_limits(self) -> tuple[int, int]:
        """The current (intensity1, intensity2) limits. See :meth:`set_intensity1_limit`."""
        return self._toy().intensity_limits

    @property
    def pattern_version(self) -> int:
        """Incremented each time the pattern state changes."""
        return self._toy().pattern_version

    def get_pattern_time(self) -> float:
        """Elapsed time in the current pattern in milliseconds (paused time does not count). 0.0 if no pattern."""
        return self._toy().get_pattern_time()

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """The ``(intensity1, intensity2)`` values at ``pattern_time`` (milliseconds) in the current pattern."""
        return self._toy().get_pattern_values(pattern_time)

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """The full pattern state: ``(pattern, wraparound, is_paused, elapsed_ms)``."""
        return self._toy().get_pattern_data()

    # ------------------------------------------------------------------------------------------------------------------
    # State changes
    # ------------------------------------------------------------------------------------------------------------------

    def toggle_pause(self) -> bool:
        """
        Toggle pattern playback pause state. See :meth:`set_paused`.

        Returns:
            bool: True if now paused, False if now unpaused.

        Example::

                is_paused = toy.toggle_pause()  # Pause pattern playback
                is_paused = toy.toggle_pause()  # Resume
        """

        def toggle(toy: _ToyController) -> bool:
            return toy.apply_paused(not toy.is_paused)

        paused = self._apply(toggle)
        if paused:
            self._send_stop("toggle_pause")
        return paused

    def toggle_block(self) -> bool:
        """
        Toggle block state. See :meth:`set_blocked`.

        Returns:
            bool: True if now blocked, False if now unblocked.

        Example::

                is_blocked = toy.toggle_block()  # Block all toy commands
                toy.intensity1(10, callback=lambda success: print(success))  # False
                is_blocked = toy.toggle_block()  # Unblock
        """

        def toggle(toy: _ToyController) -> bool:
            return toy.apply_blocked(not toy.is_blocked)

        blocked = self._apply(toggle)
        if blocked:
            self._send_stop("toggle_block")
        return blocked

    def set_paused(self, pause: bool) -> None:
        """
        Set the pattern playback pause state.

        When paused:

        - If a pattern is active, it stops advancing.
        - Toy intensities are set to zero, but manual commands can override this.
        - Block state is cleared if active (toy cannot be paused and blocked at the same time)

        Args:
            pause: If true will be paused, if false will be unpaused.
        """

        def change(toy: _ToyController) -> bool:
            if toy.is_paused == pause:
                return False
            return toy.apply_paused(pause)

        if self._apply(change):
            self._send_stop("set_paused")

    def set_blocked(self, block: bool) -> None:
        """
        Set the block state.

        When blocked:

        - All intensity commands are rejected (return False via callback)
        - Toy intensities are forced to zero
        - Pattern continues advancing but doesn't control the toy
        - Pause state is cleared if active (toy cannot be paused and blocked at the same time)

        Args:
            block: If true will be blocked, if false will be unblocked.
        """

        def change(toy: _ToyController) -> bool:
            if toy.is_blocked == block:
                return False
            return toy.apply_blocked(block)

        if self._apply(change):
            self._send_stop("set_blocked")

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

        The maximum possible intensity can be looked up via :attr:`max_intensity`. An empty list clears the pattern and
        stops the toy.

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
            Call ``set_paused(False)`` to resume the pattern.
        """
        if self._apply(lambda toy: toy.apply_pattern(pattern, wraparound, reset_time)):
            self._send_stop("set_pattern")

    def set_intensity1_limit(self, level: int | None) -> None:
        """
        Set the upper limit for the primary intensity. Every intensity1 command and pattern value is clamped to it.

        A toy already running above the new limit is brought down to it right away.

        Args:
            level: Maximum allowed intensity1 value (0 – max_intensity). Clamped to that range. None removes the limit.
        """
        if self._apply(lambda toy: toy.apply_intensity1_limit(level)):
            self._send(
                "set_intensity1_limit", lambda toy: toy.enforce_intensity1_limit()
            )

    def set_intensity2_limit(self, level: int | None) -> None:
        """
        Set the upper limit for the secondary intensity. Behaves like :meth:`set_intensity1_limit`.

        Args:
            level: Maximum allowed intensity2 value (0 – max_intensity). Clamped to that range. None removes the limit.
        """
        if self._apply(lambda toy: toy.apply_intensity2_limit(level)):
            self._send(
                "set_intensity2_limit", lambda toy: toy.enforce_intensity2_limit()
            )

    # ------------------------------------------------------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------------------------------------------------------

    def intensity1(
        self, level: int, callback: Optional[Callable[[bool | None], Any]] = None
    ) -> None:
        """
        Set the intensity of the primary capability.

        If a pattern is active and not paused, calling this method pauses the pattern to avoid conflicts.

        Args:
            level: Intensity level. The valid range is [0, self.max_intensity]. Values outside the range are clamped.
                The intensity1 limit applies as well.
            callback: Optional callback, invoked when the command completes. Receives True if the toy took it, False if
                the toy is blocked (right away), None if it could not be delivered.

        Example::

                toy.intensity1(15)  # Simple command

                def on_complete(success):
                    print("Succeeded:", success)

                toy.intensity1(toy.max_intensity, callback=on_complete)  # With callback
        """
        self._manual_intensity("intensity1", level, callback)

    def intensity2(
        self, level: int, callback: Optional[Callable[[bool | None], Any]] = None
    ) -> None:
        """
        Set the intensity of the secondary capability.

        Behavior is identical to :meth:`intensity1` but controls the secondary capability (e.g., rotation, air pump).
        Safe to call on toys without a secondary capability: nothing happens, and the callback receives False.

        Args:
            level: Intensity level. The valid range is [0, self.max_intensity]. Values outside the range are clamped.
            callback: Optional callback, see :meth:`intensity1`.

        Example::

                toy.intensity2(toy.max_intensity // 2)  # Set secondary capability intensity to medium
        """
        self._manual_intensity("intensity2", level, callback)

    def stop(self, callback: Optional[Callable[[bool | None], Any]] = None) -> None:
        """
        Stop all toy actions (set all intensities to zero).

        If a pattern is active and not paused, this method pauses the pattern.

        Args:
            callback: Optional callback invoked when the command completes. Receives True if the toy stopped, None if
                the stop could not be delivered.

        Example::

                toy.stop()
                toy.stop(callback=lambda ok: print("Stopped" if ok else "Failed"))
        """
        self._apply(lambda toy: toy.apply_stop())
        self._send_stop("stop", callback)

    def change_rotation_direction(
        self, callback: Optional[Callable[[bool | None], Any]] = None
    ) -> None:
        """
        Change rotation direction (if supported).

        This method toggles the rotation direction for toys with rotation capability.
        Safe to call on all toys: nothing happens if rotation is not supported.

        Args:
            callback: Optional callback invoked when the command completes. Receives True if the direction changed,
                False if the toy does not support rotation, None if the command could not be delivered.

        Example::

                toy.change_rotation_direction(callback=lambda ok: print("Direction changed" if ok else "Failed"))

        Note:
            You can use :attr:`change_rotation_direction_available` to check support before calling.
        """
        self._command(
            "change_rotation_direction",
            lambda: self._core.change_rotation_direction(self._toy_id),
            callback,
        )

    def get_battery_level(self, callback: Callable[[Optional[int]], Any]) -> None:
        """
        Ask the toy for its battery level.

        Args:
            callback: Callback invoked with the battery level (0-100%), or None if the toy has no battery or the query
                could not be delivered. Unlike most methods, here providing a callback is required (not optional).

        Example::

                def show_battery(level):
                    if level is not None:
                        print(f"Battery: {level}%")
                    else:
                        print("Battery unavailable")
                toy.get_battery_level(show_battery)

        Note:
            ToyHub also reports the battery levels of all toys to its battery callback whenever one changes.
        """
        self._command(
            "get_battery_level",
            lambda: self._core.fetch_battery(self._toy_id),
            callback,
        )

    def get_information(self, callback: Callable[[dict[str, Any] | None], Any]) -> None:
        """
        Gather detailed information about the toy.

        The dictionary always contains:

        - ``toy_id`` (str), ``name`` (str, e.g., the Bluetooth name), ``model_name`` (str), ``brand`` (str)
        - ``intensity_names`` (list of two str; the second is empty for single-capability toys)
        - ``supports_rotation`` (bool), ``max_intensity`` (int)
        - ``recommended_min_interval`` (int): recommended minimum interval between intensity commands, in ms
        - ``battery`` (int or None): battery level (0-100), None if the toy has no battery

        Depending on the brand, there is more. Lovense toys add ``status`` (e.g., "2" for normal), ``batch_number``
        (e.g., "241015") and ``device_type`` (e.g., "C:11:ADDRESS").

        Args:
            callback: Callback invoked with the dictionary, or None if the toy could not be queried.

        Example::

                def show_info(info):
                    for key, value in info.items():
                        print(f"{key}: {value}")
                toy.get_information(show_info)
        """

        async def gather() -> dict[str, Any]:
            info = await self._core.get_info(self._toy_id, full=True)
            info["battery"] = await self._core.fetch_battery(self._toy_id)
            return info

        self._command("get_information", gather, callback)

    def direct_command(
        self, command: str, callback: Callable[[str | None], Any]
    ) -> None:
        """
        Send a raw command directly to the toy.

        Use this for accessing toy features not exposed by the library. Requires knowledge of the toy's protocol.

        Args:
            command: Command string in the toy's protocol format (e.g., "DeviceType").
            callback: Callback invoked with the toy's response string, or None if the command could not be delivered.
                This callback is required (not optional).

        Example::

                def handle_response(response):
                    print(f"Device type response: {response}")
                    # Example: "C:11:0082059AD3BD"
                toy.direct_command("DeviceType", callback=handle_response)
        """
        self._command(
            "direct_command",
            lambda: self._core.direct_command(self._toy_id, command),
            callback,
        )

    def set_model_name(
        self,
        model_name: str,
        callback: Optional[Callable[[Optional[str]], Any]] = None,
    ) -> None:
        """
        Set the model name of the toy.

        The model name determines which commands are available and how they're interpreted. Validating the model
        involves sending commands to the toy, so the result is delivered via the optional callback rather than raised.
        On success, the ToyCache remembers the new model for this toy.

        Args:
            model_name: New model name. Must be a valid model for this toy's brand.
            callback: Optional callback is invoked when the command completes. Receives the toy's new model name on
                success, or None if the update failed (e.g., an invalid model name).

        Example::

                # Correct a model that was set incorrectly while connecting
                toy.set_model_name("Nora", callback=lambda name: print(f"Model is now {name}"))

        Note:
            For a blocking call that returns the error, use :meth:`ToyHub.update_model_name` instead.
        """

        async def change() -> str:
            await self._core.set_model(self._toy_id, model_name)
            return self.model_name

        self._command("set_model_name", change, callback)

    # ------------------------------------------------------------------------------------------------------------------
    # Private Methods
    # ------------------------------------------------------------------------------------------------------------------

    def _toy(self) -> _ToyController:
        """
        The toy's controller in the core, for reading its state.

        Once the toy is no longer part of the hub (disconnected, lost, powered off), this stays the last controller it
        had, so reading its state keeps working.
        """
        current = self._core.get_controller(self._toy_id)
        if current is not None:
            self._last_known = current
        return self._last_known

    def _apply(self, transition: Callable[[_ToyController], T]) -> T:
        """
        Apply a state transition right away, on the hub's event loop (see :meth:`ToyHub._on_loop`).

        A toy no longer part of the hub only changes the state this controller reports.
        """

        def apply() -> T:
            try:
                return self._core.apply_state(self._toy_id, transition)
            except UnknownToyError:
                return transition(self._last_known)

        return self._hub._on_loop(apply)

    def _manual_intensity(
        self, command: str, level: int, callback: Optional[Callable[[Any], Any]]
    ) -> None:
        """Accept a manual intensity command (pausing the pattern), then send it. See :meth:`intensity1`."""
        level = max(0, min(level, self.max_intensity))

        def accept(toy: _ToyController) -> bool | None:
            if not self._core.is_connected(self._toy_id):
                return None  # nothing is sent, and nothing changes
            return toy.accept_manual_intensity()

        accepted = self._apply(accept)
        if not accepted:
            self._hub._run_callback(callback, accepted, command)
            return
        if command == "intensity1":
            self._send(command, lambda toy: toy.send_intensity1(level), callback)
        else:
            self._send(command, lambda toy: toy.send_intensity2(level), callback)

    def _send_stop(
        self, command: str, callback: Optional[Callable[[Any], Any]] = None
    ) -> None:
        """Send the stop a state change requires. See :meth:`_send`."""
        self._send(command, lambda toy: toy.stop_output(), callback)

    def _send(
        self,
        command: str,
        send: Callable[[_ToyController], Awaitable[Any]],
        callback: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """Send what a state change requires, in order with every other command to the toy. See :meth:`_command`."""
        self._command(
            command, lambda: self._core.send(self._toy_id, command, send), callback
        )

    def _command(
        self,
        command: str,
        run: Callable[[], Awaitable[Any]],
        callback: Optional[Callable[[Any], Any]],
    ) -> None:
        """
        Run a command in the background and report its result to *callback* (None if it could not be delivered).

        A toy that is not connected is not sent anything: the callback receives None right away, in the caller's
        thread. Commands start in the order they are called, and the core sends the ones for one toy in that order.
        """
        if not self.is_connected:
            self._hub._log.info(
                f"Did not send '{command}' to {self._toy_id}: not connected."
            )
            self._hub._run_callback(callback, None, command)
            return
        self._hub._submit(run, callback, command)
