import unittest
from unittest.mock import AsyncMock, Mock, patch
import asyncio

from tikal import LovenseBLED, ValidationError
from tikal import LOVENSE_TOY_NAMES


class TestLovenseBLEDInitialization(unittest.TestCase):
    """Tests for LovenseBLED initialization."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.on_power_off = Mock()

    def test_initialization_with_valid_model(self):
        """LovenseBLED initializes correctly with a valid model name."""
        bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

        self.assertEqual(bled.model_name, "Gush")
        self.assertEqual(bled.address, "00:11:22:33:44:55")
        self.assertEqual(bled.name, "LVS-A123")
        self.assertFalse(bled._notifications_started)

    def test_initialization_with_invalid_model(self):
        """LovenseBLED raises ValueError for invalid model name."""
        with self.assertRaises(ValidationError) as context:
            LovenseBLED(
                self.mock_client,
                "tx_uuid",
                "rx_uuid",
                "InvalidModel",
                self.on_power_off,
                "",
            )

        self.assertIn("invalid model_name 'InvalidModel'", str(context.exception))
        self.assertIn("00:11:22:33:44:55", str(context.exception))

    def test_set_model_name_valid(self):
        """set_model_name updates model with valid name."""
        bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

        bled.set_model_name("Nora")
        self.assertEqual(bled.model_name, "Nora")

    def test_set_model_name_invalid(self):
        """set_model_name raises ValueError for an invalid name."""
        bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

        with self.assertRaises(ValidationError) as context:
            bled.set_model_name("InvalidModel")

        self.assertIn("invalid model_name 'InvalidModel'", str(context.exception))


class TestLovenseBLEDNotifications(unittest.IsolatedAsyncioTestCase):
    """Tests for notification handling in LovenseBLED."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

    async def test_start_notifications_success(self):
        """start_notifications successfully starts BLE notifications."""
        await self.bled.start_notifications()
        self.assertTrue(self.bled._notifications_started)
        self.mock_client.start_notify.assert_called_once_with(
            "rx_uuid", self.bled._notification_callback
        )

    async def test_start_notifications_already_started(self):
        """start_notifications does nothing if already started."""
        await self.bled.start_notifications()
        self.mock_client.start_notify.reset_mock()
        await self.bled.start_notifications()
        self.mock_client.start_notify.assert_not_called()

    async def test_start_notifications_no_client(self):
        """start_notifications raises RuntimeError if a client is None."""
        self.bled._client = None
        with self.assertRaises(RuntimeError) as context:
            await self.bled.start_notifications()
        self.assertIn("client is None", str(context.exception))

    def test_notification_callback_normal_message(self):
        """_notification_callback queues normal messages."""
        data = b"OK;"
        self.bled._notification_callback(0, data)
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.01))
        self.assertFalse(self.bled._response_queue.empty())
        message = self.bled._response_queue.get_nowait()
        self.assertEqual(message, "OK")


class TestLovenseBLEDCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for command execution in LovenseBLED."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

    async def test_send_command_success(self):
        """_send_command successfully sends command to toy."""
        result = await self.bled._send_command("Vibrate:10")
        self.assertTrue(result)
        self.mock_client.write_gatt_char.assert_called_once_with(
            "tx_uuid", b"Vibrate:10;", response=False
        )

    async def test_send_command_no_duplicate_semicolon(self):
        """_send_command doesn't add semicolon if already present."""
        await self.bled._send_command("Battery;")
        self.mock_client.write_gatt_char.assert_called_once_with(
            "tx_uuid", b"Battery;", response=False
        )

    async def test_send_command_no_client(self):
        """_send_command returns False if a client is None."""
        self.bled._client = None
        result = await self.bled._send_command("Vibrate:10")
        self.assertFalse(result)

    async def test_send_command_not_connected(self):
        """_send_command returns False if not connected."""
        self.mock_client.is_connected = False
        result = await self.bled._send_command("Vibrate:10")
        self.assertFalse(result)

    async def test_send_command_exception(self):
        """_send_command returns False on exception."""
        self.mock_client.write_gatt_char.side_effect = Exception("BLE error")
        result = await self.bled._send_command("Vibrate:10")
        self.assertFalse(result)

    async def test_wait_for_response_success(self):
        """_wait_for_response returns a message from the queue."""
        # Put message in queue
        await self.bled._response_queue.put("OK")
        result = await self.bled._wait_for_response(timeout=1.0)
        self.assertEqual(result, "OK")

    async def test_wait_for_response_timeout(self):
        """_wait_for_response returns None on timeout."""
        result = await self.bled._wait_for_response(timeout=0.1)
        self.assertIsNone(result)

    async def test_clear_response_queue(self):
        """_clear_response_queue empties the queue."""
        await self.bled._response_queue.put("msg1")
        await self.bled._response_queue.put("msg2")
        await self.bled._response_queue.put("msg3")
        self.bled._clear_response_queue()
        self.assertTrue(self.bled._response_queue.empty())

    async def test_execute_command_success(self):
        """_execute_command successfully executes command and returns response."""
        await self.bled.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.bled._response_queue.put("OK")

        asyncio.create_task(mock_response())
        result = await self.bled._execute_command("Vibrate:10", timeout=1.0)
        self.assertEqual(result, "OK")
        self.mock_client.write_gatt_char.assert_called_once()

    async def test_execute_command_notifications_not_started(self):
        """_execute_command returns None if notifications not started."""
        result = await self.bled._execute_command("Vibrate:10")
        self.assertIsNone(result)

    async def test_execute_command_send_fails(self):
        """_execute_command returns None if send fails."""
        await self.bled.start_notifications()
        self.mock_client.write_gatt_char.side_effect = Exception("Send failed")
        result = await self.bled._execute_command("Vibrate:10")
        self.assertIsNone(result)

    async def test_execute_command_timeout(self):
        """_execute_command returns None on timeout."""
        await self.bled.start_notifications()
        result = await self.bled._execute_command("Vibrate:10", timeout=0.01)
        self.assertIsNone(result)

    async def test_execute_level_command_success(self):
        """_execute_level_command executes level command correctly."""
        await self.bled.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.bled._response_queue.put("OK")

        asyncio.create_task(mock_response())
        result = await self.bled._execute_level_command("Vibrate", 15)
        self.assertTrue(result)
        # Check the command was sent
        call_args = self.mock_client.write_gatt_char.call_args
        self.assertIn(b"Vibrate:15;", call_args[0])

    async def test_execute_level_command_clamps_max(self):
        """_execute_level_command clamps level to max."""
        await self.bled.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.bled._response_queue.put("OK")

        asyncio.create_task(mock_response())
        _ = await self.bled._execute_level_command("Vibrate", 25, max_level=20)
        # Should clamp to 20
        call_args = self.mock_client.write_gatt_char.call_args
        self.assertIn(b"Vibrate:20;", call_args[0])

    async def test_execute_level_command_clamps_min(self):
        """_execute_level_command clamps the level to minimum (0)."""
        await self.bled.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.bled._response_queue.put("OK")

        asyncio.create_task(mock_response())
        _ = await self.bled._execute_level_command("Vibrate", -5)
        # Should clamp to 0
        call_args = self.mock_client.write_gatt_char.call_args
        self.assertIn(b"Vibrate:0;", call_args[0])

    async def test_execute_level_command_returns_false_on_non_ok(self):
        """_execute_level_command returns False if the response is not OK."""
        await self.bled.start_notifications()

        async def mock_response():
            await asyncio.sleep(0.01)
            await self.bled._response_queue.put("ERROR")

        asyncio.create_task(mock_response())
        result = await self.bled._execute_level_command("Vibrate", 10)
        self.assertFalse(result)


class TestLovenseBLEDIntensityCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for intensity control commands."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

    async def test_intensity1_gush(self):
        """intensity1 sends the correct command for Gush."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        await bled.start_notifications()
        with patch.object(
            bled, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await bled.intensity1(15)
            mock_exec.assert_called_once_with("Vibrate", 15)
            self.assertTrue(result)

    async def test_intensity1_nora(self):
        """intensity1 sends the correct vibration command for Nora."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Nora", self.on_power_off, "")
        await bled.start_notifications()
        with patch.object(
            bled, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await bled.intensity1(10)
            mock_exec.assert_called_once_with("Vibrate", 10)
            self.assertTrue(result)

    async def test_intensity2_nora_rotation(self):
        """intensity2 sends a rotation command for Nora."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Nora", self.on_power_off, "")
        await bled.start_notifications()
        with patch.object(
            bled, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await bled.intensity2(12)
            mock_exec.assert_called_once_with("Rotate", 12)
            self.assertTrue(result)

    async def test_intensity2_gush_no_secondary(self):
        """intensity2 returns True for toys without a secondary capability."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        result = await bled.intensity2(10)
        self.assertTrue(result)

    async def test_intensity2_max_air_level(self):
        """intensity2 converts 0-20 to 0-5 for Max Air command."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Max", self.on_power_off, "")
        await bled.start_notifications()
        with patch.object(
            bled, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            result = await bled.intensity2(20)
            # 20 / 4 = 5
            mock_exec.assert_called_once_with("Air:Level", 5)
            self.assertTrue(result)

    async def test_stop_command(self):
        """stop sets both intensities to zero."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Nora", self.on_power_off, "")
        with patch.object(bled, "intensity1", new_callable=AsyncMock) as mock_int1:
            with patch.object(bled, "intensity2", new_callable=AsyncMock) as mock_int2:
                mock_int1.return_value = True
                mock_int2.return_value = True
                result = await bled.stop()
                mock_int1.assert_called_once_with(0)
                mock_int2.assert_called_once_with(0)
                self.assertTrue(result)

    async def test_stop_returns_false_if_either_fails(self):
        """stop returns False if either intensity command fails."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Nora", self.on_power_off, "")
        with patch.object(bled, "intensity1", new_callable=AsyncMock) as mock_int1:
            with patch.object(bled, "intensity2", new_callable=AsyncMock) as mock_int2:
                mock_int1.return_value = True
                mock_int2.return_value = False
                result = await bled.stop()
                self.assertFalse(result)


class TestLovenseBLEDQueryCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for query commands (battery, status, etc.)."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Nora", self.on_power_off, ""
        )

    async def test_get_battery_level_success(self):
        """get_battery_level returns battery percentage."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "85"
            result = await self.bled.get_battery_level()
            self.assertEqual(result, 85)
            mock_exec.assert_called_once_with("Battery")

    async def test_get_battery_level_with_s_prefix(self):
        """get_battery_level handles 's' prefix (reconnection quirk)."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "s72"
            result = await self.bled.get_battery_level()
            self.assertEqual(result, 72)

    async def test_get_battery_level_invalid_response(self):
        """get_battery_level returns None for invalid response."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "invalid"
            result = await self.bled.get_battery_level()
            self.assertIsNone(result)

    async def test_get_battery_level_no_response(self):
        """get_battery_level returns None if no response."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = None
            result = await self.bled.get_battery_level()
            self.assertIsNone(result)

    async def test_get_device_type(self):
        """get_device_type returns a device information string."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "C:11:0082059AD3BD"
            result = await self.bled.get_device_type()
            self.assertEqual(result, "C:11:0082059AD3BD")
            mock_exec.assert_called_once_with("DeviceType")

    async def test_get_status_success(self):
        """get_status returns status code."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "2"
            result = await self.bled.get_status()
            self.assertEqual(result, 2)
            mock_exec.assert_called_once_with("Status:1")

    async def test_get_status_invalid_response(self):
        """get_status returns None for invalid response."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "invalid"
            result = await self.bled.get_status()
            self.assertIsNone(result)

    async def test_get_batch_number(self):
        """get_batch_number returns batch number string."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "240815"
            result = await self.bled.get_batch_number()
            self.assertEqual(result, "240815")
            mock_exec.assert_called_once_with("GetBatch")

    async def test_direct_command(self):
        """direct_command sends an arbitrary command."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await self.bled.direct_command("CustomCommand:5", timeout=2.0)
            self.assertEqual(result, "OK")
            mock_exec.assert_called_once_with("CustomCommand:5", 2.0)

    async def test_rotate_change_direction(self):
        """rotate_change_direction sends a rotation direction change command."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"

            result = await self.bled.rotate_change_direction()

            self.assertTrue(result)
            mock_exec.assert_called_once_with("RotateChange")

    async def test_rotate_change_direction_failure(self):
        """rotate_change_direction returns False if the response is not OK."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "ERROR"
            result = await self.bled.rotate_change_direction()
            self.assertFalse(result)

    async def test_power_off_success(self):
        """power_off sends power off command."""
        with patch.object(
            self.bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "OK"
            result = await self.bled.power_off()
            self.assertTrue(result)
            mock_exec.assert_called_once_with("PowerOff")


class TestLovenseBLEDConnectionManagement(unittest.IsolatedAsyncioTestCase):
    """Tests for connection and disconnection management."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.disconnect = AsyncMock()
        self.mock_client.stop_notify = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

    async def test_is_connected_true(self):
        """is_connected returns True when a client is connected."""
        self.assertTrue(self.bled.is_connected)

    async def test_is_connected_false_not_connected(self):
        """is_connected returns False when a client reports not connected."""
        self.mock_client.is_connected = False
        self.assertFalse(self.bled.is_connected)

    async def test_is_connected_false_no_client(self):
        """is_connected returns False when a client is None."""
        self.bled._client = None
        self.assertFalse(self.bled.is_connected)

    async def test_disconnect_full_cleanup(self):
        """disconnect performs full cleanup."""
        await self.bled.start_notifications()

        with patch.object(self.bled, "stop", new_callable=AsyncMock) as mock_stop:
            mock_stop.return_value = True
            await self.bled.disconnect()
            mock_stop.assert_called_once()
            self.mock_client.stop_notify.assert_called_once_with("rx_uuid")
            self.mock_client.disconnect.assert_called_once()
            self.assertFalse(self.bled._notifications_started)

    async def test_disconnect_no_notifications(self):
        """disconnect skips stop_notify if notifications not started."""
        with patch.object(self.bled, "stop", new_callable=AsyncMock) as mock_stop:
            mock_stop.return_value = True
            await self.bled.disconnect()
            self.mock_client.stop_notify.assert_not_called()
            self.mock_client.disconnect.assert_called_once()

    async def test_disconnect_handles_exceptions(self):
        """disconnect handles exceptions gracefully."""
        await self.bled.start_notifications()
        self.mock_client.stop_notify.side_effect = Exception("BLE error")
        # Should not raise exception
        await self.bled.disconnect()


class TestLovenseBLEDCommandLocking(unittest.IsolatedAsyncioTestCase):
    """Tests for command lock to prevent response mixing."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

    async def test_commands_execute_sequentially(self):
        """Commands execute sequentially due to lock."""
        await self.bled.start_notifications()
        execution_order = []

        async def mock_send_1(_):
            execution_order.append("start1")
            await asyncio.sleep(0.05)
            await self.bled._response_queue.put("OK1")
            execution_order.append("end1")
            return True

        async def mock_send_2(_):
            execution_order.append("start2")
            await asyncio.sleep(0.05)
            await self.bled._response_queue.put("OK2")
            execution_order.append("end2")
            return True

        # Create an iterator that returns each mock function
        mock_functions = iter([mock_send_1, mock_send_2])

        async def side_effect_wrapper(cmd):
            func = next(mock_functions)
            return await func(cmd)

        # Patch _send_command to track execution order
        with patch.object(self.bled, "_send_command", side_effect=side_effect_wrapper):
            # Execute two commands concurrently
            task1 = asyncio.create_task(self.bled._execute_command("Command1"))
            task2 = asyncio.create_task(self.bled._execute_command("Command2"))

            await asyncio.gather(task1, task2)

        # Due to the lock, command2 should not start until command1 finishes
        self.assertEqual(execution_order, ["start1", "end1", "start2", "end2"])


class TestLovenseBLEDEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Tests for edge cases and error handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

    async def test_multiple_poweroff_messages(self):
        """Multiple POWEROFF messages handled gracefully."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        # Send first POWEROFF
        bled._notification_callback(0, b"POWEROFF;")
        await asyncio.sleep(0.01)
        # First POWEROFF should trigger callback
        self.assertEqual(self.on_power_off.call_count, 1)
        # Send second POWEROFF (a client is already None, should not crash)
        bled._notification_callback(0, b"POWEROFF;")
        await asyncio.sleep(0.01)
        self.assertEqual(self.on_power_off.call_count, 2)

    async def test_response_with_multiple_semicolons(self):
        """Response with multiple semicolons strips only trailing ones."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        bled._notification_callback(0, b"OK;;;")
        await asyncio.sleep(0.01)
        message = await bled._response_queue.get()
        self.assertEqual(message, "OK")

    async def test_all_lovense_models_valid(self):
        """All models in LOVENSE_TOY_NAMES can be instantiated."""
        for model_name in LOVENSE_TOY_NAMES.keys():
            mock_client = Mock()
            mock_client.address = f"00:11:22:33:44:{model_name[:2]}"
            mock_client.name = f"LVS-{model_name}"
            # Should not raise
            bled = LovenseBLED(
                mock_client, "tx", "rx", model_name, self.on_power_off, ""
            )
            self.assertEqual(bled.model_name, model_name)

    async def test_intensity_commands_for_all_models(self):
        """Intensity commands work for all model types."""
        test_models = [
            ("Gush", "Vibrate", None),
            ("Nora", "Vibrate", "Rotate"),
            ("Max", "Vibrate", "Air:Level"),
            ("Solace", "Thrusting", "Depth"),
        ]

        for model_name, cmd1, cmd2 in test_models:
            mock_client = Mock()
            mock_client.address = "00:11:22:33:44:55"
            mock_client.name = "LVS-TEST"
            mock_client.is_connected = True
            mock_client.write_gatt_char = AsyncMock()
            mock_client.start_notify = AsyncMock()
            bled = LovenseBLED(
                mock_client, "tx", "rx", model_name, self.on_power_off, ""
            )
            await bled.start_notifications()
            # Test intensity1
            with patch.object(
                bled, "_execute_level_command", new_callable=AsyncMock
            ) as mock_exec:
                mock_exec.return_value = True
                await bled.intensity1(10)
                mock_exec.assert_called_with(cmd1, 10)
            # Test intensity2
            if cmd2:
                with patch.object(
                    bled, "_execute_level_command", new_callable=AsyncMock
                ) as mock_exec:
                    mock_exec.return_value = True
                    result = await bled.intensity2(10)
                    self.assertTrue(result)
            else:
                # Models without a secondary capability should return True
                result = await bled.intensity2(10)
                self.assertTrue(result)

    async def test_clear_queue_with_empty_queue(self):
        """_clear_response_queue handles empty queue gracefully."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        # Should not raise exception
        bled._clear_response_queue()
        self.assertTrue(bled._response_queue.empty())

    async def test_command_execution_clears_queue_before_send(self):
        """_execute_command clears old messages before sending a new command."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        await bled.start_notifications()
        # Add old messages to the queue
        await bled._response_queue.put("old_msg_1")
        await bled._response_queue.put("old_msg_2")

        async def mock_response():
            await asyncio.sleep(0.01)
            await bled._response_queue.put("new_response")

        asyncio.create_task(mock_response())
        result = await bled._execute_command("TestCommand", timeout=1.0)
        # Should get new response, not old messages
        self.assertEqual(result, "new_response")


class TestLovenseBLEDProperties(unittest.TestCase):
    """Tests for property accessors."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

    def test_model_name_property(self):
        """model_name property returns the correct value."""
        self.assertEqual(self.bled.model_name, "Gush")

    def test_address_property(self):
        """address property returns the correct value."""
        self.assertEqual(self.bled.address, "00:11:22:33:44:55")

    def test_name_property(self):
        """name property returns the correct value."""
        self.assertEqual(self.bled.name, "LVS-A123")

    def test_name_property_none(self):
        """name property can be None."""
        self.mock_client.name = None
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        self.assertIsNone(bled.name)


class TestLovenseBLEDCommandRetry(unittest.IsolatedAsyncioTestCase):
    """Tests for command retry and reliability."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

        self.bled = LovenseBLED(
            self.mock_client, "tx_uuid", "rx_uuid", "Gush", self.on_power_off, ""
        )

    async def test_command_lock_prevents_race_condition(self):
        """Command lock prevents race conditions with concurrent commands."""
        await self.bled.start_notifications()
        responses_received = []
        call_count = 0

        async def mock_send_command(_):
            nonlocal call_count
            call_count += 1
            current_call = call_count
            # Simulate sending and getting a response
            await asyncio.sleep(0.02)
            await self.bled._response_queue.put(f"response_{current_call}")
            return True

        async def execute_and_track(cmd_num):
            # Execute command and track response
            result = await self.bled._execute_command(f"Command{cmd_num}")
            responses_received.append(result)

        # Patch _send_command
        with patch.object(self.bled, "_send_command", side_effect=mock_send_command):
            # Execute multiple commands concurrently
            tasks = [execute_and_track(i) for i in range(3)]
            await asyncio.gather(*tasks)

        # Each command should get a response (order guaranteed by lock)
        self.assertEqual(len(responses_received), 3)
        # All responses should be valid (not None)
        for response in responses_received:
            self.assertIsNotNone(response)
        # Responses should be in sequential order due to lock
        self.assertEqual(responses_received, ["response_1", "response_2", "response_3"])


class TestLovenseBLEDSpecialCommands(unittest.IsolatedAsyncioTestCase):
    """Tests for special commands and features."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_client = Mock()
        self.mock_client.address = "00:11:22:33:44:55"
        self.mock_client.name = "LVS-A123"
        self.mock_client.is_connected = True
        self.mock_client.write_gatt_char = AsyncMock()
        self.mock_client.start_notify = AsyncMock()
        self.on_power_off = Mock()

    async def test_max_air_level_conversion_boundary_cases(self):
        """Max Air:Level command converts levels correctly at boundaries."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Max", self.on_power_off, "")
        await bled.start_notifications()

        test_cases = [
            (0, 0),  # 0 / 4 = 0
            (4, 1),  # 4 / 4 = 1
            (8, 2),  # 8 / 4 = 2
            (12, 3),  # 12 / 4 = 3
            (16, 4),  # 16 / 4 = 4
            (20, 5),  # 20 / 4 = 5
        ]

        for input_level, expected_level in test_cases:
            with patch.object(
                bled, "_execute_level_command", new_callable=AsyncMock
            ) as mock_exec:
                mock_exec.return_value = True
                await bled.intensity2(input_level)
                mock_exec.assert_called_once_with("Air:Level", expected_level)

    async def test_solace_thrust_and_depth(self):
        """Solace uses Thrusting and Depth commands correctly."""
        bled = LovenseBLED(
            self.mock_client, "tx", "rx", "Solace", self.on_power_off, ""
        )
        await bled.start_notifications()
        # Test intensity1 (Thrust)
        with patch.object(
            bled, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            await bled.intensity1(15)
            mock_exec.assert_called_once_with("Thrusting", 15)
        # Test intensity2 (Depth)
        with patch.object(
            bled, "_execute_level_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = True
            await bled.intensity2(10)
            mock_exec.assert_called_once_with("Depth", 10)

    async def test_direct_command_custom_timeout(self):
        """direct_command respects custom timeout."""
        bled = LovenseBLED(self.mock_client, "tx", "rx", "Gush", self.on_power_off, "")
        with patch.object(
            bled, "_execute_command", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = "CustomResponse"
            await bled.direct_command("CustomCmd", timeout=5.0)
            # Verify timeout was passed through
            mock_exec.assert_called_once_with("CustomCmd", 5.0)


if __name__ == "__main__":
    unittest.main()
