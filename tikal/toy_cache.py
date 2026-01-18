"""
Persistent storage for toy model name mappings.

This module provides the ToyCache class, which maintains a persistent mapping between bluetooth_names and model_names.
This allows you to remember which model each discovered toy is between sessions, so the user does not need to
re-select models every time. The cache is stored as a JSON file on the disk.

Note:
    The cache file is created if it doesn't exist.
    ToyCache does not raise exceptions. Encountered errors are logged, and ToyCache fails silently.
    :class:`ToyHub` (part of the High-Level-API) uses ToyCache internally.
"""

import json
from logging import getLogger
from pathlib import Path
import traceback


class ToyCache:
    """
    Persistent storage mapping Bluetooth names to toy model names.

    Maintains a JSON file on the disk that maps Bluetooth device names (e.g., "LVS-A123") to their corresponding
    model names (e.g., "Nora"). This eliminates the need for users to manually identify toys every time they connect.

    The cache is loaded on initialization. Encountered errors are logged, and ToyCache fails silently.

    Args:
        cache_path: Path to the JSON cache file. If the path is empty (=``Path()``), the cache operates with no persistence.
        default_model: Default model name to return when a Bluetooth name is not found in the cache.
        logger_name: Name of the logger to use. Use empty string for root logger.

    Example::

        from pathlib import Path
        from toy_cache import ToyCache

        cache = ToyCache(
            cache_path=Path("./toys.json"),
            default_model="unknown",
            logger_name="myapp"
        )

        # First time: toy not in cache
        model = cache.get_model_name("LVS-A123")  # Returns "unknown"

        # User selects a model, you can update the cache
        cache.update({"LVS-A123": "Nora"})

        # Next time: toy is in cache
        model = cache.get_model_name("LVS-A123")  # Returns "Nora"
    """

    def __init__(self, cache_path: Path, default_model: str, logger_name: str):
        self._cache_path = cache_path
        self._default_model = default_model
        self._cache = dict()
        self._log = getLogger(logger_name)
        if cache_path.name:
            self._read()
        self._log.info(
            f"Initialized ToyCache with {len(self._cache)} entries from {cache_path}. Default model: {default_model}"
        )

    def update(self, updates: dict[str, str]) -> None:
        """
        Update cache entries and persist to disk.

        Merges the provided updates into the cache, overwriting existing entries and adding new ones.
        The updated cache is immediately written to the disk.

        Args:
            updates: Dictionary mapping Bluetooth names (keys) to model names (values)

        Example::

                # Update single entry
                cache.update({"LVS-A123": "Nora"})

                # Update multiple entries
                cache.update({
                    "LVS-B456": "Lush",
                    "LVS-C789": "Max"
                })
                # Change existing model
                cache.update({"LVS-A123": "Ridge"})  # Overwrites "Nora"

        Note:
            Fails silently if disk I/O errors occur. Errors are logged and do not raise exceptions.
            The in-memory cache is always updated even if writing to disk fails.
            If the cache was initialized with an empty path (``Path()``), no disk write is attempted.
        """
        self._log.info(f"Updating ToyCache with updates={updates}")
        if not self._cache_path.name:
            return
        self._cache.update(updates)
        try:
            self._cache_path.write_text(
                json.dumps(self._cache, indent=2), encoding="utf-8"
            )
        except Exception as e:
            self._log.warning(
                f"Error while updating ToyCache: {e} with details: {traceback.format_exc()}"
            )

    def get_model_name(self, bluetooth_name: str) -> str:
        """
        Retrieve the cached model name for a Bluetooth device.

        Looks up the model name associated with the given Bluetooth name.
        If the name is not found in the cache, it returns the default model name specified during initialization.

        Args:
            bluetooth_name: Bluetooth device name to look up (e.g., "LVS-A123"). This is obtained from ``ToyData.name`` during discovery.

        Returns:
            str: The cached model name (e.g., "Nora") if found, otherwise the default model name.

        Example::

            # Check if the toy is cached
            model = cache.get_model_name("LVS-A123")
            if model == default_model_name:
                print("Unknown toy")
            else:
                print(f"Known toy: {model}")
        """
        name = self._cache.get(bluetooth_name, self._default_model)
        self._log.debug(f"ToyCache retrieved {name} for {bluetooth_name}")
        return name

    def _ensure_cache_exists(self) -> None:
        """
        Ensure the cache directory and file exist.

        Creates the parent directory structure if it doesn't exist and initializes an empty cache file if one isn't present.

        Raises:
            any exception raised by the filesystem
        """
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._cache_path.exists():
            self._cache_path.write_text("{}", encoding="utf-8")

    def _read(self):
        """
        Load the cache from the disk.

        Reads the JSON cache file and populates the in-memory cache dictionary. Creates the cache file if it doesn't exist.
        """
        if not self._cache_path.name:
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
