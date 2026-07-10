"""Private Module: Used by the High-Level API to run asyncio coroutines."""

import asyncio
import threading
from typing import Any, Awaitable, Callable, Coroutine, Optional, Sequence, TypeVar

T = TypeVar("T")


class AsyncRunner:

    def __init__(self) -> None:
        """
        AsyncRunner is a utility class for executing asyncio coroutines within a synchronous Application.
        Besides supporting single coroutines, it can run multiple coroutines in parallel for increased speed.
        """
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self._setup_event_loop()

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
            RuntimeError: If the event loop has not been initialized. This should never occur, since the event loop is
            initialized as part of the __init__ function of AsyncRuner
            TimeoutError: If the coroutine execution exceeds the specified timeout.
            Exception: Any exception raised by the coroutine will be propagated to the caller

        Returns:
            T: The result returned by the executed coroutine.
        """
        if self.loop is None:
            raise RuntimeError("Event loop not initialized")

        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def run_async_parallel(
        self, coroutines: Sequence[Awaitable[T]], timeout: Optional[float] = 30.0
    ) -> Sequence[T | BaseException]:
        """
        Run multiple coroutines in parallel, returning results and exceptions.
        This function blocks until all coroutines are finished or the timeout occurs!
        You can assume that the order of the results and exceptions is the same as the order of the coroutines.

        Args:
            coroutines: The asynchronous coroutines to be executed in parallel
            timeout: timeout in seconds to wait for the
            coroutines to complete. If None, there is no timeout. Defaults to 30.0

        Raises:
            RuntimeError: If the event loop has not been initialized. This should never occur, since the event loop is
            initialized as part of the __init__ function of AsyncRuner
            TimeoutError: If the execution of any coroutine exceeds the specified timeout.

        Returns:
            Sequence[T]: Each element is either the result or the exception that occurred while running the coroutine
        """
        if not coroutines:
            return []
        if self.loop is None:
            raise RuntimeError("Event loop not initialized")

        async def gather_safe() -> Sequence[T | BaseException]:
            return await asyncio.gather(*coroutines, return_exceptions=True)

        future = asyncio.run_coroutine_threadsafe(gather_safe(), self.loop)
        return future.result(timeout)

    def run_callback(
        self,
        coro: Awaitable[T],
        callback: Callable[[Any], Any],
        timeout: Optional[float] = 30.0,
    ) -> None:
        """
        Run an async coroutine and invoke a callback with result or exception (non-blocking).
        Returns immediately and executes the coroutine in the event loop thread.

        Args:
            coro: The asynchronous coroutine to be executed
            callback: Callback invoked with result or exception
            timeout: timeout in seconds. Defaults to 30.0

        Raises:
            RuntimeError: If the event loop has not been initialized
        """
        if self.loop is None:
            raise RuntimeError("Event loop not initialized")

        async def run_with_callback() -> None:
            try:
                result = await asyncio.wait_for(coro, timeout=timeout)
                callback(result)
            except Exception as e:
                callback(e)

        asyncio.run_coroutine_threadsafe(run_with_callback(), self.loop)

    def schedule_recurring(
        self, coro_factory: Callable[[], Awaitable[None]], interval: float
    ) -> Callable[[], None]:
        """
        Schedule a coroutine to run repeatedly at a specified interval.

        Args:
            coro_factory: Factory function that creates the coroutine to run
            interval: Time between executions in seconds

        Returns:
            Callable[[], None]: Function to call to cancel the recurring task

        Raises:
            RuntimeError: If the event loop has not been initialized
        """
        if self.loop is None:
            raise RuntimeError("Event loop not initialized")

        task_handle = None
        cancelled = False

        async def recurring_task() -> None:
            loop = asyncio.get_running_loop()
            while not cancelled:
                start = loop.time()
                try:
                    await coro_factory()
                except Exception:
                    pass  # Silently continue on error
                # Sleep only the time left, so the true period stays ~interval instead of (interval + work_time).
                remaining = interval - (loop.time() - start)
                await asyncio.sleep(max(0.0, remaining))

        def cancel() -> None:
            nonlocal cancelled
            cancelled = True
            if task_handle:
                task_handle.cancel()

        task_handle = asyncio.run_coroutine_threadsafe(recurring_task(), self.loop)
        return cancel

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
