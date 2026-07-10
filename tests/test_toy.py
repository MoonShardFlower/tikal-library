import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tikal.low_level import (
    LOVENSE_TOY_NAMES,
    BleTransport,
    InvalidModelError,
    Lovense,
    UnexpectedToyResponse,
)


class MockBleTransport(Mock):
    """Mock BleTransport for testing Lovense class."""

    def __init__(self, toy_id="00:11:22:33:44:55", name="LVS-A123", is_connected=True):
        super().__init__(spec=BleTransport)
        self.toy_id = toy_id
        self.name = name
        self.is_connected = is_connected
        self.send = AsyncMock()
        self.start_notify = AsyncMock()
        self.disconnect = AsyncMock()
        self.reconnect = AsyncMock()
        # For tracking the notification callback
        self.notification_callback = None


class TestLovenseInitialization(unittest.IsolatedAsyncioTestCase):
    """Tests for Lovense initialization."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()

    async def test_initialization_with_valid_model(self):
        """Lovense initializes correctly with a valid model name."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        self.assertEqual(toy.model_name, "Gush")
        self.assertEqual(toy.toy_id, "00:11:22:33:44:55")
        self.assertEqual(toy.name, "LVS-A123")
        self.transport.start_notify.assert_not_called()

    async def test_set_model_name_valid(self):
        """set_model_name updates model with valid name."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")

        with (
            patch.object(toy, "strict_intensity1", new_callable=AsyncMock) as mock_int1,
            patch.object(toy, "strict_intensity2", new_callable=AsyncMock) as mock_int2,
        ):
            mock_int1.return_value = True
            mock_int2.return_value = True
            await toy.set_model_name("Nora")
            self.assertEqual(toy.model_name, "Nora")

    async def test_set_model_name_case_insensitive(self):
        """set_model_name accepts a lower-case name and normalizes it."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")

        with (
            patch.object(toy, "strict_intensity1", new_callable=AsyncMock) as mock_int1,
            patch.object(toy, "strict_intensity2", new_callable=AsyncMock) as mock_int2,
        ):
            mock_int1.return_value = True
            mock_int2.return_value = True
            await toy.set_model_name("nora")
            self.assertEqual(toy.model_name, "Nora")

    async def test_set_model_name_rolls_back_on_bad_model(self):
        """A rejected model change raises BadModelError and keeps the previous name."""
        from tikal.low_level import BadModelError

        toy = Lovense(self.transport, "Gush", self.on_power_off, "")

        with patch.object(
            toy, "strict_intensity1", new_callable=AsyncMock
        ) as mock_int1:
            mock_int1.side_effect = ConnectionError("toy rejected command")
            with self.assertRaises(BadModelError):
                await toy.set_model_name("Nora")
        # Name must be restored to the original after a failed change.
        self.assertEqual(toy.model_name, "Gush")


