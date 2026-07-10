# Securing the Tikal Toy Server

## TL;DR

- tikal performs **no authentication of its own**. Anyone who can reach the port has full control of every connected toy.
- Therefore, the server **only binds `localhost` by default**, and **refuses a non-loopback `--host` unless you pass `--insecure`**.
- To expose it to a network safely, keep tikal on `localhost` and put a **reverse proxy** in front that terminates
  **TLS** and enforces **authentication**.

## Warning

The WebSocket API is fully privileged: a client can connect a toy, drive its intensities, and control it without limit.
There is no login inside tikal. The only thing standing between a toy and the outside world is 
**who can open a TCP connection to the port**.

On `localhost` that's just processes on the same machine. The moment the port is reachable from a network, 
control is open to that whole network. To avoid accidental exposure, you need to explicitly allow non-localhosts 
bindings by providing the --insecure flag:

| `--host`                                          | Result                                                             |
|---------------------------------------------------|--------------------------------------------------------------------|
| `localhost`, `127.0.0.1` (any `127.x.x.x`), `::1` | Starts normally (loopback only)                                    |
| `0.0.0.0`, `::`, a LAN IP, a hostname             | **Refused** with `InsecureBindError` unless `--insecure` is passed |
| any of the above **+ `--insecure`**               | Starts, with a warning logged                                      |

`--insecure` is intended for when the server is protected some other way (a host firewall, a trusted LAN...).

## Recommended setup: a reverse proxy

Run tikal bound to `localhost` and let a proxy handle TLS and authentication. Two examples that both do
**HTTPS + HTTP Basic auth** and forward to a local tikal on `127.0.0.1:8142`.

### Caddy

Caddy provisions TLS automatically (public domain via Let's Encrypt, or a local CA for LAN names).

```caddy
toys.example.com {
    basic_auth {
        # generate the hash with:  caddy hash-password
        tikal $2a$14$Fq0m...replace-with-your-bcrypt-hash...
    }
    reverse_proxy 127.0.0.1:8142
}
```

### nginx

```nginx
server {
    listen 443 ssl;
    server_name toys.example.com;

    ssl_certificate     /etc/ssl/tikal.crt;
    ssl_certificate_key /etc/ssl/tikal.key;

    auth_basic           "tikal";
    auth_basic_user_file /etc/nginx/tikal.htpasswd;   # created with: htpasswd -c ... tikal

    location / {
        proxy_pass http://127.0.0.1:8142;

        # Required so the WebSocket upgrade is forwarded:
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Tikal is then started with no exposed bind (this stays on the default loopback host):

```sh
tikal-server
```

## How clients authenticate through the proxy

The proxy enforces auth at the HTTP layer, so it protects **both** surfaces with the same credentials:

- **WebSocket API (native clients).** The opening handshake is an HTTP request, and native clients (the Tikal app,
  a Python client) can set headers on it, so they send `Authorization: Basic base64("user:password")` on connect.
  The proxy validates it before the connection ever reaches tikal.
- **Status page (browser, read-only).** Visiting `https://toys.example.com/` is a normal GET, so the browser shows its
  built-in Basic-auth login dialog. This is the only browser-facing surface; the WebSocket control API is native-only
  (browsers cannot set `Authorization` on `new WebSocket()`).

## The origin check

Independently of any proxy, tikal guards the WebSocket handshake against cross-site WebSocket hijacking (a bad page the
user has open silently opening a socket). Only native clients (which send no `Origin` header) may open the WebSocket.
Every browser origin is rejected.

When you put a reverse proxy in front, make sure it does **not** inject an `Origin` header onto the forwarded
handshake (the example configs above do not).
