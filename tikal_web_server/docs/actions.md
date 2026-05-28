# Tikal Toy Server WebSocket API

The Toy Server provides a WebSocket-based API for controlling toys.
This document describes the different actions that can be performed.

I suggest reading the documentation in the following order:
- **documentation.md**
- **actions.md** (this file) while keeping **error.md** open in parallel.
- **events.md** while keeping **error.md** (this file) open in parallel

## Actions

Below is a list of all available actions.
For each action I will detail the structure of the request and reply data and provide a list of all possible errors.
The full request and response envelope is described in **documentation.md**.
See **errors.md** for information about errors.

There are a lot of possible actions. I suggest focusing on one at a time, starting from the top and working your way down.
I sorted the actions in a way that allows for this approach.

**Tip** The Server provides the option of mocking toys. The example values of the commands below are written specifically for usage with a mocked Lovense Solace.


### 1. `get_brands`

Retrieve the mapping of supported toy brands to their model names.

**Request data**: `{}` (empty dict)

**Response data**:
```json
{
  "brands": {
    "Lovense": ["Gush", "Solace", "Nora"]
  }
}
```
This is an example shape. The list of Lovense models is a lot longer, and there might be other brands present.

**Possible errors**
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., not the empty dict.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 2. `start_scan`
This action allows you to discover toys that you can connect to, by subscribing to the `discovered_toys` event.
You should skip events for now. Instead, start the ToyServer with the --mock-toys flag, then start the scan, wait 3 seconds,
before using "00:00:00:00:00:01" as toy_id for the other actions. **events.md** will later explain the `discovered_toys` event.

**Request data**: `{}`

** Response data**:
```json
{
  "ack": true,
  "toy_id": null
}
```

