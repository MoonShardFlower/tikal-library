"""
Tikal ToyServer API walkthrough script.

Disclaimer: Fully AI written. Not the prettiest thing, but good enough to demonstrate the WebSocket API.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import websockets

# ── Configuration ─────────────────────────────────────────────────────────────
HOST = "localhost"
PORT = 8142
WS_URL = f"ws://{HOST}:{PORT}"
MOCK_TOY_ID = "00:00:00:00:00:01"
MOCK_MODEL = "Solace"

# How long to wait for the mock toy to appear after start_scan / add
SCAN_WAIT_S = 3
ADD_WAIT_S = 5

# ── Helpers ───────────────────────────────────────────────────────────────────
_id_counter = 0


def make_request(action: str, data: dict) -> tuple[str, str]:
    """Return (json_string, request_id) for a request envelope."""
    global _id_counter
    _id_counter += 1
    req_id = f"req-{_id_counter:03d}"
    envelope = {"request": action, "id": req_id, "data": data}
    return json.dumps(envelope), req_id


async def send(ws, action: str, data: dict) -> dict:
    """Send one request and return the parsed reply (including any events that arrive first)."""
    payload, req_id = make_request(action, data)
    await ws.send(payload)

    # Drain messages until we get the reply that matches our request id.
    # Any events that arrive in the meantime are printed but not returned.
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)

        if "event" in msg:
            _print_message(f"[EVENT during '{action}']", msg)
            continue

        if msg.get("id") == req_id:
            return msg

        # Unexpected reply from a different request – print and keep waiting
        _print_message(f"[UNEXPECTED REPLY during '{action}']", msg)


def _print_message(label: str, msg: dict) -> None:
    pretty = json.dumps(msg, indent=2)
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print("─" * 60)
    print(pretty)


def print_reply(action: str, reply: dict) -> None:
    status = "✓ success" if reply.get("success") else "✗ FAILED"
    _print_message(f"[{status}]  {action}", reply)


# ── Server lifecycle ──────────────────────────────────────────────────────────


def start_server() -> subprocess.Popen:
    """Launch the toy server as a subprocess and return the handle."""
    sys.path.insert(0, os.path.abspath("../.."))
    cmd = [
        sys.executable,
        "-m",
        "tikal_web_server.main",
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--mock-toys",
        "--log-level",
        "WARNING",  # keep server output quiet
        "--toy-cache-path",
        "",  # in-memory only, no leftover state
    ]
    print(f"▶  Starting server:  {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc


async def wait_for_server(timeout: float = 10.0) -> None:
    """Poll until the WebSocket endpoint is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with websockets.connect(WS_URL):
                print(f"✓  Server is up at {WS_URL}\n")
                return
        except OSError:
            await asyncio.sleep(0.25)
    raise RuntimeError(f"Server did not become ready within {timeout}s")


# ── Main walkthrough ──────────────────────────────────────────────────────────


