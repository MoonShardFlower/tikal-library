import asyncio
from bleak import BleakClient
from logging import getLogger, INFO, Formatter, StreamHandler

from tikal import LovenseConnectionBuilder, LOVENSE_TOY_NAMES, ROTATION_TOY_NAMES
from tikal.mock import MockBleakClient, MockBleakScanner

# All classes use the logging module
LOGGER_NAME = "toy"


def on_disconnect(client: BleakClient):
    """
    This function is invoked when a toy disconnects unexpectedly
    (Meaning that the disconnection was not initiated by you, and the toy did not send a POWEROFF message)
    """
    print(f"Callback triggered: Disconnected {client.address}")


def on_power_off(bluetooth_address: str):
    """
    This function is invoked when a toy sends a POWEROFF message
    Some toys (not all) send this message when they are turned off by the user.
    """
    print(f"Callback triggered: Powered off {bluetooth_address}")


def prepare_logger():
    """All classes use the logging module. This function sets up a simple console logger."""
    logger = getLogger(LOGGER_NAME)
    formatting = Formatter(
        "%(asctime)s [%(levelname)s] : %(module)s.%(funcName)s reports: %(message)s"
    )
    handler = StreamHandler()
    handler.setFormatter(formatting)
    logger.addHandler(handler)
    logger.setLevel(INFO)  # Switch to 'DEBUG' if you want more information


# ----------------------------------------------------------------------------------------------------------------------
# Example Start
# ----------------------------------------------------------------------------------------------------------------------


