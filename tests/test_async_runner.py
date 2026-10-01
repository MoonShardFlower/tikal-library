import asyncio
import time
import unittest

from tikal._private import AsyncRunner


class TestAsyncRunner(unittest.TestCase):
    """Test suite for AsyncRunner class"""

    def setUp(self):
        """Set up a fresh AsyncRunner instance for each test"""
        self.runner = AsyncRunner()
        # Give the event loop time to initialize
        time.sleep(0.15)

    def tearDown(self):
        """Clean up the AsyncRunner after each test"""
        self.runner.shutdown()
        time.sleep(0.1)

    # Test run_async method
    def test_run_async_simple_coroutine(self):
        """Test running a simple async coroutine"""

        async def simple_coro():
            return 42

        result = self.runner.run_async(simple_coro())
        self.assertEqual(result, 42)

    def test_run_async_with_delay(self):
        """Test running a coroutine with asyncio.sleep"""

        async def delayed_coro():
            await asyncio.sleep(0.1)
            return "completed"

        result = self.runner.run_async(delayed_coro())
        self.assertEqual(result, "completed")

    def test_run_async_with_exception(self):
        """Test that exceptions are propagated correctly"""

        async def failing_coro():
            raise ValueError("Test error")

        with self.assertRaises(ValueError) as context:
            self.runner.run_async(failing_coro())
        self.assertEqual(str(context.exception), "Test error")

    def test_run_async_timeout(self):
        """Test that timeout is enforced"""

        async def long_running_coro():
            await asyncio.sleep(10)
            return "should not reach here"

        with self.assertRaises(TimeoutError):
            self.runner.run_async(long_running_coro(), timeout=0.2)

    def test_run_async_no_timeout(self):
        """Test running with no timeout (None)"""

        async def quick_coro():
            await asyncio.sleep(0.05)
            return "success"

        result = self.runner.run_async(quick_coro(), timeout=None)
        self.assertEqual(result, "success")

    # Test submit / in_loop_thread
    def test_submit_returns_at_once_and_runs_in_order(self):
        """submit() does not wait, and coroutines submitted one after the other start in that order."""
        order = []

        async def record(value):
            order.append(value)
            await asyncio.sleep(0.01)

        start = time.monotonic()
        futures = [self.runner.submit(record(i)) for i in range(20)]
        self.assertLess(time.monotonic() - start, 0.1)
        for future in futures:
            future.result(timeout=2)
        self.assertEqual(order, list(range(20)))

    def test_in_loop_thread(self):
        """Only code running on the event loop's thread counts as being in it."""
        self.assertFalse(self.runner.in_loop_thread())

        async def check():
            return self.runner.in_loop_thread()

        self.assertTrue(self.runner.run_async(check()))

    def test_run_async_on_the_loop_thread_raises_instead_of_deadlocking(self):
        """Waiting for the loop from its own thread would block it forever."""

        async def inner():
            return 1

        async def outer():
            try:
                self.runner.run_async(inner())
            except RuntimeError as e:
                return str(e)
            return "no error"

        self.assertIn("deadlock", self.runner.run_async(outer(), timeout=2))

    def test_submit_from_the_loop_thread(self):
        """submit() works from the loop's own thread too (e.g., from a callback)."""
        done = []

        async def inner():
            done.append(True)

        async def outer():
            self.runner.submit(inner())

        self.runner.run_async(outer())
        deadline = time.monotonic() + 2
        while not done and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(done, [True])

    def test_is_running(self):
        self.assertTrue(self.runner.is_running)
        self.runner.shutdown()
        time.sleep(0.2)
        self.assertFalse(self.runner.is_running)

    # Test initialization and shutdown
    def test_initialization_creates_event_loop(self):
        """Test that initialization creates a running event loop"""
        self.assertIsNotNone(self.runner.loop)
        self.assertTrue(self.runner.loop.is_running())
        self.assertIsNotNone(self.runner.loop_thread)
        self.assertTrue(self.runner.loop_thread.is_alive())

    def test_shutdown_stops_event_loop(self):
        """Test that shutdown properly stops the event loop"""
        self.runner.shutdown()
        time.sleep(0.2)

        self.assertFalse(self.runner.loop.is_running())

    def test_operations_after_shutdown_fail(self):
        """Test that operations fail after shutdown"""
        self.runner.shutdown()
        time.sleep(0.2)

        async def simple_coro():
            return 42

        # Scheduled on the stopped loop but never awaited; close it so it doesn't emit a "coroutine was never awaited" RuntimeWarning
        c = simple_coro()
        self.addCleanup(c.close)

        # The loop is stopped but still exists, so this might raise different errors
        # depending on timing. We just verify it doesn't succeed normally.
        with self.assertRaises(Exception):
            self.runner.run_async(c, timeout=0.5)

    # Test error conditions
    def test_run_async_with_none_loop_raises_error(self):
        """Test that RuntimeError is raised if the loop is None"""
        runner = AsyncRunner()
        runner.shutdown()  # we don't need its background loop/thread here
        runner.loop = None

        async def coro():
            return 1

        c = coro()
        self.addCleanup(c.close)

        with self.assertRaises(RuntimeError) as context:
            runner.run_async(c)
        self.assertIn("Event loop not initialized", str(context.exception))

    # Integration tests
    def test_multiple_sequential_runs(self):
        """Test running multiple coroutines sequentially"""

        async def coro(value):
            await asyncio.sleep(0.05)
            return value * 2

        result1 = self.runner.run_async(coro(5))
        result2 = self.runner.run_async(coro(10))
        result3 = self.runner.run_async(coro(15))

        self.assertEqual(result1, 10)
        self.assertEqual(result2, 20)
        self.assertEqual(result3, 30)


if __name__ == "__main__":
    unittest.main()
