"""
Tests for the per-client outbox of the WebSocket server (:mod:`tikal.websocket._outbox`).

A stand-in connection records what is sent, or never finishes sending, the way a client that stopped reading looks to
the server: ``send`` waits forever in websockets' ``drain()``.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.exceptions import ConnectionClosedOK

from tikal.websocket import _outbox
from tikal.websocket._outbox import _Outbox, close_or_abort

pytestmark = pytest.mark.asyncio

_LOG = logging.getLogger("tikal.tests.outbox")


class _Connection:
    """Stands in for a client's connection. Sends complete at once, unless *stuck*."""

    def __init__(self, stuck: bool = False) -> None:
        self.sent: list[str] = []
        self.stuck = stuck
        self.transport = Mock()

    async def send(self, message: str) -> None:
        if self.stuck:
            await asyncio.Event().wait()  # never set: the client does not read
        self.sent.append(message)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_messages_are_sent_in_order():
    connection = _Connection()
    outbox = _Outbox(connection, _LOG)  # type: ignore[arg-type]
    for message in ("a", "b", "c"):
        outbox.put(message)
    await _settle()
    assert connection.sent == ["a", "b", "c"]
    outbox.close()


async def test_put_does_not_wait_for_a_client_that_stopped_reading():
    connection = _Connection(stuck=True)
    outbox = _Outbox(connection, _LOG)  # type: ignore[arg-type]
    outbox.put("a")  # returns at once, while the send for it never completes
    outbox.put("b")
    await _settle()
    assert connection.sent == []
    connection.transport.abort.assert_not_called()
    outbox.close()


async def test_a_client_that_does_not_accept_a_message_in_time_is_dropped(
    monkeypatch,
):
    monkeypatch.setattr(_outbox, "SEND_TIMEOUT", 0.05)
    connection = _Connection(stuck=True)
    outbox = _Outbox(connection, _LOG)  # type: ignore[arg-type]
    outbox.put("a")
    await asyncio.sleep(0.2)
    connection.transport.abort.assert_called_once()
    outbox.put("b")  # ignored: the client was dropped
    connection.transport.abort.assert_called_once()


@pytest.mark.parametrize(
    "limit, value, message",
    [("MAX_WAITING_MESSAGES", 3, "x"), ("MAX_WAITING_CHARS", 10, "xxxx")],
)
async def test_a_client_with_too_much_waiting_is_dropped(
    monkeypatch, limit, value, message
):
    monkeypatch.setattr(_outbox, limit, value)
    connection = _Connection(stuck=True)
    outbox = _Outbox(connection, _LOG)  # type: ignore[arg-type]
    outbox.put(message)
    await _settle()  # the first message is being sent, and never finishes
    for _ in range(5):
        outbox.put(message)
    connection.transport.abort.assert_called_once()


async def test_a_closed_outbox_sends_nothing_more():
    connection = _Connection()
    outbox = _Outbox(connection, _LOG)  # type: ignore[arg-type]
    outbox.close()
    outbox.put("a")
    await _settle()
    assert connection.sent == []
    connection.transport.abort.assert_not_called()


async def test_a_connection_that_closed_ends_the_outbox_quietly():
    connection = _Connection()
    connection.send = AsyncMock(side_effect=ConnectionClosedOK(None, None))  # type: ignore[method-assign]
    outbox = _Outbox(connection, _LOG)  # type: ignore[arg-type]
    outbox.put("a")
    await _settle()
    outbox.put("b")
    await _settle()
    assert (
        connection.send.await_count == 1
    )  # nothing more is tried once the connection is gone
    connection.transport.abort.assert_not_called()


async def test_close_or_abort_closes_a_connection_that_answers():
    connection = Mock()
    connection.close = AsyncMock()
    await close_or_abort(connection, 1001, "bye")
    connection.close.assert_awaited_once_with(code=1001, reason="bye")
    connection.transport.abort.assert_not_called()


async def test_close_or_abort_aborts_a_connection_that_cannot_take_the_close_frame(
    monkeypatch,
):
    monkeypatch.setattr(_outbox, "CLOSE_TIMEOUT", 0.05)
    connection = Mock()
    connection.close = AsyncMock(side_effect=asyncio.Event().wait)
    await asyncio.wait_for(close_or_abort(connection, 1001), 1)
    connection.transport.abort.assert_called_once()