class TestLovenseNotifications(unittest.IsolatedAsyncioTestCase):
    """Tests for notification handling in Lovense."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_start_notifications_success(self):
        """start_notifications successfully starts BLE notifications."""
        await self.toy.start_notifications()
        self.transport.start_notify.assert_called_once()
        # Capture the callback for later use
        self.notification_callback = self.transport.start_notify.call_args[0][0]

    async def test_start_notifications_no_transport(self):
        """start_notifications raises ConnectionError if the transport fails."""
        # Simulate transport.start_notify raising
        self.transport.start_notify.side_effect = ConnectionError("No client")
        with self.assertRaises(ConnectionError):
            await self.toy.start_notifications()

    def test_notification_callback_normal_message(self):
        """_notification_callback queues normal messages."""

        # Manually call the callback after starting notifications
        async def start():
            await self.toy.start_notifications()
            callback = self.transport.start_notify.call_args[0][0]
            callback(b"OK;")
            await asyncio.sleep(0.01)
            self.assertFalse(self.toy._response_queue.empty())
            message = self.toy._response_queue.get_nowait()
            self.assertEqual(message, "OK")

        asyncio.run(start())


class TestLovenseCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for command execution in Lovense."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_send_command_success(self):
        """_send_command successfully sends command to toy."""
        # _send_command now returns None on success (it raises on error)
        await self.toy._send_command("Vibrate:10")
        self.transport.send.assert_called_once_with(b"Vibrate:10;")

    async def test_send_command_no_duplicate_semicolon(self):
        """_send_command doesn't add semicolon if already present."""
        await self.toy._send_command("Battery;")
        self.transport.send.assert_called_once_with(b"Battery;")

    async def test_send_command_not_connected(self):
        """_send_command raises ConnectionError if not connected."""
        self.transport.is_connected = False
        with self.assertRaises(ConnectionError):
            await self.toy._send_command("Vibrate:10")

    async def test_send_command_exception(self):
        """_send_command raises ConnectionError on exception."""
        self.transport.send.side_effect = Exception("BLE error")
        with self.assertRaises(ConnectionError):
            await self.toy._send_command("Vibrate:10")

    async def test_wait_for_response_success(self):
        """_wait_for_response returns a message from the queue."""
        await self.toy._response_queue.put("OK")
        result = await self.toy._wait_for_response(timeout=1.0)
        self.assertEqual(result, "OK")

    async def test_wait_for_response_timeout(self):
        """_wait_for_response returns None on timeout."""
        result = await self.toy._wait_for_response(timeout=0.1)
        self.assertIsNone(result)

    async def test_clear_response_queue(self):
        """_clear_response_queue empties the queue."""
        await self.toy._response_queue.put("msg1")
        await self.toy._response_queue.put("msg2")
        await self.toy._response_queue.put("msg3")
        self.toy._clear_response_queue()
        self.assertTrue(self.toy._response_queue.empty())

    async def test_execute_command_success(self):
        """_execute_command successfully executes command and returns response."""
        await self.toy.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.toy._response_queue.put("OK")

        asyncio.create_task(mock_response())
        result = await self.toy._execute_command("Vibrate:10", timeout=1.0)
        self.assertEqual(result, "OK")
        self.transport.send.assert_called_once()

    async def test_execute_command_send_fails(self):
        """_execute_command returns None if send fails."""
        await self.toy.start_notifications()
        self.transport.send.side_effect = Exception("Send failed")
        result = await self.toy._execute_command("Vibrate:10")
        self.assertIsNone(result)

    async def test_execute_command_timeout(self):
        """_execute_command returns None on timeout."""
        await self.toy.start_notifications()
        result = await self.toy._execute_command("Vibrate:10", timeout=0.01)
        self.assertIsNone(result)

    async def test_execute_level_command_success(self):
        """_execute_level_command executes level command correctly."""
        await self.toy.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.toy._response_queue.put("OK")

        asyncio.create_task(mock_response())
        result = await self.toy._execute_level_command("Vibrate", 15)
        self.assertTrue(result)
        # Check the command was sent
        self.transport.send.assert_called_once_with(b"Vibrate:15;")

    async def test_execute_level_command_returns_false_on_non_ok(self):
        """_execute_level_command returns False if the response is not OK."""
        await self.toy.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.toy._response_queue.put("ERROR")

        asyncio.create_task(mock_response())
        result = await self.toy._execute_level_command("Vibrate", 10)
        self.assertFalse(result)


class TestLovenseIntensityCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for intensity control commands."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()

    async def test_intensity1_gush(self):
        """intensity1 sends the correct command for Gush."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        await toy.start_notifications()
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await toy.intensity1(15)
            mock_exec.assert_called_once_with("Vibrate", 15)
            self.assertTrue(result)

    async def test_intensity1_nora(self):
        """intensity1 sends the correct vibration command for Nora."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        await toy.start_notifications()
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await toy.intensity1(10)
            mock_exec.assert_called_once_with("Vibrate", 10)
            self.assertTrue(result)

    async def test_intensity2_nora_rotation(self):
        """intensity2 sends a rotation command for Nora."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        await toy.start_notifications()
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await toy.intensity2(12)
            mock_exec.assert_called_once_with("Rotate", 12)
            self.assertTrue(result)

    async def test_intensity2_gush_no_secondary(self):
        """intensity2 returns True for toys without a secondary capability."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        result = await toy.intensity2(10)
        self.assertTrue(result)

    async def test_intensity2_max_air_level(self):
        """intensity2 converts 0-20 to 0-5 for Max Air command."""
        toy = Lovense(self.transport, "Max", self.on_power_off, "")
        await toy.start_notifications()
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await toy.intensity2(20)
            # 20 / 4 = 5
            mock_exec.assert_called_once_with("Air:Level", 5)
            self.assertTrue(result)

    async def test_stop_command(self):
        """stop sets both intensities to zero."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(toy, "intensity1", new_callable=AsyncMock) as mock_int1:
            with patch.object(toy, "intensity2", new_callable=AsyncMock) as mock_int2:
                mock_int1.return_value = True
                mock_int2.return_value = True
                result = await toy.stop()
                mock_int1.assert_called_once_with(0)
                mock_int2.assert_called_once_with(0)
                self.assertTrue(result)

    async def test_stop_returns_false_if_either_fails(self):
        """stop returns False if either intensity command fails."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(toy, "intensity1", new_callable=AsyncMock) as mock_int1:
            with patch.object(toy, "intensity2", new_callable=AsyncMock) as mock_int2:
                mock_int1.return_value = True
                mock_int2.return_value = False
                result = await toy.stop()
                self.assertFalse(result)

    async def test_intensity_clamping(self):
        """intensity1 and intensity2 clamp level to 0-20."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        await toy.start_notifications()
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            await toy.intensity1(25)
            mock_exec.assert_called_once_with("Vibrate", 20)  # Clamped to max
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            await toy.intensity1(-5)
            mock_exec.assert_called_once_with("Vibrate", 0)  # Clamped to min


class TestLovenseQueryCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for query commands (battery, status, etc.)."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Nora", self.on_power_off, "")

    async def test_get_battery_level_success(self):
        """get_battery_level returns battery percentage."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "85"
            result = await self.toy.get_battery_level()
            self.assertEqual(result, 85)
            mock_exec.assert_called_once_with("Battery")

    async def test_get_battery_level_with_s_prefix(self):
        """get_battery_level handles 's' prefix (reconnection quirk)."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "s72"
            result = await self.toy.get_battery_level()
            self.assertEqual(result, 72)

    async def test_get_battery_level_invalid_response(self):
        """get_battery_level returns None for invalid response."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "invalid"
            result = await self.toy.get_battery_level()
            self.assertIsNone(result)

    async def test_get_battery_level_no_response(self):
        """get_battery_level returns None if no response."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = None
            result = await self.toy.get_battery_level()
            self.assertIsNone(result)

    async def test_get_device_type(self):
        """get_device_type returns a device information string."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "C:11:0082059AD3BD"
            result = await self.toy.get_device_type()
            self.assertEqual(result, "C:11:0082059AD3BD")
            mock_exec.assert_called_once_with("DeviceType")

    async def test_get_status_success(self):
        """get_status returns status code."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "2"
            result = await self.toy.get_status()
            self.assertEqual(result, 2)
            mock_exec.assert_called_once_with("Status:1")

    async def test_get_status_invalid_response(self):
        """get_status returns None for invalid response."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "invalid"
            result = await self.toy.get_status()
            self.assertIsNone(result)

    async def test_get_batch_number(self):
        """get_batch_number returns batch number string."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "240815"
            result = await self.toy.get_batch_number()
            self.assertEqual(result, "240815")
            mock_exec.assert_called_once_with("GetBatch")

    async def test_direct_command(self):
        """direct_command sends an arbitrary command."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await self.toy.direct_command("CustomCommand:5", timeout=2.0)
            self.assertEqual(result, "OK")
            mock_exec.assert_called_once_with("CustomCommand:5", 2.0)

    async def test_rotate_change_direction(self):
        """rotate_change_direction sends a rotation direction change command."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(toy, "_execute_command", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "OK"
            result = await toy.change_rotation_direction()
            self.assertTrue(result)
            mock_exec.assert_called_once_with("RotateChange")

    async def test_rotate_change_direction_not_supported(self):
        """rotate_change_direction returns True if rotation not supported."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        result = await toy.change_rotation_direction()
        self.assertTrue(result)  # Does nothing and returns True

    async def test_rotate_change_direction_failure(self):
        """rotate_change_direction returns False if the response is not OK."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(toy, "_execute_command", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "ERROR"
            result = await toy.change_rotation_direction()
            self.assertFalse(result)

    async def test_power_off_success(self):
        """power_off sends power off command."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await self.toy.power_off()
            self.assertTrue(result)
            mock_exec.assert_called_once_with("PowerOff")


