"""Private module of the WebSocket API: the outbox of each client of :class:`~tikal.websocket.toy_server.ToyServer`"""

import asyncio
from collections import deque
from logging import Logger

from websockets.asyncio.server import ServerConnection

#: Maximum seconds one pending send may take before the connection is aborted.
SEND_TIMEOUT = 20.0
#: Seconds the closing handshake may take before the connection is aborted. The same as the server's close timeout.
CLOSE_TIMEOUT = 10.0
#: Messages that may wait in the application queue, excluding the in-flight send and networking buffers.
MAX_WAITING_MESSAGES = 256
#: Characters that may wait for a client before it is dropped (the messages are JSON, mostly ASCII).
MAX_WAITING_CHARS = 8 * 2**20


async def close_or_abort(
    connection: ServerConnection, code: int, reason: str = ""
) -> None:
    """
    Close a connection, or abort it if the closing handshake does not finish within :data:`CLOSE_TIMEOUT`.

    ``ServerConnection.close`` waits, without a timeout, until it can write the close frame, which never happens for a
    client that stopped reading. Never raises.

    Args:
        connection: The client's connection.
        code: The close code, e.g., ``CloseCode.GOING_AWAY``.
        reason: The close reason.
    """
    try:
        await asyncio.wait_for(
            connection.close(code=code, reason=reason), CLOSE_TIMEOUT
        )
    except Exception:
        # Timed out, or the connection broke while closing. Either way, nothing more is sent.
        connection.transport.abort()


class _Outbox:
    """
    The messages waiting for one client.

    :meth:`put` never waits. A client that does not keep up is dropped (its connection aborted), which ends its
    connection as a disconnect would: when it takes more than :data:`SEND_TIMEOUT` to accept a message, or when more
    than :data:`MAX_WAITING_MESSAGES` messages or :data:`MAX_WAITING_CHARS` characters wait for it.

    Create it on the server's event loop.

    Args:
        connection: The client's connection.
        log: Logger to use.
    """

    def __init__(self, connection: ServerConnection, log: Logger) -> None:
        self._connection = connection
        self._log = log
        self._messages: deque[str] = deque()
        self._chars = 0
        self._ready = asyncio.Event()
        # Set once nothing more is sent: the client was dropped, its connection closed, or close() was called.
        self._closed = False
        self._task = asyncio.get_running_loop().create_task(
            self._send_loop(), name="client-outbox"
        )

    def put(self, message: str) -> None:
        """
        Queue a message for the client. Does nothing once the outbox is closed.

        Args:
            message: The JSON message.
        """
        if self._closed:
            return
        if (
            len(self._messages) >= MAX_WAITING_MESSAGES
            or self._chars + len(message) > MAX_WAITING_CHARS
        ):
            self._drop(
                f"{len(self._messages)} messages ({self._chars} characters) wait for it"
            )
            return
        self._messages.append(message)
        self._chars += len(message)
        self._ready.set()

    def close(self) -> None:
        """Stop sending. Messages still waiting are discarded: the client is gone."""
        self._closed = True
        self._messages.clear()
        self._chars = 0
        self._task.cancel()

    def _drop(self, reason: str) -> None:
        """
        Drop a client that does not keep up: discard its messages and abort its connection.

        Args:
            reason: For the log.
        """
        self._log.warning("Dropping a client that does not keep up: %s.", reason)
        self.close()
        self._connection.transport.abort()

    async def _send_loop(self) -> None:
        """Send the waiting messages in order, until :meth:`close`, a drop, or the connection is gone."""
        while True:
            await self._ready.wait()
            self._ready.clear()
            while self._messages:
                message = self._messages.popleft()
                self._chars -= len(message)
                try:
                    await asyncio.wait_for(self._connection.send(message), SEND_TIMEOUT)
                except TimeoutError:
                    self._drop(
                        f"it did not accept a message within {SEND_TIMEOUT:.0f} s"
                    )
                    return
                except Exception:
                    # The connection is closing or gone. Its handler cleans up.
                    self._closed = True
                    self._messages.clear()
                    self._chars = 0
                    return
