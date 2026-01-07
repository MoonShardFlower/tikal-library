import asyncio
from bleak import BleakClient
from logging import getLogger, INFO, Formatter, StreamHandler

from tikal import LovenseConnectionBuilder, LOVENSE_TOY_NAMES, ValidationError
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
    Here we look at the usage of the LovenseConnectionBuilder class.
    This is the first example you should look at.
    """
    prepare_logger()

    # ------------------------------------------------------------------------------------------------------------------
    # Scanning for toys
    # ------------------------------------------------------------------------------------------------------------------

    # There's a bit more information in the docstring. Put briefly, this class is used to scan for and connect to toys
    # We use a mocked version of the BleakScanner and BleakClient classes here, so I know what mocked toys I will get.
    # In the real application you would leave these arguments empty,
    # so the default values (BleakScanner/BleakClient) are used.
    builder = LovenseConnectionBuilder(
        on_disconnect, on_power_off, LOGGER_NAME, MockBleakScanner, MockBleakClient
    )  # type: ignore

    # Scans for toys for 10 seconds. Note that the MockBleakScanner only scans for timeout/10 seconds so 1s in this case.
    # BleakScanner scans until timeout is reached.
    lovense_data = await builder.discover_toys(10.0)

    # Because we used the MockBleakScanner I know the results
    print(f"Discovered {len(lovense_data)} toys")
    first_discovery = len(lovense_data)

    print(
        f"\nAs you will see below the model name is always empty and needs to be manually assigned.\n"
        f"(e.g. by letting the user select the correct toy). View the README as to why automatic assignment is difficult\n"
        f"A full list of all valid model names can be found in lovense.LOVENSE_TOY_NAMES.keys():\n"
        f"{LOVENSE_TOY_NAMES.keys()}"
        f"In a later example I will show ToyCache as a way to alleviate this problem a bit "
    )

    # The first toy is a mocked version of a SolacePro
    solace_data = lovense_data[0]
    print(
        f"Solace Info: "
        f"Bluetooth Name={solace_data.name}, "
        f"Bluetooth Address={solace_data.toy_id}, "
        f"model_name is emtpy={solace_data.model_name == ''}"
    )

    # The second toy is a mocked version of a Gush2
    gush_data = lovense_data[1]
    print(
        f"Gush Info: "
        f"Bluetooth Name={gush_data.name}, "
        f"Bluetooth Address={gush_data.toy_id}, "
        f"model_name is emtpy={gush_data.model_name == ''}"
    )

    # ------------------------------------------------------------------------------------------------------------------
    # Connecting to toys
    # ------------------------------------------------------------------------------------------------------------------

    # First, we need to set the model_name. Remember that LOVENSE_TOY_NAMES.keys() contains all valid model names.
    gush_data.model_name = "Gush"  # Valid name
    print(
        f"\n'Gush' is in LOVENSE_TOY_NAMES.keys(): {'Gush' in LOVENSE_TOY_NAMES.keys()}'"
    )
    solace_data.model_name = "blabla"  # Invalid name
    to_connect = [gush_data, solace_data]

    # Connects to the toys. This returns a list of LovenseBLED instances or exceptions for failed connections.
    # The order of the returned list matches the order of the input list.
    toys = await builder.create_toys(to_connect)

    gush = toys[0]
    solace = toys[1]

    print(f"Gush is of type {type(gush)} and is connected: {gush.is_connected}")
    print(
        f"Solace is of type 'ValidationError': {isinstance(solace, ValidationError)}"
    )  # Exception due to an invalid model name
    solace_data.model_name = "Solace"
    toys = await builder.create_toys([solace_data])
    solace = toys[0]
    print(
        f"Solace is no longer of type 'ValidationError': {not isinstance(solace, ValidationError)}"
    )

    # ------------------------------------------------------------------------------------------------------------------
    # Scanning, while some toys are already connected
    # ------------------------------------------------------------------------------------------------------------------

    # Already connected toys do not send advertisements and are therefore not found by the BleakScanner.
    # The MockBleakScanner behaves in the same way
    new_data = await builder.discover_toys(10.0)
    print(f"Discovered 5 toys in the first scan: {first_discovery == 5}")
    print(f"Discovered 3 toys in the second scan: {len(new_data) == 3}")


if __name__ == "__main__":
    asyncio.run(main())
