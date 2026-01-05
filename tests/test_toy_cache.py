import json
import os
import tempfile
import unittest

from lovense import ToyCache


class TestToyCache(unittest.TestCase):
    """Test suite for ToyCache class."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        # Create a temporary directory for test cache files
        self.test_dir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.test_dir, "test_cache.json")
        self.default_model = "DefaultModel"

    def tearDown(self):
        """Clean up after each test method."""
        # Remove test cache file and directory
        import shutil

        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_init_creates_cache_file(self):
        """Test that __init__ creates a cache file if it doesn't exist."""
        _ = ToyCache(self.cache_path, self.default_model, "")
        self.assertTrue(os.path.exists(self.cache_path))

    def test_init_creates_directory(self):
        """Test that __init__ creates the directory structure if it doesn't exist."""
        nested_path = os.path.join(self.test_dir, "subdir", "cache.json")
        _ = ToyCache(nested_path, self.default_model, "")
        self.assertTrue(os.path.exists(nested_path))

    def test_init_reads_existing_cache(self):
        """Test that __init__ reads an existing cache file."""
        test_data = {"LVS-A123": "Gush", "LVS-B456": "Edge"}
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f)

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")
        self.assertEqual(cache.get_model_name("LVS-B456"), "Edge")

    def test_init_with_empty_cache_path(self):
        """Test initialization with an empty cache path."""
        cache = ToyCache("", self.default_model, "")
        self.assertEqual(cache.get_model_name("any_name"), self.default_model)

    def test_get_model_name_returns_cached_value(self):
        """Test that get_model_name returns a cached value when it exists."""
        test_data = {"LVS-A123": "Gush"}
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f)

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")

    def test_get_model_name_returns_default_when_not_cached(self):
        """Test that get_model_name returns a default model when name not cached."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("LVS-UNKNOWN"), self.default_model)

    def test_update_adds_new_entry(self):
        """Test that update adds new entries to the cache."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        cache.update({"LVS-A123": "Gush"})
        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")

    def test_update_overwrites_existing_entry(self):
        """Test that update overwrites existing entries."""
        test_data = {"LVS-A123": "OldModel"}
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f)

        cache = ToyCache(self.cache_path, self.default_model, "")
        cache.update({"LVS-A123": "NewModel"})
        self.assertEqual(cache.get_model_name("LVS-A123"), "NewModel")

    def test_update_multiple_entries(self):
        """Test that update handles multiple entries at once."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        updates = {"LVS-A123": "Gush", "LVS-B456": "Edge", "LVS-C789": "Hush"}
        cache.update(updates)

        self.assertEqual(cache.get_model_name("LVS-A123"), "Gush")
        self.assertEqual(cache.get_model_name("LVS-B456"), "Edge")
        self.assertEqual(cache.get_model_name("LVS-C789"), "Hush")

    def test_update_with_empty_cache_path(self):
        """Test that update with empty cache_path doesn't raise an error."""
        cache = ToyCache("", self.default_model, "")
        cache.update({"LVS-A123": "Gush"})
        # Should not raise an exception

    def test_read_handles_corrupted_json(self):
        """Test that _read handles corrupted JSON gracefully."""
        # Write invalid JSON
        with open(self.cache_path, "w", encoding="utf-8") as f:
            f.write("{invalid json")

        # Should not raise exception, should initialize empty cache
        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("any_name"), self.default_model)

    def test_read_handles_non_dict_json(self):
        """Test that _read handles non-dict JSON gracefully."""
        # Write valid JSON but not a dict
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)

        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name("any_name"), self.default_model)

    def test_cache_persistence(self):
        """Test that a cache persists between instances."""
        cache1 = ToyCache(self.cache_path, self.default_model, "")
        cache1.update({"LVS-A123": "Gush"})

        # Create a new instance with the same cache path
        cache2 = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache2.get_model_name("LVS-A123"), "Gush")

    def test_empty_bluetooth_name(self):
        """Test behavior with an empty bluetooth name."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        self.assertEqual(cache.get_model_name(""), self.default_model)

    def test_special_characters_in_names(self):
        """Test handling of special characters in names."""
        cache = ToyCache(self.cache_path, self.default_model, "")
        special_name = "LVS-A123!@#$%"
        cache.update({special_name: "SpecialModel"})
        self.assertEqual(cache.get_model_name(special_name), "SpecialModel")


if __name__ == "__main__":
    unittest.main()
