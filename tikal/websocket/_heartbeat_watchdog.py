"""
Private module of the WebSocket API: the heartbeat watchdog of :class:`~tikal.websocket.toy_server.ToyServer`.

A dead-man's switch. A client that arms it promises to send a ``heartbeat`` regularly. When an armed client fails, every
toy not blocked yet gets blocked and stopped. Only the user unblocks them.

The rules:

- An armed client that misses its deadline is *overdue*: the watchdog trips, once. It stays overdue until it proves it
  is still there: a ``heartbeat``, arming again, or disarming. Its next miss trips the watchdog again.
- An armed client that disconnects trips the watchdog as well.
- A client that stays overdue for the grace period is given up and its connection closed

Every trip is announced with a ``heartbeat_timeout`` event, naming the toys it blocked. See docs/websocket/events.md.
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
    Tracks the clients that armed the heartbeat, and blocks every toy when one of them fails.

    The server reports what its clients do (:meth:`arm`, :meth:`disarm`, :meth:`beat`, :meth:`client_disconnected`).
    The watchdog touches no toy or connection itself, except that it closes a client it gave up on.

    Args:
        block_all: Blocks and stops every toy that is not blocked yet. Returns the ids of the toys it blocked, and of
            those among them it could not stop.
        broadcast: Sends an event (its name and its data) to every connected client.
        log: Logger to use.
    """

    def __init__(
        self,
        block_all: Callable[[], Awaitable[tuple[list[str], list[str]]]],
        broadcast: Callable[[str, dict[str, Any]], Awaitable[None]],
        log: Logger,
    ) -> None:
        self._block_all = block_all
        self._broadcast = broadcast
        self._log = log

        #: Seconds an armed client may let pass between two heartbeats.
        self.timeout = 3.0
        #: Seconds between two checks of the deadlines.
        self.check_interval = 1.0
        #: Seconds a client may stay overdue before its connection is closed.
        self.grace_period = 30.0

        # Armed clients -> time of their last heartbeat (time.monotonic).
        self._last_beat: dict[ServerConnection, float] = {}
        # Armed clients that are currently past their deadline. They stay in _last_beat (and so stay watched) until
        # they disarm or disconnect; each one trips the watchdog once per time it goes overdue.
        self._overdue: set[ServerConnection] = set()
        # Serializes the trips, so blocking and announcing one cannot interleave with another.
        self._trip_lock = asyncio.Lock()
        # Clients given up for staying overdue whose connection is still closing. Their messages are ignored.
        self._kicked: set[ServerConnection] = set()
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._check_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # What ToyServer reports
    # ------------------------------------------------------------------

    def is_kicked(self, client: ServerConnection) -> bool:
        """
        Whether the watchdog gave up on a client whose connection is still closing. Ignore whatever such a client
        sends, e.g., a command it queued before it froze.

        Args:
            client: The client.

        Returns:
            True from the moment the watchdog gives up on *client* until it disconnects.
        """
        return client in self._kicked

    async def arm(self, client: ServerConnection) -> None:
        """
        Start (or keep) watching a client. Arming counts as a heartbeat.

        Args:
            client: The client.
        """
        self._last_beat[client] = time.monotonic()
        if self._check_task is None or self._check_task.done():
            self._check_task = asyncio.get_running_loop().create_task(
                self._check_loop(), name="heartbeat-check"
            )
        self._recovered(client)

    async def disarm(self, client: ServerConnection) -> None:
        """
        Stop watching a client. An overdue client counts as back.

        Args:
            client: The client. Need not be armed.
        """
        self._last_beat.pop(client, None)
        self._recovered(client)
        self._stop_checking_if_unused()

    async def beat(self, client: ServerConnection) -> None:
        """
        Record that a client is still there. An overdue client counts as back.

        Args:
            client: The client. Ignored if it is not armed.
        """
        if client in self._last_beat:
            self._last_beat[client] = time.monotonic()
            self._recovered(client)
        else:
            self._log.debug("Heartbeat received from non-subscribed client.")

    async def client_disconnected(self, client: ServerConnection) -> None:
        """
        Forget a client that disconnected. Trips the watchdog if it was armed.

        Args:
            client: The client.
        """
        self._kicked.discard(client)
        was_armed = self._last_beat.pop(client, None) is not None
        self._overdue.discard(client)
        self._stop_checking_if_unused()
        if was_armed:
            await self._trip(
                "disconnect",
                "Heartbeat client disconnected. All toys were blocked.",
            )

    def shutdown(self) -> None:
        """Stop watching everyone. The hub has disconnected every toy, so there is nothing left to block."""
        self._last_beat.clear()
        self._overdue.clear()
        self._kicked.clear()
        self._stop_checking_if_unused()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _stop_checking_if_unused(self) -> None:
        """Cancel the deadline checks once no client is armed."""
        if not self._last_beat and self._check_task is not None:
            self._check_task.cancel()
            self._check_task = None

    def _recovered(self, client: ServerConnection) -> None:
        """
        Mark an overdue client as back: its next miss trips the watchdog again. The toys it got blocked stay blocked.

        Args:
            client: The client. Nothing happens if it is not overdue.
        """
        if client not in self._overdue:
            return
        self._overdue.discard(client)
        self._log.info(
            "Heartbeat client recovered; %d still overdue.", len(self._overdue)
        )

    async def _check_loop(self) -> None:
        """
        Every :attr:`check_interval`, trip the watchdog for clients that just went overdue, and give up on those overdue
        for the whole grace period. Ends once no client is armed.
        """
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
                    "Heartbeat timeout for %d client(s). Blocking all toys.",
                    len(newly_overdue),
                )
                self._overdue.update(newly_overdue)
                # Shielded: this loop is canceled when the last armed client leaves. Cancelling mid-trip could leave
                # toys blocked but never stopped, or a trip that is never announced.
                await asyncio.shield(
                    self._trip(
                        "timeout",
                        "Heartbeat timeout. All toys were blocked.",
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
                self._give_up_on(client)

    def _give_up_on(self, client: ServerConnection) -> None:
        """
        Give up on a client that stayed overdue for the whole grace period: stop watching it and close its connection.

        A stuck client can keep its connection open (a browser answers pings while the page's JavaScript hangs) and send
        stale commands once it wakes up. Its toys were blocked when it went overdue, so this blocks nothing more.
        Messages it sends while its connection closes are ignored (see :meth:`is_kicked`).

        Args:
            client: The client.
        """
        self._last_beat.pop(client, None)
        self._overdue.discard(client)
        self._kicked.add(client)
        self._log.warning(
            "Heartbeat client overdue for more than %.0f s. Closing its connection.",
            self.grace_period,
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
        Block every not blocked yet, then broadcast ``heartbeat_timeout``. Never raises.

        The event is broadcast on every trip, also when every toy was blocked already. A timeout trip is dropped if the
        overdue client came back meanwhile.

        Args:
            reason: ``"timeout"`` for an overdue client, ``"disconnect"`` for one that vanished.
            message: Human-readable description, sent with the event.
        """
        async with self._trip_lock:
            if reason == "timeout" and not self._overdue:
                # The client that went overdue is back already: its heartbeat came before this ran (the deadline check
                # runs the trip as a task). Nothing calls for blocking the toys anymore.
                self._log.info(
                    "Heartbeat timeout resolved before the toys were blocked."
                )
                return
            blocked: list[str] = []
            failed: list[str] = []
            try:
                blocked, failed = await self._block_all()
            except Exception:
                self._log.exception("Failed to block the toys.")
            if failed:
                self._log.error(
                    "Could not stop %d toy(s) after blocking them: %s",
                    len(failed),
                    failed,
                )
            await self._broadcast(
                "heartbeat_timeout",
                dict(
                    message=message,
                    reason=reason,
                    blocked_toy_ids=blocked,
                    failed_toy_ids=failed,
                ),
            )
