"""Private module: the reconnect policy shared by the High-Level and WebSocket layers."""

import asyncio
from logging import Logger
from typing import Any, Awaitable, Callable

from .constants import RECONNECT_PAUSE, RECONNECT_WINDOW


async def retry_within_window(
    attempt: Callable[[], Awaitable[Any]], toy_id: str, log: Logger
) -> bool:
    """
    Run ``attempt`` until it succeeds or RECONNECT_WINDOW has passed, with RECONNECT_PAUSE between attempts.

    A single attempt would give up on a toy after one Bluetooth hiccup, while retrying forever would leave it in limbo
    (neither usable nor given up) indefinitely. An attempt still running when the window closes is cut off, so the
    window is a hard bound even though one Bluetooth connect can take up to 30 s.

    Args:
        attempt: One reconnect attempt. It succeeds by returning and fails by raising.
        toy_id: The toy being reconnected, for the log.
        log: Logger for failed attempts and for giving up.

    Returns:
        True as soon as an attempt succeeded, False once the window has run out.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RECONNECT_WINDOW
    attempts = 0
    while True:
        attempts += 1
        try:
            await asyncio.wait_for(attempt(), timeout=deadline - loop.time())
            return True
        except Exception as exc:
            log.warning(
                "Reconnect attempt %d for toy %s failed: %r",
                attempts,
                toy_id,
                exc,
                exc_info=True,
            )
        if deadline - loop.time() <= RECONNECT_PAUSE:
            # No time left for another attempt after the pause.
            log.warning(
                "Giving up on toy %s after %d reconnect attempt(s).", toy_id, attempts
            )
            return False
        await asyncio.sleep(RECONNECT_PAUSE)
