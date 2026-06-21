"""
Lovense-specific toy data: model capabilities and per-model recommendations.

This module is the single place to edit when adding or adjusting a Lovense model. It defines:
- :data:`LOVENSE_TOY_NAMES`: model name -> :class:`ToyCommands` (capability/command mapping)
- :data:`ROTATION_TOY_NAMES`: models that support changing the rotation direction
- :data:`MIN_SEGMENT_LENGTH`: suggested minimum interval between intensity changes (ms)

The brand-agnostic data structures these build on (``ToyData``, ``ToyCommands`` and the validation exceptions)
live in :mod:`tikal.low_level.toy_data`.
"""

from ...toy_data import ToyCommands

#: Mapping of Lovense toy model names to their command configurations.
#:
#: This dictionary defines all supported Lovense toy models and their capabilities.
#: Keys are model names, values are ToyCommands objects specifying what commands each toy supports.
#:
#: Models of different versions are treated the same (e.g., Lush 1, Lush 2, and Lush 3 all use the "Lush" key).
#: Some commands are uncertain and assumed based on similar toys. Please notify me if some commands don't work.
#:
#: Type:
#:     dict[str, ToyCommands]
#:
#: Example:
#:     ::
#:
#:         # Check capabilities
#:         commands = LOVENSE_TOY_NAMES["Nora"]
#:         print(f"{commands.intensity1_name}: {commands.intensity1_command}")
#:         if commands.intensity2_name:
#:             print(f"{commands.intensity2_name}: {commands.intensity2_command}")
LOVENSE_TOY_NAMES = {
    "Solace": ToyCommands("Thrust", "Thrusting", "Depth", "Depth"),
    "Sex Machine": ToyCommands(
        "Thrust", "Thrusting", "Depth", "Depth"
    ),  # Commands unknown, assume it uses the same ones as Solace
    "Lush": ToyCommands("Vibration", "Vibrate"),
    "Ferri": ToyCommands("Vibration", "Vibrate"),
    "Nora": ToyCommands("Vibration", "Vibrate", "Rotation", "Rotate"),
    "Osci": ToyCommands(
        "Vibration", "Vibrate", "Oscillation", "Oscillate"
    ),  # Unsure about the second command, Oscillate assumed
    "Mission": ToyCommands("Vibration", "Vibrate"),
    "Flexer": ToyCommands(
        "Vibration", "Vibrate", "Fingering", "Finger"
    ),  # Second command unknown; I just assume 'Finger'
    "Gravity": ToyCommands("Vibration", "Vibrate", "Thrust", "Thrusting"),
    "Dolce": ToyCommands("Vibration", "Vibrate"),
    "Vulse": ToyCommands("Vibration", "Vibrate"),
    "Tenera": ToyCommands("Sucking", "Suck"),  # Command unknown, just assume 'Suck'
    "Lapis": ToyCommands(
        "Vibration", "Vibrate"
    ),  # Has 3 independent vibrators, no idea how to independently control them
    "Ambi": ToyCommands("Vibration", "Vibrate"),
    "Hyphy": ToyCommands("Vibration", "Vibrate"),
    "Exomoon": ToyCommands("Vibration", "Vibrate"),
    "Gush": ToyCommands(
        "Vibration", "Vibrate"
    ),  # Apparently, Oscillation cannot be controlled independently
    "Edge": ToyCommands(
        "Vibration", "Vibrate"
    ),  # Has 2 independent vibrators, no idea how to independently control them
    "Max": ToyCommands("Vibration", "Vibrate", "Air", "Air:Level"),
    "Diamo": ToyCommands("Vibration", "Vibrate"),
    "Calor": ToyCommands("Vibration", "Vibrate"),  # Heat function control unknown
    "Ridge": ToyCommands("Vibration", "Vibrate", "Rotation", "Rotate"),
    "Hush": ToyCommands("Vibration", "Vibrate"),
    "Domi": ToyCommands("Vibration", "Vibrate"),
    "Gemini": ToyCommands(
        "Vibration", "Vibrate"
    ),  # Has 2 independent vibrators, no idea how to independently control them
    "Lush Anal": ToyCommands("Vibration", "Vibrate"),
    # Second Command unknown, assume Thrusting
    "Spinel": ToyCommands("Vibration", "Vibrate", "Thrust", "Thrusting"),
}


#: List of toys (by model_name) that support rotation direction changes.
#:
#: Toys in this list can use the ``rotate_change_direction()`` method to toggle their rotation direction.
#:
#: Type:
#:     list[str]
#:
#: Example:
#:     ::
#:
#:         if toy.model_name in ROTATION_TOY_NAMES:
#:             await toy.rotate_change_direction()
ROTATION_TOY_NAMES = ["Nora", "Ridge"]


#: Maps toys to my suggested minimum segment length, meaning the minimum interval between intensity changes (In milliseconds)
#:
#: Especially useful if you want to implement any pattern playback-related functionality.
#: This is just a suggestion. You're free to use whatever you want.
#:
#: Type:
#:     dict[str, int]
#:
#: Example:
#:     ::
#:
#:         print(MIN_SEGMENT_LENGTH["Nora"])  # 200
MIN_SEGMENT_LENGTH = {
    "Solace": 800,
    "Sex Machine": 800,
    "Lush": 200,
    "Ferri": 200,
    "Nora": 200,
    "Osci": 200,
    "Mission": 200,
    "Flexer": 200,
    "Gravity": 200,
    "Dolce": 200,
    "Vulse": 200,
    "Tenera": 400,
    "Lapis": 200,
    "Ambi": 200,
    "Hyphy": 200,
    "Exomoon": 200,
    "Gush": 200,
    "Edge": 200,
    "Max": 200,
    "Diamo": 200,
    "Calor": 200,
    "Ridge": 200,
    "Hush": 200,
    "Domi": 200,
    "Gemini": 200,
    "Lush Anal": 200,
    "Spinel": 200,
}
