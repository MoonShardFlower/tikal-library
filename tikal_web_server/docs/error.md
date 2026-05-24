# Tikal Toy Server WebSocket API

The Toy Server provides a WebSocket-based API for controlling toys.
This document describes the different errors that can occur.

I suggest reading the documentation in the following order:
- **documentation.md**
- **actions.md** while keeping **error.md** (this file) open in parallel
- **events.md** while keeping **error.md** (this file) open in parallel

Most errors only occur in replies. I will specifically mention if an error can occur in an event.

## Error Payload
If the `success` field of a response or event envelope is `false`, the `data` field always follows the schema below:

```json
{
  "error": "Some Human readable error name",
  "message": "Some Human readable error message",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": null,
  "brand": null
}
```
The fields error and message are always strings. The other keys are either strings or null depending on the error.

## Common Errors

### 1. Malformed Request

**Payload:**
```json
{
  "error": "Malformed Request",
  "message": "Unable to parse request. Please verify the envelope is correctly formed and contains all required fields (request, id, data).",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": null,
  "model_name": null,
  "brand": null
}
```
In this special case the fields `reply` and `id` of the response envelope are set to `"?"`

---

### 2. Unknown Command

**Payload:**
```json
{
  "error": "Unknown Command",
  "message": "Unknown command '{cmd}'. Please verify the command name is correct.",
  "traceback": null,
  "toy_id": null,
  "model_name": null,
  "brand": null
}
```

---

### 3. Invalid Data

**Payload:**
```json
{
  "error": "Validation Error",
  "message": "Validation of data field failed for command '{cmd}' failed. Please check field types and required keys: {details}",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": null,
  "model_name": null,
  "brand": null
}
```

---

### 4. Developer Error

**Payload:**
```json
{
  "error": "Developer Error",
  "message": "Unexpected error occurred in the TIKAL Web-API. If you see this, please contact MoonShardFlower@gmail.com and provide the following: {details}",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": null,
  "model_name": null,
  "brand": null
}
```
This error can occur in replies AND events.

---

## Command-specific errors

### 1. Unknown Toy

**Payload:**
```json
{
  "error": "Unknown Toy",
  "message": "Unable to execute '{cmd}' on '{toy_id}'. Please add the toy first.",
  "traceback": null,
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": null,
  "brand": null
}
```

---

### 2. Connection Error

**Payload:**
```json
{
  "error": "Connection Error",
  "message": "Unable to add toy '{toy_id}'. Please verify that the toy is still turned on and not connected anywhere else. Contact the developer if the problem persists.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Solace",
  "brand": null
}
```

OR

```json
{
  "error": "Connection Error",
  "message": "Unable to execute '{cmd}' on toy '{toy_id}'. Please verify that the toy is still turned on and not connected anywhere else. Will attempt to reconnect.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": null,
  "brand": null
}
```

---

### 3. Bad Model

**Payload:**
```json
{
  "error": "Bad Model",
  "message": "The model name '{model_name}' is valid but they toy '{toy_id}' does not correctly respond to commands. Please check if the model name is correct. It it is please contact the developer.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Solace",
  "brand": null
}
```
This error can only occur in the `add` and `set_model` commands.

---

### 4. Invalid Model

**Payload:**
```json
{
  "error": "Invalid Model",
  "message": "The model name '{model_name}' is not a valid model name for '{toy_id}'. Use get_brands to get a list of valid model names for each brand.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Solace",
  "brand": "Lovense"
}
```
This error can only occur in the `add` and `set_model` commands.

---

### 5. Undiscovered Toy

**Payload:**
```json
{
  "error": "Undiscovered Toy",
  "message": "Unable to add toy '{toy_id}'. This toy was never discovered.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Solace",
  "brand": null
}
```
This error can only occur in the `add` command.

---

### 6. Unavailable Toy

**Payload:**
```json
{
  "error": "Unavailable Toy",
  "message": "Unable to add toy '{toy_id}'. This toy was discovered at some point, but has since then become unavailable.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Solace",
  "brand": null
}
```
This error can only occur in the `add` command.

---

### 7. Toy Already Added

**Payload:**
```json
{
  "error": "Toy Already Added",
  "message": "Unable to add toy '{toy_id}'. This toy was already added or is currently being added.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": "AA:BB:CC:DD:EE:FF",
  "model_name": "Solace",
  "brand": null
}
```
This error can only occur in the `add` command.

---

### 8. Discovery Start Error

**Payload:**
```json
{
  "error": "Discovery Start Error",
  "message": "Unable start discovery of toys. Please verify that Bluetooth is enabled. Contact the developer if the problem persists.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": null,
  "model_name": null,
  "brand": null
}
```
This error can only occur in the `start_scan` command.

---

### 9. Discovery Error

**Payload:**
```json
{
  "error": "Discovery Error",
  "message": "Discovery of toys failed. Please verify that Bluetooth is enabled. Contact the developer if the problem persists.",
  "traceback": "Traceback (most recent call last): ...",
  "toy_id": null,
  "model_name": null,
  "brand": null
}
```
This error can only occur in the `discovered_toys` event.

---
