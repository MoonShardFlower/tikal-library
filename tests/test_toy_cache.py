import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tikal.high_level import ToyCache


class TestToyCache(unittest.TestCase):
    """Tests of ToyCache."""

    def setUp(self):
        # Create a temporary directory for test cache files
        self.test_dir = Path(tempfile.mkdtemp())
        self.cache_path = self.test_dir / "test_cache.json"
        self.default_model = "DefaultModel"

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_init_creates_cache_file(self):
        """The constructor creates a missing cache file."""
        _ = ToyCache(self.cache_path, self.default_model, "")
        self.assertTrue(self.cache_path.exists())

    def test_init_creates_directory(self):
        """The constructor creates missing directories."""
        nested_path = self.test_dir / "subdir" / "cache.json"
        _ = ToyCache(nested_path, self.default_model, "")
        self.assertTrue(nested_path.exists())

    def test_init_reads_existing_cache(self):
        """The constructor loads an existing cache file."""
        test_data = {"LVS-A123": "Gush", "LVS-B456": "Edge"}
        self.cache_path.write_text(json.dumps(test_data), encoding="utf-8")

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")
        self.assertEqual(cache.get_model_name("LVS-B456"), "Edge")

    def test_init_with_empty_cache_path(self):
        """Without a file (Path()), unknown names give the default model."""
        cache = ToyCache(Path(), self.default_model, "")
        self.assertEqual(cache.get_model_name("any_name"), self.default_model)

    def test_get_model_name_returns_cached_value(self):
        """get_model_name returns the cached model."""
        test_data = {"LVS-A123": "Gush"}
        self.cache_path.write_text(json.dumps(test_data), encoding="utf-8")

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")

    def test_get_model_name_returns_default_when_not_cached(self):
        """get_model_name returns the default model for an unknown name."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("LVS-UNKNOWN"), self.default_model)

    def test_update_adds_new_entry(self):
        """update adds an entry."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        cache.update({"LVS-A123": "Gush"})
        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")

    def test_update_overwrites_existing_entry(self):
        """update replaces the model of a known name."""
        test_data = {"LVS-A123": "OldModel"}
        self.cache_path.write_text(json.dumps(test_data), encoding="utf-8")

        cache = ToyCache(self.cache_path, self.default_model, "")
        cache.update({"LVS-A123": "NewModel"})
        self.assertEqual(cache.get_model_name("LVS-A123"), "NewModel")

    def test_update_multiple_entries(self):
        """update takes several entries at once."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        updates = {"LVS-A123": "Gush", "LVS-B456": "Edge", "LVS-C789": "Hush"}
        cache.update(updates)

        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")
        self.assertEqual(cache.get_model_name("LVS-B456"), "Edge")
        self.assertEqual(cache.get_model_name("LVS-C789"), "Hush")

    def test_update_with_empty_cache_path(self):
        """update works without a file (Path())."""
        cache = ToyCache(Path(), self.default_model, "")
        cache.update({"LVS-A123": "Gush"})
        # must not raise

    def test_read_handles_corrupted_json(self):
        """A file with invalid JSON gives an empty cache, without raising."""
        # Write invalid JSON
        self.cache_path.write_text("{invalid json", encoding="utf-8")

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("any_name"), self.default_model)

    def test_read_handles_non_dict_json(self):
        """A file that holds no JSON object gives an empty cache."""
        # Write valid JSON but not a dict
        self.cache_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("any_name"), self.default_model)

    def test_read_drops_non_string_values(self):
        """
        A cached model name that is not a str never reaches the caller.

        It would be assigned to ToyData.model_name during discovery (in a Bluetooth callback), where the TypeError would
        be far from the bad entry that caused it.
        """
        self.cache_path.write_text(
            json.dumps(
                {
                    "LVS-GOOD": "Gush",
                    "LVS-INT": 123,
                    "LVS-NULL": None,
                    "LVS-LIST": ["Gush"],
                    "LVS-DICT": {"model": "Gush"},
                    "LVS-BOOL": True,
                }
            ),
            encoding="utf-8",
        )

        cache = ToyCache(self.cache_path, self.default_model, "")

        self.assertEqual(cache.get_model_name("LVS-GOOD"), "Gush")  # good entry kept
        for rejected in ("LVS-INT", "LVS-NULL", "LVS-LIST", "LVS-DICT", "LVS-BOOL"):
            self.assertEqual(cache.get_model_name(rejected), self.default_model)
            self.assertIsInstance(cache.get_model_name(rejected), str)

    def test_read_drops_non_string_keys(self):
        """JSON object keys are always strings, but the file may have been written by something else."""
        self.cache_path.write_text('{"1": "Gush"}', encoding="utf-8")
        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(
            cache.get_model_name("1"), "Gush"
        )  # a numeric-looking str is fine

        # A dict that survived a non-JSON round trip (e.g. written by a different tool) must not poison the cache.
        cache.update({2: "Edge"})  # type: ignore[dict-item]
        self.assertEqual(cache.get_model_name(2), self.default_model)  # type: ignore[arg-type]

    def test_update_drops_non_string_values(self):
        """update() is typed str -> str, but a caller ignoring that must not poison the cache either."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        cache.update({"LVS-A123": "Gush", "LVS-BAD": None})  # type: ignore[dict-item]

        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")
        self.assertEqual(cache.get_model_name("LVS-BAD"), self.default_model)

        # The bad entry must not have been persisted either.
        reloaded = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(reloaded.get_model_name("LVS-A123"), "Gush")
        self.assertEqual(reloaded.get_model_name("LVS-BAD"), self.default_model)

    def test_non_string_default_model_falls_back_to_empty(self):
        """The default is handed out for every unknown toy, so it has to be a str as well."""
        cache = ToyCache(self.cache_path, None, "")  # type: ignore[arg-type]
        self.assertEqual(cache.get_model_name("LVS-UNKNOWN"), "")

    def test_cache_persistence(self):
        """A new ToyCache on the same file sees earlier updates."""
        cache1 = ToyCache(self.cache_path, self.default_model, "")
        cache1.update({"LVS-A123": "Gush"})

        # Create a new instance with the same cache path
        cache2 = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache2.get_model_name("LVS-A123"), "Gush")

    def test_empty_bluetooth_name(self):
        """An empty Bluetooth name gives the default model."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name(""), self.default_model)

    def test_special_characters_in_names(self):
        """Names with special characters work like any other."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        special_name = "LVS-A123!@#$%"
        cache.update({special_name: "SpecialModel"})
        self.assertEqual(cache.get_model_name(special_name), "SpecialModel")


if __name__ == "__main__":
    unittest.main()
