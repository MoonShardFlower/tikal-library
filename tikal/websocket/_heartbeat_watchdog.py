"""
Private Module of the WebSocket API: the heartbeat watchdog of ToyServer.

A dead-man's switch. A client that arms it promises to send a ``heartbeat`` regularly. When it stops, every toy is put
under the safety hold (see ``_ToyHub.set_safety_hold``): kept at zero.

The rules:

- An armed client that misses its deadline is *overdue*. The hold goes on and stays on until that client proves it is
  still there: a ``heartbeat``, arming again, or disarming.
- A client that stays overdue for the grace period is given up: its connection is closed, and it counts as disconnected.
- An armed client that disconnects leaves a *disconnect hold*, which ends when a client sends ``release_hold``.
- The hold ends once no armed client is overdue and there is no disconnect hold.

Every trip is announced with a ``heartbeat_timeout`` event, the end of the hold with ``hold_released``. What the hold
is on for at any moment ("timeout": a client is overdue, "disconnect": a disconnect hold, or both) is part of every
toy's state (``hold_reasons``), so a client can tell whether ``release_hold`` would help.
See docs/websocket/events.md.
"""

import asyncio
import time
from logging import Logger
from typing import Any, Awaitable, Callable, Literal

from websockets.asyncio.server import ServerConnection

#: WebSocket close code sent to a client that stayed overdue for the whole heartbeat grace period.
CLOSE_HEARTBEAT_OVERDUE = 4000


