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

**Data**
```json
{
  "toy_id": "00:00:00:00:00:01",
  "status": "reconnecting"
}
```

| Field    | Type   | Description                                                                    |
|----------|--------|--------------------------------------------------------------------------------|
| toy_id   | string | Unique identifier of the toy (e.g., Bluetooth address).                        |
| status   | string | Current status: `"connected"`, `"reconnecting"`, `"lost"`, or `"powered_off"`. |

---

### 2. `toy_ids_changed`
The set of toys managed by the server changed: a toy was added or removed.

**Data**
```json
{
  "toy_ids": ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"]
}
```

| Field   | Type         | Description                                      |
|---------|--------------|--------------------------------------------------|
| toy_ids | list[string] | Snapshot of all toy identifiers currently known. |

---

### 3. `toy_state_changed`
Any part of a toy’s internal state has changed (intensities, pattern, pause/block state, pattern version, elapsed time)

**Data**
```json
{
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "current_intensities": [0, 0],
  "is_blocked": false,
  "pattern_version": 3,
  "pattern": [[500, 100, 0], [500, 0, 100]],
  "wraparound": true,
  "is_paused": false,
  "elapsed": 123.4
}
```

| Field               | Type                     | Description                                                                                                  |
|---------------------|--------------------------|--------------------------------------------------------------------------------------------------------------|
| toy_id              | string                   | Unique identifier of the toy.                                                                                |
| current_intensities | list[int]                | `[intensity1, intensity2]`; second value is always `0` for single‑intensity toys.                            |
| is_blocked          | bool                     | `true` if the toy is forced to zero intensities.                                                             |
| pattern_version     | int                      | Increments each time the pattern state changes.                                                              |
| pattern             | list[tuple[int,int,int]] | Active pattern as a list of `(duration_ms, intensity1, intensity2)` segments.                                |
| wraparound          | bool                     | `true` if the pattern loops after the last segment; `false` if it stops.                                     |
| is_paused           | bool                     | `true` when pattern playback is paused (intensities zero, timer frozen).                                     |
| elapsed             | float                    | Milliseconds elapsed since the start of the pattern or the last wraparound (does not advance during pauses). |

---

### 4. `model_changed`
The model name assigned to an already‑added toy has changed (via the `set_model` command).

**Data**
```json
{
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Gush",
  "brand": "Lovense"
}
```

| Field       | Type   | Description                               |
|-------------|--------|-------------------------------------------|
| toy_id      | string | Unique identifier of the toy.             |
| model_name  | string | New model name assigned to the toy.       |

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

| Field   | Type                      | Description                                                                       |
|---------|---------------------------|-----------------------------------------------------------------------------------|
| updates | dict[string, int or None] | Mapping of `toy_id` -> battery level (0‑100) or `None` if the toy has no battery. |


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

| Field       | Type          | Description                                                                               |
|-------------|---------------|-------------------------------------------------------------------------------------------|
| discovered  | list[dict]    | List of newly discovered toys. Each dict contains `toy_id`, `name`, `model_name`, `brand` |

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

| Field     | Type | Description                  |
|-----------|------|------------------------------|
| error     | str  | Human readable error title   |
| message   | str  | Human readable error message |
| traceback | str  | Traceback of the error       |

Similar to replies, you can use the success field of the event envelope to determine whether the data field contains an error or success payload.

---
