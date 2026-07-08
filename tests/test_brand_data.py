"""
Guards the per-model brand data invariants.

Every brand defines its models **once** in a ``*_SPECIFICATIONS`` mapping of ``model_name -> ToySpecification`` and
derives its public lookup tables (commands, recommended interval, rotation support) from it. These tests lock in that
the derived tables stay consistent, so that a future change which re-introduces hand-maintained parallel dicts (the
historic hazard: a model present in the command table but missing from the interval table -> runtime ``KeyError`` in
``recommended_min_interval``) is caught here instead of in production.
"""

import unittest

from tikal.low_level import (
    BRANDS,
    LOVENSE_TOY_NAMES,
    LOVENSE_TOY_SPECIFICATIONS,
    MIN_SEGMENT_LENGTH,
    ROTATION_TOY_NAMES,
    ToyCommands,
    ToySpecification,
)
from tikal.low_level.brands.mock_estim.data import (
    MIN_SEGMENT_LENGTH as MOCK_MIN_SEGMENT_LENGTH,
)
from tikal.low_level.brands.mock_estim.data import (
    MOCK_ESTIM_TOY_NAMES,
    MOCK_ESTIM_TOY_SPECIFICATIONS,
)


class TestLovenseBrandData(unittest.TestCase):
    """The Lovense lookup tables are derived from LOVENSE_TOY_SPECIFICATIONS and stay in sync."""

    def test_derived_tables_cover_exactly_the_same_models(self):
        spec_models = set(LOVENSE_TOY_SPECIFICATIONS)
        self.assertEqual(set(LOVENSE_TOY_NAMES), spec_models)
        self.assertEqual(set(MIN_SEGMENT_LENGTH), spec_models)

    def test_commands_match_the_specification(self):
        for name, spec in LOVENSE_TOY_SPECIFICATIONS.items():
            self.assertIs(LOVENSE_TOY_NAMES[name], spec.commands)

    def test_min_interval_matches_the_specification(self):
        for name, spec in LOVENSE_TOY_SPECIFICATIONS.items():
            self.assertEqual(MIN_SEGMENT_LENGTH[name], spec.min_interval)

    def test_rotation_list_matches_the_specification(self):
        expected = {
            name
            for name, spec in LOVENSE_TOY_SPECIFICATIONS.items()
            if spec.supports_rotation
        }
        self.assertEqual(set(ROTATION_TOY_NAMES), expected)
        # Every rotation model is a real model.
        self.assertTrue(expected.issubset(set(LOVENSE_TOY_NAMES)))

    def test_specification_types(self):
        for name, spec in LOVENSE_TOY_SPECIFICATIONS.items():
            self.assertIsInstance(spec, ToySpecification)
            self.assertIsInstance(spec.commands, ToyCommands)
            self.assertIsInstance(spec.min_interval, int)
            self.assertGreater(spec.min_interval, 0)


class TestMockEstimBrandData(unittest.TestCase):
    """The MockEstimToys lookup tables are derived from MOCK_ESTIM_TOY_SPECIFICATIONS and stay in sync."""

    def test_derived_tables_cover_exactly_the_same_models(self):
        spec_models = set(MOCK_ESTIM_TOY_SPECIFICATIONS)
        self.assertEqual(set(MOCK_ESTIM_TOY_NAMES), spec_models)
        self.assertEqual(set(MOCK_MIN_SEGMENT_LENGTH), spec_models)

    def test_values_match_the_specification(self):
        for name, spec in MOCK_ESTIM_TOY_SPECIFICATIONS.items():
            self.assertIs(MOCK_ESTIM_TOY_NAMES[name], spec.commands)
            self.assertEqual(MOCK_MIN_SEGMENT_LENGTH[name], spec.min_interval)


class TestEveryAdvertisedModelResolves(unittest.TestCase):
    """
    Every model listed in BRANDS resolves through all per-model lookups without KeyError.

    This is the end-to-end version of the invariant: BRANDS is what callers pick a model_name from, and every one of
    those names must have both a command mapping and a recommended interval.
    """

    def test_lovense_models_resolve(self):
        for model in BRANDS["Lovense"]:
            self.assertIn(model, LOVENSE_TOY_NAMES)
            self.assertIn(model, MIN_SEGMENT_LENGTH)

    def test_mock_estim_models_resolve(self):
        for model in BRANDS["MockEstimToys"]:
            self.assertIn(model, MOCK_ESTIM_TOY_NAMES)
            self.assertIn(model, MOCK_MIN_SEGMENT_LENGTH)


if __name__ == "__main__":
    unittest.main()
