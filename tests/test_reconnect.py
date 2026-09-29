"""Tests for the reconnect policy shared by the High-Level and WebSocket layers (``retry_within_window``)."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest

from tikal._private import retry_within_window

pytestmark = pytest.mark.asyncio

_LOG = logging.getLogger("test_reconnect")


async def test_first_success_returns_immediately(short_reconnect_window):
    attempt = AsyncMock(return_value=None)
    assert await retry_within_window(attempt, "a1", _LOG) is True
    assert attempt.await_count == 1


async def test_failed_attempts_are_retried_until_one_succeeds(short_reconnect_pause):
    attempt = AsyncMock(side_effect=[ConnectionError("gone"), OSError("adapter"), None])
    assert await retry_within_window(attempt, "a1", _LOG) is True
    assert attempt.await_count == 3


async def test_gives_up_once_the_window_has_run_out(short_reconnect_window):
    attempt = AsyncMock(side_effect=ConnectionError("still gone"))
    started = time.monotonic()
    assert await retry_within_window(attempt, "a1", _LOG) is False
    assert attempt.await_count > 1
    assert time.monotonic() - started < 1.0  # window 0.3 s


async def test_a_hanging_attempt_is_cut_off_at_the_window(short_reconnect_window):
    async def hang() -> None:
        await asyncio.Event().wait()

    started = time.monotonic()
    assert await retry_within_window(hang, "a1", _LOG) is False
    assert time.monotonic() - started < 1.0  # window 0.3 s, not "forever"
