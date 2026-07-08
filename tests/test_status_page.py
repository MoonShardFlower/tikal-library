"""
Tests for the HTML status page (:mod:`tikal.websocket._status_page`).

The renderer is pure (no I/O); ``build_html`` pulls its data from a hub, which is faked with an ``AsyncMock`` here.
Tests assert on the presence of meaningful content rather than exact markup, so cosmetic HTML tweaks don't break them.
"""

import datetime
from unittest.mock import AsyncMock

import pytest

from tikal.websocket._status_page import ToyServerStatusPage


def _info(**overrides):
    base = dict(
        toy_id="toy-1",
        name="Thunder1",
        model_name="Thunder",
        brand="MockEstimToys",
        intensity_names=["Stim A", "Stim B"],
        supports_rotation=False,
        max_intensity=100,
        recommended_min_interval=100,
    )
    base.update(overrides)
    return base


def _state(**overrides):
    base = dict(
        toy_id="toy-1",
        current_intensities=[10, 20],
        intensity_limits=[100, 100],
        is_blocked=False,
        pattern_version=3,
        pattern=[],
        wraparound=True,
        is_paused=False,
        elapsed=1234.5,
    )
    base.update(overrides)
    return base


def _card(**overrides):
    base = dict(
        id="toy-1",
        info=_info(),
        state=_state(),
        status="CONNECTED",
        battery=77,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Uptime
# ---------------------------------------------------------------------------


def test_uptime_unknown_without_start_time():
    page = ToyServerStatusPage(AsyncMock(), "localhost", 8142)
    assert page._format_uptime() == "unknown"


def test_uptime_formatted_when_started():
    page = ToyServerStatusPage(AsyncMock(), "localhost", 8142)
    page.set_start_time(datetime.datetime.now() - datetime.timedelta(seconds=3661))
    # 3661s = 1h 1m 1s
    assert page._format_uptime() == "1h 1m 1s"


# ---------------------------------------------------------------------------
# build_html
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_html_no_toys():
    hub = AsyncMock()
    hub.get_toy_ids.return_value = []
    page = ToyServerStatusPage(hub, "example.com", 9999)

    html = await page.build_html(client_count=2)
    assert "<!DOCTYPE html>" in html
    assert "ws://example.com:9999" in html
    assert "No toys connected." in html
    assert "Connected clients</td><td>2" in html


@pytest.mark.asyncio
async def test_build_html_get_toy_ids_error_degrades_gracefully():
    hub = AsyncMock()
    hub.get_toy_ids.side_effect = RuntimeError("bluetooth down")
    page = ToyServerStatusPage(hub, "localhost", 8142)

    html = await page.build_html(client_count=0)
    assert "No toys connected." in html  # empty toy list, page still renders


@pytest.mark.asyncio
async def test_build_html_renders_toy():
    hub = AsyncMock()
    hub.get_toy_ids.return_value = ["toy-1"]
    hub.get_info.return_value = _info()
    hub.get_state.return_value = _state(current_intensities=[15, 0])
    hub.get_status.return_value = "CONNECTED"
    hub.get_battery.return_value = 77
    page = ToyServerStatusPage(hub, "localhost", 8142)

    html = await page.build_html(client_count=1)
    assert "toy-1" in html
    assert "Thunder" in html
    assert "77%" in html
    assert "badge-success" in html  # CONNECTED -> success badge


@pytest.mark.asyncio
async def test_build_html_skips_toy_that_errors():
    hub = AsyncMock()
    hub.get_toy_ids.return_value = ["toy-1"]
    hub.get_info.side_effect = RuntimeError("gone")
    page = ToyServerStatusPage(hub, "localhost", 8142)

    html = await page.build_html(client_count=1)
    # The toy is skipped, so the "no toys" placeholder is shown instead of a card.
    assert "No toys connected." in html


# ---------------------------------------------------------------------------
# _render_toy_card branches
# ---------------------------------------------------------------------------


def test_card_battery_none_shows_na():
    html = ToyServerStatusPage._render_toy_card(_card(battery=None))
    assert "N/A" in html
    assert "bar-fill-danger" not in html  # no battery bar rendered at all


def test_card_battery_low_uses_danger_color():
    html = ToyServerStatusPage._render_toy_card(_card(battery=10))
    assert "bar-fill-danger" in html


def test_card_battery_medium_uses_warn_color():
    html = ToyServerStatusPage._render_toy_card(_card(battery=35))
    assert "bar-fill-warn" in html


def test_card_unknown_status_falls_back_to_info_badge():
    html = ToyServerStatusPage._render_toy_card(_card(status="mystery"))
    assert "badge-info" in html


def test_card_single_capability_omits_second_intensity_row():
    info = _info(intensity_names=["Stim", ""])
    state = _state(current_intensities=[12, 0])
    html = ToyServerStatusPage._render_toy_card(_card(info=info, state=state))
    assert "Stim" in html
    # No second-capability name/row when the second name is blank
    assert "Stim B" not in html


def test_card_renders_pattern_segments():
    state = _state(pattern=[(500, 5, 3), (1000, 10, 0)])
    html = ToyServerStatusPage._render_toy_card(_card(state=state))
    assert "2 segments" in html
    assert "Seg 0: 500ms" in html
    assert "(5, 3)" in html


def test_card_no_pattern_shows_placeholder():
    html = ToyServerStatusPage._render_toy_card(_card(state=_state(pattern=[])))
    assert "No active pattern" in html
