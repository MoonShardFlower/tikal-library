import json
import shutil
import tempfile
import time
from logging import INFO, Formatter, StreamHandler, getLogger
from pathlib import Path

from tikal.funscript import Funscript, funscript_to_pattern
from tikal.high_level import ToyHub
from tikal.mock import MockBleakClient, MockBleakScanner

# All classes use the logging module
LOGGER_NAME = "toy"


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


def write_demo_script(path: Path, start_pos: int = 0) -> Path:
    """
    Write a small funscript to *path*.

    A funscript is a JSON file. Its "actions" are timed positions: "at" is the time in milliseconds, "pos" the position
    from 0 (bottom) to 100 (top). This one strokes slowly for 4 seconds, faster for 4 seconds, very fast for 4 seconds,
    and then holds still for 3 seconds.
    """
    actions = []
    at, pos = 0, start_pos
    for stroke_ms in [1000] * 4 + [500] * 8 + [250] * 16:
        actions.append({"at": at, "pos": pos})
        at += stroke_ms
        pos = 100 - pos
    actions.append({"at": at, "pos": pos})
    actions.append({"at": at + 3000, "pos": pos})  # hold still
    path.write_text(
        json.dumps({"version": "1.0", "actions": actions}), encoding="utf-8"
    )
    return path


# ----------------------------------------------------------------------------------------------------------------------
# Example Start
# ----------------------------------------------------------------------------------------------------------------------


def main():
    """
    This is the main function, containing the actual example code.
    Here we look at playing funscripts on toys.
    This is the third example you should look at (after the ToyHub and the ToyController examples)
    """
    prepare_logger()
    folder = Path(tempfile.mkdtemp())
    script_path = write_demo_script(folder / "video.funscript")

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

    gush = toys[1]
    nora = toys[2]

    # ------------------------------------------------------------------------------------------------------------------
    # Playing a funscript
    # ------------------------------------------------------------------------------------------------------------------

    # Funscripts are written for strokers, which move to a position. Lovense toys take an intensity instead, so the
    # script has to be mapped onto intensities. By default, the intensity follows how fast the script moves: a full
    # stroke (0 to 100) in 250 ms or faster gives the maximum intensity, a full stroke over one second a quarter of it,
    # and holding still gives 0.
    # set_funscript takes the path of a .funscript file (or a Funscript, see below) and turns the script into an
    # ordinary pattern, played once. Everything you know about patterns applies: pause, block and the intensity limits.
    gush.set_funscript(script_path)

    # The script plays right away. The demo script starts with slow strokes...
    time.sleep(1.5)
    print(f"Gush after 1.5 s (slow strokes): {gush.current_intensities}")

    # The converted pattern is visible like any other pattern. A dense script would have many short segments, but the
    # conversion never goes below the toy's recommended minimum interval (200 ms for Gush) to avoid flooding the toy
    # with commands.
    pattern = gush.get_pattern_data()[0]
    print(f"Gush plays {len(pattern)} segments, the first ones are {pattern[:3]}")

    # ------------------------------------------------------------------------------------------------------------------
    # Following a video
    # ------------------------------------------------------------------------------------------------------------------

    # Funscripts usually belong to a video. If the video jumps, move the toy along with seek (position in milliseconds).
    gush.seek(9000)  # The video jumped to 0:09, where the script strokes very fast
    time.sleep(0.5)
    print(f"Gush at 0:09 (very fast strokes): {gush.current_intensities}")

    # If the video is paused, pause the toy. Pausing stops the toy and freezes the script where it is.
    # Resuming continues from exactly that point.
    gush.set_paused(True)
    time.sleep(0.2)  # The stop is sent in the background
    print(
        f"Gush is paused at {gush.get_pattern_time():.0f} ms: {gush.current_intensities}"
    )
    gush.set_paused(False)

    # If your video player is already somewhere in the video when you load the script, use start_ms
    gush.set_funscript(script_path, start_ms=4000)
    time.sleep(0.5)
    print(f"Gush started at 0:04 (faster strokes): {gush.current_intensities}")

    # Note: like a pattern, a funscript is paused by any manual intensity command and by stop(). Resume it with
    # set_paused(False). Once the script is over, the toy stays at 0.

    # ------------------------------------------------------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------------------------------------------------------

    # max_speed sets which stroke speed (positions per second) maps to the maximum intensity. The default is 400.
    # With 200, the "faster strokes" part of the demo script already gives the maximum.
    gush.set_funscript(script_path, start_ms=4000, max_speed=200)
    time.sleep(0.5)
    print(f"Gush with max_speed=200: {gush.current_intensities}")

    # With mode="position", the intensity follows the position instead of the speed (position 0 to 100 maps to
    # intensity 0 to 20). For the demo script, this means the toy goes up and down with every stroke.
    gush.set_funscript(script_path, mode="position")

    # A second script can drive the secondary capability, e.g., a ".twist.funscript" for Nora's rotation.
    # Without it, the secondary capability stays at 0.
    twist_path = write_demo_script(folder / "video.twist.funscript", start_pos=100)
    nora.set_funscript(script_path, secondary=twist_path)
    time.sleep(1.5)
    print(f"Nora (vibration and rotation): {nora.current_intensities}")

    # Intensity limits apply to funscripts as they do to every pattern
    nora.set_intensity1_limit(2)
    time.sleep(0.5)
    print(f"Nora with an intensity1 limit of 2: {nora.current_intensities}")
    nora.set_intensity1_limit(None)

    # ------------------------------------------------------------------------------------------------------------------
    # Loading and converting scripts yourself
    # ------------------------------------------------------------------------------------------------------------------

    # Funscript loads and checks a script. A broken file raises an error (pydantic.ValidationError, a ValueError).
    script = Funscript.load(script_path)
    print(
        f"The script has {len(script.actions)} actions and lasts {script.duration_ms} ms"
    )

    # You can also build a script in code: a list of (at, pos) pairs
    my_script = Funscript(actions=[(0, 0), (500, 100), (1000, 0), (1500, 100)])
    gush.set_funscript(my_script)
    pattern = funscript_to_pattern(my_script, max_intensity=20, min_segment_ms=200)
    print(f"My script as a pattern: {pattern}")

    # ------------------------------------------------------------------------------------------------------------------
    # Shutting down
    # ------------------------------------------------------------------------------------------------------------------

    toy_hub.shutdown()
    shutil.rmtree(folder)  # Remove the demo scripts


if __name__ == "__main__":
    main()
