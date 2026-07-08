"""
Tests for the WebSocket server CLI entry point (:mod:`tikal.websocket.cli`).

``main`` is exercised with ``ToyServer`` and ``asyncio.run`` patched out, so nothing actually binds a socket. Tests
assert that command-line arguments are parsed into the right ``ToyServer`` kwargs, that file logging is wired up, and
that an unhandled error from ``serve()`` is logged rather than propagated.
"""

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tikal.websocket import cli


@pytest.fixture(autouse=True)
def _isolate_ws_logger():
    """Keep the module-level ``tikal_ws`` logger clean between tests (handlers leak otherwise)."""
    logger = logging.getLogger("tikal_ws")
    saved = logger.handlers[:]
    logger.handlers.clear()
    yield
    for handler in logger.handlers[:]:
        handler.close()
    logger.handlers.clear()
    logger.handlers.extend(saved)


def _run_main(argv):
    """Run ``cli.main()`` with ToyServer + asyncio.run patched. Returns (ToyServer_mock, run_mock)."""
    with (
        patch.object(cli, "ToyServer") as toy_server,
        patch.object(cli.asyncio, "run") as run,
        patch.object(sys, "argv", argv),
    ):
        cli.main()
    return toy_server, run


def test_defaults_build_expected_server():
    toy_server, run = _run_main(["tikal-server", "--log-path", "None"])

    run.assert_called_once()
    kwargs = toy_server.call_args.kwargs
    assert kwargs["host"] == "localhost"
    assert kwargs["port"] == 8142
    assert kwargs["idle_shutdown_delay"] == 3
    assert kwargs["mock_toys"] is False
    assert kwargs["toy_cache_path"] == Path("data/toy_cache.json")


def test_custom_arguments_are_forwarded():
    toy_server, run = _run_main(
        [
            "tikal-server",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--timeout",
            "10",
            "--mock-toys",
            "--toy-cache-path",
            "custom/cache.json",
            "--log-path",
            "None",
        ]
    )

    kwargs = toy_server.call_args.kwargs
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000
    assert kwargs["idle_shutdown_delay"] == 10
    assert kwargs["mock_toys"] is True
    assert kwargs["toy_cache_path"] == Path("custom/cache.json")


def test_serve_is_run():
    toy_server, run = _run_main(["tikal-server", "--log-path", "None"])
    # serve() of the constructed server is what gets handed to asyncio.run
    server_instance = toy_server.return_value
    run.assert_called_once_with(server_instance.serve.return_value)


def test_file_logging_is_configured(tmp_path):
    log_file = tmp_path / "logs" / "server.log"

    _run_main(["tikal-server", "--log-path", str(log_file), "--log-level", "DEBUG"])

    logger = logging.getLogger("tikal_ws")
    assert log_file.exists()  # parent dir + file created by the FileHandler
    assert any(isinstance(h, logging.FileHandler) for h in logger.handlers)
    assert logger.level == logging.DEBUG

    # Release the file handle so tmp_path cleanup succeeds on Windows.
    for handler in logger.handlers[:]:
        handler.close()
    logger.handlers.clear()


def test_serve_exception_is_logged_not_raised():
    with (
        patch.object(cli, "ToyServer"),
        patch.object(cli.asyncio, "run", side_effect=RuntimeError("boom")),
        patch.object(cli.logging, "critical") as critical,
        patch.object(sys, "argv", ["tikal-server", "--log-path", "None"]),
    ):
        cli.main()  # must not raise

    critical.assert_called_once()
