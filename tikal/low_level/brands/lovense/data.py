"""
Lovense-specific toy data: model capabilities and per-model recommendations.

This module is the single place to edit when adding or adjusting a Lovense model. Every model is defined in
:data:`LOVENSE_TOY_SPECIFICATIONS`; the three public lookup tables are derived from it:

- :data:`LOVENSE_TOY_SPECIFICATIONS`: model name -> :class:`ToySpecification` (the single source of truth)
- :data:`LOVENSE_TOY_NAMES`: model name -> :class:`ToyCommands` (capability/command mapping)
- :data:`ROTATION_TOY_NAMES`: models that support changing the rotation direction
- :data:`MIN_SEGMENT_LENGTH`: suggested minimum interval between intensity changes (ms)

:data:`LOVENSE_UUID_PREFIXES` maps the start of the service UUID a toy advertises to these model names.

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
        ToyCommands("Vibration", "Vibrate1", "Oscillation", "Vibrate2"),
        min_interval=200,
    ),
    "Mission": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Flexer": ToySpecification(
        ToyCommands("Vibration", "Vibrate", "Fingering", "Finger"), min_interval=200
    ),  # Second command unknown; I just assume 'Finger'
    "Gravity": ToySpecification(
        ToyCommands("Vibration", "Vibrate1", "Thrust", "Vibrate2"), min_interval=200
    ),
    "Dolce": ToySpecification(
        ToyCommands("Vibration1", "Vibrate1", "Vibration2", "Vibrate2"),
        min_interval=200,
    ),
    "Vulse": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Tenera": ToySpecification(ToyCommands("Sucking", "Vibrate"), min_interval=400),
    "Lapis": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),  # Has 3 independent vibrators, no idea how to independently control them
    "Ambi": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Hyphy": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Exomoon": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Gush": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Edge": ToySpecification(
        ToyCommands("Vibration1", "Vibrate1", "Vibration2", "Vibrate2"),
        min_interval=200,
    ),
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
        ToyCommands("Vibration1", "Vibrate1", "Vibration2", "Vibrate2"),
        min_interval=200,
    ),
    "Lush Anal": ToySpecification(
        ToyCommands("Vibration", "Vibrate"), min_interval=200
    ),
    "Spinel": ToySpecification(
        ToyCommands("Vibration", "Vibrate1", "Thrust", "Vibrate2"), min_interval=200
    ),
    "Velvo": ToySpecification(
        ToyCommands("Vibration1", "Vibrate1", "Vibration2", "Vibrate2"),
        min_interval=200,
    ),
    "Fizz": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
    "Glint": ToySpecification(ToyCommands("Vibration", "Vibrate"), min_interval=200),
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


#: Maps the start of a Lovense service UUID to the model name (a key of :data:`LOVENSE_TOY_SPECIFICATIONS`).
#:
#: A Lovense service UUID looks like ``42410001-0023-4bd4-bbd5-a6920e4c5653``. Its first four hex digits differ per
#  model. Most toys advertise that UUID, so discovery can tell the model before connecting.
#:
#: Derived from the device configuration of Buttplug (https://github.com/buttplugio/buttplug),
#: Copyright (c) 2016-2023, Nonpolynomial Labs, LLC, under the BSD 3-Clause license.
#: ``4241`` (Solace Pro) and ``455a`` (Gush 2) are confirmed on my own toys. The others are assumed as working.
#:
#: Type:
#:     dict[str, str]
#:
#: Example:
#:     ::
#:
#:         print(LOVENSE_UUID_PREFIXES["4241"])  # "Solace"
LOVENSE_UUID_PREFIXES: dict[str, str] = {
    "4130": "Nora",
    "414e": "Lush Anal",
    "4230": "Max",
    "4241": "Solace",  # Solace Pro
    "4330": "Nora",
    "4341": "Mission",  # Mission 2
    "4342": "Velvo",
    "4541": "Gravity",
    "4542": "Hyphy",
    "4544": "Gush",
    "4549": "Flexer",
    "454c": "Ridge",
    "455a": "Gush",  # Gush 2
    "4630": "Sex Machine",
    "4653": "Sex Machine",  # Mini Sex Machine
    "4830": "Solace",
    "4a30": "Dolce",
    "4c30": "Ambi",
    "4e30": "Gemini",
    "4f30": "Osci",
    "4f43": "Osci",  # Osci 3
    "5030": "Edge",
    "5130": "Tenera",
    "5142": "Fizz",
    "5230": "Diamo",
    "5330": "Lush",
    "5344": "Vulse",
    "5430": "Calor",
    "5530": "Lapis",
    "5630": "Mission",
    "5730": "Domi",
    "5744": "Spinel",
    "5830": "Ferri",
    "5a30": "Hush",
}
