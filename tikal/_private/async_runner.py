"""Private module: a background event loop for synchronous callers (used by the High-Level API)."""

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")


class AsyncRunner:
    """
    Runs an asyncio event loop on a daemon thread, so synchronous code can run coroutines on it.

    The loop starts in the constructor and runs until :meth:`shutdown`.

    Attributes:
        loop: The event loop.
        loop_thread: The thread that runs it.

    Raises:
        RuntimeError: The loop did not start within 5 seconds.
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self._setup_event_loop()

    @property
    def is_running(self) -> bool:
        """Whether the event loop is running (from construction until :meth:`shutdown`)."""
        return self.loop is not None and self.loop.is_running()

    def in_loop_thread(self) -> bool:
        """Whether the caller runs on the loop's own thread, e.g., in a callback that a coroutine invoked."""
        return threading.current_thread() is self.loop_thread

    def run_async(
        self, coro: Coroutine[Any, Any, T], timeout: Optional[float] = 30.0
    ) -> T:
        """
        Run a coroutine on the loop and block until it returns.

        Args:
            coro: The coroutine to run.
            timeout: Seconds to wait for the result. None waits indefinitely.

        Raises:
            RuntimeError: Called on the loop's own thread, where waiting would deadlock the loop. *coro* is closed
                without running. Use :meth:`submit` there.
            TimeoutError: *coro* did not finish within *timeout*. It keeps running on the loop.
            Exception: Whatever *coro* raises.

        Returns:
            The result of *coro*.
        """
        if self.loop is None:
            coro.close()
            raise RuntimeError("Event loop not initialized")
        if self.in_loop_thread():
            coro.close()
            raise RuntimeError(
                "Cannot wait for the event loop on its own thread (e.g., from a callback): that would deadlock it."
            )

        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """
        Schedule a coroutine on the loop and return at once. Thread-safe.

        Coroutines submitted one after the other start in that order.

        Args:
            coro: The coroutine to run.

        Returns:
            A future for the result of *coro*. It may be ignored.
        """
        if self.loop is None:
            coro.close()
            raise RuntimeError("Event loop not initialized")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _setup_event_loop(self) -> None:
        """
        Start the loop on a daemon thread. Returns once the loop exists.

        Raises:
            RuntimeError: The loop did not start within 5 seconds.
        """
        loop_ready = threading.Event()

        def run_loop() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            loop_ready.set()
            self.loop.run_forever()

        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()
        if not loop_ready.wait(timeout=5.0):
            raise RuntimeError(
                "AsyncRunner event loop failed to start within 5 seconds"
            )

    def shutdown(self) -> None:
        """
        Gracefully shut down the event loop and background thread.
        Should be called before the AsyncRunner is destroyed to ensure clean cleanup of resources.
        Pending tasks get up to 2 seconds to finish. Tasks still running then are abandoned with the loop.
        """
        if self.loop and self.loop.is_running():

            async def _cleanup() -> None:
                tasks = [
                    t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            future = asyncio.run_coroutine_threadsafe(_cleanup(), self.loop)
            try:
                future.result(timeout=2.0)
            except Exception:
                pass

            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=2.0)

    def __del__(self) -> None:
        """Clean up the event loop when the object is destroyed."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
