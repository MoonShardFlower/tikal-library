# Tikal Toy Server WebSocket API

The Toy Server provides a WebSocket-based API for controlling toys.
Multiple clients can connect to the server at the same time (Warning: unlike single connections, this is poorly tested)

I suggest reading the documentation in the following order:
- **documentation.md** (this file)
- **actions.md** while keeping **error.md** open in parallel
- **events.md** while keeping **error.md** (this file) open in parallel

## Starting the Server
The server can be started with the command `python3 -m tikal_web_server.toy_server`.

Optional arguments are:
- `--host <host>`: defaults to `localhost` and defines the host to bind to.
No authentication is performed, so I strongly disadvise choosing a different host.
- `--port <port>`: defaults to `8142` and defines the port to bind to.
- `--timeout <seconds_to_timeout>`: defaults to `3` and defines how long the server waits for a client to connect. If timeout expires, the server shuts itself down.
- `--toy-cache-path <path_to_cache_file>`: defaults to `./data/toy_cache.json` and defines the path and name of the toy cache file.
If set to the string "None", the cache degrades to in-memory only (No persistence)
If the file does not exist, it will be created. If the path up to the file does not exist, it will be created.
- `--log-level <level>`: defaults to `ERROR` and defines the log level. Must be one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
- `--log-file <path_to_log_file>`: defaults to `"./data/tikal_ws.log"` and defines the path and name of the log file.
If set to the string "None", no log file is used. If the file does not exist, it will be created. If the path up to the file does not exist, it will be created.

Example including all arguments:
`python3 -m tikal_web_server.toy_server --host 0.0.0.0 --port 8081  --timeout 4 --toy_cache_path ./pretty/cache.json --log-level DEBUG --log-file ./important/log.txt`

The server shuts down automatically if no client is connected to it for a period of 3 seconds.


## Message Format

All messages are JSON-encoded. All keys are lower-case strings encoded in utf-8. Values can be of different types.
Assume case sensitivity for all strings (unless otherwise specified)

### Request Envelope
Every request from the client must contain the keys `request` (str), `id` (str) and `data` (dict). No additional keys are allowed.
The server does not care about the value of `id`, just that it is a string and present. It serves for you to match replies to your requests.
All possible requests and their necessary data can be found **actions.md**.

```json
{
  "request": "some_action",
  "id": "some_id",
  "data": {"some":  "data"}
}
```

### Reply Envelope
The server responds to every request with the keys `reply` (str), `success` (bool),  `id` (str) and `data` (dict).
The `reply` key holds the name of the action that was requested.
The `success` key holds a boolean indicating whether the action was successful or not.
The `id` key holds the same value as in the request.
The `data` key holds a dictionary containing the reply-specific data defined in **actions.md**

```json
{
  "reply": "some_action",
  "id": "some_id",
  "success": true,
  "data": {"some":  "data"}
}
```

If an error occurs, the keys `reply`, `success`, `id` and `data` will be present, same as above.
The data dictionary will in this case contain the keys `error` (str) and `message` (str) and may contain additional keys depending on the error.

```json
{
  "reply": "some_action",
  "id": "some_id",
  "success": false,
  "data": {
    "error": "Human readable error title e.g. Not Authenticated",
    "message": "Human readable error message e.g. Please authenticate first."
  }
}
```

**error.md** will detail all possible errors. I suggest keeping it open in parallel while reading **actions.md** later.


### Event Envelope
Changes to the server state that are not a result of a request are communicated via events instead of replies.
Events contain the keys `event` (str) holding the name of the event, `success` (bool) and `data` (dict).
`data` contains event-specific keys. See **events.md** for all possible events.

```json
{
  "event": "a_very_important_event",
  "success": true,
  "data": {"some":  "data"}
}
```

If an error occurs, `data` will contain the keys `error` (str) and `message` (str).
Similar to the above, data` can contain additional keys depending on the event.

```json
{
  "event": "a_very_important_error_event",
  "success": false,
  "data": {
    "error": "Human readable error title e.g. Developer Error",
    "message": "Human readable error message e.g. see below"
  }
}
```
For developer errors, the message would be:
"Unexpected error occurred in the TIKAL Web-API. If you see this, please contact MoonShardFlower@gmail.com and provide the following: {details}"

### Status Page
The Server can serve you a simple status webpage (Read-Only, you currently can't adjust the server state from it).
To access the status page, call http://<host>:<port>/ in your web browser, 
replacing <host> and <port> with the values you used to start the server, e.g., http://localhost:8142/