async def run_walkthrough() -> None:
    async with websockets.connect(WS_URL) as ws:

        # ── 1. get_brands ──────────────────────────────────────────────────
        reply = await send(ws, "get_brands", {})
        print_reply("1. get_brands", reply)

        # ── 2. start_scan ──────────────────────────────────────────────────
        reply = await send(ws, "start_scan", {})
        print_reply("2. start_scan", reply)

        print(f"\n   ⏳ Waiting {SCAN_WAIT_S}s for mock toy to be discovered …")
        await asyncio.sleep(SCAN_WAIT_S)

        # ── 3. add ─────────────────────────────────────────────────────────
        reply = await send(ws, "add", {"toy_id": MOCK_TOY_ID, "model_name": MOCK_MODEL})
        print_reply("3. add", reply)

        print(f"\n   ⏳ Waiting {ADD_WAIT_S}s for mock toy to finish connecting …")
        await asyncio.sleep(ADD_WAIT_S)

        # ── 4. stop_scan ───────────────────────────────────────────────────
        reply = await send(ws, "stop_scan", {})
        print_reply("4. stop_scan", reply)

        # ── 5. get_toy_ids ─────────────────────────────────────────────────
        reply = await send(ws, "get_toy_ids", {})
        print_reply("5. get_toy_ids", reply)

        # ── 6. get_battery ─────────────────────────────────────────────────
        reply = await send(ws, "get_battery", {"toy_id": MOCK_TOY_ID})
        print_reply("6. get_battery", reply)

        # ── 7. get_connection_status ───────────────────────────────────────
        reply = await send(ws, "get_connection_status", {"toy_id": MOCK_TOY_ID})
        print_reply("7. get_connection_status", reply)

        # ── 8. get_state ───────────────────────────────────────────────────
        reply = await send(ws, "get_state", {"toy_id": MOCK_TOY_ID})
        print_reply("8. get_state  (initial)", reply)

        # ── 9a. get_info (fast) ────────────────────────────────────────────
        reply = await send(ws, "get_info", {"toy_id": MOCK_TOY_ID, "full": False})
        print_reply("9a. get_info  (full=false)", reply)

        # ── 9b. get_info (full) ────────────────────────────────────────────
        reply = await send(ws, "get_info", {"toy_id": MOCK_TOY_ID, "full": True})
        print_reply("9b. get_info  (full=true)", reply)

        # ── 10a. get_all (fast) ────────────────────────────────────────────
        reply = await send(ws, "get_all", {"toy_id": MOCK_TOY_ID, "full": False})
        print_reply("10a. get_all  (full=false)", reply)

        # ── 10b. get_all (full) ────────────────────────────────────────────
        reply = await send(ws, "get_all", {"toy_id": MOCK_TOY_ID, "full": True})
        print_reply("10b. get_all  (full=true)", reply)

        # ── 11. intensity1 ─────────────────────────────────────────────────
        reply = await send(ws, "intensity1", {"toy_id": MOCK_TOY_ID, "intensity": 10})
        print_reply("11. intensity1  (set to 10)", reply)

        # ── 12. intensity2 ─────────────────────────────────────────────────
        reply = await send(ws, "intensity2", {"toy_id": MOCK_TOY_ID, "intensity": 5})
        print_reply("12. intensity2  (set to 5)", reply)

        # ── 13. change_rotation_direction ──────────────────────────────────
        reply = await send(ws, "change_rotation_direction", {"toy_id": MOCK_TOY_ID})
        print_reply("13. change_rotation_direction", reply)

        # ── 14. set_pattern ────────────────────────────────────────────────
        pattern = [[1000, 10, 0], [500, 5, 0], [750, 0, 0]]
        reply = await send(
            ws,
            "set_pattern",
            {
                "toy_id": MOCK_TOY_ID,
                "pattern": pattern,
                "wraparound": True,
                "reset_time": True,
            },
        )
        print_reply("14. set_pattern  (looping 3-segment pattern)", reply)

        # ── 8b. get_state – after pattern ──────────────────────────────────
        await asyncio.sleep(0.2)  # let the server tick once
        reply = await send(ws, "get_state", {"toy_id": MOCK_TOY_ID})
        print_reply("8b. get_state  (after set_pattern)", reply)

        # ── 16. toggle_pause ───────────────────────────────────────────────
        reply = await send(ws, "toggle_pause", {"toy_id": MOCK_TOY_ID})
        print_reply("16. toggle_pause  (→ paused)", reply)

        # ── 17. set_paused ─────────────────────────────────────────────────
        reply = await send(ws, "set_paused", {"toy_id": MOCK_TOY_ID, "pause": False})
        print_reply("17. set_paused  (pause=false → resumed)", reply)

        # ── 15. toggle_block ───────────────────────────────────────────────
        reply = await send(ws, "toggle_block", {"toy_id": MOCK_TOY_ID})
        print_reply("15. toggle_block  (→ blocked)", reply)

        # ── 15b. set_blocked ───────────────────────────────────────────────
        reply = await send(ws, "set_blocked", {"toy_id": MOCK_TOY_ID, "block": False})
        print_reply("15b. set_blocked  (block=false → unblocked)", reply)

        # ── 13. stop ───────────────────────────────────────────────────────
        reply = await send(ws, "stop", {"toy_id": MOCK_TOY_ID})
        print_reply("13. stop", reply)

        # ── 19. direct_command ─────────────────────────────────────────────
        reply = await send(
            ws, "direct_command", {"toy_id": MOCK_TOY_ID, "command": "GetBatch"}
        )
        print_reply("19. direct_command  ('GetBatch')", reply)

        # ── 20. set_model ──────────────────────────────────────────────────
        reply = await send(
            ws, "set_model", {"toy_id": MOCK_TOY_ID, "model_name": "Sex Machine"}
        )
        print_reply(f"20. set_model to 'Sex Machine')", reply)

        # ── 21. remove ─────────────────────────────────────────────────────
        reply = await send(ws, "remove", {"toy_id": MOCK_TOY_ID})
        print_reply("21. remove", reply)

    print(f"\n{'═' * 60}")
    print("  Walkthrough complete.")
    print("═" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    server_proc = start_server()
    try:
        await wait_for_server()
        await run_walkthrough()
    except Exception as exc:
        print(f"\n❌  Error: {exc}", file=sys.stderr)
        raise
    finally:
        # The server exits on its own after 3 s with no clients, but we
        # terminate it explicitly to avoid any dangling process.
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        stderr_output = server_proc.stderr.read().decode(errors="replace").strip()
        if stderr_output:
            print("\n── Server stderr ─────────────────────────────────────")
            print(stderr_output)


if __name__ == "__main__":
    asyncio.run(main())
