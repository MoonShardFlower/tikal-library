"""Fixtures shared by several test modules."""

import pytest


@pytest.fixture
def short_reconnect_window(monkeypatch):
    """Shrink the reconnect policy (both API layers) so giving up on a toy takes a fraction of a second."""
    monkeypatch.setattr("tikal._private.reconnect.RECONNECT_WINDOW", 0.3)
    monkeypatch.setattr("tikal._private.reconnect.RECONNECT_PAUSE", 0.05)


@pytest.fixture
def short_reconnect_pause(monkeypatch):
    """Keep the full reconnect window, but retry almost immediately, for tests that expect a later attempt to succeed."""
    monkeypatch.setattr("tikal._private.reconnect.RECONNECT_PAUSE", 0.05)
