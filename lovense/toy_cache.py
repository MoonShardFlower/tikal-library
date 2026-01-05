import json
from logging import getLogger
import os
import traceback


class ToyCache:

    def __init__(self, cache_path: str, default_model: str, logger_name: str):
        """
        ToyCache is a persistent storage mapping bluetooth names (e.g. "LVS-A123") to model names (e.g. "Gush") so users
        don't have to re-select models every time.
        Args:
            cache_path: Path to the JSON cache file
            default_model: Default model name to use if a bluetooth name is not found in the cache
            logger_name: Name of the logger to use. Empty string for root logger.
        """
        self._cache_path = cache_path
        self._default_model = default_model
        self._cache = dict()
        self._log = getLogger(logger_name)
        if cache_path:
            self._read()
        self._log.info(
            f"Initialized ToyCache with {len(self._cache)} entries from {cache_path}. Default model: {default_model}"
        )

    def update(self, updates: dict[str, str]) -> None:
        """
        Update specific entries in the cache. Existing entries are overwritten, new entries are added.
        Fails silently if an error occurs.
        Args:
            updates: Dictionary of toy names to model names to add/update
        """
        self._log.info(f"Updating ToyCache with updates={updates}")
        if not self._cache_path:
            return
        self._cache.update(updates)
        try:
            with open(self._cache_path, "w", encoding="utf-8") as file:
                json.dump(self._cache, file, indent=2)
        except Exception as e:
            self._log.warning(
                f"Error while updating ToyCache: {e} with details: {traceback.format_exc()}"
            )

    def get_model_name(self, bluetooth_name: str) -> str:
        """
        Get the cached model name for a toy.
        If the toy is not cached, it returns the default model name (set in the constructor).
        Args:
            bluetooth_name: Name of the toy (e.g. "LVS-A123")
        Returns:
            Model name (e.g. "Gush") if cached, otherwise default model name
        """
        name = self._cache.get(bluetooth_name, self._default_model)
        self._log.debug(f"ToyCache retrieved {name} for {bluetooth_name}")
        return name

    def _ensure_cache_exists(self) -> None:
        """Ensure the cache directory and file exist."""
        os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
        if not os.path.exists(self._cache_path):
            with open(self._cache_path, "w", encoding="utf-8") as file:
                json.dump({}, file)

    def _read(self):
        """Read the cache from the disk. Fails silently if the cache file cannot be read."""
        if not self._cache_path:
            self._log.warning(
                "No filepath to ToyCache given, ToyCache is non-functional"
            )
            return
        try:
            self._ensure_cache_exists()
            with open(self._cache_path, "r", encoding="utf-8") as file:
                data = json.load(file)
                if isinstance(data, dict):
                    self._log.debug(f"Found {len(data)} entries in ToyCache")
                    self._cache = data
        except Exception as e:
            self._log.warning(
                f"Error reading ToyCache: {e} with details: {traceback.format_exc()}"
            )