class TestLovenseConnectionManagement(unittest.IsolatedAsyncioTestCase):
    """Tests for connection and disconnection management."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_is_connected_true(self):
        """is_connected returns True when transport is connected."""
        self.assertTrue(self.toy.is_connected)

    async def test_is_connected_false(self):
        """is_connected returns False when transport reports not connected."""
        self.transport.is_connected = False
        self.assertFalse(self.toy.is_connected)

    async def test_disconnect_full_cleanup(self):
        """disconnect performs full cleanup."""
        await self.toy.start_notifications()
        with patch.object(self.toy, "stop", new_callable=AsyncMock) as mock_stop:
            mock_stop.return_value = True
            await self.toy.disconnect()
            mock_stop.assert_called_once()
            self.transport.disconnect.assert_called_once()

    async def test_disconnect_handles_exceptions(self):
        """disconnect handles exceptions gracefully."""
        await self.toy.start_notifications()
        self.transport.disconnect.side_effect = Exception("BLE error")
        # Should not raise exception
        await self.toy.disconnect()


class TestLovenseCommandLocking(unittest.IsolatedAsyncioTestCase):
    """Tests for command lock to prevent response mixing."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_commands_execute_sequentially(self):
        """Commands execute sequentially due to lock."""
        await self.toy.start_notifications()
        execution_order = []

        async def mock_send_1(_):
            execution_order.append("start1")
            await asyncio.sleep(0.05)
            await self.toy._response_queue.put("OK1")
            execution_order.append("end1")

        async def mock_send_2(_):
            execution_order.append("start2")
            await asyncio.sleep(0.05)
            await self.toy._response_queue.put("OK2")
            execution_order.append("end2")

        mock_functions = iter([mock_send_1, mock_send_2])

        async def side_effect_wrapper(cmd):
            func = next(mock_functions)
            await func(cmd)

        with patch.object(self.toy, "_send_command", side_effect=side_effect_wrapper):
            task1 = asyncio.create_task(self.toy._execute_command("Command1"))
            task2 = asyncio.create_task(self.toy._execute_command("Command2"))
            await asyncio.gather(task1, task2)

        self.assertEqual(execution_order, ["start1", "end1", "start2", "end2"])


class TestLovenseEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Tests for edge cases and error handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()

    async def test_multiple_poweroff_messages(self):
        """Multiple POWEROFF messages handled gracefully."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        await toy.start_notifications()
        callback = self.transport.start_notify.call_args[0][0]
        callback(b"POWEROFF;")
        await asyncio.sleep(0.01)
        self.assertEqual(self.on_power_off.call_count, 1)
        callback(b"POWEROFF;")
        await asyncio.sleep(0.01)
        self.assertEqual(self.on_power_off.call_count, 2)

    async def test_response_with_multiple_semicolons(self):
        """Response with multiple semicolons strips only trailing ones."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        await toy.start_notifications()
        callback = self.transport.start_notify.call_args[0][0]
        callback(b"OK;;;")
        await asyncio.sleep(0.01)
        message = await toy._response_queue.get()
        self.assertEqual(message, "OK")

    async def test_all_lovense_models_valid(self):
        """All models in LOVENSE_TOY_NAMES can be instantiated."""
        for model_name in LOVENSE_TOY_NAMES.keys():
            transport = MockBleTransport(toy_id=f"00:11:22:33:44:{model_name[:2]}")
            toy = Lovense(transport, model_name, self.on_power_off, "")
            self.assertEqual(toy.model_name, model_name)

    async def test_intensity_commands_for_all_models(self):
        """Intensity commands work for all model types."""
        test_models = [
            ("Gush", "Vibrate", None),
            ("Nora", "Vibrate", "Rotate"),
            ("Max", "Vibrate", "Air:Level"),
            ("Solace", "Thrusting", "Depth"),
        ]
        for model_name, cmd1, cmd2 in test_models:
            transport = MockBleTransport()
            toy = Lovense(transport, model_name, self.on_power_off, "")
            await toy.start_notifications()
            # Test intensity1
            with patch.object(
                toy, "_execute_level_command", new_callable=AsyncMock
            ) as mock_exec:
                mock_exec.return_value = True
                await toy.intensity1(10)
                mock_exec.assert_called_with(cmd1, 10)
            # Test intensity2
            if cmd2:
                with patch.object(
                    toy, "_execute_level_command", new_callable=AsyncMock
                ) as mock_exec:
                    mock_exec.return_value = True
                    result = await toy.intensity2(10)
                    self.assertTrue(result)
            else:
                result = await toy.intensity2(10)
                self.assertTrue(result)

    async def test_clear_queue_with_empty_queue(self):
        """_clear_response_queue handles empty queue gracefully."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        toy._clear_response_queue()
        self.assertTrue(toy._response_queue.empty())

    async def test_command_execution_clears_queue_before_send(self):
        """_execute_command clears old messages before sending a new command."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        await toy.start_notifications()
        await toy._response_queue.put("old_msg_1")
        await toy._response_queue.put("old_msg_2")

        async def mock_response():
            await asyncio.sleep(0.01)
            await toy._response_queue.put("new_response")

        asyncio.create_task(mock_response())
        result = await toy._execute_command("TestCommand", timeout=1.0)
        self.assertEqual(result, "new_response")


