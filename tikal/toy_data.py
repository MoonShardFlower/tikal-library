from dataclasses import dataclass


class ValidationError(Exception):
    """Raised when a model_name is not a valid model name for this toy."""

    pass


@dataclass
class ToyData:
    """Base class for toy discovery data."""

    name: str
    toy_id: str
    model_name: str = ""


@dataclass
class LovenseData(ToyData):
    """Lovense-specific toy discovery data."""

    pass


@dataclass
class ToyCommands:
    """
    Command configuration for a Lovense toy model.
    Attributes:
        intensity1_name: Display name for the primary capability (e.g. "Vibration")
        intensity1_command: Command string for primary capability (e.g. "Vibrate")
        intensity2_name: Display name for secondary capability (e.g. "Rotation"), None if not available
        intensity2_command: Command string for secondary capability (e.g. "Rotate"), None if not available
    """

    intensity1_name: str
    intensity1_command: str
    intensity2_name: str | None = None
    intensity2_command: str | None = None


# Maps Lovense toy models to their respective command sets
# Models of different versions are treated the same (e.g., Lush1, Lush2, Lush3 all use "Lush")
LOVENSE_TOY_NAMES = {
    "Solace": ToyCommands("Thrust", "Thrusting", "Depth", "Depth"),
    "SexMachine": ToyCommands(
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
}

ROTATION_TOY_NAMES = ["Nora", "Ridge"]
