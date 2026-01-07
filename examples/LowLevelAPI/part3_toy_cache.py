import asyncio
from pathlib import Path

from bleak import BleakClient
from logging import getLogger, INFO, Formatter, StreamHandler

from tikal import LovenseConnectionBuilder, ToyCache
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
    Here we look at the usage of the ToyCache class.
    This is the third example you should look at
    (The first two contain information about the ConnectionBuilder and Lovense Toys)
    """

    # As seen in the previous two examples, there's currently no way to automatically set the correct model of a toy
    # (see ReadMe for more information about why).
    # Knowing the correct model is crucial for the intensity commands to adjust the level of the toys' capabilities.
    # ToyCache can be used to store a mapping of bluetooth name to model between sessions, so the user only needs to
    # select the correct model once.

    # Choose any path and filename you like. The file type has to be .json
    # If the directory or file does not exist, it will be created automatically
    cache_path = Path("./data") / "toy_cache.json"

    # Choose a default model name. This will be returned if the bluetooth name is not found in the cache.
    default = "unknown"

    cache = ToyCache(cache_path, default, LOGGER_NAME)

    # Whenever the user selects a model, you can use 'update' to add it to the cache.
    cache.update({"LVS-Gush": "Gush", "LVS-B12": "Solace"})

    # If the model is in the cache, its model name is returned
    gush_name = cache.get_model_name("LVS-Gush")
    print(f"LVS-Gush is of model {gush_name}")

    # Otherwise the default model name is returned
    unknown_name = cache.get_model_name("LVS-blabla")
    print(f"LVS-blabla is of model {unknown_name}")

    # If the cache is updated with an already existing bluetooth name, the old model name is overwritten.
    print(f"LVS-B12 is of model {cache.get_model_name('LVS-B12')}")
    cache.update({"LVS-B12": "Nora"})
    print(f"LVS-B12 is now of model {cache.get_model_name('LVS-B12')}")

    # Let's use the cache in combination with the mock toys
    builder = LovenseConnectionBuilder(on_disconnect, on_power_off, LOGGER_NAME, MockBleakScanner, MockBleakClient)  # type: ignore
    toy_data = await builder.discover_toys(10.0)
    gush_data = toy_data[1]
    model_name = cache.get_model_name(gush_data.name)
    if model_name == default:
        print(f"Sorry. I don't know the model of {gush_data.name}")
        model_name = input("Please enter the correct model name: ")
        cache.update({gush_data.name: model_name})

    gush_data.model_name = model_name
    toys = await builder.create_toys([gush_data])
    gush = toys[0]
    print(
        f"Successfully initialized mocked Gush. Toy is of type {type(gush)} and is connected: {gush.is_connected}"
    )


# ----------------------------------------------------------------------------------------------------------------------
# Clean up artifacts produced by the example
# ----------------------------------------------------------------------------------------------------------------------


def cleanup_cache(data_dir: Path = Path("./data"), cache_name: str = "toy_cache.json"):
    """Safely clean up the ToyCache artifact produced by the example code"""
    cache = data_dir / cache_name
    if not data_dir.exists():
        return  # Nothing to do if the directory doesn't exist

    if not data_dir.is_dir():
        raise RuntimeError(f"Expected '{data_dir}' to be a directory")

    # Check for unexpected files
    unexpected = [p.name for p in data_dir.iterdir() if p.name != cache.name]
    if unexpected:
        raise RuntimeError(
            f"Refusing to delete '{data_dir}' due to unexpected files: {unexpected}"
        )

    if cache.exists():
        cache.unlink()
    data_dir.rmdir()


if __name__ == "__main__":
    asyncio.run(main())
    cleanup_cache()
