import time

from bleak import BleakClient
from logging import getLogger, INFO, Formatter, StreamHandler

from tikal import ToyHub
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


def main():
    """
    This is the main function, containing the actual example code.
    Here we look at the usage of the LovenseController class.
    This is the second example you should look at (The first one containing information about the ToyHub)
    """
    prepare_logger()

    # ------------------------------------------------------------------------------------------------------------------
    # Scanning and connecting to toys (see first example)
    # ------------------------------------------------------------------------------------------------------------------

    toy_hub = ToyHub(
        logger_name=LOGGER_NAME,
        default_model="unknown",
        bluetooth_scanner=MockBleakScanner,
        bluetooth_client=MockBleakClient,
    )

    toy_data_list = toy_hub.discover_toys_blocking()
    toy_data_list[0].model_name = "Solace"
    toy_data_list[1].model_name = "Gush"
    toy_data_list[2].model_name = "Nora"

    toys = toy_hub.connect_toys_blocking(toy_data_list)

    solace = toys[0]
    gush = toys[1]
    nora = toys[2]

    # ------------------------------------------------------------------------------------------------------------------
    # Basic usage
    # ------------------------------------------------------------------------------------------------------------------

    # The toys' capabilities are controlled via intensity commands. Some toys have one capability, others have two.
    # Capability levels always range from 0 to 20. Levels outside this range will be clamped.
    # The 'Max' toy is an edge case that only supports 0 to 5 for its secondary capability.
    # I handle this by dividing the given level by 4 if needed, so you still always use 0-20
    solace.intensity1(10)  # Sets thrusting speed to level 10
    solace.intensity2(5)  # Sets depth to level 5

    gush.intensity1(20)  # Sets Vibration intensity to level 20
    gush.intensity2(8)  # Does nothing

    # If you like, you can provide a callback to get notified when the command was executed and if it was successful
    def on_executed(success: bool):
        print(
            f"Callback triggered: Successfully executed setting gush's vibration back to 0: {success}"
        )

    gush.intensity1(0, on_executed)
    time.sleep(1)

    # If you want to stop a toy you can use stop() as shortcut for setting both intensities to 0
    solace.stop()
    time.sleep(1)

    # Some toys have a rotation capability. These toys can have their rotation direction changed
    nora.change_rotate_direction()
    time.sleep(1)
    # You can call change_rotate_direction_available() to check if the toy supports this capability
    print(f"Nora has a rotation capability: {nora.change_rotate_direction_available()}")

    # You can retrieve the current battery level of a toy
    def on_battery_available(battery_level: int | None):
        print(f"Callback triggered: Battery level of Nora is {battery_level}")

    nora.get_battery_level(on_battery_available)
    time.sleep(1)

    # You can retrieve some device information
    def _on_information_available(info: dict[str, str]):
        print(f"Callback triggered: Information about Nora is {info}")

    nora.get_information(_on_information_available)
    time.sleep(1)

    # Commands can be sent directly to the toy, allowing you to use commands that are not implemented in this library.
    def on_response(response: str):
        print(f"Callback triggered: Response from Nora is {response}")

    nora.direct_command("DeviceType;", on_response)
    time.sleep(1)

    # ------------------------------------------------------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------------------------------------------------------

    # You can access the toy id, which is a unique identifier commonly used by the toy_hub (needed to update the model
    # name and to disconnect toys.) For bluetooth toys: toy_id == bluetooth address
    # You can also access the model name. To change it, you need to call toy_hub.update_model_name(toy_id, new_model_name)
    print(f"Solace has the toy_id {solace.toy_id} and is of model {solace.model_name}")

    # Internally, all functions that send commands to the toy do not send directly but schedule the command in a
    # queue instead. The toy hub regularly (every 50ms) polls this queue and sends the command if the toy is connected.
    # If it isn't, the command will just remain in the queue until the connection is re-established. You can view the
    # connection status here
    print(f"Gush is currently connected {gush.connected}")

    # The names of the toys' capabilities and the maximal intensity level can be accessed as well
    # max intensity will always be 20 for lovense toys. This is future proofing for other toy brands that might be
    # implemented in the future and might have other level ranges
    print(
        f"Solace has the capabilities: {solace.intensity_names[0]} and {solace.intensity_names[1]}"
    )
    print(f"Gush has only one capability: {gush.intensity_names[1] is None}")
    print(f"Max intensity for lovense toys: {solace.intensity_max_value}")

    # ------------------------------------------------------------------------------------------------------------------
    # Patterns, blocking and pausing
    # ------------------------------------------------------------------------------------------------------------------

    # ToyController provides some basic pattern-related functionality.

    # Patterns are defined as a list of tuples of (time, intensity1, intensity2), where time is in milliseconds
    # The pattern below sets:
    # intensity1 = 5, intensity2 = 6 for 500ms, then
    # intensity1 = intensity2 = 0 for 600ms
    pattern = [(500, 5, 6), (600, 0, 0)]

    # If wraparound is set to False, the pattern is unset upon completing the last segment. If True (default), the pattern
    # repeats from the start.
    # If reset_time is set to False, the new pattern is not played from the start, but instead we jump to the point in
    # time where we left off the previous pattern.
    gush.set_pattern(pattern)

    # Unset the pattern
    gush.set_pattern([])

    # You can pause a toy
    # If a pattern was set to the toy before pausing, the pattern does no longer progress. Upon unpausing, the pattern
    # progresses exactly where it was left off. If a pattern is set after pausing, the pattern does not progress.
    # Upon unpausing, the pattern starts. Pausing does not affect your ability to manually send intensity commands.
    gush.toggle_pause()

    # Pausing can happen automatically. If you set a pattern, then use an intensity or stop function, the pattern is
    # paused (to avoid the pattern overwriting your command)
    solace.set_pattern(pattern)
    print(f"Solace is paused: {solace.is_paused}")
    solace.intensity1(9)
    print(f"Solace is paused: {solace.is_paused}")

    # You can block a toy
    # If a pattern was set before blocking or is set after blocking, it will progress normally.
    # However, the toys' intensities remain forced to 0 (so the pattern progresses without influencing the toy)
    # Blocking also blocks your ability to adjust the toy's intensities manually (If a callback is provided, intensity
    # commands will return False)
    gush.toggle_block()

    # You cannot pause and block at the same time. If block is set to true, then pause is set to false and vice versa.
    print(f"Gush is blocked: {gush.is_blocked}")
    gush.toggle_pause()
    print(f"Gush is blocked: {gush.is_blocked}")

    # You can access for how long the current pattern has been playing. If no pattern is set, this will be 0.0
    nora.set_pattern(pattern)
    time.sleep(1)
    print(f"Nora's pattern has been playing for {nora.get_pattern_time()} ms")

    # You can access the intensity levels of a pattern at a specified time. If no pattern is set, this will be (0, 0)
    print(f"Nora's pattern intensities at 400ms are {nora.get_pattern_values(400)}")

    # ------------------------------------------------------------------------------------------------------------------
    # Shutting down
    # ------------------------------------------------------------------------------------------------------------------

    toy_hub.shutdown()


if __name__ == "__main__":
    main()