class _HeartbeatWatchdog:
    """
    Tracks the clients that armed the heartbeat, and decides when the safety hold is on.

    It does not touch toys or connections itself, except for closing a client it gave up on. ToyServer tells it what
    its clients do (:meth:`arm`, :meth:`disarm`, :meth:`beat`, :meth:`release`, :meth:`client_disconnected`).

    Args:
        set_safety_hold: Puts every toy under the safety hold for the given reasons, changes the reasons, or takes the
            hold off (no reasons). Returns the ids of the toys that could not be stopped when the hold went on.
        broadcast: Sends an event (its name and its data) to every connected client.
        log: Logger to use.
    """

    def __init__(
        self,
        set_safety_hold: Callable[[list[str]], Awaitable[list[str]]],
        broadcast: Callable[[str, dict[str, Any]], Awaitable[None]],
        log: Logger,
    ) -> None:
        self._set_safety_hold = set_safety_hold
        self._broadcast = broadcast
        self._log = log

        #: Seconds an armed client may let pass between two heartbeats.
        self.timeout = 3.0
        #: Seconds between two checks of the deadlines.
        self.check_interval = 1.0
        #: Seconds a client may stay overdue before it is treated as disconnected and its connection is closed.
        self.grace_period = 30.0

        # Armed clients -> time of their last heartbeat (time.monotonic).
        self._last_beat: dict[ServerConnection, float] = {}
        # Armed clients that are currently past their deadline. They stay in _last_beat (and so stay watched) until
        # they disarm or disconnect; while any of them is overdue, the safety hold stays on.
        self._overdue: set[ServerConnection] = set()
        # Set when an armed client disconnected. It can never prove it is back, so only release() clears this.
        self._disconnect_hold = False
        # The reasons the safety hold is currently applied for (see _current_reasons); empty while it is off.
        self._applied: tuple[str, ...] = ()
        self._hold_lock = asyncio.Lock()
        # Clients given up for staying overdue whose connection is still closing. Their messages are ignored.
        self._kicked: set[ServerConnection] = set()
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._check_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # What ToyServer reports
    # ------------------------------------------------------------------

    def is_kicked(self, client: ServerConnection) -> bool:
        """
        Whether the watchdog gave up on *client* (see :meth:`_give_up_on`) and its connection is still closing.

        Whatever such a client still sends must not count, e.g., a release_hold for the hold it caused.
        """
        return client in self._kicked

    async def arm(self, client: ServerConnection) -> None:
        """Start (or keep) watching *client*. Arming is proof of life just like a heartbeat is."""
        self._last_beat[client] = time.monotonic()
        if self._check_task is None or self._check_task.done():
            self._check_task = asyncio.get_running_loop().create_task(
                self._check_loop(), name="heartbeat-check"
            )
        await self._recovered(client)

    async def disarm(self, client: ServerConnection) -> None:
        """Stop watching *client*. A deliberate opt-out, so an overdue client counts as back."""
        self._last_beat.pop(client, None)
        await self._recovered(client)
        self._stop_checking_if_unused()

    async def beat(self, client: ServerConnection) -> None:
        """Record that the armed client *client* is still there."""
        if client in self._last_beat:
            self._last_beat[client] = time.monotonic()
            await self._recovered(client)
        else:
            self._log.debug("Heartbeat received from non-subscribed client.")

    async def release(self) -> None:
        """
        End a hold caused by a disconnected client (the release_hold command).

        A client that is still overdue keeps the hold on until it is back (or gone, which turns it into a disconnect
        hold that this then releases).
        """
        if self._disconnect_hold:
            self._log.info("release_hold received; clearing the disconnect hold.")
        self._disconnect_hold = False
        await self._update_hold()

    async def client_disconnected(self, client: ServerConnection) -> None:
        """
        Forget *client*. If it was armed, this trips the watchdog.

        A client that armed the heartbeat and vanished (crash or abrupt close) can never send the heartbeat that would
        end the hold, so the hold stays on until a client sends release_hold.
        """
        self._kicked.discard(client)
        was_armed = self._last_beat.pop(client, None) is not None
        if was_armed:
            # Set before it leaves _overdue, so the hold never looks releasable in between.
            self._disconnect_hold = True
        self._overdue.discard(client)
        self._stop_checking_if_unused()
        if was_armed:
            await self._trip(
                "disconnect",
                "Heartbeat client disconnected. All toys held until a client sends release_hold.",
            )

    def shutdown(self) -> None:
        """Stop watching everyone. The hub has disconnected every toy, so there is nothing left to hold."""
        self._last_beat.clear()
        self._overdue.clear()
        self._kicked.clear()
        self._disconnect_hold = False
        self._stop_checking_if_unused()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _stop_checking_if_unused(self) -> None:
        """Cancel the deadline checks once no client is armed."""
        if not self._last_beat and self._check_task is not None:
            self._check_task.cancel()
            self._check_task = None

    async def _recovered(self, client: ServerConnection) -> None:
        """
        Mark a previously overdue client as alive again, and end the safety hold if nothing else calls for it.

        Called from every path where an armed client proves it is still there (a heartbeat, a re-arm, or a deliberate
        opt-out). A client that simply vanished never reaches this; it leaves a disconnect hold instead.
        """
        if client not in self._overdue:
            return
        self._overdue.discard(client)
        self._log.info(
            "Heartbeat client recovered; %d still overdue.", len(self._overdue)
        )
        await self._update_hold()

    def _current_reasons(self) -> tuple[str, ...]:
        """
        What calls for the safety hold right now, sorted as the toys report it: "disconnect" for a disconnect hold
        (which release() ends), "timeout" while an armed client is overdue (which ends once it is back). Empty if
        nothing does.
        """
        reasons = []
        if self._disconnect_hold:
            reasons.append("disconnect")
        if self._overdue:
            reasons.append("timeout")
        return tuple(reasons)

    async def _check_loop(self) -> None:
        """Background loop that checks heartbeat deadlines and puts on the safety hold on timeout."""
        while self._last_beat:
            await asyncio.sleep(self.check_interval)
            now = time.monotonic()
            newly_overdue = [
                client
                for client, last in self._last_beat.items()
                if (now - last) > self.timeout and client not in self._overdue
            ]
            if newly_overdue:
                self._log.warning(
                    "Heartbeat timeout for %d client(s). Holding all toys.",
                    len(newly_overdue),
                )
                self._overdue.update(newly_overdue)
                # Shielded: this loop is canceled when the last armed client leaves. Cancelling mid-trip could leave
                # toys never stopped while the hold already counts as on.
                await asyncio.shield(
                    self._trip(
                        "timeout",
                        "Heartbeat timeout. All toys held until a heartbeat is received again.",
                    )
                )

            # Measured again: the trip above awaited toy commands, and a heartbeat may have arrived meanwhile.
            now = time.monotonic()
            limit = self.timeout + self.grace_period
            given_up = [
                client
                for client, last in self._last_beat.items()
                if (now - last) > limit
            ]
            for client in given_up:
                # Shielded for the same reason as the trip above.
                await asyncio.shield(self._give_up_on(client))

    async def _give_up_on(self, client: ServerConnection) -> None:
        """
        Give up on a client that stayed overdue for the whole grace period: treat it as disconnected and close it.

        A client whose app is stuck can keep its connection open (a browser answers pings even while the page's
        JavaScript is hung), and ``release_hold`` deliberately cannot override a client that is merely overdue. Without
        this, such a client could keep the hold on forever. From here on it is a disconnect hold, which any client can
        end with ``release_hold``.

        The client leaves the watchdog right away, and whatever it still sends while its connection closes is ignored
        (see :meth:`is_kicked`), so it can neither end the hold itself nor re-arm. The close runs in the background: a
        stuck client may never answer the closing handshake, and waiting for ``close_timeout`` would delay the
        heartbeat checks of every other client.
        """
        # Set before it leaves _overdue, so the hold never looks releasable in between.
        self._disconnect_hold = True
        self._last_beat.pop(client, None)
        self._overdue.discard(client)
        self._kicked.add(client)
        self._log.warning(
            "Heartbeat client overdue for more than %.0f s. Treating it as disconnected.",
            self.grace_period,
        )
        await self._trip(
            "disconnect",
            "Heartbeat client stayed overdue and was disconnected. All toys held until a client sends release_hold.",
        )
        task = asyncio.get_running_loop().create_task(
            client.close(code=CLOSE_HEARTBEAT_OVERDUE, reason="Heartbeat overdue"),
            name="close-overdue-client",
        )
        # Keep a reference: a bare create_task may be garbage-collected before the close completes.
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    async def _trip(
        self, reason: Literal["timeout", "disconnect"], message: str
    ) -> None:
        """
        Make sure the safety hold is on, for what calls for it now, then tell every client.

        Shared by the deadline check (a client stopped sending heartbeats) and the disconnect path (a client that armed
        the heartbeat vanished): both are "we lost the controlling client, make the toys safe". The caller has already
        recorded why (``_overdue`` or ``_disconnect_hold``), so :meth:`_current_reasons` includes it, and
        :meth:`_update_hold` knows when the hold may end.

        ``heartbeat_timeout`` is broadcast on every trip, also when the hold was already on, so clients learn about each
        client that went overdue or vanished. Its ``reason`` tells them whether the hold can end on its own.

        Never raises: it runs in the deadline check and in the disconnect handler, and neither may die.

        Args:
            reason: "timeout" for an overdue client, "disconnect" for one that vanished.
            message: Human-readable description, sent with the event.
        """
        async with self._hold_lock:
            reasons = self._current_reasons()
            if not reasons:
                # What tripped it is gone already: the client sent a heartbeat (or a release_hold came) before this ran.
                self._log.info("Heartbeat %s resolved before the hold went on.", reason)
                return
            failed: list[str] = []
            if reasons != self._applied:
                self._applied = reasons
                try:
                    failed = await self._set_safety_hold(list(reasons))
                except Exception:
                    self._log.exception("Failed to put on the safety hold.")
                if failed:
                    self._log.error(
                        "Could not stop %d toy(s) for the safety hold: %s",
                        len(failed),
                        failed,
                    )
            # Broadcast under the lock, so this event can never arrive after the hold_released that ends it.
            await self._broadcast(
                "heartbeat_timeout",
                dict(message=message, reason=reason, failed_toy_ids=failed),
            )

    async def _update_hold(self) -> None:
        """
        Bring the safety hold in line with what calls for it, after a reason for it may have gone (a client is back, or
        a release_hold came).

        With nothing left (no armed client overdue, no disconnect hold), the hold ends: every toy follows its own state
        again (a running pattern resumes, a paused or blocked toy stays that way), so nothing has to be restored, and
        ``hold_released`` is broadcast. Otherwise only its reasons change, which every toy's state reports (e.g., a
        release_hold ended the disconnect hold, but another client is still overdue).
        """
        async with self._hold_lock:
            if not self._applied:
                return  # The hold is off. Putting it on is up to _trip, which announces it.
            reasons = self._current_reasons()
            if reasons == self._applied:
                return
            self._applied = reasons
            try:
                await self._set_safety_hold(list(reasons))
            except Exception:
                self._log.exception("Failed to change the safety hold.")
            if reasons:
                self._log.info("Safety hold stays on, now for: %s", ", ".join(reasons))
                return
            self._log.info("Safety hold released.")
            await self._broadcast(
                "hold_released",
                dict(
                    message="Safety hold released. Toys follow their own state again."
                ),
            )
