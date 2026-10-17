"""
MockEstimToys model data and fake devices. Each model is defined once, in :data:`MOCK_ESTIM_TOY_SPECIFICATIONS`. The
other tables derive from it:

- :data:`MOCK_ESTIM_TOY_NAMES`: model name -> :class:`~tikal.low_level.ToyCommands`
- :data:`MIN_SEGMENT_LENGTH`: model name -> recommended minimum interval between intensity changes, in ms
- :data:`MAX_INTENSITY`: the highest level of every model
- :data:`MOCK_ESTIM_DEVICES`: the fake devices discovery always finds
"""

from dataclasses import dataclass

from ...toy_data import ToyCommands, ToySpecification

#: Highest intensity level of every MockEstimToys model. The lowest is 0.
MAX_INTENSITY = 100


#: Single source of truth for every MockEstimToys model.
#:
#: - ``Thunder``: a single-channel device (one intensity).
#: - ``Lightning``: a dual-channel device (two independent intensities).
#:
#: Type:
#:     dict[str, ToySpecification]
MOCK_ESTIM_TOY_SPECIFICATIONS: dict[str, ToySpecification] = {
    "Thunder": ToySpecification(ToyCommands("Stimulation", "Channel1"), 100),
    "Lightning": ToySpecification(
        ToyCommands("Stimulation A", "Channel1", "Stimulation B", "Channel2"),
        100,
    ),
}


#: Mapping of MockEstimToys model names to their command configurations.
#: Derived from :data:`MOCK_ESTIM_TOY_SPECIFICATIONS`.
#:
#: Type:
#:     dict[str, ToyCommands]
MOCK_ESTIM_TOY_NAMES: dict[str, ToyCommands] = {
    name: spec.commands for name, spec in MOCK_ESTIM_TOY_SPECIFICATIONS.items()
}


#: Suggested minimum interval between intensity changes (in milliseconds), per model.
#: Derived from :data:`MOCK_ESTIM_TOY_SPECIFICATIONS`.
#:
#: Type:
#:     dict[str, int]
MIN_SEGMENT_LENGTH: dict[str, int] = {
    name: spec.min_interval for name, spec in MOCK_ESTIM_TOY_SPECIFICATIONS.items()
}


@dataclass(frozen=True)
class MockEstimDevice:
    """
    A fake MockEstimToys device.

    Attributes:
        toy_id: Unique identifier, e.g., ``"Thunder_ID"``.
        name: Human-readable name, e.g., ``"Thunder1"``.
        model_name: Its model, a key of :data:`MOCK_ESTIM_TOY_NAMES`.
    """

    toy_id: str
    name: str
    model_name: str


#: A set of fake devices the MockEstimToys brand always "discovers".
#:
#: Unlike Lovense (which advertises only a Bluetooth name and leaves the model unset), these fake devices self-identify
#: their model, so discovery can fill ``model_name`` in directly.
MOCK_ESTIM_DEVICES = [
    MockEstimDevice("Thunder_ID", "Thunder1", "Thunder"),
    MockEstimDevice("Lightning_ID", "Lightning1", "Lightning"),
]
