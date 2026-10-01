"""Private Module: Used by the High-Level API to run asyncio coroutines."""

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")


class AsyncRunner:

    def __init__(self) -> None:
        """
        AsyncRunner is a utility class for executing asyncio coroutines within a synchronous Application.
        It runs a dedicated event loop in a background thread.
        """
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self._setup_event_loop()

    @property
    def is_running(self) -> bool:
        """Whether the event loop is running (from construction until :meth:`shutdown`)."""
        return self.loop is not None and self.loop.is_running()

    def in_loop_thread(self) -> bool:
        """Whether the caller runs on the event loop's own thread (e.g., in a callback invoked from a coroutine)."""
        return threading.current_thread() is self.loop_thread

    def run_async(
        self, coro: Coroutine[Any, Any, T], timeout: Optional[float] = 30.0
    ) -> T:
        """
        Run an async coroutine using the dedicated event loop.
        This function blocks until the coroutine is finished or the timeout occurs!

        Args:
            coro: The asynchronous coroutine to be executed.
            timeout: timeout in seconds to wait for the coroutine to complete. If None, there is no timeout. Defaults to 30.0

        Raises:
            RuntimeError: If called on the event loop's own thread, where waiting for the loop would deadlock it (use
                :meth:`submit` there). Also if the event loop has not been initialized, which should never occur.
            TimeoutError: If the coroutine execution exceeds the specified timeout.
            Exception: Any exception raised by the coroutine will be propagated to the caller

        Returns:
            T: The result returned by the executed coroutine.
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
        Schedule a coroutine on the event loop and return at once (non-blocking). Safe to call from any thread.

        Coroutines submitted one after the other start in that order.

        Args:
            coro: The asynchronous coroutine to be executed.

        Raises:
            RuntimeError: If the event loop has not been initialized. This should never occur.

        Returns:
            A future for the coroutine's result. Nothing has to wait for it.
        """
        if self.loop is None:
            coro.close()
            raise RuntimeError("Event loop not initialized")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _setup_event_loop(self) -> None:
        """
        Set up a dedicated event loop in a separate thread.

        Blocks until the background thread has created the loop. Callers may schedule work immediately after construction.
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
