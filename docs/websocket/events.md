# Tikal Toy Server WebSocket API

The Toy Server provides a WebSocket-based API for controlling toys.
This document describes the events that the Server broadcasts.

I suggest reading the documentation in the following order:
- **documentation.md**
- **actions.md** while keeping **error.md** open in parallel.
- **events.md** (this file) while keeping **error.md** open in parallel.

## Events

Events are broadcasted to all connected clients (exception scan events which are only sent to clients that have subscribed to scan results)
They serve to keep all clients in sync with the server state.
Once you connect to the server, you should first use the `get_toy_ids` command to get a list of all toys known to the server.
Followed by `get_all` for each toy to get its current state. Now possessing the full server state, listening to events is
enough to keep your client in sync with the server as you or other clients modify its state.
The event envelope is already defined in **documentation.md** (which you should read first).
In the following I only define the data fields of each event and describe when the event occurs.

---

### 1. `connection_status_changed`
A toy’s connection status has changed. The server monitors the connection and broadcasts this event whenever the status transitions (e.g., connected -> reconnecting -> lost).

- `reconnecting`: a command failed or the connection dropped. The server tries to reconnect, with several attempts over
  up to about a minute. After reconnecting it first stops the toy, which also pauses its pattern: whatever the toy was
  last told no longer applies. Until then, nothing is sent to the toy: a command that needs it is refused with a
  Connection Error, while a state change such as a block, pause, pattern, or limit is still recorded (see **error.md**).
- `connected`: reconnecting succeeded. A `toy_state_changed` with the stopped state is sent just before.
- `lost`: no attempt succeeded within that minute. The toy is removed (a `toy_ids_changed` follows) and has to be added
  again once it is discovered again. The server can no longer tell whether the toy is still running.
- `powered_off`: the toy was switched off with its power button (it reports this). It is removed as well.

**Data**
```json
{
  "toy_id": "00:00:00:00:00:01",
  "status": "reconnecting"
}
```

| Field     | Type   | Description                                                                    |
|-----------|--------|--------------------------------------------------------------------------------|
| `toy_id ` | string | Unique identifier of the toy (e.g., Bluetooth address).                        |
| `status`  | string | Current status: `"connected"`, `"reconnecting"`, `"lost"`, or `"powered_off"`. |

---

### 2. `toy_ids_changed`
The set of toys managed by the server changed: a toy was added or removed.

**Data**
```json
{
  "toy_ids": ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]
}
```

| Field     | Type         | Description                                      |
|-----------|--------------|--------------------------------------------------|
| `toy_ids` | list[string] | Snapshot of all toy identifiers currently known. |

---

### 3. `toy_state_changed`
Any part of a toy’s internal state has changed (intensities, intensity limits, pattern, pause/block state, pattern version, elapsed time)

**Data**
```json
{
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "current_intensities": [0, 0],
  "intensity_limits": [20, 20],
  "is_blocked": false,
  "pattern_version": 3,
  "pattern": [[500, 100, 0], [500, 0, 100]],
  "wraparound": true,
  "is_paused": false,
  "elapsed": 123.4
}
```

| Field                 | Type                     | Description                                                                                                    |
|-----------------------|--------------------------|----------------------------------------------------------------------------------------------------------------|
| `toy_id`              | string                   | Unique identifier of the toy.                                                                                  |
| `current_intensities` | list[int]                | `[intensity1, intensity2]`; second value is always `0` for single‑intensity toys.                              |
| `intensity_limits`    | list[int]                | `[limit1, limit2]`; current intensity limits. All intensity commands are clamped to these values.              |
| `is_blocked`          | bool                     | `true` if the toy is forced to zero intensities.                                                               |
| `pattern_version`     | int                      | Increments each time the pattern state changes.                                                                |
| `pattern`             | list[tuple[int,int,int]] | Active pattern as a list of `(duration_ms, intensity1, intensity2)` segments.                                  |
| `wraparound`          | bool                     | `true` if the pattern loops after the last segment; `false` if it stops.                                       |
| `is_paused`           | bool                     | `true` when pattern playback is paused (intensities zero, timer frozen).                                       |
| `elapsed`             | float                    | Milliseconds elapsed since the start of the pattern or the last wraparound (does not advance during pauses).   |

---

### 4. `model_changed`
The model name assigned to an already‑added toy has changed (via the `set_model` command).

**Data**
```json
{
  "toy_id": "00:00:00:00:00:01",
  "name": "LVS-B12",
  "model_name": "Solace",
  "brand": "Lovense",
  "intensity_names": ["Thrust", "Depth"],
  "supports_rotation": false,
  "max_intensity": 20,
  "recommended_min_interval": 400
}
```

| Field                      | Type                 | Description                                                                                                            |
|----------------------------|----------------------|------------------------------------------------------------------------------------------------------------------------|
| `toy_id`                   | string               | Unique identifier of the toy.                                                                                          |
| `name`                     | string               | Human-readable name of the toy.                                                                                        |
| `model_name`               | string               | New model name assigned to the toy.                                                                                    |
| `brand`                    | string               | Brand of the toy                                                                                                       |
| `intensity_names`          | list[string, string] | New Human readable names for both capabilities. Second string is empty if the toy only has one capability.             |
| `model_name`               | string               | New model name assigned to the toy.                                                                                    |
| `supports_rotation`        | boolean              | If `true`, the toy allows for its rotation direction to be changed.                                                    |
| `max_intensity`            | int                  | New maximum intensity level (equal for both capabilities). Values outside the range (0-max) are clamped automatically. |
| `recommended_min_interval` | int                  | New recommended minimum interval between intensity commands (in ms). Especially useful for pattern playback.           |