**Possible errors**
- Discovery Start Error: Scanning failed to start (Bluetooth off / permission issues).
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., not the empty dict.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 3. `add`
This action allows you to connect to a previously discovered toy.
This action takes a long time, so the reply only acknowledges the request, and the actual result will be communicated via an event.
You should skip events for now. After adding the mock toy (Solace, 00:00:00:00:00:01) wait 5 s, then use "00:00:00:00:00:01" as toy_id for the other actions.
You can start a new `add` request (ONLY for a DIFFERENT toy!) while the previous one is still pending.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "model_name": "Solace"
}
```

| Field        | Type   | Description                                                                       |
|--------------|--------|-----------------------------------------------------------------------------------|
| `toy_id`     | string | Toy ID (as discovered via scan).                                                  |
| `model_name` | string | model name must be valid for the toy’s brand (see `get_brands`). Case‑insensitive |

**Response data**:
```json
{
  "ack": true,
  "toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Undiscovered Toy: The toy was never seen during scanning.
- Unavailable Toy: The toy was discovered earlier but is no longer advertising.
- Toy Already Added: The toy is already connected or being added.
- Connection Error: Could not connect to the toy.
- Invalid Model: the `model_name` is not valid for the brand.
- Bad Model: the model name is valid, but the toy does not respond correctly.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 4. `stop_scan`
This action unsubscribes from the `discovered_toys` event. You should not add toys while unsubscribed.

**Request data**: `{}`

**Response data**:
```json
{
  "ack": true,
  "toy_id": null
}
```

**Possible errors**
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., not the empty dict.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 5. `get_toy_ids`

Return a snapshot list of all toy IDs currently managed by the server.

**Request data**: `{}`

**Response data**:
```json
{
  "toy_ids": ["00:00:00:00:00:01"]
}
```
| Field     | Type         | Description                    |
|-----------|--------------|--------------------------------|
| `toy_ids` | list[string] | list of unique toy identifiers |

**Possible errors**:
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., not the empty dict.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 6. `get_battery`

Retrieve the in‑memory battery level (kept up‑to‑date in the background, no request to the toy, so this is fast).

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```
| Field    | Type   | Description            |
|----------|--------|------------------------|
| `toy_id` | string | Unique toy identifier. |

**Response data**:
```json
{
  "battery": 85,
  "toy_id": "00:00:00:00:00:01"
}
```
`battery` may be `null` if the toy has no battery.

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 7. `get_connection_status`

Retrieve the in‑memory connection status of the toy.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

**Response data**:
```json
{
  "connection_status": "connected",
  "toy_id": "00:00:00:00:00:01"
}
```
`connection_status` may be any of `connected`, `reconnecting`, `lost` and `powered_off`.

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 8. `get_state`
Retrieve the in‑memory state of a toy. This is completely retrieved from the software representation of the toy, so this is fast.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

| Field    | Type   | Description            |
|----------|--------|------------------------|
| `toy_id` | string | Unique toy identifier. |

**Response data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "current_intensities": [5, 0],
  "is_blocked": false,
  "pattern_version": 3,
  "pattern": [[1000, 5, 0], [500, 0, 0]],
  "wraparound": true,
  "is_paused": false,
  "elapsed": 245.0
}
```

| Field                 | Type            | Description                                                                                     |
|-----------------------|-----------------|-------------------------------------------------------------------------------------------------|
| `toy_id`              | string          | Unique Toy identifier.                                                                          |
| `current_intensities` | [int, int]      | Current intensity levels. Second value is always `0` if the toy has no second capability.       |
| `is_blocked`          | boolean         | `true` if the toy is blocked (both intensities forced to zero).                                 |
| `pattern_version`     | integer         | Increments each time the pattern state changes.                                                 |
| `pattern`             | array of arrays | Active pattern segments: `[duration_ms, intensity1, intensity2]`. `[]` if no pattern is active. |
| `wraparound`          | boolean         | Whether the pattern loops back to the start after finishing.                                    |
| `is_paused`           | boolean         | `true` if pattern playback is paused.                                                           |
| `elapsed`             | float           | Milliseconds elapsed since pattern start / last wraparound. Does not advance during pauses.     |

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 9. `get_info`
Retrieve information about a toy.
Use `full=false` for fast in‑memory data only; `full=true` may request additional brand‑specific data from the toy.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "full": false
}
```

| Field    | Type    | Description                                       |
|----------|---------|---------------------------------------------------|
| `toy_id` | string  | Toy identifier.                                   |
| `full`   | boolean | If `true`, query additional brand‑dependent info. |

**Response data** (always present, even if `full=false`):
```json
{
  "toy_id": "00:00:00:00:00:01",
  "name": "LVS-B12",
  "model_name": "Solace",
  "brand": "Lovense",
  "intensity_names": ["Thrust", "Depth"],
  "supports_rotation": false,
  "max_intensity": 20
}
```
When `full` is `true`, extra brand-specific fields (e.g., `batch`) may appear.

| Field               | Type                 | Description                                                                                                        |
|---------------------|----------------------|--------------------------------------------------------------------------------------------------------------------|
| `toy_id`            | string               | Unique toy identifier.                                                                                             |
| `name`              | string               | Human readable toy identifier.                                                                                     |
| `model_name`        | string               | Model of the toy.                                                                                                  |
| `brand`             | string               | Brand of the toy.                                                                                                  |
| `intensity_names`   | list[string, string] | Human readable names for both capabilities. Second string is empty if the toy only has one capability.             |
| `supports_rotation` | boolean              | If `true`, the toy allows for its rotation direction to be changed.                                                |
| `max_intensity`     | int                  | Maximum intensity level (equal for both capabilities). Values outside the range (0-max) are clamped automatically. |

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding. Only possible if `full=true`
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 10. `get_all`
Convenience action that returns combined battery, connection status and state and information about the toy.
Use `full=false` for fast in‑memory data only; `full=true` may request additional brand‑specific data from the toy.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "full": false
}
```

**Response data** (always present, even if `full=false`):
```json
{
  "toy_id": "00:00:00:00:00:01",
  "name": "LVS-B12",
  "model_name": "Solace",
  "brand": "Lovense",
  "intensity_names": ["Thrust", "Depth"],
  "supports_rotation": false,
  "max_intensity": 20,
  "battery": 85,
  "connection_status": "connected",
  "current_intensities": [5, 0],
  "is_blocked": false,
  "pattern_version": 3,
  "pattern": [[1000, 5, 0], [500, 0, 0]],
  "wraparound": true,
  "is_paused": false,
  "elapsed": 245.0
}
```
When `full` is `true`, extra brand-specific fields (e.g., `batch`) may appear.

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding. Only possible if `full=true`
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 11. `intensity1` / `intensity2`
Set the primary (intensity1) or secondary (intensity2) capability intensity.
Sending a manual intensity command automatically **pauses** any running pattern.
Intensity commands are ignored if the toy is **blocked** (which forces both intensities to zero).
Intensity values are clamped to the range (0-`max_intensity`).

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "intensity": 10
}
```

| Field       | Type    | Description                                                                                          |
|-------------|---------|------------------------------------------------------------------------------------------------------|
| `toy_id`    | string  | Unique toy identifier                                                                                |
| `intensity` | integer | Target intensity level (0-`max_intensity`). Values outside the valid range are clamped automatically |

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```
`ack` is `true` if the intensity was set; `false` if the toy is blocked, or you try to set intensity2 on a toy that only has one capability.

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 12. `change_rotation_direction`
Toggle the rotation direction of a toy that supports it.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

