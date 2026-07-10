# Tikal Toy Server WebSocket API

The Toy Server provides a WebSocket-based API for controlling toys.
Multiple clients can connect to the server at the same time (Warning: unlike single connections, this is poorly tested)

I suggest reading the documentation in the following order:
- **documentation.md** (this file)
- **actions.md** while keeping **error.md** open in parallel
- **events.md** while keeping **error.md** open in parallel
- **security.md** before exposing the server beyond localhost

## Starting the Server
The server can be started with the command `tikal-server` (installed with the package) or, equivalently, `python -m tikal.websocket.cli`.

> **Security:** tikal performs **no authentication of its own** — anyone who can reach the port has full control of the connected toys.
> The server therefore refuses to bind a non-loopback `--host` unless you also pass `--insecure`.
> To expose it safely, keep it on `localhost` and put a reverse proxy (TLS + auth) in front. See **security.md**.

Optional arguments are:
- `--host <host>`: defaults to `localhost` and defines the host to bind to. Binding a non-loopback host (including `0.0.0.0`) is refused unless `--insecure` is also passed. See **security.md**.
- `--port <port>`: defaults to `8142` and defines the port to bind to.
- `--insecure`: allow binding a non-loopback `--host`. Only use this when the server is protected (reverse proxy, firewall, trusted LAN, or testing). See **security.md**.
- `--timeout <seconds_to_timeout>`: defaults to `3` and defines how long the server stays up after the last client disconnects before shutting itself down. Set to `0` to disable auto-shutdown.
- `--toy-cache-path <path_to_cache_file>`: defaults to `./data/toy_cache.json` and defines the path and name of the toy cache file.
If set to the string "None", the cache degrades to in-memory only (No persistence)
If the file does not exist, it will be created. If the path up to the file does not exist, it will be created.
- `--log-level <level>`: defaults to `INFO` and defines the log level. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`.
- `--log-path <path_to_log_file>`: defaults to `./data/tikal_ws.log` and defines the path and name of the log file.
If set to the string "None", no log file is used. If the file does not exist, it will be created. If the path up to the file does not exist, it will be created.

Example including all arguments:
`tikal-server --host 0.0.0.0 --insecure --port 8081 --timeout 4 --toy-cache-path ./pretty/cache.json --log-level DEBUG --log-path ./important/log.txt`

The server shuts down automatically if no client is connected to it for a period of `--timeout` seconds (default 3).


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
