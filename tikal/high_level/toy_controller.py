"""
High-Level API: :class:`ToyController`, the synchronous handle of one connected toy.

:class:`~tikal.high_level.ToyHub` creates the controllers; do not instantiate them.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, TypeVar

from .._core import UnknownToyError, _ToyController
from ..funscript import Funscript, FunscriptMode

if TYPE_CHECKING:
    from .toy_hub import ToyHub

T = TypeVar("T")


class ToyController:
    """
    Synchronous control of one connected toy. Created by :class:`~tikal.high_level.ToyHub`.

    - State changes (pause, block, pattern, funscript, :meth:`seek`, limits) take effect at once: the state read right
      afterward reflects them. A stop or limit correction they require is sent in the background.
    - Commands (intensities, :meth:`stop`, queries) are sent in the background, in call order. Their optional callback
      receives the result, or None if the command could not be delivered (e.g., the toy is not connected).
    - A manual intensity command pauses the pattern. Resume it with ``set_paused(False)``.
    - Callbacks run on the hub's background thread. Keep them short.
    - Once the toy is disconnected (or lost, or powered off), the controller reports its last state, and commands report
      None.

    Example::

        toy.intensity1(15)  # pauses the pattern
        toy.set_pattern([(1000, 10, 5), (500, 0, 0)], wraparound=True)  # (duration_ms, intensity1, intensity2)
        toy.set_paused(False)  # play it
        toy.toggle_pause()  # pause it: the toy stops

    Args:
        hub: The hub that manages the toy.
        toy: The toy's controller in the hub's core.
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
        """Unique identifier, e.g., the Bluetooth address."""
        return self._toy_id

    @property
    def model_name(self) -> str:
        """Model name, e.g., ``"Nora"``. Change it with :meth:`set_model_name`."""
        return self._toy().model_name

    @property
    def name(self) -> str:
        """Human-readable name, e.g., the Bluetooth name."""
        return self._toy().name

    @property
    def brand(self) -> str:
        """Brand, e.g., ``"Lovense"``."""
        return self._toy().brand

    @property
    def max_intensity(self) -> int:
        """Highest intensity level, e.g., 20 for Lovense toys. The lowest is 0."""
        return self._toy().max_intensity

    @property
    def current_intensities(self) -> tuple[int, int]:
        """
        ``(intensity1, intensity2)``: the levels the toy last acknowledged.
        If the toy only has one capability, intensity2 is always 0.
        """
        return self._toy().current_intensities

    @property
    def intensity_names(self) -> tuple[str, str | None]:
        """
        Display names ``(primary, secondary)`` of the capabilities, e.g., ``("Vibration", "Rotation")``. The secondary
        If the toy only has one capability, the second name is None.
        """
        return self._toy().intensity_names

    @property
    def change_rotation_direction_available(self) -> bool:
        """Whether the toy can change its rotation direction (see :meth:`change_rotation_direction`)."""
        return self._toy().change_rotation_direction_available

    @property
    def is_connected(self) -> bool:
        """
        Whether the toy is connected.

        While it is not (e.g., while the hub reconnects it), nothing is sent to it: commands report None, state changes
        still take effect. After reconnecting, the toy is stopped and its pattern paused.
        """
        return self._core.is_connected(self._toy_id)

    @property
    def is_paused(self) -> bool:
        """Whether pattern playback is currently paused (the pattern timer stops advancing)."""
        return self._toy().is_paused

    @property
    def is_blocked(self) -> bool:
        """Whether the toy is blocked: it stays at 0, and manual intensity commands are refused."""
        return self._toy().is_blocked

    @property
    def intensity_limits(self) -> tuple[int, int]:
        """The current (intensity1, intensity2) limits. See :meth:`set_intensity1_limit`."""
        return self._toy().intensity_limits

    @property
    def pattern_version(self) -> int:
        """Increases whenever the pattern, the pause, or the position changes."""
        return self._toy().pattern_version

    def get_pattern_time(self) -> float:
        """
        The pattern time: the playback position, without paused time.

        Returns:
            The pattern time in ms. It does not wrap around: past the end of a wraparound pattern, it keeps growing.
        """
        return self._toy().get_pattern_time()

    def get_pattern_values(self, pattern_time: float) -> tuple[int, int]:
        """
        The intensities the pattern plays at a pattern time, without applied limits.

        Args:
            pattern_time: The pattern time in ms, see :meth:`get_pattern_time`.

        Returns:
            ``(intensity1, intensity2)``. ``(0, 0)`` without a pattern, and past the end of a pattern without
            wraparound.
        """
        return self._toy().get_pattern_values(pattern_time)

    def get_pattern_data(self) -> tuple[list[tuple[int, int, int]], bool, bool, float]:
        """
        The full pattern and playback state.

        Returns:
            ``(pattern, wraparound, is_paused, pattern_time)``. A copy of the pattern, whether it loops, whether it is
            paused, and :meth:`get_pattern_time`.
        """
        return self._toy().get_pattern_data()

    # ------------------------------------------------------------------------------------------------------------------
    # State changes
    # ------------------------------------------------------------------------------------------------------------------

    def toggle_pause(self) -> bool:
        """
        Toggle pattern playback pause state. See :meth:`set_paused`.

        Returns:
            True if now paused, False if now playing.
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
            True if now blocked, False if now unblocked.
        """

        def toggle(toy: _ToyController) -> bool:
            return toy.apply_blocked(not toy.is_blocked)

        blocked = self._apply(toggle)
        if blocked:
            self._send_stop("toggle_block")
        return blocked

    def set_paused(self, pause: bool) -> None:
        """
        Pause or resume pattern playback. Does nothing if the toy is in that state already.

        - Pausing stops the toy, freezes the pattern time, and unblocks the toy.
        - Resuming continues where the pattern was paused.
        - Manual intensity commands still work while paused.

        Args:
            pause: True to pause, False to resume.
        """

        def change(toy: _ToyController) -> bool:
            if toy.is_paused == pause:
                return False
            return toy.apply_paused(pause)

        if self._apply(change):
            self._send_stop("set_paused")

    def set_blocked(self, block: bool) -> None:
        """
        Block or unblock the toy. Does nothing if it is in that state already.

        - While blocked, the toy stays at 0: manual intensity commands report False, and :meth:`direct_command` reports
          None. The pattern keeps advancing without driving the toy.
        - Blocking stops the toy and unpauses the pattern.

        Args:
            block: True to block, False to unblock.
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
        Set a pattern for automatic toy control. An empty list clears it and stops the toy. The pause stays as it is.

        Args:
            pattern: Segments as ``(duration_ms, intensity1, intensity2)``, levels from 0 to :attr:`max_intensity`.
                Stored as given: limits are applied during playback.
            wraparound: Loop the pattern. If False, it plays once, then the intensities are 0.
            reset_time: Start at the beginning. If False, keep the current pattern time.

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

    def set_funscript(
        self,
        script: Funscript | str | os.PathLike[str],
        *,
        mode: FunscriptMode = "speed",
        max_speed: float = 400.0,
        secondary: Funscript | str | os.PathLike[str] | None = None,
        start_ms: float = 0.0,
    ) -> None:
        """
        Play a funscript: convert it into a pattern for this toy and set it, played once. The pause stays as it is: a
        paused toy starts the script once resumed.

        The conversion uses the toy's :attr:`max_intensity` and recommended minimum interval, and runs in the calling
        thread. To follow a video, call :meth:`seek` when the video jumps, and :meth:`set_paused` when it pauses or
        resumes.

        Args:
            script: The script for the primary capability, or the path of a ``.funscript`` file.
            mode: ``"speed"`` (the intensity follows the stroke speed) or ``"position"`` (the intensity follows the
                position).
            max_speed: Stroke speed in positions per second that maps to :attr:`max_intensity` in ``"speed"`` mode. With
                the default of 400, a full stroke in 250 ms gives the maximum.
            secondary: Optional script (or path) for the secondary capability, e.g., a ``.twist.funscript`` for a Nora's
                rotation. Without it, the secondary capability stays at 0.
            start_ms: Pattern time to start at in ms, e.g., the video's position. Negative values count as 0.

        Raises:
            OSError: A script file could not be read.
            pydantic.ValidationError: (a ValueError) A script file is not valid JSON or not a valid funscript.
            ValueError: *max_speed* is not positive, *mode* is unknown, or *start_ms* is not a finite number.

        Example::

            toy.set_funscript("video.funscript", start_ms=player.position_ms)
            toy.seek(player.position_ms)  # the video jumped
            toy.set_paused(True)  # the video paused
        """
        pattern = self._toy().convert_funscript(
            _as_funscript(script),
            mode=mode,
            max_speed=max_speed,
            secondary=_as_funscript(secondary) if secondary is not None else None,
        )
        if self._apply(lambda toy: toy.apply_funscript(pattern, start_ms)):
            self._send_stop("set_funscript")

    def seek(self, pattern_time: float) -> None:
        """
        Move pattern playback to a position, e.g., to follow a video that was skipped forward or back.

        Nothing is sent to the toy: the pattern simply continues from the new position. A paused pattern stays paused
        at it until it is resumed.

        Args:
            pattern_time: The new pattern time in ms. Negative values count as 0. Past the end of a wraparound pattern,
                playback wraps around.

        Raises:
            ValueError: *pattern_time* is not a finite number.

        Example::

                toy.set_funscript("video.funscript")
                toy.seek(90_000)  # continue 1:30 into the script
        """
        self._apply(lambda toy: toy.apply_seek(pattern_time))

    def set_intensity1_limit(self, level: int | None) -> None:
        """
        Set the upper limit for the primary intensity. Every intensity1 command and pattern value is clamped to it.

        A toy already running above the new limit is brought down to it right away.

        Args:
            level: The limit, clamped to 0 to :attr:`max_intensity`. None removes it.
        """
        self._set_limit(0, level)

    def set_intensity2_limit(self, level: int | None) -> None:
        """
        Set the upper limit for the secondary intensity. Behaves like :meth:`set_intensity1_limit`.

        Args:
            level: The limit, clamped to 0 to :attr:`max_intensity`. None removes it.
        """
        self._set_limit(1, level)

    # ------------------------------------------------------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------------------------------------------------------

    def intensity1(
        self, level: int, callback: Optional[Callable[[bool | None], Any]] = None
    ) -> None:
        """
        Set the primary capability, e.g., Gush: vibration, Solace: thrust. Pauses the pattern.

        Args:
            level: 0 to :attr:`max_intensity`. Values outside are clamped. The intensity1 limit applies.
            callback: Called with True if the toy took the level, False if the toy is blocked (nothing changes), or
            None if the command could not be delivered (nothing changes).

        Example::

                toy.intensity1(15)  # Simple command

                def on_complete(success):
                    print("Succeeded:", success)

                toy.intensity1(toy.max_intensity, callback=on_complete)  # With callback
        """
        self._manual_intensity(0, level, callback)

    def intensity2(
        self, level: int, callback: Optional[Callable[[bool | None], Any]] = None
    ) -> None:
        """
        Set the secondary capability, e.g., Solace: depth, Max: air pump. Like :meth:`intensity1`.

        Without a secondary capability, nothing is sent and *callback* receives False. The pattern is still paused in
        that case.

        Args:
            level: 0 to :attr:`max_intensity`. Values outside are clamped. The intensity2 limit applies.
            callback: Optional callback, see :meth:`intensity1`.

        Example::

                toy.intensity2(toy.max_intensity // 2)  # Set secondary capability intensity to medium
        """
        self._manual_intensity(1, level, callback)

    def stop(self, callback: Optional[Callable[[bool | None], Any]] = None) -> None:
        """
        Stop all toy actions (set all intensities to zero).

        If a pattern is active and not paused, this method pauses the pattern.

        Args:
            callback: Receives True if the toy stopped, None if the stop could not be delivered.

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
        Reverse the rotation direction (see :attr:`change_rotation_direction_available`).

        Args:
            callback: Called with True if the direction changed, False if the toy does not support rotation, None if
            the command could not be delivered.

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
        Ask the toy for information about itself.

        The dict always contains:

        - ``toy_id``, ``name`` (e.g., the Bluetooth name), ``model_name``, ``brand`` (str)
        - ``intensity_names`` (list of 2 str): the second is empty without a secondary capability
        - ``supports_rotation`` (bool), ``max_intensity`` (int)
        - ``recommended_min_interval`` (int): recommended minimum interval between intensity changes, in ms
        - ``battery`` (int or None): the level in percent, None without battery

        Lovense toys add ``status`` (int, 2 is normal), ``batch_number`` (str, e.g., ``"241015"``) and ``device_type``
        (str, e.g., ``"C:11:0082059AD3BD"``). ``status`` and ``batch_number`` are None if the toy does not report them.

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
        Send a raw command. Use only for commands the library does not implement.
        The tracked state does not update by this. Don't use it for commands that the library implements.

        Args:
            command: Command string in the toy's protocol format (e.g., "DeviceType").
            callback: Callback invoked with the toy's response string, or None if the command could not be delivered.

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
        Change the toy's model. Capabilities both models share keep running; the others are switched off. On success,
        the toy cache remembers the model.

        For a blocking call that returns the error, see :meth:`ToyHub.update_model_name`.

        Args:
        model_name: New model name. Must be a valid model for this toy's brand. Case-insensitive.
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

        Returns:
            The current one, or the last one the toy had once it left the hub.
        """
        current = self._core.get_controller(self._toy_id)
        if current is not None:
            self._last_known = current
        return self._last_known

    def _apply(self, transition: Callable[[_ToyController], T]) -> T:
        """
        Apply a state transition right away, on the hub's loop thread. Once the toy left the hub, only the state this
        controller reports changes.

        Args:
            transition: Changes the state of the toy's core controller, without I/O.

        Returns:
            What *transition* returns, e.g., whether the toy has to be stopped now.

        Raises:
            TimeoutError: The hub's loop did not run *transition* within 5 seconds.
        """

        def apply() -> T:
            try:
                return self._core.apply_state(self._toy_id, transition)
            except UnknownToyError:
                return transition(self._last_known)

        return self._hub._on_loop(apply)

    def _manual_intensity(
        self, channel: int, level: int, callback: Optional[Callable[[Any], Any]]
    ) -> None:
        """
        Set one capability manually, see :meth:`intensity1`. Pauses the pattern, unless the toy is blocked or not
        connected.

        Args:
            channel: 0 for intensity1, 1 for intensity2.
            level: The level, clamped to 0 to :attr:`max_intensity`.
            callback: Called with True, False (blocked or no such capability), or None (not delivered). Optional.
        """
        command = f"intensity{channel + 1}"
        level = max(0, min(level, self.max_intensity))

        def accept(toy: _ToyController) -> bool | None:
            if not self._core.is_connected(self._toy_id):
                return None  # nothing is sent, and nothing changes
            return toy.accept_manual_intensity()

        accepted = self._apply(accept)
        if not accepted:
            self._hub._run_callback(callback, accepted, command)
            return
        self._send(command, lambda toy: toy.send_intensity(channel, level), callback)

    def _set_limit(self, channel: int, level: int | None) -> None:
        """
        Set the limit of one channel, then bring the toy down to it in the background if it runs above.

        Args:
            channel: 0 for intensity1, 1 for intensity2.
            level: The limit, clamped to 0 to :attr:`max_intensity`. None removes it.
        """
        if self._apply(lambda toy: toy.apply_limit(channel, level)):
            self._send(
                f"set_intensity{channel + 1}_limit",
                lambda toy: toy.enforce_limit(channel),
            )

    def _send_stop(
        self, command: str, callback: Optional[Callable[[Any], Any]] = None
    ) -> None:
        """
        Set both intensities to 0 in the background, in order with the toy's other commands. Pause and block stay.

        Args:
            command: Name of the command that requires the stop, for the log.
            callback: Called with True once the toy stopped, or None if the stop could not be delivered. Optional.
        """
        self._send(command, lambda toy: toy.stop_output(), callback)

    def _send(
        self,
        command: str,
        send: Callable[[_ToyController], Awaitable[Any]],
        callback: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """
        Send a command through the toy's core controller in the background, in order with the toy's other commands.

        Args:
            command: Name of the command, for the log.
            send: Sends the command, given the toy's core controller.
            callback: Called with the command's result, or None if it could not be delivered. Optional.
        """
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
        Run a command in the background. Commands start in call order.

        Args:
            command: Name of the command, for the log.
            run: Starts the command.
            callback: Called with the command's result, or None if it could not be delivered. For a toy that is not
                connected, nothing is sent: *callback* receives None right away, in the calling thread. Optional.
        """
        if not self.is_connected:
            self._hub._log.info(
                f"Did not send '{command}' to {self._toy_id}: not connected."
            )
            self._hub._run_callback(callback, None, command)
            return
        self._hub._submit(run, callback, command)


def _as_funscript(script: Funscript | str | os.PathLike[str]) -> Funscript:
    """
    A funscript, loaded from a file if needed.

    Args:
        script: The script, or the path of a ``.funscript`` file.

    Returns:
        *script* as given, or the script in the file.

    Raises:
        OSError: The file could not be read.
        pydantic.ValidationError: (a ValueError) The file is not valid JSON or not a valid funscript.
    """
    return script if isinstance(script, Funscript) else Funscript.load(script)
