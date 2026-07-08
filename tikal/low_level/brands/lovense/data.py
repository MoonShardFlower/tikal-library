"""
Lovense-specific toy data: model capabilities and per-model recommendations.

This module is the single place to edit when adding or adjusting a Lovense model. Every model is defined in
:data:`LOVENSE_TOY_SPECIFICATIONS`; the three public lookup tables are derived from it:

- :data:`LOVENSE_TOY_SPECIFICATIONS`: model name -> :class:`ToySpecification` (the single source of truth)
- :data:`LOVENSE_TOY_NAMES`: model name -> :class:`ToyCommands` (capability/command mapping)
- :data:`ROTATION_TOY_NAMES`: models that support changing the rotation direction
- :data:`MIN_SEGMENT_LENGTH`: suggested minimum interval between intensity changes (ms)

The brand-agnostic data structures these build on (``ToyData``, ``ToyCommands``, ``ToySpecification`` and the validation
exceptions) live in :mod:`tikal.low_level.toy_data`.
"""

from ...toy_data import ToyCommands, ToySpecification

#: Single source of truth for every supported Lovense model.
#:
#: Each entry bundles the model's capabilities, recommended playback interval, and rotation support into one
#: :class:`ToySpecification`. The public lookup tables (:data:`LOVENSE_TOY_NAMES`, :data:`ROTATION_TOY_NAMES`,
#: :data:`MIN_SEGMENT_LENGTH`) are derived from this dict.
#:
#: Models of different versions are treated the same (e.g., Lush 1, Lush 2, and Lush 3 all use the "Lush" key).
#: Some commands are uncertain and assumed based on similar toys. Please notify me if some commands don't work.
#:
#: Type:
#:     dict[str, ToySpecification]
#:
#: Example:
#:     ::
#:
#:         spec = LOVENSE_TOY_SPECIFICATIONS["Nora"]
#:         print(spec.commands.intensity1_name)   # "Vibration"
#:         print(spec.min_interval)               # 200
#:         print(spec.supports_rotation)          # True
LOVENSE_TOY_SPECIFICATIONS: dict[str, ToySpecification] = {
    "Solace": ToySpecification(
        ToyCommands("Thrust", "Thrusting", "Depth", "Depth"), min_interval=800
    ),
    "Sex Machine": ToySpecification(
        ToyCommands("Thrust", "Thrusting", "Depth", "Depth"), min_interval=800
    ),  # Commands unknown, assume it uses the same ones as Solace
    "Lush": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Ferri": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Nora": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Rotation", "Rotate"),
        min_interval=200,
        supports_rotation=True,
    ),
    "Osci": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Oscillation", "Oscillate"),
        min_interval=200,
    ),  # Unsure about the second command, Oscillate assumed
    "Mission": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Flexer": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Fingering", "Finger"), min_interval=200
    ),  # Second command unknown; I just assume 'Finger'
    "Gravity": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Thrust", "Thrusting"), min_interval=200
    ),
    "Dolce": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Vulse": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Tenera": ToySpecification(
        ToyCommands("Sucking", "Suck"), min_interval=400
    ),  # Command unknown, just assume 'Suck'
    "Lapis": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),  # Has 3 independent vibrators, no idea how to independently control them
    "Ambi": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Hyphy": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Exomoon": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Gush": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),  # Apparently, Oscillation cannot be controlled independently
    "Edge": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),  # Has 2 independent vibrators, no idea how to independently control them
    "Max": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Air", "Air:Level"), min_interval=200
    ),
    "Diamo": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Calor": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),  # Heat function control unknown
    "Ridge": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Rotation", "Rotate"),
        min_interval=200,
        supports_rotation=True,
    ),
    "Hush": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Domi": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Gemini": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),  # Has 2 independent vibrators, no idea how to independently control them
    "Lush Anal": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),
    "Spinel": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Thrust", "Thrusting"), min_interval=200
    ),  # Second command unknown, assume Thrusting
}


#: Mapping of Lovense toy model names to their command configurations.
#:
#: Derived from :data:`LOVENSE_TOY_SPECIFICATIONS`. Keys are model names, values are :class:`ToyCommands` objects
#: specifying what commands each toy supports.
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
LOVENSE_TOY_NAMES: dict[str, ToyCommands] = {
    name: spec.commands for name, spec in LOVENSE_TOY_SPECIFICATIONS.items()
}


#: List of toys (by model_name) that support rotation direction changes.
#:
#: Derived from :data:`LOVENSE_TOY_SPECIFICATIONS`. Toys in this list can use the ``rotate_change_direction()`` method
#: to toggle their rotation direction.
#:
#: Type:
#:     list[str]
#:
#: Example:
#:     ::
#:
#:         if toy.model_name in ROTATION_TOY_NAMES:
#:             await toy.rotate_change_direction()
ROTATION_TOY_NAMES: list[str] = [
    name for name, spec in LOVENSE_TOY_SPECIFICATIONS.items() if spec.supports_rotation
]


#: Maps toys to my suggested minimum segment length, meaning the minimum interval between intensity changes (in ms).
#:
#: Derived from :data:`LOVENSE_TOY_SPECIFICATIONS`. Especially useful if you want to implement any pattern
#: playback-related functionality. This is just a suggestion. You're free to use whatever you want.
#:
#: Type:
#:     dict[str, int]
#:
#: Example:
#:     ::
#:
#:         print(MIN_SEGMENT_LENGTH["Nora"])  # 200
MIN_SEGMENT_LENGTH: dict[str, int] = {
    name: spec.min_interval for name, spec in LOVENSE_TOY_SPECIFICATIONS.items()
}
