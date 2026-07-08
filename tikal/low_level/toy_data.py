"""
Part of both the Low-Level and High-Level API: brand-agnostic data structures for toy device management.

This module defines the shared data classes used throughout the toy control system:
- Exception classes for validation errors (raised if model_name is invalid)
- :class:`ToyData` returned by the connection builder after discovery
- :class:`ToyCommands` describing a toy model's capabilities

Brand-specific model data (e.g., Lovense's model -> command mapping in ``LOVENSE_TOY_NAMES``) lives in the brand
subpackages under ``tikal/low_level/brands/``. The ``BRANDS`` mapping of brand name -> supported model names is built
from the brand registry in ``tikal/low_level/brands/__init__.py``.
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

    def __post_init__(self) -> None:
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
    def model_name(self, value: str) -> None:
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


@dataclass(frozen=True)
class ToySpecification:
    """
    The complete per-model definition of a toy: its capabilities plus behavior hints.

    Lets a brand keep its per-model data in **one** place. The brand's public lookup tables (command mapping,
    recommended interval, rotation support) are then derived from a mapping of ``model_name -> ToySpecification``

    Attributes:
        commands: The model's capability/command mapping (see :class:`ToyCommands`).
        min_interval: Recommended minimum interval between intensity changes, in milliseconds. Callers are free to ignore it.
        supports_rotation: Whether the model can change its rotation direction. Defaults to ``False``.

    Example:
        ::

            spec = ToySpecification(
                ToyCommands("Vibration", "Vibrate", "Rotation", "Rotate"),
                min_interval=200,
                supports_rotation=True,
            )
    """

    commands: ToyCommands
    min_interval: int
    supports_rotation: bool = False
