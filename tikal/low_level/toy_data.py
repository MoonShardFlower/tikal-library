"""
Low-Level API: brand-agnostic data types, also used by the High-Level API.

- :class:`ToyData`: a discovered toy.
- :class:`ToyCommands`: a model's capabilities and the commands that drive them.
- :class:`ToySpecification`: everything a brand defines per model.
- :func:`carried_over_capabilities`: which capabilities keep working across a model change.
- :class:`ValidationError`, :class:`InvalidModelError`, :class:`BadModelError`: a model name was rejected.

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
    The model name is valid, but the toy refused the model's commands.

    1) A valid, but wrong model_name is being set
    2) the commands being incorrect -> the Library does not handle this model correctly.
        Please contact the library maintainer in this case.

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
        name: Read-only human-readable identifier for the toy, e.g., the Bluetooth name "LVS-B12".
        toy_id: Read-only unique identifier for the toy, e.g., the Bluetooth address "DC:F5:05:A3:6D:1E".
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
        """Human-readable name, e.g., the Bluetooth name ``"LVS-Z36D"``. Read-only."""
        return self._name

    @property
    def toy_id(self) -> str:
        """Unique identifier, e.g., the Bluetooth address ``"DC:F5:05:A3:6D:1E"``. Read-only."""
        return self._toy_id

    @property
    def model_name(self) -> str:
        """Model name, e.g., ``"Nora"``. Empty if unknown. Assigning a non-str raises TypeError."""
        return self._model_name

    @model_name.setter
    def model_name(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("model_name must be str")
        self._model_name = value

    @property
    def brand(self) -> str:
        """Brand, e.g., ``"Lovense"``. Read-only."""
        return self._brand


@dataclass
class ToyCommands:
    """
    A model's capabilities: their display names and the commands that drive them.

    Every model has a primary capability (``intensity1``), some have a secondary one (``intensity2``).

    Example::

            # Check capabilities
            commands = LOVENSE_TOY_NAMES["Nora"]
            print(f"{commands.intensity1_name}: {commands.intensity1_command}")
            if commands.intensity2_name:
                print(f"{commands.intensity2_name}: {commands.intensity2_command}")
    """

    #: Display name of the primary capability, e.g., ``"Vibration"``, ``"Thrust"``.
    intensity1_name: str
    #: Command of the primary capability, e.g., ``"Vibrate"``, ``"Thrusting"``.
    intensity1_command: str
    #: Display name for the secondary capability, or None if the toy has only one capability (e.g., "Rotation", "Air").
    intensity2_name: str | None = None
    #: Command for the secondary capability, or None if the toy has only one capability (e.g., "Rotate", "Air:Level").
    intensity2_command: str | None = None


def carried_over_capabilities(old: ToyCommands, new: ToyCommands) -> tuple[bool, bool]:
    """
    Which capabilities keep working across a change of model name.

    - A capability carries over if both models drive it with the same command. The change only relabels it (e.g.,
      Solace to Sex Machine).
    - Otherwise, the new model replaces its command (Lush ``Vibrate``, Solace ``Thrusting``) or drops it (Nora's
      ``Rotate`` has no Lush counterpart). If it is running, it has to be switched off through the old command:
      nothing addresses it after the change.

    Args:
        old: Command mapping of the model the toy currently has.
        new: Command mapping of the model it is changing to.

    Returns:
        tuple[bool, bool]: ``(keeps_intensity1, keeps_intensity2)``. ``True`` means the capability keeps its level.
        ``False`` means the new model has different capabilities.

    Example::

        carried_over_capabilities(LOVENSE_TOY_NAMES["Nora"], LOVENSE_TOY_NAMES["Lush"])  # (True, False)
    """
    return (
        old.intensity1_command == new.intensity1_command,
        old.intensity2_command == new.intensity2_command,
    )


@dataclass(frozen=True)
class ToySpecification:
    """
    Everything a brand defines per model.
    A brand derives its lookup tables from one ``model name -> ToySpecification`` mapping.

    Example::

        ToySpecification(
            ToyCommands("Vibration", "Vibrate", "Rotation", "Rotate"), min_interval=200, supports_rotation=True
        )
    """

    #: The model's capability/command mapping (see :class:`ToyCommands`).
    commands: ToyCommands
    #: Recommended minimum interval between intensity changes in ms, e.g., for pattern segments. Only a suggestion.
    min_interval: int
    #: Whether the model can change its rotation direction. Defaults to ``False``.
    supports_rotation: bool = False