---

### 5. `battery_changed`
One or more toys reported a new battery level. Battery values are updated in the background without client interaction.

**Data**
```json
{
  "updates": {
    "AA:BB:CC:DD:EE:FF": 85,
    "11:22:33:44:55:66": 12
  }
}
```

| Field     | Type                      | Description                                                                       |
|-----------|---------------------------|-----------------------------------------------------------------------------------|
| `updates` | dict[string, int or None] | Mapping of `toy_id` -> battery level (0‑100) or `None` if the toy has no battery. |


---

### 6. `on_scan_update`
One or more new toys are discovered or no longer available.
This event is sent only to clients that have started a scan (sent a `start_scan` request and not yet stopped it). Each event contains all currently discovered toys.


#### Success case

**Data fields**
```json
{
  "discovered": [
    { "toy_id": "AA:BB:CC:DD:EE:FF", "name": "Vibe", "model_name": "", "brand": "Vibe" },
    { "toy_id": "11:22:33:44:55:66", "name": "LVS-Gush", "model_name": "Gush", "brand": "Lovense"}
  ]
}
```

| Field        | Type       | Description                                                                               |
|--------------|------------|-------------------------------------------------------------------------------------------|
| `discovered` | list[dict] | List of newly discovered toys. Each dict contains `toy_id`, `name`, `model_name`, `brand` |

Where:
- `toy_id` is a unique identifier of the toy.
- `name` is a human-readable identifier of the toy.
- `model_name` is the model name of the toy (if known), else the emtpy string.
- `brand` is the brand of the toy.


#### Error case
If an error occurs (e.g., Bluetooth hardware becomes unavailable), the server broadcasts an error update.

**Data (Discovery Error)**
```json
{
  "error": "Discovery Error",
  "message": "Discovery of toys failed. Please verify that Bluetooth is enabled. Contact the developer if the problem persists.",
  "traceback": "Traceback (most recent call last): ..."
}
```

**Data (Developer Error)**
```json
{
  "error": "Developer Error",
  "message": "Unexpected error occurred in the TIKAL Web-API. If you see this, please contact MoonShardFlower@gmail.com and provide the following: ...",
  "traceback": "Traceback (most recent call last): ..."
}
```

| Field       | Type | Description                  |
|-------------|------|------------------------------|
| `error`     | str  | Human readable error title   |
| `message`   | str  | Human readable error message |
| `traceback` | str  | Traceback of the error       |

Similar to replies, you can use the success field of the event envelope to determine whether the data field contains an error or success payload.

---

### 7. `heartbeat_timeout`
Fired when the heartbeat watchdog trips. This happens in two cases:
- A client subscribed to the heartbeat watchdog failed to send a `heartbeat` command within 3 seconds (`reason: "timeout"`), or
- A subscribed client **disconnected** while still subscribed, e.g., a crash or dropped connection (`reason: "disconnect"`).

In both cases the server **blocks every toy** that is not blocked yet, and stops it. The toys stay blocked until a
client unblocks them (`set_blocked` / `toggle_block`), toy by toy; nothing unblocks them on its own. The watchdog's block
keeps a toy's pause, so a paused toy stays paused once it is unblocked. Each blocked toy's new state is broadcast as
`toy_state_changed`. See the `enable_heartbeat` action in **actions.md** for the full rules.

A client that stays overdue for 30 seconds is disconnected with close code `4000`. That blocks nothing more: the toys
were blocked when it went overdue.

If a toy cannot be stopped (the stop and an immediate retry both failed, e.g., because the connection dropped), it is
listed in `failed_toy_ids`. It is blocked regardless, and the server reconnects to it for up to about a minute, stopping
it as soon as the connection is back. Watch `connection_status_changed`: `connected` means the toy was stopped; `lost`
means the server gave up and removed it, and can no longer tell whether it is still running.

The event is sent every time the watchdog trips, also when every toy was blocked already, so you learn about each client
that went overdue or disconnected. It is broadcast to **all** connected clients, not just the affected one.

**Data**
```json
{
  "message": "Heartbeat timeout. All toys were blocked.",
  "reason": "timeout",
  "blocked_toy_ids": ["AA:BB:CC:DD:EE:FF"],
  "failed_toy_ids": []
}
```

| Field             | Type            | Description                                                                                        |
|-------------------|-----------------|----------------------------------------------------------------------------------------------------|
| `message`         | string          | Human-readable description of what happened.                                                       |
| `reason`          | string          | `"timeout"` (a subscribed client is overdue) or `"disconnect"` (a subscribed client disconnected). |
| `blocked_toy_ids` | list of strings | Toys this trip blocked. Toys that were blocked already are not listed.                             |
| `failed_toy_ids`  | list of strings | Toys among them whose stop failed even after an immediate retry (see above).                       |