class TestLovenseProperties(unittest.IsolatedAsyncioTestCase):
    """Tests for property accessors."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_model_name_property(self):
        """model_name property returns the correct value."""
        self.assertEqual(self.toy.model_name, "Gush")

    async def test_toy_id_property(self):
        """toy_id property returns the correct value."""
        self.assertEqual(self.toy.toy_id, "00:11:22:33:44:55")

    async def test_name_property(self):
        """name property returns the correct value."""
        self.assertEqual(self.toy.name, "LVS-A123")

    async def test_brand_property(self):
        """brand property returns 'Lovense'."""
        self.assertEqual(self.toy.brand, "Lovense")

    async def test_max_intensity(self):
        """max_intensity returns 20."""
        self.assertEqual(self.toy.max_intensity, 20)

    async def test_intensity_names(self):
        """intensity_names returns correct tuple."""
        names = self.toy.intensity_names
        self.assertEqual(names[0], "Vibration")
        self.assertIsNone(names[1])

    async def test_recommended_min_interval(self):
        """recommended_min_interval returns the per-model suggested interval."""
        self.assertEqual(self.toy.recommended_min_interval, 200)  # Gush

    async def test_change_rotation_direction_available(self):
        """change_rotation_direction_available reflects ROTATION_TOY_NAMES membership."""
        self.assertFalse(self.toy.change_rotation_direction_available)  # Gush
        nora = Lovense(MockBleTransport(), "Nora", self.on_power_off, "")
        self.assertTrue(nora.change_rotation_direction_available)


class TestLovenseCommandRetry(unittest.IsolatedAsyncioTestCase):
    """Tests for command retry and reliability."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_command_lock_prevents_race_condition(self):
        """Command lock prevents race conditions with concurrent commands."""
        await self.toy.start_notifications()
        responses_received = []
        call_count = 0

        async def mock_send_command(_):
            nonlocal call_count
            call_count += 1
            current_call = call_count
            await asyncio.sleep(0.02)
            await self.toy._response_queue.put(f"response_{current_call}")

        async def execute_and_track(cmd_num):
            result = await self.toy._execute_command(f"Command{cmd_num}")
            responses_received.append(result)

        with patch.object(self.toy, "_send_command", side_effect=mock_send_command):
            tasks = [execute_and_track(i) for i in range(3)]
            await asyncio.gather(*tasks)

        self.assertEqual(len(responses_received), 3)
        for response in responses_received:
            self.assertIsNotNone(response)
        self.assertEqual(responses_received, ["response_1", "response_2", "response_3"])


class TestLovenseSpecialCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for special commands and features."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()

    async def test_max_air_level_conversion_boundary_cases(self):
        """Max Air:Level command converts levels correctly at boundaries."""
        toy = Lovense(self.transport, "Max", self.on_power_off, "")
        await toy.start_notifications()
        test_cases = [
            (0, 0),
            (4, 1),
            (8, 2),
            (12, 3),
            (16, 4),
            (20, 5),
        ]
        for input_level, expected_level in test_cases:
            with patch.object(
                toy, "_execute_level_command", new_callable=AsyncMock
            ) as mock_exec:
                mock_exec.return_value = True
                await toy.intensity2(input_level)
                mock_exec.assert_called_once_with("Air:Level", expected_level)

    async def test_solace_thrust_and_depth(self):
        """Solace uses Thrusting and Depth commands correctly."""
        toy = Lovense(self.transport, "Solace", self.on_power_off, "")
        await toy.start_notifications()
        # Test intensity1 (Thrust)
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            await toy.intensity1(15)
            mock_exec.assert_called_once_with("Thrusting", 15)
        # Test intensity2 (Depth)
        with patch.object(
            toy, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            await toy.intensity2(10)
            mock_exec.assert_called_once_with("Depth", 10)

    async def test_direct_command_custom_timeout(self):
        """direct_command respects custom timeout."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(toy, "_execute_command", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = "CustomResponse"
            await toy.direct_command("CustomCmd", timeout=5.0)
            mock_exec.assert_called_once_with("CustomCmd", 5.0)


class TestLovenseStrictIntensity(unittest.IsolatedAsyncioTestCase):
    """Tests for the exception-raising strict_* intensity commands."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()

    async def test_strict_intensity1_success(self):
        """strict_intensity1 sends the level command and records the new intensity."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await toy.strict_intensity1(15)
            self.assertTrue(result)
            mock_exec.assert_called_once_with("Vibrate:15")
            self.assertEqual(toy.current_intensities[0], 15)

    async def test_strict_intensity1_clamps_level(self):
        """strict_intensity1 clamps the level into the 0-20 range."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            await toy.strict_intensity1(999)
            mock_exec.assert_called_once_with("Vibrate:20")

    async def test_strict_intensity1_raises_on_bad_response(self):
        """strict_intensity1 raises UnexpectedToyResponse when the toy does not reply OK."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "ERROR"
            with self.assertRaises(UnexpectedToyResponse):
                await toy.strict_intensity1(10)

    async def test_strict_intensity2_secondary(self):
        """strict_intensity2 sends the secondary command for a two-capability toy."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await toy.strict_intensity2(12)
            self.assertTrue(result)
            mock_exec.assert_called_once_with("Rotate:12")

    async def test_strict_intensity2_air_level_scaling(self):
        """strict_intensity2 scales 0-20 down to 0-5 for Max's Air:Level command."""
        toy = Lovense(self.transport, "Max", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            await toy.strict_intensity2(20)
            mock_exec.assert_called_once_with("Air:Level:5")

    async def test_strict_intensity2_no_secondary_returns_false(self):
        """strict_intensity2 is a no-op (returns False) on a single-capability toy."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            result = await toy.strict_intensity2(10)
            self.assertFalse(result)
            mock_exec.assert_not_called()

    async def test_strict_stop_zeroes_both_capabilities(self):
        """strict_stop sends a zero to both capabilities of a two-capability toy."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await toy.strict_stop()
            self.assertTrue(result)
            self.assertEqual(mock_exec.call_count, 2)
            mock_exec.assert_any_call("Vibrate:0")
            mock_exec.assert_any_call("Rotate:0")


class TestLovenseStrictQueries(unittest.IsolatedAsyncioTestCase):
    """Tests for the strict_* query commands (battery, status, batch, etc.)."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Nora", self.on_power_off, "")

    async def test_strict_get_battery_level(self):
        """strict_get_battery_level parses the percentage."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "85"
            self.assertEqual(await self.toy.strict_get_battery_level(), 85)
            mock_exec.assert_called_once_with("Battery")

    async def test_strict_get_battery_level_s_prefix(self):
        """strict_get_battery_level strips the 's' reconnection-quirk prefix."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "s72"
            self.assertEqual(await self.toy.strict_get_battery_level(), 72)

    async def test_strict_get_battery_level_raises_on_garbage(self):
        """strict_get_battery_level raises UnexpectedToyResponse on a non-numeric reply."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "??"
            with self.assertRaises(UnexpectedToyResponse):
                await self.toy.strict_get_battery_level()

    async def test_strict_get_status(self):
        """strict_get_status parses the status code."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "2"
            self.assertEqual(await self.toy.strict_get_status(), 2)
            mock_exec.assert_called_once_with("Status:1")

    async def test_strict_get_status_raises_on_garbage(self):
        """strict_get_status raises UnexpectedToyResponse on a non-numeric reply."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "nope"
            with self.assertRaises(UnexpectedToyResponse):
                await self.toy.strict_get_status()

    async def test_strict_get_batch_number(self):
        """strict_get_batch_number returns the batch string."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "240815"
            self.assertEqual(await self.toy.strict_get_batch_number(), "240815")
            mock_exec.assert_called_once_with("GetBatch")

    async def test_strict_get_batch_number_raises_on_non_digit(self):
        """strict_get_batch_number raises UnexpectedToyResponse on a non-digit reply."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "24-08"
            with self.assertRaises(UnexpectedToyResponse):
                await self.toy.strict_get_batch_number()

    async def test_strict_get_device_type_passthrough(self):
        """strict_get_device_type returns the raw device-type string unverified."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "C:11:0082059AD3BD"
            result = await self.toy.strict_get_device_type()
            self.assertEqual(result, "C:11:0082059AD3BD")
            mock_exec.assert_called_once_with("DeviceType")

    async def test_strict_direct_command_forwards_timeout(self):
        """strict_direct_command forwards the command and timeout and returns the reply."""
        with patch.object(
            self.toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "PONG"
            result = await self.toy.strict_direct_command("PING", timeout=2.0)
            self.assertEqual(result, "PONG")
            mock_exec.assert_called_once_with("PING", 2.0)


class TestLovenseStrictSpecialCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for strict rotation-direction and power-off commands."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()

    async def test_strict_change_rotation_direction_success(self):
        """strict_change_rotation_direction returns True on OK for a rotation toy."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            self.assertTrue(await toy.strict_change_rotation_direction())
            mock_exec.assert_called_once_with("RotateChange")

    async def test_strict_change_rotation_direction_unsupported(self):
        """strict_change_rotation_direction returns False and sends nothing when unsupported."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            self.assertFalse(await toy.strict_change_rotation_direction())
            mock_exec.assert_not_called()

    async def test_strict_change_rotation_direction_raises_on_bad_response(self):
        """strict_change_rotation_direction raises UnexpectedToyResponse on a non-OK reply."""
        toy = Lovense(self.transport, "Nora", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "ERROR"
            with self.assertRaises(UnexpectedToyResponse):
                await toy.strict_change_rotation_direction()

    async def test_strict_power_off_success(self):
        """strict_power_off returns True on OK."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            self.assertTrue(await toy.strict_power_off())
            mock_exec.assert_called_once_with("PowerOff")

    async def test_strict_power_off_raises_on_bad_response(self):
        """strict_power_off raises UnexpectedToyResponse on a non-OK reply."""
        toy = Lovense(self.transport, "Gush", self.on_power_off, "")
        with patch.object(
            toy, "_strict_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "ERROR"
            with self.assertRaises(UnexpectedToyResponse):
                await toy.strict_power_off()


class TestLovenseReconnectAndDisconnect(unittest.IsolatedAsyncioTestCase):
    """Tests for reconnect / strict_reconnect / strict_disconnect."""

    # The reconnect paths sleep before retrying; patch it out so tests stay fast.
    _SLEEP = "tikal.low_level.brands.lovense.toy.asyncio.sleep"

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_reconnect_already_connected(self):
        """reconnect returns True and does nothing when already connected."""
        self.transport.is_connected = True
        with patch(self._SLEEP, new_callable=AsyncMock):
            self.assertTrue(await self.toy.reconnect())
        self.transport.reconnect.assert_not_called()

    async def test_reconnect_success(self):
        """reconnect returns True after the transport reconnects."""
        self.transport.is_connected = False
        with patch(self._SLEEP, new_callable=AsyncMock):
            self.assertTrue(await self.toy.reconnect())
        self.transport.reconnect.assert_called_once()

    async def test_reconnect_failure_returns_false(self):
        """reconnect swallows the error and returns False when the transport fails."""
        self.transport.is_connected = False
        self.transport.reconnect.side_effect = ConnectionError("still gone")
        with patch(self._SLEEP, new_callable=AsyncMock):
            self.assertFalse(await self.toy.reconnect())

    async def test_strict_reconnect_already_connected(self):
        """strict_reconnect returns True without reconnecting when already connected."""
        self.transport.is_connected = True
        with patch(self._SLEEP, new_callable=AsyncMock):
            self.assertTrue(await self.toy.strict_reconnect())
        self.transport.reconnect.assert_not_called()

    async def test_strict_reconnect_success(self):
        """strict_reconnect returns True after the transport reconnects."""
        self.transport.is_connected = False
        with patch(self._SLEEP, new_callable=AsyncMock):
            self.assertTrue(await self.toy.strict_reconnect())
        self.transport.reconnect.assert_called_once()

    async def test_strict_reconnect_raises_on_failure(self):
        """strict_reconnect re-raises the error when the transport fails."""
        self.transport.is_connected = False
        self.transport.reconnect.side_effect = ConnectionError("still gone")
        with patch(self._SLEEP, new_callable=AsyncMock):
            with self.assertRaises(ConnectionError):
                await self.toy.strict_reconnect()

    async def test_strict_disconnect_success(self):
        """strict_disconnect stops the toy and closes the transport without raising."""
        with patch.object(self.toy, "strict_stop", new_callable=AsyncMock):
            await self.toy.strict_disconnect()
        self.transport.disconnect.assert_called_once()

    async def test_strict_disconnect_still_disconnects_when_stop_fails(self):
        """strict_disconnect closes the transport (then raises) even if stop fails."""
        with patch.object(self.toy, "strict_stop", new_callable=AsyncMock) as mock_stop:
            mock_stop.side_effect = UnexpectedToyResponse("bad reply")
            with self.assertRaises(ConnectionError):
                await self.toy.strict_disconnect()
        self.transport.disconnect.assert_called_once()

    async def test_strict_disconnect_raises_when_transport_fails(self):
        """strict_disconnect raises ConnectionError when the transport disconnect fails."""
        self.transport.disconnect.side_effect = Exception("BLE error")
        with patch.object(self.toy, "strict_stop", new_callable=AsyncMock):
            with self.assertRaises(ConnectionError):
                await self.toy.strict_disconnect()


class TestLovenseNotificationAndModelEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Notification-callback branches and model-name validation."""

    def setUp(self):
        """Set up test fixtures."""
        self.transport = MockBleTransport()
        self.on_power_off = Mock()
        self.toy = Lovense(self.transport, "Gush", self.on_power_off, "")

    async def test_notification_before_start_queues_directly(self):
        """Before notifications start (no captured loop) messages queue synchronously."""
        # start_notifications() has not run, so self._loop is None -> the direct-queue branch.
        self.toy._notification_callback(b"OK;")
        self.assertEqual(self.toy._response_queue.get_nowait(), "OK")

    async def test_notification_poweroff_before_start(self):
        """A POWEROFF received before notifications start still fires the callback."""
        self.toy._notification_callback(b"POWEROFF;")
        self.on_power_off.assert_called_once_with(self.transport.toy_id)

    async def test_notification_swallows_decode_error(self):
        """Invalid UTF-8 is logged and swallowed (no raise) and fires no callback."""
        self.toy._notification_callback(b"\xff\xfe")  # not valid UTF-8
        self.on_power_off.assert_not_called()

    async def test_set_model_name_invalid_raises(self):
        """set_model_name rejects an unknown model and leaves the current name intact."""
        with self.assertRaises(InvalidModelError):
            await self.toy.set_model_name("NotARealModel")
        self.assertEqual(self.toy.model_name, "Gush")

    async def test_get_status_no_response_returns_none(self):
        """get_status returns None when the toy gives no response."""
        with patch.object(
            self.toy, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = None
            self.assertIsNone(await self.toy.get_status())

    async def test_disconnect_when_stop_fails_still_disconnects(self):
        """disconnect logs a failing stop() and still closes the transport."""
        await self.toy.start_notifications()
        with patch.object(self.toy, "stop", new_callable=AsyncMock) as mock_stop:
            mock_stop.side_effect = Exception("stop failed")
            await self.toy.disconnect()  # must not raise
        self.transport.disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
