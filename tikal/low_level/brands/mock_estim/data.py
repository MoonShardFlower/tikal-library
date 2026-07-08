"""
MockEstimToys-specific toy data: model capabilities and the fake devices this brand "discovers".

``MockEstimToys`` is a fictional brand used to explore the library's architecture without any real hardware. It speaks
a tiny line protocol simulated by :class:`tikal.low_level.transport.MockTransport`. This module is the single place to
edit when adjusting a mock model. Every model is defined **once** in :data:`MOCK_ESTIM_TOY_SPECIFICATIONS`; the public
lookup tables are derived from it (mirroring the Lovense brand) so they can never drift apart. It defines:
- :data:`MOCK_ESTIM_TOY_SPECIFICATIONS`: model name -> :class:`ToySpecification` (the single source of truth)
- :data:`MOCK_ESTIM_TOY_NAMES`: model name -> :class:`ToyCommands` (capability/command mapping)
- :data:`MAX_INTENSITY`: the intensity scale of the brand (0 - MAX_INTENSITY)
- :data:`MIN_SEGMENT_LENGTH`: suggested minimum interval between intensity changes (ms)
- :data:`MOCK_ESTIM_DEVICES`: the fixed set of fake devices the brand always advertises

The brand-agnostic data structures these build on (``ToyData``, ``ToyCommands``, ``ToySpecification`` and the validation
exceptions) live in :mod:`tikal.low_level.toy_data`.
"""

from dataclasses import dataclass

from ...toy_data import ToyCommands, ToySpecification

#: Maximum intensity value for MockEstimToys
MAX_INTENSITY = 100


#: Single source of truth for every MockEstimToys model.
#:
#: - ``Thunder``: a single-channel device (one intensity).
#: - ``Lightning``: a dual-channel device (two independent intensities).
#:
#: The public lookup tables below are derived from this dict. MockEstimToys have no rotation, so every spec leaves
#: ``supports_rotation`` at its ``False`` default.
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
    """One fake device advertised by the MockEstimToys brand."""

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
