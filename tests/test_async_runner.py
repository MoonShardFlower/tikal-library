import unittest
import asyncio
import time

from lovense.utils import AsyncRunner


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

    # Test run_async_parallel method
    def test_run_async_parallel_multiple_coroutines(self):
        """Test running multiple coroutines in parallel"""

        async def coro1():
            await asyncio.sleep(0.1)
            return 1

        async def coro2():
            await asyncio.sleep(0.1)
            return 2

        async def coro3():
            await asyncio.sleep(0.1)
            return 3

        start_time = time.time()
        results = self.runner.run_async_parallel([coro1(), coro2(), coro3()])
        elapsed = time.time() - start_time

        self.assertEqual(results, [1, 2, 3])
        # Should complete in ~0.1s (parallel) not ~0.3s (sequential)
        self.assertLess(elapsed, 0.25)

    def test_run_async_parallel_empty_list(self):
        """Test running parallel with an empty coroutine list"""
        results = self.runner.run_async_parallel([])
        self.assertEqual(results, [])

    def test_run_async_parallel_with_exceptions(self):
        """Test that exceptions are returned, not raised"""

        async def success_coro():
            return "success"

        async def failing_coro():
            raise ValueError("Error in coroutine")

        async def another_success():
            return "another success"

        results = self.runner.run_async_parallel(
            [success_coro(), failing_coro(), another_success()]
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0], "success")
        self.assertIsInstance(results[1], ValueError)
        self.assertEqual(str(results[1]), "Error in coroutine")
        self.assertEqual(results[2], "another success")

    def test_run_async_parallel_timeout(self):
        """Test timeout with parallel execution"""

        async def quick_coro():
            return "quick"

        async def slow_coro():
            await asyncio.sleep(10)
            return "slow"

        with self.assertRaises(TimeoutError):
            self.runner.run_async_parallel([quick_coro(), slow_coro()], timeout=0.2)

    def test_run_async_parallel_order_preserved(self):
        """Test that results order matches coroutines order"""

        async def coro_with_value(value, delay):
            await asyncio.sleep(delay)
            return value

        results = self.runner.run_async_parallel(
            [
                coro_with_value("first", 0.2),
                coro_with_value("second", 0.05),
                coro_with_value("third", 0.1),
            ]
        )

        self.assertEqual(results, ["first", "second", "third"])

    # Test run_callback method
    def test_run_callback_success(self):
        """Test callback is invoked with the result on success"""
        callback_result = []

        def callback(result):
            callback_result.append(result)

        async def success_coro():
            await asyncio.sleep(0.05)
            return "callback success"

        self.runner.run_callback(success_coro(), callback)
        time.sleep(0.2)  # Wait for callback to be invoked

        self.assertEqual(len(callback_result), 1)
        self.assertEqual(callback_result[0], "callback success")

    def test_run_callback_with_exception(self):
        """Test callback is invoked with an exception on failure"""
        callback_result = []

        def callback(result):
            callback_result.append(result)

        async def failing_coro():
            raise ValueError("Callback error")

        self.runner.run_callback(failing_coro(), callback)
        time.sleep(0.2)  # Wait for callback to be invoked

        self.assertEqual(len(callback_result), 1)
        self.assertIsInstance(callback_result[0], ValueError)
        self.assertEqual(str(callback_result[0]), "Callback error")

    def test_run_callback_timeout(self):
        """Test callback receives TimeoutError on timeout"""
        callback_result = []

        def callback(result):
            callback_result.append(result)

        async def slow_coro():
            await asyncio.sleep(10)
            return "should timeout"

        self.runner.run_callback(slow_coro(), callback, timeout=0.1)
        time.sleep(0.3)  # Wait for timeout and callback

        self.assertEqual(len(callback_result), 1)
        self.assertIsInstance(callback_result[0], asyncio.TimeoutError)

    def test_run_callback_non_blocking(self):
        """Test that run_callback returns immediately"""

        async def slow_coro():
            await asyncio.sleep(0.5)
            return "done"

        start_time = time.time()
        self.runner.run_callback(slow_coro(), lambda x: None)
        elapsed = time.time() - start_time

        # Should return immediately (< 0.1s), not wait for coroutine
        self.assertLess(elapsed, 0.1)

    # Test schedule_recurring method
    def test_schedule_recurring_executes_multiple_times(self):
        """Test recurring task executes multiple times"""
        execution_count = []

        async def recurring_coro():
            execution_count.append(1)

        cancel = self.runner.schedule_recurring(recurring_coro, interval=0.1)
        time.sleep(0.35)  # Should execute ~3 times
        cancel()
        time.sleep(0.15)  # Ensure cancellation takes effect

        final_count = len(execution_count)
        self.assertGreaterEqual(final_count, 2)
        self.assertLessEqual(final_count, 5)

    def test_schedule_recurring_can_be_cancelled(self):
        """Test that recurring task stops after cancellation"""
        execution_count = []

        async def recurring_coro():
            execution_count.append(1)

        cancel = self.runner.schedule_recurring(recurring_coro, interval=0.1)
        time.sleep(0.25)
        count_before_cancel = len(execution_count)
        cancel()
        time.sleep(0.3)
        count_after_cancel = len(execution_count)

        # Should not increase much after cancellation
        self.assertLessEqual(count_after_cancel - count_before_cancel, 2)

    def test_schedule_recurring_handles_exceptions(self):
        """Test recurring task continues even if coroutine raises exception"""
        execution_count = []

        async def failing_recurring_coro():
            execution_count.append(1)
            raise RuntimeError("Recurring error")

        cancel = self.runner.schedule_recurring(failing_recurring_coro, interval=0.1)
        time.sleep(0.35)
        cancel()

        # Should still execute multiple times despite exceptions
        self.assertGreaterEqual(len(execution_count), 2)

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

        # The loop is stopped but still exists, so this might raise different errors
        # depending on timing. We just verify it doesn't succeed normally.
        with self.assertRaises(Exception):
            self.runner.run_async(simple_coro(), timeout=0.5)

    # Test error conditions
    def test_run_async_with_none_loop_raises_error(self):
        """Test that RuntimeError is raised if the loop is None"""
        runner = AsyncRunner()
        runner.loop = None

        async def coro():
            return 1

        with self.assertRaises(RuntimeError) as context:
            runner.run_async(coro())
        self.assertIn("Event loop not initialized", str(context.exception))

    def test_run_async_parallel_with_none_loop_raises_error(self):
        """Test that RuntimeError is raised for parallel if loop is None"""
        runner = AsyncRunner()
        runner.loop = None

        async def coro():
            return 1

        with self.assertRaises(RuntimeError) as context:
            runner.run_async_parallel([coro()])
        self.assertIn("Event loop not initialized", str(context.exception))

    def test_run_callback_with_none_loop_raises_error(self):
        """Test that RuntimeError is raised for callback if the loop is None"""
        runner = AsyncRunner()
        runner.loop = None

        async def coro():
            return 1

        with self.assertRaises(RuntimeError) as context:
            runner.run_callback(coro(), lambda x: None)
        self.assertIn("Event loop not initialized", str(context.exception))

    def test_schedule_recurring_with_none_loop_raises_error(self):
        """Test that RuntimeError is raised for recurring if the loop is None"""
        runner = AsyncRunner()
        runner.loop = None

        async def coro():
            pass

        with self.assertRaises(RuntimeError) as context:
            runner.schedule_recurring(coro, interval=1.0)
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

    def test_mixing_different_methods(self):
        """Test using different methods in combination"""

        async def coro1():
            return "result1"

        async def coro2():
            return "result2"

        callback_results = []

        def callback(result):
            callback_results.append(result)

        # Run async
        result1 = self.runner.run_async(coro1())

        # Run callback
        self.runner.run_callback(coro2(), callback)

        # Run parallel
        results = self.runner.run_async_parallel([coro1(), coro2()])

        time.sleep(0.2)  # Wait for callback

        self.assertEqual(result1, "result1")
        self.assertEqual(results, ["result1", "result2"])
        self.assertEqual(callback_results, ["result2"])


if __name__ == "__main__":
    unittest.main()
