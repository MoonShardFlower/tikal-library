"""
WebSocket JSON-based server that exposes _ToyHub to connected clients.
Offers an alternative to the Low-Level / High-Level API defined by the tikal library.
Here information is exchanged via a websocket. Significantly harder to use than the Low-Level / High-Level APIs but
offers some advantages:
- Process separation
- Service can be used in applications written in other programming languages (assuming websockets are supported)
- Multiple clients can modify the same state (experimental, untested)

This module is the entry point to the ToyServer command-line-interface.
"""

# nuitka-project: --msvc=latest
# nuitka-project: --mode=standalone
# nuitka-project: --include-windows-runtime-dlls=yes
# nuitka-project: --windows-console-mode=disable

# nuitka-project: --include-package=bleak
# nuitka-project: --include-package=bleak.backends.winrt

# nuitka-project: --include-package=winrt._winrt
# nuitka-project: --include-package=winrt._winrt_windows_devices_bluetooth
# nuitka-project: --include-package=winrt._winrt_windows_devices_bluetooth_advertisement
# nuitka-project: --include-package=winrt._winrt_windows_devices_bluetooth_genericattributeprofile
# nuitka-project: --include-package=winrt._winrt_windows_devices_enumeration
# nuitka-project: --include-package=winrt._winrt_windows_devices_radios
# nuitka-project: --include-package=winrt._winrt_windows_foundation
# nuitka-project: --include-package=winrt._winrt_windows_foundation_collections
# nuitka-project: --include-package=winrt._winrt_windows_storage_streams
# nuitka-project: --include-package=winrt.runtime
# nuitka-project: --include-package=winrt.runtime._internals
# nuitka-project: --include-package=winrt.runtime.interop
# nuitka-project: --include-package=winrt.system
# nuitka-project: --include-package=winrt.system.hresult

import argparse
import asyncio
import logging
import signal
import traceback
from pathlib import Path

from .toy_server import InsecureBindError, ToyServer


async def _serve_until_signal(server: ToyServer, logger: logging.Logger) -> None:
    """
    Run the server until it stops or a terminating signal arrives.

    A signal has to be turned into a *graceful* shutdown, because a toy keeps running whatever it was last told once
    this process is gone. SIGTERM (``systemctl stop``, ``docker stop``, a session ending) terminates the process
    outright by default: no ``finally``, no ``atexit``, and a toy still going at full intensity.

    Where the event loop supports signal handlers (POSIX) both SIGINT and SIGTERM are routed to
    :meth:`ToyServer.shutdown`. On Windows ``add_signal_handler`` is unavailable, but Ctrl+C arrives as a
    ``KeyboardInterrupt`` that cancels this coroutine, and :meth:`ToyServer.serve` stops the toys in its own
    ``finally``.

    Args:
        server: The server to run.
        logger: Logger used to record which signal triggered the shutdown.
    """
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def request_shutdown(signal_name: str) -> None:
        logger.info("Received %s. Stopping all toys and shutting down.", signal_name)
        task = loop.create_task(server.shutdown(), name="signal-shutdown")
        # Keep a reference: a bare create_task may be garbage-collected mid-shutdown.
        pending.add(task)
        task.add_done_callback(pending.discard)

    installed: list[signal.Signals] = []
    for signal_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_shutdown, signal_name)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows, or not running in the main thread. Covered by serve()'s own finally.
            logger.debug("No event-loop signal handler available for %s.", signal_name)

    try:
        await server.serve()
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        if pending:
            # Let an in-flight signal shutdown finish before the loop closes.
            await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:
    """
    Entry point for the ToyServer command-line interface.

    Parses command-line arguments, configures logging, constructs a ToyServer instance.
    ToyServer shuts down automatically if no client is connected for 3 seconds. Ctrl+C and (where supported) SIGTERM
    stop and disconnect every toy before the process exits, rather than leaving them running.

    Command-line arguments:
        --host: Host to bind to (default: localhost).
        --port: Port to listen on (default: 8142).
        --toy-cache-path: Path to the toy-cache file (default: ./data/toy_cache.json).
        --mock-toys: Use a software mock instead of real Bluetooth hardware.
        --log-path: Filepath to write logs to (default: ./data/tikal_ws.log).
        --log-level: Logging verbosity: DEBUG, INFO, WARNING, or ERROR (default: INFO).
    """

    parser = argparse.ArgumentParser(description="WebSocket server for _ToyHub")
    parser.add_argument(
        "--host", default="localhost", help="Host to bind to (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=8142, help="Port to listen on (default: 8142)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3,
        help="If no client is connected for this many seconds, the server will shut down automatically (default: 3 seconds). Set to 0 to disable auto-shutdown. All toys are stopped as soon as the last client disconnects either way.",
    )

    parser.add_argument(
        "--toy-cache-path",
        type=Path,
        default=Path("./data/toy_cache.json"),
        help="Path to toy cache file (default: ./data/toy_cache.json). If the string 'None' is passed uses in-memory cache only.",
    )
    parser.add_argument(
        "--mock-toys",
        action="store_true",
        help="Use mock toys instead of real Bluetooth",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow binding to a non-loopback --host even though tikal has no built-in authentication. "
        "By default an exposed bind is refused. Only use this when the server is protected another way "
        "(e.g. firewall, trusted LAN, or testing). See docs/websocket/security.md.",
    )
    parser.add_argument(
        "--log-path",
        default="./data/tikal_ws.log",
        help="File to write the log to (default: ./data/tikal_ws.log). If the string 'None' is passed disables logging.",
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
    if args.log_path != "None":
        log_path = Path(args.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(args.log_path), "w", "utf-8")
        file_handler.setLevel(args.log_level)
        file_handler.setFormatter(formatting)
        logger.setLevel(args.log_level.upper())
        logger.addHandler(file_handler)

    logger.info("Starting ToyServer")
    toy_cache_path = (
        Path(args.toy_cache_path) if args.toy_cache_path != "None" else Path()
    )
    try:
        server = ToyServer(
            toy_cache_path=toy_cache_path,
            host=args.host,
            port=args.port,
            idle_shutdown_delay=args.timeout,
            mock_toys=args.mock_toys,
            insecure=args.insecure,
        )
    except InsecureBindError as e:
        logging.error("Failed to bind to %s:%d: %s", args.host, args.port, e)
        raise SystemExit(2)

    try:
        asyncio.run(_serve_until_signal(server, logger))
    except KeyboardInterrupt:
        # asyncio.run canceled serve(), whose finally already stopped and disconnected every toy.
        logger.info("Interrupted. All toys were stopped during shutdown.")
    except Exception:
        details = traceback.format_exc()
        logging.critical("Server shutting down due to unhandled exception: %s", details)


if __name__ == "__main__":
    main()
