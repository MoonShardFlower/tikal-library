"""
HTML status page generator for ToyServer.

Renders a self-refreshing HTML dashboard showing server metadata and per-toy info / state / battery / connection status.
"""

import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._toy_hub import _ToyHub


class ToyServerStatusPage:
    """Generates an HTML status page for the ToyServer."""

    def __init__(self, hub: "_ToyHub", host: str, port: int) -> None:
        self._hub = hub
        self._host = host
        self._port = port
        self._start_time: datetime.datetime | None = None

    def set_start_time(self, start_time: datetime.datetime) -> None:
        """Record when the server started (used for uptime display)."""
        self._start_time = start_time

    def _format_uptime(self) -> str:
        if self._start_time is None:
            return "unknown"
        delta = datetime.datetime.now() - self._start_time
        h, remainder = divmod(int(delta.total_seconds()), 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h {m}m {s}s"

    async def build_html(self, client_count: int) -> str:
        """
        Build the complete HTML status page.

        Args:
            client_count: Number of currently connected WebSocket clients.

        Returns:
            UTF-8 HTML string.
        """
        try:
            toy_ids = await self._hub.get_toy_ids()
        except Exception:
            toy_ids = []

        toys_data: list[dict[str, Any]] = []
        for toy_id in toy_ids:
            try:
                info = await self._hub.get_info(toy_id, full=False)
                state = await self._hub.get_state(toy_id)
                status = await self._hub.get_status(toy_id)
                battery = await self._hub.get_battery(toy_id)
                toys_data.append(
                    {
                        "id": toy_id,
                        "info": info,
                        "state": state,
                        "status": status,
                        "battery": battery,
                    }
                )
            except Exception:
                continue

        return self._render_page(client_count, len(toy_ids), toys_data)

    def _render_page(
        self, client_count: int, toy_count: int, toys_data: list[dict[str, Any]]
    ) -> str:
        uptime = self._format_uptime()
        now = datetime.datetime.now().strftime("%H:%M:%S")

        toy_cards = (
            "\n".join(self._render_toy_card(t) for t in toys_data)
            if toys_data
            else "<p><em>No toys connected.</em></p>"
        )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>ToyServer Status</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; line-height: 1.5; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 1rem; }}
    h2 {{ font-size: 1.2rem; margin-top: 2rem; margin-bottom: 0.75rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
    h3 {{ font-size: 1rem; margin-top: 1rem; margin-bottom: 0.5rem; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; font-size: 0.9rem; }}
    th, td {{ text-align: left; padding: 0.5rem 0.75rem; border: 1px solid #ddd; }}
    th {{ background: #f5f5f5; font-weight: 600; }}
    .badge {{ display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px; font-weight: 600; font-size: 0.85rem; }}
    .badge-success {{ background: #d4edda; color: #155724; }}
    .badge-warn    {{ background: #fff3cd; color: #856404; }}
    .badge-danger  {{ background: #f8d7da; color: #721c24; }}
    .badge-info    {{ background: #d1ecf1; color: #0c5460; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
    @media (max-width: 640px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{ border: 1px solid #ddd; border-radius: 6px; padding: 1rem; background: #fafafa; margin-bottom: 1rem; }}
    .card-header {{ font-weight: 600; margin-bottom: 0.75rem; font-size: 1rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; }}
    .mono {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 0.85rem; }}
    .section {{ margin-bottom: 1.5rem; }}
    footer {{ font-size: 0.8rem; color: #888; margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #eee; }}
    .bar-wrap {{ display: inline-flex; align-items: center; gap: 0.5rem; }}
    .bar {{ width: 80px; height: 10px; background: #e9ecef; border-radius: 5px; overflow: hidden; }}
    .bar-fill {{ height: 100%; background: #28a745; transition: width 0.3s; }}
    .bar-fill-warn {{ background: #ffc107; }}
    .bar-fill-danger {{ background: #dc3545; }}
    .pattern-list {{ font-size: 0.8rem; max-height: 120px; overflow-y: auto; background: #fff; padding: 0.5rem; border: 1px solid #ddd; border-radius: 4px; }}
    .pattern-row {{ display: flex; justify-content: space-between; padding: 0.15rem 0; border-bottom: 1px solid #f0f0f0; }}
    .pattern-row:last-child {{ border-bottom: none; }}
  </style>
</head>
<body>
  <h1>ToyServer Status</h1>

  <div class="section">
    <table>
      <tr><th>Property</th><th>Value</th></tr>
      <tr><td>Status</td><td><span class="badge badge-success">Running</span></td></tr>
      <tr><td>Uptime</td><td>{uptime}</td></tr>
      <tr><td>Address</td><td><code class="mono">ws://{self._host}:{self._port}</code></td></tr>
      <tr><td>Connected clients</td><td>{client_count}</td></tr>
      <tr><td>Connected toys</td><td>{toy_count}</td></tr>
    </table>
  </div>

  <h2>Connected Toys</h2>
  {toy_cards}

  <footer>Page refreshes every 5 seconds &mdash; {now}</footer>
</body>
</html>"""

    @staticmethod
    def _render_toy_card(toy: dict[str, Any]) -> str:
        info = toy["info"]
        state = toy["state"]
        status = toy["status"]
        battery = toy["battery"]
        toy_id = toy["id"]

        status_badge = {
            "CONNECTED": "badge-success",
            "RECONNECTING": "badge-warn",
            "LOST": "badge-danger",
            "POWERED_OFF": "badge-info",
        }.get(status, "badge-info")

        # Battery
        if battery is None:
            battery_str = "N/A"
            battery_bar = ""
        else:
            battery_str = f"{battery}%"
            color = (
                "bar-fill"
                if battery > 50
                else "bar-fill-warn" if battery > 20 else "bar-fill-danger"
            )
            battery_bar = f'<div class="bar"><div class="{color}" style="width: {battery}%"></div></div>'

        # Intensities
        max_intensity = info.get("max_intensity", 100) or 1
        intensities = state.get("current_intensities", [0, 0])
        limits = state.get("intensity_limits", [None, None])
        intensity_names = info.get("intensity_names", ["Intensity 1", ""])

        def bar(val: int) -> str:
            pct = min(100, max(0, val / max_intensity * 100))
            return f'<div class="bar"><div class="bar-fill" style="width: {pct}%"></div></div> <span class="mono">{val}/{max_intensity}</span>'

        i1_name = intensity_names[0] if intensity_names else "Intensity 1"
        i2_name = (
            intensity_names[1]
            if len(intensity_names) > 1 and intensity_names[1]
            else None
        )

        i1_row = f"<tr><td>{i1_name}</td><td>{bar(intensities[0])}</td></tr>"
        i2_row = (
            f"<tr><td>{i2_name}</td><td>{bar(intensities[1])}</td></tr>"
            if i2_name
            else ""
        )

        limit1 = limits[0] if limits[0] is not None else max_intensity
        limit2 = limits[1] if limits[1] is not None else max_intensity

        # Pattern
        pattern = state.get("pattern", [])
        if pattern:
            rows = "".join(
                f'<div class="pattern-row"><span>Seg {i}: {dur}ms</span><span class="mono">({i1}, {i2})</span></div>'
                for i, (dur, i1, i2) in enumerate(pattern)
            )
            pattern_html = f'<div class="pattern-list">{rows}</div>'
        else:
            pattern_html = "<p><em>No active pattern</em></p>"

        return f"""<div class="card">
    <div class="card-header">
      <span class="mono">{toy_id}</span>
      <span class="badge {status_badge}">{status}</span>
    </div>
    <div class="grid">
      <div>
        <h3>Info</h3>
        <table>
          <tr><td>Name</td><td>{info.get("name", "N/A")}</td></tr>
          <tr><td>Model</td><td>{info.get("model_name", "N/A")}</td></tr>
          <tr><td>Brand</td><td>{info.get("brand", "N/A")}</td></tr>
          <tr><td>Battery</td><td><div class="bar-wrap">{battery_str}{battery_bar}</div></td></tr>
          <tr><td>Max Intensity</td><td>{max_intensity}</td></tr>
          <tr><td>Rotation</td><td>{"Yes" if info.get("supports_rotation") else "No"}</td></tr>
          <tr><td>Min Interval</td><td>{info.get("recommended_min_interval", "N/A")} ms</td></tr>
        </table>
      </div>
      <div>
        <h3>State</h3>
        <table>
          {i1_row}
          {i2_row}
          <tr><td>Limit 1</td><td class="mono">{limit1}</td></tr>
          <tr><td>Limit 2</td><td class="mono">{limit2}</td></tr>
          <tr><td>Paused</td><td>{"Yes" if state.get("is_paused") else "No"}</td></tr>
          <tr><td>Blocked</td><td>{"Yes" if state.get("is_blocked") else "No"}</td></tr>
          <tr><td>Wraparound</td><td>{"Yes" if state.get("wraparound") else "No"}</td></tr>
          <tr><td>Elapsed</td><td>{state.get("elapsed", 0):.1f} ms</td></tr>
          <tr><td>Pattern Ver</td><td class="mono">{state.get("pattern_version", 0)}</td></tr>
        </table>
      </div>
    </div>
    <div style="margin-top: 0.75rem;">
      <h3>Pattern ({len(pattern)} segments)</h3>
      {pattern_html}
    </div>
  </div>"""
