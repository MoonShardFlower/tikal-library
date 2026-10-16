"""
Persistent mapping of Bluetooth name -> model name, stored as a JSON file. Public as ``tikal.high_level.ToyCache``.

The High-Level ``ToyHub`` and the WebSocket server use it to fill in the model name of discovered toys, so the user
selects a toy's model only once.
"""

import json
import traceback
from logging import getLogger
from pathlib import Path
from typing import Any


class ToyCache:
    """
    Remembers each toy's model name by its Bluetooth name, in a JSON file.

    - Never raises: errors are logged, and the cache keeps working in memory.
    - Keeps only ``str -> str`` entries, from the file and from :meth:`update`.
    - Creates the file and its directories if missing.

    Args:
        cache_path: The JSON file. ``Path()`` keeps the cache in memory only.
        default_model: What :meth:`get_model_name` returns for an unknown toy. A non-str becomes ``""``.
        logger_name: Name of the logger. Empty string for the root logger.

    Example::

        cache = ToyCache(Path("./toys.json"), default_model="", logger_name="my_app")
        cache.get_model_name("LVS-A123")  # "" the first time
        cache.update({"LVS-A123": "Nora"})
        cache.get_model_name("LVS-A123")  # "Nora", also in later sessions
    """

    def __init__(self, cache_path: Path, default_model: str, logger_name: str):
        self._log = getLogger(logger_name)
        self._cache_path = cache_path
        if not isinstance(default_model, str):
            self._log.warning(
                f"default_model must be a str, got '{type(default_model)}'. Falling back to an empty model name."
            )
            default_model = ""
        self._default_model = default_model
        self._cache: dict[str, str] = {}
        if cache_path.name:
            self._read()
        self._log.info(
            f"Initialized ToyCache with {len(self._cache)} entries from {cache_path}. Default model: {default_model}"
        )

    def update(self, updates: dict[str, str]) -> None:
        """
        Add or replace entries and write the cache to its file.

        Entries that are not ``str -> str`` are dropped and logged. If writing fails, the error is logged and the
        entries stay in memory.

        Args:
            updates: Bluetooth name -> model name.
        """
        self._log.info(f"Updating ToyCache with updates={updates}")
        self._cache.update(self._only_string_entries(updates, "update"))
        if not self._cache_path.name:
            return
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
        The model name of a toy.

        Args:
            bluetooth_name: The toy's Bluetooth name (``ToyData.name``), e.g., ``"LVS-A123"``.

        Returns:
            The cached model name, else the default model.
        """
        name = self._cache.get(bluetooth_name, self._default_model)
        self._log.debug(f"ToyCache retrieved {name} for {bluetooth_name}")
        return name

    def _ensure_cache_exists(self) -> None:
        """
        Create the file (as ``{}``) and its directories if missing. Does nothing if the file/directories already exist.

        Raises:
            OSError: Creating them failed.
        """
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._cache_path.exists():
            self._cache_path.write_text("{}", encoding="utf-8")

    def _only_string_entries(
        self, entries: dict[Any, Any], context: str
    ) -> dict[str, str]:
        """
        Returns The ``str -> str`` entries of *entries*.
        The others are logged: each value ends up as a ``ToyData.model_name`` which must be a str.

        Args:
            entries: Bluetooth name -> model name, from the file or from :meth:`update`.
            context: Where the entries come from, for the log.

        Returns:
            The ``str -> str`` entries.
        """
        clean: dict[str, str] = {}
        rejected: list[str] = []
        for name, model_name in entries.items():
            if isinstance(name, str) and isinstance(model_name, str):
                clean[name] = model_name
            else:
                rejected.append(f"{name!r}: {model_name!r}")
        if rejected:
            self._log.warning(
                f"Ignoring {len(rejected)} ToyCache entr(y/ies) from {context} that are not str -> str: "
                f"{rejected}. Affected toys fall back to the default model name."
            )
        return clean

    def _read(self) -> None:
        """
        Load the file into memory, creating it if missing. Anything but ``str -> str`` entries is logged and skipped.
        Never raises.
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
                    self._cache = self._only_string_entries(
                        data, f"'{self._cache_path}'"
                    )
                else:
                    self._log.warning(
                        f"ToyCache file '{self._cache_path}' does not contain a JSON object but a "
                        f"'{type(data)}'. Starting with an empty cache."
                    )
        except Exception as e:
            self._log.warning(
                f"Error reading ToyCache: {e} with details: {traceback.format_exc()}"
            )
