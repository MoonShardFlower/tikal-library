"""
Tests for the WebSocket server CLI entry point (:mod:`tikal.websocket.cli`).

``main`` is exercised with ``ToyServer`` and ``asyncio.run`` patched out, so nothing actually binds a socket. Tests
assert that command-line arguments are parsed into the right ``ToyServer`` kwargs, that file logging is wired up, and
that an unhandled error from ``serve()`` is logged rather than propagated.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

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
    """
    Run ``cli.main()`` with ToyServer, asyncio.run and the signal-aware serve wrapper patched.

    Returns:
        (ToyServer_mock, asyncio_run_mock, serve_until_signal_mock)
    """
    with (
        patch.object(cli, "ToyServer") as toy_server,
        patch.object(cli.asyncio, "run") as run,
        # MagicMock, not the default AsyncMock: main() only hands the result to the patched asyncio.run,
        # and a real coroutine here would never be awaited.
        patch.object(
            cli, "_serve_until_signal", new_callable=MagicMock
        ) as serve_wrapper,
        patch.object(sys, "argv", argv),
    ):
        cli.main()
    return toy_server, run, serve_wrapper


def test_defaults_build_expected_server():
    toy_server, run, _ = _run_main(["tikal-server", "--log-path", "None"])

    run.assert_called_once()
    kwargs = toy_server.call_args.kwargs
    assert kwargs["host"] == "localhost"
    assert kwargs["port"] == 8142
    assert kwargs["idle_shutdown_delay"] == 3
    assert kwargs["mock_toys"] is False
    assert kwargs["toy_cache_path"] == Path("data/toy_cache.json")


def test_custom_arguments_are_forwarded():
    toy_server, run, _ = _run_main(
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
    toy_server, run, serve_wrapper = _run_main(["tikal-server", "--log-path", "None"])
    # The constructed server is run through the signal-aware wrapper, not bare serve():
    # a terminating signal has to stop the toys instead of killing the process.
    server_instance = toy_server.return_value
    assert serve_wrapper.call_args.args[0] is server_instance
    run.assert_called_once_with(serve_wrapper.return_value)


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


def test_insecure_flag_is_forwarded():
    toy_server, _, _ = _run_main(
        ["tikal-server", "--host", "0.0.0.0", "--insecure", "--log-path", "None"]
    )
    assert toy_server.call_args.kwargs["insecure"] is True


def test_insecure_defaults_false():
    toy_server, _, _ = _run_main(["tikal-server", "--log-path", "None"])
    assert toy_server.call_args.kwargs["insecure"] is False


def test_insecure_bind_error_exits_cleanly():
    """An exposed bind without --insecure exits non-zero (code 2) instead of dumping a traceback."""
    with (
        patch.object(cli, "ToyServer", side_effect=cli.InsecureBindError("nope")),
        patch.object(cli.asyncio, "run") as run,
        patch.object(
            sys, "argv", ["tikal-server", "--host", "0.0.0.0", "--log-path", "None"]
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2
    run.assert_not_called()


def test_serve_exception_is_logged_not_raised():
    with (
        patch.object(cli, "ToyServer"),
        patch.object(cli, "_serve_until_signal", new_callable=MagicMock),
        patch.object(cli.asyncio, "run", side_effect=RuntimeError("boom")),
        patch.object(cli.logging, "critical") as critical,
        patch.object(sys, "argv", ["tikal-server", "--log-path", "None"]),
    ):
        cli.main()  # must not raise

    critical.assert_called_once()


def test_keyboard_interrupt_exits_quietly():
    """Ctrl+C is an expected way to stop the server, not a crash: serve()'s finally has already stopped the toys."""
    with (
        patch.object(cli, "ToyServer"),
        patch.object(cli, "_serve_until_signal", new_callable=MagicMock),
        patch.object(cli.asyncio, "run", side_effect=KeyboardInterrupt),
        patch.object(cli.logging, "critical") as critical,
        patch.object(sys, "argv", ["tikal-server", "--log-path", "None"]),
    ):
        cli.main()  # must not propagate the KeyboardInterrupt

    critical.assert_not_called()


# ---------------------------------------------------------------------------
# _serve_until_signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_until_signal_awaits_serve():
    """The wrapper is transparent: it runs serve() to completion and cleans its signal handlers up again."""
    server = AsyncMock()
    await cli._serve_until_signal(server, logging.getLogger("tikal_ws"))
    server.serve.assert_awaited_once()


@pytest.mark.asyncio
async def test_serve_until_signal_shuts_down_on_signal():
    """A terminating signal must reach ToyServer.shutdown() so the toys are stopped before the process ends."""
    server = AsyncMock()
    loop = asyncio.get_running_loop()
    handlers: dict[int, Callable] = {}

    def fake_add_signal_handler(sig, callback, *args):
        handlers[sig] = lambda: callback(*args)

    async def serve_until_signalled():
        # Stand in for the real serve(): return once a signal handler has fired.
        for handler in list(handlers.values()):
            handler()
        await asyncio.sleep(0)

    server.serve.side_effect = serve_until_signalled

    with (
        patch.object(loop, "add_signal_handler", fake_add_signal_handler),
        patch.object(loop, "remove_signal_handler", lambda sig: True),
    ):
        await cli._serve_until_signal(server, logging.getLogger("tikal_ws"))

    assert handlers, "no signal handler was installed"
    server.shutdown.assert_awaited()


@pytest.mark.asyncio
async def test_serve_until_signal_survives_missing_signal_support():
    """Windows has no event-loop signal handlers; the wrapper must fall back instead of failing to start."""
    server = AsyncMock()
    loop = asyncio.get_running_loop()

    def unsupported(*_args, **_kwargs):
        raise NotImplementedError

    with (
        patch.object(loop, "add_signal_handler", unsupported),
        patch.object(loop, "remove_signal_handler", unsupported),
    ):
        await cli._serve_until_signal(server, logging.getLogger("tikal_ws"))

    server.serve.assert_awaited_once()
