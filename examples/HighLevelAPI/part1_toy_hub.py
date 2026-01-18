from pathlib import Path
from time import sleep
from logging import getLogger, INFO, Formatter, StreamHandler

from tikal import LOVENSE_TOY_NAMES, ToyHub
from tikal.mock import MockBleakClient, MockBleakScanner

# All classes use the logging module
LOGGER_NAME = "toy"

# Path to the toy cache file
TOY_CACHE_PATH = Path("./data") / "toy_cache.json"


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


def main():
    """
    This is the main function, containing the actual example code.
    Here we look at the usage of the ToyHub class.
    This is the first example you should look at.
    """
    prepare_logger()

    # ------------------------------------------------------------------------------------------------------------------
    # Scanning for toys
    # ------------------------------------------------------------------------------------------------------------------

    # There's a bit more information in the docstring. Put briefly, this class is the central interface for
    # communicating with the toys. If you fill in all arguments, you don't need to use keyword arguments.
    # We use a mocked version of the BleakScanner and BleakClient classes here, so I know what mocked toys I will get.
    # In the real application you would leave these arguments empty,
    # so the default values (BleakScanner/BleakClient) are used.
    toy_hub = ToyHub(
        logger_name=LOGGER_NAME,
        default_model="unknown",
        toy_cache_path=TOY_CACHE_PATH,
        bluetooth_scanner=MockBleakScanner,
        bluetooth_client=MockBleakClient,
    )

    # I will show the blocking method here.
    # You can also use toy_hub.discover_toys_callback() which needs a callback function as an argument. This callback
    # will then be executed when the toys are found (receiving the toy_data_list as an argument). Both functions allow
    # you to specify a timeout (default 10s), which is how long the scan will take if BleakScanner is used.
    # In MockBleakScanner, timeout is divided by 10, so this will return much faster than the real scanner
    toy_data_list = toy_hub.discover_toys_blocking()
    print(f"Discovered {len(toy_data_list)} toys")

    # Because ToyCache is currently empty, all returned toys have the default model name.
    default_model = True
    for toy_data in toy_data_list:
        if toy_data.model_name != "unknown":
            default_model = False
    print(f"All toys have the default model name: {default_model}")

    # When we connect to a toy, ToyCache is updated to remember the model name. Connecting comes later, so for now I
    # manually adjust ToyCache.
    toy_hub._toy_cache._cache = {"LVS-Gush": "Gush"}

    # Let's scan again. This time we should see the correct model name for the Gush
    toy_data_list = toy_hub.discover_toys_blocking()
    for toy_data in toy_data_list:
        if toy_data.name == "LVS-Gush":
            print(f"{toy_data.name} is of model {toy_data.model_name}")

    # As a sidenote: If you hand over the empty Path as toy_cache_path, ToyCache works in-memory only. This means that
    # ToyCache will be lost when the program terminates.

    # ------------------------------------------------------------------------------------------------------------------
    # Connecting to toys
    # ------------------------------------------------------------------------------------------------------------------
    solace_data = toy_data_list[0]
    gush_data = toy_data_list[1]
    nora_data = toy_data_list[2]

    # for toys that come with the default model name, you'll need to manually set the model name
    # valid model names can be found in LOVENSE_TOY_NAMES.keys(). Invalid model names will raise a
    # ValidationError, which is caught by the connect method and returned as an unsuccessful result
    print("valid model names:\n", LOVENSE_TOY_NAMES.keys())
    solace_data.model_name = "Ridge"  # valid but incorrect model
    nora_data.model_name = "Nora"

    # Similar to toy discovery, the connect method also has a callback version
    toys = toy_hub.connect_toys_blocking([solace_data, gush_data, nora_data])

    # Because we use a mock and because all model names are valid, we can assume that the all connections are successful
    # This means each of the following is of type LovenseController. If exceptions occur, then instead of
    # LovenseController we would find the Exception at the same index in the list
    solace = toys[0]
    gush = toys[1]
    nora = toys[2]
    print(
        "Solace is of type",
        type(solace),
        "Gush is of type",
        type(gush),
        "Nora is of type",
        type(nora),
    )

    # If you realize that a model name is valid but wrong, you don't need to disconnect and reconnect the toy. Instead,
    # you can use the update_model_name method. Doing this will also update the ToyCache file
    toy_hub.update_model_name(solace.toy_id, "Solace")

    # ------------------------------------------------------------------------------------------------------------------
    # Disconnecting toys
    # ------------------------------------------------------------------------------------------------------------------

    # Similar to toy discovery and connection, the disconnect method also has a callback version. Normally results is a
    # list consisting only of None entries. Should an exception occur, then instead of None we would find the Exception
    results = toy_hub.disconnect_toys_blocking([solace.toy_id, gush.toy_id])
    exceptions = [e for e in results if e is not None]
    print(f"Disconnecting toys raised {len(exceptions)} exceptions")

    # During cleanup, you should call shutdown. Besides cleaning up ToyHub, this also disconnects all connected toys.
    toy_hub.shutdown()

    # ------------------------------------------------------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------------------------------------------------------

    # Previously we instantiated ToyHub without any callbacks. You can provide callbacks during instantiation.
    # Callbacks can also be set/updated later

    def on_disconnect(toy_id: str):
        """
        This function is invoked when a toy disconnects unexpectedly
        (Meaning that the disconnection was not initiated by you, and the toy did not send a POWEROFF message)
        ToyHub automatically tries to reconnect to the toy. If this fails, then reconnect_failure_callback is invoked,
        otherwise reconnect_success_callback is invoked. All methods of the associated toy_controller are still safe to
        call. However, methods that send commands to the toy will have the command scheduled for when the toy reconnects.
        """
        print(f"Callback triggered: Disconnected {toy_id}")

    def on_reconnect_failure(toy_id: str):
        """
        This function is invoked when ToyHub fails to reconnect to a toy after the connection was lost unexpectedly.
        (Meaning that the disconnection was not initiated by you, and the toy did not send a POWEROFF message)
        ToyHub automatically cleanly removes the toy from its internal state. You don't need to do anything. All methods
        of the associated toy_controller are still safe to call. However, methods that send commands to the toy will
        have the command scheduled (for when the toy reconnects, which will now never happen)
        """
        print(
            f"Callback triggered: Connection to the toy {toy_id} permanently lost."
            "If you like to use this toy again, you'll need to use ToyHub.connect_toys() again."
            "Preferably you use ToyHub.discover_toys() first to see if the toy is available again"
        )

    def on_reconnect_success(toy_id: str):
        """
        This function is invoked when ToyHub successfully reconnects to a toy after the connection was lost unexpectedly.
        You don't need to do anything in response to this callback.
        """
        print(f"Callback triggered: Re-established connection to the toy {toy_id}")

    def on_power_off(toy_id: str):
        """
        This function is invoked when a toy sends a POWEROFF message
        Some toys (not all) send this message when they are turned off by the user.
        Upon POWEROFF ToyHub will disconnect the toy, then trigger the callback (if a callback was provided)
        """
        print(f"Callback triggered: Powered off {toy_id}")

    def on_error(exception: Exception, context: str, traceback: str):
        """
        This function is invoked when an unhandled error occurs in the Toy Communication. Ideally, this is never invoked.
        Unideally, if invoked, it will get the exception, a context message, and a traceback. The traceback is unlikely
        to be helpful to you, however, it might be very useful to me. If you encounter an on_error function call, please
        send me the exception, context, and traceback. Thank you. If this callback is not provided, then ToyHub will
        write the information in the log instead.
        """
        print(
            f"Callback triggered: The exception {exception} occurred with context {context} and traceback {traceback}"
        )

    def on_battery_update(batteries: dict[str, int | None]):
        """
        This function is invoked periodically to update the battery level of all connected toys. The dictionary maps
        toy_id to battery level or None if the battery level could not be retrieved.
        """
        print(f"Callback triggered: Batteries are {batteries}")

    # This time we instantiate ToyHub with callbacks.
    new_toy_hub = ToyHub(
        None,
        on_error,
        on_disconnect,
        on_reconnect_failure,
        on_reconnect_success,
        on_power_off,
        LOGGER_NAME,
        TOY_CACHE_PATH,
        "unknown",
        MockBleakScanner,
        MockBleakClient,
    )
    # Setting/Updating a callback can be done like this:
    new_toy_hub.battery_update_callback(on_battery_update)
    print(
        f"The battery callback will be called every {new_toy_hub.BATTERY_UPDATE_INTERVAL} seconds"
    )

    # Connecting new toys will trigger the battery update callback immediately.
    MockBleakScanner.reset()  # you obviously don't need to do this with the real BleakScanner
    new_toy_data = new_toy_hub.discover_toys_blocking()
    new_toy_data[3].model_name = "Gush"
    new_toy_data[4].model_name = "Gush"
    new_toys = new_toy_hub.connect_toys_blocking([new_toy_data[3], new_toy_data[4]])

    # Let's take a look at the other callbacks. I can't show on_error (because it's not meant to be invoked in the first
    # place), but I can show on_disconnect and on_power_off.

    gush_connection_failure = new_toys[0]
    gush_power_off = new_toys[1]

    gush_connection_failure.intensity1(0)
    gush_power_off.intensity1(0)
    # Both toys will fail 5s after receiving the first intensity command, so we just wait a bit
    sleep(8)

    # Remember to shut down ToyHub even when no toy is connected.
    new_toy_hub.shutdown()


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
    main()
    cleanup_cache()
