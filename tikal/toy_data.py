"""
Data structures and constants for toy device management.

Part of both the Low-Level and High-Level API.
This module defines the data classes and configuration used throughout the toy control system. It provides:

- Exception classes for validation errors (raised if model_name is invalid)
- ToyData returned by the connection builder after discovery
- Lookup tables mapping model names to their capabilities

Constants:
    LOVENSE_TOY_NAMES (dict[str, ToyCommands]): Maps Lovense toy model names to their command configurations.
     Keys are model names (e.g., "Nora", "Lush"), values are ToyCommands objects defining the toy's capabilities.

    ROTATION_TOY_NAMES (list[str]): List of Lovense toy model names that support rotation direction changes.
"""

from dataclasses import dataclass


class ValidationError(Exception):
    """
    Exception raised when a model_name is invalid.

    This exception is raised when attempting to set a model name that is not recognized.
    Can be raised during toy initialization or when setting a toy's model name.

    Example:
        ::

            try:
                toy.set_model_name("InvalidModel")
            except ValidationError as e:
                print(f"Invalid model: {e}")
    """

    pass


class InvalidModelError(ValidationError):
    """
    Exception raised when a model_name is invalid.

    This exception is raised when attempting to set a model name that is not recognized.
    Can be raised during toy initialization or when setting a toy's model name.

    Example:
        ::

            try:
                toy.set_model_name("InvalidModel")
            except InvalidModelError as e:
                print(f"Invalid model: {e}")
    """

    pass


class BadModelError(ValidationError):
    """
    Exception raised when a model_name is valid, but its associated commands are not accepted by the toy.

    This exception can mean two things:
        1) A valid, but wrong model_name is being set
        2) the commands being incorrect -> the Library does not handle this model correctly. Please contact the library maintainer in this case.
    Can be raised during toy initialization or when setting a toy's model name.
    """

    pass


@dataclass
class ToyData:
    """
    Base class for toy discovery data.

    Contains the information needed to identify and connect to a toy device.
    You shouldn't need to instantiate this class yourself. ``ConnectionBuilder.discover_toys()`` creates instances of
    this class for you.

    Properties:
        name: Read-only human-readable identifier for the toy. For Bluetooth toys, this is the Bluetooth name (e.g., "LVS-B12").
        toy_id: Read-only unique identifier for the toy. For Bluetooth toys, this is the Bluetooth address (e.g., "DC:F5:05:A3:6D:1E").
        model_name: Model name of the toy (e.g., "Lush"). For Lovense toys, this is empty and must be set manually.
        brand: Read-only brand name of the toy (e.g., "Lovense").

    Example:
        ::

            # Created during discovery
            print(lovense_data.name)  # "LVS-Z36D"
            print(lovense_data.toy_id)  # "DC:F5:05:A3:6D:1E"
            print(lovense_data.model_name)  # ""

            # User selects model
            lovense_data.model_name = "Nora"
    """

    _name: str
    _toy_id: str
    _model_name: str
    _brand: str

    def __post_init__(self):
        if not isinstance(self._name, str):
            raise TypeError("name must be str")
        if not isinstance(self._toy_id, str):
            raise TypeError("toy_id must be str")
        if not isinstance(self._model_name, str):
            raise TypeError("model_name must be str")
        if not isinstance(self._brand, str):
            raise TypeError("brand must be str")

    @property
    def name(self) -> str:
        return self._name

    @property
    def toy_id(self) -> str:
        return self._toy_id

    @property
    def model_name(self) -> str:
        return self._model_name

    @model_name.setter
    def model_name(self, value: str):
        if not isinstance(value, str):
            raise TypeError("model_name must be str")
        self._model_name = value

    @property
    def brand(self) -> str:
        return self._brand


@dataclass
class ToyCommands:
    """
    Command configuration for a toy model.

    Defines the available capabilities for a specific toy model. Provides names for user display and commands for
    protocol communication. You shouldn't need to instantiate this class, but if you use LOVENSE_TOY_NAMES you will
    use instances of this class.

    Attributes:
        intensity1_name: Display name for the primary capability shown to users (e.g., "Vibration", "Thrust").
        intensity1_command: Command string for the primary capability sent to the toy (e.g., "Vibrate", "Thrusting").
        intensity2_name: Display name for the secondary capability, or None if the toy has only one capability (e.g., "Rotation", "Air").
        intensity2_command: Command string for the secondary capability, or None if the toy has no secondary capability (e.g., "Rotate", "Air:Level").

    Example:
        ::

            # Check capabilities
            commands = LOVENSE_TOY_NAMES["Nora"]
            print(f"{commands.intensity1_name}: {commands.intensity1_command}")
            if commands.intensity2_name:
                print(f"{commands.intensity2_name}: {commands.intensity2_command}")
    """

    intensity1_name: str
    intensity1_command: str
    intensity2_name: str | None = None
    intensity2_command: str | None = None


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
}

#: Mapping of brands to all their toy models
#: This dictionary defines all valid model_names (list[str]) for each brand (str)
BRANDS = {
    "Lovense": [toy for toy in LOVENSE_TOY_NAMES.keys()],
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