async def main():
    """
    This is the main function, containing the actual example code.
    Here we look at the usage of the LovenseToy class.
    This is the second example you should look at (The first one containing information about the ConnectionBuilder)
    """
    prepare_logger()

    # ------------------------------------------------------------------------------------------------------------------
    # Scanning and connecting to toys (see first example)
    # ------------------------------------------------------------------------------------------------------------------

    builder = LovenseConnectionBuilder(
        on_disconnect, on_power_off, LOGGER_NAME, MockBleakScanner, MockBleakClient
    )  # type: ignore
    lovense_data = await builder.discover_toys(10.0)

    # Because we used the MockBleakScanner I know the results

    # The first toy is a mocked version of a SolacePro
    solace_data = lovense_data[0]
    solace_data.model_name = "Solace"

    # The second toy is a mocked version of a Gush2
    gush_data = lovense_data[1]
    gush_data.model_name = "Gush"

    # The third toy is a mocked version of a Nora
    nora_data = lovense_data[2]
    nora_data.model_name = "Nora"

    # The fourth toy is a mocked Gush2 that disconnects 10 s after the first intensity command has been sent.
    disconnect_gush_data = lovense_data[3]
    disconnect_gush_data.model_name = "Gush"

    # The fifth toy is a mocked Gush2 that sends a POWEROFF message 10 s after the first intensity command has been sent.
    poweroff_gush_data = lovense_data[4]
    poweroff_gush_data.model_name = "Gush"

    to_connect = [
        solace_data,
        gush_data,
        nora_data,
        disconnect_gush_data,
        poweroff_gush_data,
    ]
    toys = await builder.create_toys(to_connect)
    solace = toys[0]
    gush = toys[1]
    nora = toys[2]
    disconnect_gush = toys[3]
    poweroff_gush = toys[4]

    # ------------------------------------------------------------------------------------------------------------------
    # First steps into the toy usage
    # ------------------------------------------------------------------------------------------------------------------

    # The toys' capabilities are controlled via intensity commands. Some toys have one capability, others have two.
    # Capability levels always range from 0 to 20.  Levels outside this range will be clamped.
    # The 'Max' toy is an edge case that only supports 0 to 5 for its secondary capability.
    # I handle this by dividing the given level by 4 if needed, so you still always use 0-20

    # Let's set the SolacePro Thrusting capability to 10 = Medium thrusting speed
    await solace.intensity1(10)
    # Let's set the SolacePro Depth capability to 20 = Maximum travel range
    await solace.intensity2(20)

    # You can look up the capabilities of a toy by inspecting the LOVENSE_TOY_NAMES dictionary
    print(
        f"SolacePro has {LOVENSE_TOY_NAMES['Solace'].intensity1_name} and {LOVENSE_TOY_NAMES['Solace'].intensity2_name}"
    )
    print(
        f"Gush has {LOVENSE_TOY_NAMES['Gush'].intensity1_name} and {LOVENSE_TOY_NAMES['Gush'].intensity2_name}"
    )

    # If an intensity command is executed successfully, the function returns True, else False.
    print(f"Gush successfully set intensity1 to 5: {await gush.intensity1(5)}")

    # As we saw above, Gush has no second capability. In this case intensity2 returns True but does nothing.
    print(f"Gush successfully set intensity2 to 5: {await gush.intensity2(5)}")

    # Obviously, you can stop a toy by setting its intensities to zero.
    await gush.intensity1(0)
    # However, there's also a shortcut for this
    await solace.stop()

    # ------------------------------------------------------------------------------------------------------------------
    # Gathering Information
    # ------------------------------------------------------------------------------------------------------------------

    # The model name of the toy can be read via the model_name property.
    print(f"SolacePro model name: {solace.model_name}")

    # Via properties, we can also retrieve the Bluetooth address and Bluetooth name of the toy.
    print(
        f"SolacePro Bluetooth address: {solace.address}, SolacePro Bluetooth name: {solace.name}"
    )

    # The most interesting information is likely the battery level
    battery_level = await solace.get_battery_level()
    print(f"SolacePro battery level: {battery_level}")

    # There's also StatusCode, Batch number and device type:
    status = await solace.get_status()
    batch = await solace.get_batch_number()
    device_type = await solace.get_device_type()
    print(
        f"SolacePro status: {status}, batch number: {batch}, device type: {device_type}"
    )

    # ------------------------------------------------------------------------------------------------------------------
    # Advanced usage
    # ------------------------------------------------------------------------------------------------------------------

    # If you set the model_name to the wrong model, you can change it later.
    # Note that invalid names will still raise a ValidationError.
    gush.set_model_name("Solace")  # valid, but wrong model
    print(
        f"Gush changed to: {gush.model_name}. "
        f"This causes the intensity command to fail, so the following is now false: {await gush.intensity1(5)}"
    )

    gush.set_model_name("Gush")
    print(
        f"Gush changed back to: {gush.model_name}. "
        f"This causes the intensity command to succeed: {await gush.intensity1(5)}"
    )

    # Some toys have a Rotation capability. These toys can have their rotation direction changed
    await nora.rotate_change_direction()
    print(
        f"List of toys that can have their rotation direction changed:\n{ROTATION_TOY_NAMES}"
    )

    # The current connection status is available through the is_connected property. This just returns
    # BleakClient.is_connected, which can change at any time. Meaning that if is_connected is false, it can be true
    # again when next called without you doing anything.
    print(f"SolacePro is connected: {solace.is_connected}")

    # Commands can be sent directly to the toy, allowing you to use commands that are not implemented in this library.
    await solace.direct_command("DeviceType;")

    # Though I discourage it, you can shut off toys. Note that this effectively produces a connection failure
    await solace.power_off()

    # ------------------------------------------------------------------------------------------------------------------
    # Connection Failure and PowerOff handling
    # ------------------------------------------------------------------------------------------------------------------

    print(
        "Exploring Connection Failure and PowerOff handling. This will take a short while."
    )

    # Lovense toys can be turned off via a button on the toy. Some toys are nice enough to send a POWEROFF message
    # when this happens, which will then trigger a call to on_power_off (provided to the connection builder earlier)
    # Note that not all toys send this message. I recommend handling this message by calling toy.disconnect() and then
    # removing the toy from your application.

    # This Mock toy sends a POWEROFF message 5 s after the first intensity command has been sent.
    await poweroff_gush.intensity1(0)

    # Every time a toy disconnects unexpectedly, on_disconnect is called with the BleakClient as an argument.
    # You could then try to reconnect via client.connect()
    # (On windows at least the backend also tries to reestablish connection by itself,
    # which is why toy.is_connected can change at any time)
    # if you want to cleanly disconnect, call client.disconnect() here.

    # This Mock toy disconnects 5 s after the first intensity command has been sent.
    await disconnect_gush.intensity1(0)

    await asyncio.sleep(6.0)

    # ------------------------------------------------------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------------------------------------------------------

    # To clean up, please call disconnect() on all toys you created. Doing this attempts to set the toys' intensities
    # to 0, followed by attempting to cleanly disconnect. If a connection failure occurred, some of these might fail.
    # That is to be expected. The exceptions raised by connection failures are caught and logged. The disconnect
    # method does not raise any exceptions.
    print("Cleaning up")
    await solace.disconnect()
    await gush.disconnect()
    await disconnect_gush.disconnect()
    await poweroff_gush.disconnect()
    await nora.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