**Response data**:
```json
{
  "ack": true,
  "toy_id": "00:00:00:00:00:01"
}
```
`ack` is `true` if the toy’s direction was toggled; `false` if changing the rotation direction is not supported by the toy.

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 13. `stop`
Sets both intensities to zero and pauses any active pattern.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 14. `toggle_block`
Toggle the blocked state. When blocked, both intensities are forced to zero regardless of the pattern or manual commands.
Blocking a paused toy clears the paused state.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 15. `set_blocked`
Explicitly set the blocked state. See `toggle_block` for details.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "block": true
}
```

| Field    | Type    | Description                          |
|----------|---------|--------------------------------------|
| `toy_id` | string  | Unique toy identifier                |
| `block`  | boolean | `true` to block, `false` to unblock. |

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 16 `toggle_pause`
Toggle the paused state (stopping the pattern playback). While paused, patterns do not advance.
If unpaused, pattern playback resumes from the current position. Manual intensity commands can still set the toys' intensity.
If the toy is blocked, pausing the toy will unblock it (A toy can't be paused and blocked at the same time).

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

### 17 `set_paused`
Explicitly set the paused state. See `toggle_pause` for details.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "pause": true
}
```

| Field    | Type    | Required | Description                         |
|----------|---------|----------|-------------------------------------|
| `toy_id` | string  | yes      | Toy identifier.                     |
| `pause`  | boolean | yes      | `true` to pause, `false` to resume. |

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 18. `set_pattern`
Sets a new pattern to a toy. Playback starts immediately. Patterns are lists of segments `[duration_ms, intensity1, intensity2]`.
You can clear the pattern by setting `pattern` to an empty list.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "pattern": [[1000, 10, 0], [500, 5, 0]],
  "wraparound": true,
  "reset_time": false
}
```

| Field        | Type                   | Description                                                                    |
|--------------|------------------------|--------------------------------------------------------------------------------|
| `toy_id`     | string                 | Unique toy identifier.                                                         |
| `pattern`    | array of [int,int,int] | Sequence of segments. Each segment is `[duration_ms, intensity1, intensity2]`. |
| `wraparound` | boolean                | If `true` loop after final segment; else stop and set intensities to zero.     |
| `reset_time` | boolean                | If `true` restart elapsed counter; else continue from current elapsed time.    |

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Validation Error: At least one pattern segment is not exactly three integers.
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 19. `direct_command`
Send a raw command string directly to the toy.
Use this to access functionality not exposed by the API.
**Do not** change tracked state (like intensities) this way as the server would be unaware of the change.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "command": "GetBatch"
}
```

| Field     | Type   | Description            |
|-----------|--------|------------------------|
| `toy_id`  | string | Unique toy identifier. |
| `command` | string | Raw command string.    |

**Response data**:
```json
{
  "response": "241015",
  "toy_id": "00:00:00:00:00:01"
}
```
`response` is the raw response string of the toy.

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Connection Error: The toy is currently not responding.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 20. `set_model`
Change the model name assigned to an already‑added toy. In most cases bad model names are caught while adding the toy, as
model-dependent commands are attempted and would fail if the wrong model name is assigned to the toy.
Similar to `add` the `model_name` is case-insensitive here.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01",
  "model_name": "Sex Machine"
}
```
Similar to `add` this can also raise Validation / BadModel errors.
Solace and Sex Machine use the same commands, so setting our mocked Solace to Sex Machine will not raise any errors.

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Unknown Toy: The provided toy ID is not known to the server.
- Invalid Model: The model name is invalid for the toys' brand.
- Bad Model: The model name is valid, but the toy does not accept commands.
This means either the toy is of a different model or the commands are wrong (developer issue)
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 21 `remove`
Disconnect a toy and remove it from the server.

**Request data**:
```json
{
  "toy_id": "00:00:00:00:00:01"
}
```

**Response data**:
```json
{
"ack": true,
"toy_id": "00:00:00:00:00:01"
}
```

**Possible errors**
- Connection Error: Cleanly disconnecting failed. Just for logging. The toy is still removed.
- Unknown Toy: The provided toy ID is not known to the server.
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, e.g., missing the `toy_id` field.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com

---

### 22 `shutdown`
Shut down the server. Any toys still connected will be automatically stopped and disconnected.
The command is acknowledged right away. 
However, the actual shutdown is only triggered once the client who made the request has closed its Websocket connection.

**Request data**
```json
{}
```

**Response data**
```json
{
"ack": true,
"toy_id": null
}
```

**Possible errors**
- Malformed Request: Your request is wrong. See the request envelope defined in **documentation.md**.
- Invalid Data: Your request data is wrong, meaning not the empty dict.
- Developer Error: Congratulations, you found a bug! Please report it to MoonShardFlower@gmail.com
