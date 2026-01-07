import unittest
from unittest.mock import AsyncMock, Mock, patch, call

from tikal import LovenseConnectionBuilder
from tikal import LovenseData, ValidationError
from tikal import LovenseBLED


class TestLovenseConnectionBuilder(unittest.IsolatedAsyncioTestCase):
    """Tests for LovenseConnectionBuilder implementation."""

    def setUp(self):
        """Set up test fixtures."""
        self.on_disconnect = Mock()
        self.on_power_off = Mock()
        self.builder = LovenseConnectionBuilder(
            self.on_disconnect, self.on_power_off, ""
        )

    def test_initialization(self):
        """Builder initializes with correct attributes."""
        self.assertEqual(self.builder._on_disconnect, self.on_disconnect)
        self.assertEqual(self.builder._on_power_off, self.on_power_off)
        self.assertEqual(self.builder._cached_ble_devices, {})
        self.assertEqual(
            self.builder._LOVENSE_SERVICE_PATTERN, "-4bd4-bbd5-a6920e4c5653"
        )

    async def test_discover_toys_empty(self):
        """Discovery returns an empty list when no toys found."""
        with patch(
            "lovense.connection_builder.BleakScanner.discover", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = []

            result = await self.builder.discover_toys(timeout=5.0)

            self.assertEqual(result, [])
            self.assertEqual(self.builder._cached_ble_devices, {})
            mock_discover.assert_called_once_with(timeout=5.0)

    async def test_discover_toys_filters_lovense_devices(self):
        """Discovery only returns devices with LVS-prefix."""
        mock_device1 = Mock()
        mock_device1.name = "LVS-A123"
        mock_device1.address = "00:11:22:33:44:55"

        mock_device2 = Mock()
        mock_device2.name = "Other-Device"
        mock_device2.address = "AA:BB:CC:DD:EE:FF"

        mock_device3 = Mock()
        mock_device3.name = None
        mock_device3.address = "11:22:33:44:55:66"

        mock_device4 = Mock()
        mock_device4.name = "LVS-B456"
        mock_device4.address = "77:88:99:AA:BB:CC"

        with patch(
            "lovense.connection_builder.BleakScanner.discover", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = [
                mock_device1,
                mock_device2,
                mock_device3,
                mock_device4,
            ]

            result = await self.builder.discover_toys(timeout=10.0)

            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].name, "LVS-A123")
            self.assertEqual(result[0].toy_id, "00:11:22:33:44:55")
            self.assertEqual(result[1].name, "LVS-B456")
            self.assertEqual(result[1].toy_id, "77:88:99:AA:BB:CC")

            # Check cache
            self.assertEqual(len(self.builder._cached_ble_devices), 2)
            self.assertIn("00:11:22:33:44:55", self.builder._cached_ble_devices)
            self.assertIn("77:88:99:AA:BB:CC", self.builder._cached_ble_devices)

    async def test_discover_toys_clears_cache(self):
        """Discovery clears the previous cache before populating new results."""
        # Populate cache with old data
        old_device = Mock()
        old_device.address = "OLD:ADDRESS"
        self.builder._cached_ble_devices["OLD:ADDRESS"] = old_device

        mock_device = Mock()
        mock_device.name = "LVS-NEW"
        mock_device.address = "NEW:ADDRESS"

        with patch(
            "lovense.connection_builder.BleakScanner.discover", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = [mock_device]

            _ = await self.builder.discover_toys(timeout=5.0)

            self.assertEqual(len(self.builder._cached_ble_devices), 1)
            self.assertNotIn("OLD:ADDRESS", self.builder._cached_ble_devices)
            self.assertIn("NEW:ADDRESS", self.builder._cached_ble_devices)

    async def test_create_toys_empty_list(self):
        """create_toys handles an empty input list."""
        result = await self.builder.create_toys([])
        self.assertEqual(result, [])

    async def test_create_toys_successful_connection(self):
        """create_toys successfully connects to valid toys."""
        # Set up mock device in cache
        mock_device = Mock()
        mock_device.name = "LVS-A123"
        mock_device.address = "00:11:22:33:44:55"
        self.builder._cached_ble_devices["00:11:22:33:44:55"] = mock_device

        toy_data = LovenseData(
            name="LVS-A123", toy_id="00:11:22:33:44:55", model_name="Gush"
        )

        # Mock the _create_toy method
        mock_bled = Mock(spec=LovenseBLED)
        with patch.object(
            self.builder, "_create_toy", new_callable=AsyncMock
        ) as mock_create:
            mock_create.return_value = mock_bled

            result = await self.builder.create_toys([toy_data])

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0], mock_bled)
            mock_create.assert_called_once_with("Gush", mock_device)

    async def test_create_toys_multiple_devices(self):
        """create_toys handles multiple toys in parallel."""
        # Set up mock devices
        mock_device1 = Mock()
        mock_device1.address = "00:11:22:33:44:55"
        mock_device2 = Mock()
        mock_device2.address = "AA:BB:CC:DD:EE:FF"

        self.builder._cached_ble_devices["00:11:22:33:44:55"] = mock_device1
        self.builder._cached_ble_devices["AA:BB:CC:DD:EE:FF"] = mock_device2

        toy_data1 = LovenseData(
            name="LVS-A123", toy_id="00:11:22:33:44:55", model_name="Gush"
        )
        toy_data2 = LovenseData(
            name="LVS-B456", toy_id="AA:BB:CC:DD:EE:FF", model_name="Nora"
        )

        mock_bled1 = Mock(spec=LovenseBLED)
        mock_bled2 = Mock(spec=LovenseBLED)

        with patch.object(
            self.builder, "_create_toy", new_callable=AsyncMock
        ) as mock_create:
            mock_create.side_effect = [mock_bled1, mock_bled2]

            result = await self.builder.create_toys([toy_data1, toy_data2])

            self.assertEqual(len(result), 2)
            self.assertEqual(result[0], mock_bled1)
            self.assertEqual(result[1], mock_bled2)

    async def test_create_toys_handles_exceptions(self):
        """create_toys returns exceptions for failed connections."""
        mock_device = Mock()
        mock_device.address = "00:11:22:33:44:55"
        self.builder._cached_ble_devices["00:11:22:33:44:55"] = mock_device

        toy_data = LovenseData(
            name="LVS-A123", toy_id="00:11:22:33:44:55", model_name="Gush"
        )

        connection_error = ConnectionError("Failed to connect")

        with patch.object(
            self.builder, "_create_toy", new_callable=AsyncMock
        ) as mock_create:
            mock_create.side_effect = connection_error

            result = await self.builder.create_toys([toy_data])

            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], ConnectionError)
            self.assertEqual(str(result[0]), "Failed to connect")

    async def test_find_uuid_by_type_tx(self):
        """_find_uuid_by_type correctly identifies TX UUID."""
        mock_client = Mock()

        mock_char = Mock()
        mock_char.uuid = "40000002-4BD4-BBD5-A6920E4C5653"

        mock_service = Mock()
        mock_service.uuid = "40000001-4bd4-bbd5-a6920e4c5653"
        mock_service.characteristics = [mock_char]

        mock_client.services = [mock_service]

        result = await self.builder._find_uuid_by_type(mock_client, "tx")

        self.assertEqual(result, "40000002-4BD4-BBD5-A6920E4C5653")

    async def test_find_uuid_by_type_rx(self):
        """_find_uuid_by_type correctly identifies RX UUID."""
        mock_client = Mock()

        mock_char = Mock()
        mock_char.uuid = "40000003-4BD4-BBD5-A6920E4C5653"

        mock_service = Mock()
        mock_service.uuid = "40000001-4bd4-bbd5-a6920e4c5653"
        mock_service.characteristics = [mock_char]

        mock_client.services = [mock_service]

        result = await self.builder._find_uuid_by_type(mock_client, "rx")

        self.assertEqual(result, "40000003-4BD4-BBD5-A6920E4C5653")

    async def test_find_uuid_by_type_invalid_type(self):
        """_find_uuid_by_type raises ValueError for an invalid type."""
        mock_client = Mock()

        with self.assertRaises(ValueError) as context:
            await self.builder._find_uuid_by_type(mock_client, "invalid")

        self.assertIn("Invalid UUID type", str(context.exception))

    async def test_find_uuid_by_type_not_found(self):
        """_find_uuid_by_type raises ConnectionError when UUID not found."""
        mock_client = Mock()
        mock_client.address = "00:11:22:33:44:55"
        mock_client.services = []

        with self.assertRaises(ConnectionError) as context:
            await self.builder._find_uuid_by_type(mock_client, "tx")

        self.assertIn("Unable to find tx-UUID", str(context.exception))

    async def test_create_toy_invalid_model_name(self):
        """_create_toy raises ValidationError for an invalid model name."""
        mock_device = Mock()
        mock_device.address = "00:11:22:33:44:55"

        with self.assertRaises(ValidationError) as context:
            await self.builder._create_toy("InvalidModel", mock_device)

        self.assertIn("Invalid model_name 'InvalidModel'", str(context.exception))

    async def test_create_toy_connection_failure(self):
        """_create_toy raises ConnectionError when BLE connection fails."""
        mock_device = Mock()
        mock_device.address = "00:11:22:33:44:55"

        with patch("lovense.connection_builder.BleakClient") as mock_client_class:
            mock_client = Mock()
            mock_client.connect = AsyncMock(side_effect=Exception("Connection timeout"))
            mock_client_class.return_value = mock_client

            with self.assertRaises(ConnectionError) as context:
                await self.builder._create_toy("Gush", mock_device)

            self.assertIn("Error connecting to Gush", str(context.exception))

    async def test_create_toy_notification_setup_failure(self):
        """_create_toy disconnects and raises error if notification setup fails."""
        mock_device = Mock()
        mock_device.address = "00:11:22:33:44:55"

        with patch("lovense.connection_builder.BleakClient") as mock_client_class:
            mock_client = Mock()
            mock_client.connect = AsyncMock()
            mock_client.disconnect = AsyncMock()
            mock_client_class.return_value = mock_client

            with patch.object(
                self.builder, "_find_uuid_by_type", new_callable=AsyncMock
            ) as mock_find_uuid:
                mock_find_uuid.side_effect = Exception("UUID not found")

                with self.assertRaises(ConnectionError) as context:
                    await self.builder._create_toy("Gush", mock_device)

                self.assertIn("Error setting up notifications", str(context.exception))
                mock_client.disconnect.assert_called_once()

    async def test_create_toy_successful_full_flow(self):
        """_create_toy successfully creates toy through full connection flow."""
        mock_device = Mock()
        mock_device.address = "00:11:22:33:44:55"

        with patch("lovense.connection_builder.BleakClient") as mock_client_class:
            mock_client = Mock()
            mock_client.connect = AsyncMock()
            mock_client.disconnect = AsyncMock()
            mock_client_class.return_value = mock_client

            mock_bled = Mock(spec=LovenseBLED)

            with patch.object(
                self.builder, "_find_uuid_by_type", new_callable=AsyncMock
            ) as mock_find_uuid:
                mock_find_uuid.side_effect = ["TX_UUID", "RX_UUID"]

                with patch("lovense.connection_builder.LovenseBLED") as mock_bled_class:
                    mock_bled.start_notifications = AsyncMock()
                    mock_bled_class.return_value = mock_bled

                    result = await self.builder._create_toy("Gush", mock_device)

                    self.assertEqual(result, mock_bled)
                    mock_client.connect.assert_called_once()
                    mock_find_uuid.assert_has_calls(
                        [call(mock_client, "tx"), call(mock_client, "rx")]
                    )
                    mock_bled_class.assert_called_once_with(
                        mock_client, "TX_UUID", "RX_UUID", "Gush", self.on_power_off
                    )
                    mock_bled.start_notifications.assert_called_once()


class TestLovenseConnectionBuilderIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests for LovenseConnectionBuilder workflows."""

    def setUp(self):
        """Set up test fixtures."""
        self.on_disconnect = Mock()
        self.on_power_off = Mock()
        self.builder = LovenseConnectionBuilder(
            self.on_disconnect, self.on_power_off, ""
        )

    async def test_discovery_to_connection_workflow(self):
        """Test the complete workflow from discovery to connection."""
        # Mock discovery
        mock_device = Mock()
        mock_device.name = "LVS-A123"
        mock_device.address = "00:11:22:33:44:55"

        with patch(
            "lovense.connection_builder.BleakScanner.discover", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = [mock_device]

            # Discover toys
            discovered = await self.builder.discover_toys(timeout=5.0)

            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].toy_id, "00:11:22:33:44:55")

            # Update model name
            discovered[0].model_name = "Gush"

            # Mock connection
            with patch.object(
                self.builder, "_create_toy", new_callable=AsyncMock
            ) as mock_create:
                mock_bled = Mock(spec=LovenseBLED)
                mock_create.return_value = mock_bled

                # Connect to discovered toys
                connected = await self.builder.create_toys(discovered)

                self.assertEqual(len(connected), 1)
                self.assertEqual(connected[0], mock_bled)


if __name__ == "__main__":
    unittest.main()
