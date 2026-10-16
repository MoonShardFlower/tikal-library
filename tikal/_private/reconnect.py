"""Private module: the reconnect policy of the async core."""

import asyncio
from logging import Logger
from typing import Any, Awaitable, Callable

from .constants import RECONNECT_PAUSE, RECONNECT_WINDOW


async def retry_within_window(
    attempt: Callable[[], Awaitable[Any]], toy_id: str, log: Logger
) -> bool:
    """
    Repeat *attempt* until it succeeds or :data:`RECONNECT_WINDOW` runs out, with :data:`RECONNECT_PAUSE` in between.

    - An attempt still running when the window closes is canceled: the window is a hard bound.
    - No new attempt starts once at most :data:`RECONNECT_PAUSE` is left.
    - Failed attempts and giving up are logged as warnings.

    Args:
        attempt: One reconnect attempt. Succeeds by returning, fails by raising.
        toy_id: The toy being reconnected, for the log.
        log: Logger to use.

    Returns:
        True once an attempt succeeded, False once the window ran out.
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
            # No time left for an attempt after the pause.
            log.warning(
                "Giving up on toy %s after %d reconnect attempt(s).", toy_id, attempts
            )
            return False
        await asyncio.sleep(RECONNECT_PAUSE)
