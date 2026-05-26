"""
WebSocket JSON-based server that exposes ToyHub to connected clients.
Offers an alternative to the Low-Level / High-Level API defined by the tikal library.
Here information is exchanged via a websocket. Significantly harder to use than the Low-Level / High-Level APIs but
offers some advantages:
- Process separation
- Service can be used in applications written in other programming languages (assuming websockets are supported)
- Multiple clients can modify the same state (experimental, untested)

This module is the entry point to the ToyServer command-line-interface.
"""

# nuitka-project: --msvc=latest
# nuitka-project: --mode=onefile
# nuitka-project: --include-windows-runtime-dlls=yes
# nuitka-project: --windows-console-mode=disable

import argparse
import asyncio
import logging
import traceback
from pathlib import Path

from tikal_web_server.toy_server import ToyServer


def main() -> None:
    """
    Entry point for the ToyServer command-line interface.

    Parses command-line arguments, configures logging, constructs a ToyServer instance.
    ToyServer shuts down automatically if no client is connected for 3 seconds.

    Command-line arguments:
        --host: Host to bind to (default: localhost).
        --port: Port to listen on (default: 8142).
        --toy-cache-path: Path to the toy-cache file (default: ./data/toy_cache.json).
        --mock-toys: Use a software mock instead of real Bluetooth hardware.
        --log-path: Filepath to write logs to (default: ./data/tikal_ws.log).
        --log-level: Logging verbosity: DEBUG, INFO, WARNING, or ERROR (default: INFO).
    """

    parser = argparse.ArgumentParser(description="WebSocket server for ToyHub")
    parser.add_argument(
        "--host", default="localhost", help="Host to bind to (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=8142, help="Port to listen on (default: 8142)"
    )
    parser.add_argument(
        "--toy-cache-path",
        type=Path,
        default=Path("./data/toy_cache.json"),
        help="Path to toy cache file (default: ./data/toy_cache.json). If empty uses in-memory cache only.",
    )
    parser.add_argument(
        "--mock-toys",
        action="store_true",
        help="Use mock toys instead of real Bluetooth",
    )
    parser.add_argument(
        "--log-path",
        default="./data/tikal_ws.log",
        help="File to write the log to (default: ./data/tikal_ws.log). If empty disables logging.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )

    args = parser.parse_args()

    formatting = logging.Formatter(
        "%(asctime)s [%(levelname)s] : %(module)s.%(funcName)s reports: %(message)s"
    )
    logger = logging.getLogger("tikal_ws")
    if args.log_path != "":
        log_path = Path(args.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(args.log_path), "w", "utf-8")
        file_handler.setLevel(args.log_level)
        file_handler.setFormatter(formatting)
        logger.setLevel(args.log_level.upper())
        logger.addHandler(file_handler)

    logger.info("Starting ToyServer")
    server = ToyServer(
        toy_cache_path=Path(args.toy_cache_path),
        host=args.host,
        port=args.port,
        mock_toys=args.mock_toys,
    )

    try:
        asyncio.run(server.serve())
    except Exception:
        details = traceback.format_exc()
        logging.critical("Server shutting down due to unhandled exception: %s", details)


if __name__ == "__main__":
    main()
