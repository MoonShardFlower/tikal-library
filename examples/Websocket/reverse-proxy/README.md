# Exposing the Tikal Toy Server beyond localhost

By default, Tikal only listens on `localhost` and has **no built‑in authentication**. 
Anyone who can reach that port has full control over every connected toy. To use it from another device, you need to 
add **encryption** and **authentication** yourself. This folder shows you how.

## Tailscale or Caddy?

| Your situation                                                                                    | What to use           | Why                                                                             |
|---------------------------------------------------------------------------------------------------|-----------------------|---------------------------------------------------------------------------------|
| Only your own devices (phone, laptop)                                                             | Tailscale (Option C)  | Easier: No proxy, no certificates, no domain, no port‑forwarding.               |
| Giving someone else access with just a URL + password, or using a device outside your own network | Caddy (Option A or B) | Provides HTTPS and username/password that any native client or browser can use. |

---

## Choose your setup

### Option A: Public domain (Caddy + Let's Encrypt) · *you own a domain*

This gives you a real certificate, no security warnings, and reachability from anywhere.

Requirements: a domain you can point to this machine, and ports **80 + 443** forwarded to it from your router.

1. Edit the `Caddyfile`, keep **Variant A**, and set your domain.
2. Hash a password: `caddy hash-password` – paste the output after `tikal` in the Caddyfile.
3. Start tikal on its default localhost bind: ```sh tikal-server```
4. Start the proxy from this directory: ```shcaddy run```

Clients connect to `wss://toys.example.com/` with username `tikal` and your password.  
The status page is available at `https://toys.example.com/` in a browser.

---

### Option B: LAN only (Caddy + `tls internal`) · *home network, no domain*

Use this when you want to control toys from another device on the same network. 

1. Edit the `Caddyfile`, switch to **Variant B**, and set your machine’s LAN IP (e.g. `192.168.1.50`).
2. Hash a password: `caddy hash-password` and put the hash into the Caddyfile.
3. Trust Caddy's local CA so clients don't show warnings:
   - on the server machine: run `caddy trust`
   - on other devices: install Caddy's `root.crt` as a trusted root (the path is printed in the Caddyfile comments)
4. Run tikal and Caddy (in separate terminals):
   ```sh
   tikal-server
   caddy run
   ```

Clients connect to `wss://192.168.1.50/` with username `tikal` and your password.

---

### Option C: Tailscale · *secure access across your own devices*

This is the simplest way to reach your PC from anywhere without a domain, certificates, or port‑forwarding. 
Tailscale creates a private WireGuard network between your devices. Only your signed‑in devices can connect.

1. Install Tailscale on the server and on each client, signed in to the same account: https://tailscale.com/download
2. Find this machine's Tailscale IP (starts with `100.`): ```sh tailscale ip -4```
3. Bind tikal to that address. Since this isn't loopback, you need `--insecure` . 
   That’s ok because Tailscale protects the connection: ```sh tikal-server --host 100.x.y.z --insecure```

Clients connect to `ws://100.x.y.z:8142/`. 
Plain `ws://` is safe because Tailscale encrypts everything on the wire, and only your tailnet peers can reach that IP.

> Want a real `https://...` hostname and a valid certificate on top of the tailnet? 
> `tailscale serve` can front a localhost port for you. Keep tikal on its default `tikal-server` bind and point 
> `tailscale serve` at port `8142` (check your Tailscale version's docs for the exact syntax). To reach it from devices
> **not** on your tailnet, you can use a Tailscale Funnel or a **Cloudflare Tunnel** (which can add its own auth).
> Both are alternatives to the Caddy options above, not additions to them.

---

## Windows quick‑start (Caddy)

1. Download `caddy.exe` from https://caddyserver.com/download (select Windows and download).
2. Place `caddy.exe` in this folder, next to the `Caddyfile`.
3. Open PowerShell here and generate a password hash:
   ```powershell
   .\caddy.exe hash-password
   ```
   Paste the result into the `Caddyfile` after `tikal`.
4. Start tikal in another terminal, then run Caddy:
   ```powershell
   tikal-server
   .\caddy.exe run
   ```
   If Windows Firewall asks to allow Caddy, say yes. It needs to listen on port 443.

`caddy run` automatically loads the `Caddyfile` from the current directory.

---

## What NOT to do

- **Do not inject an `Origin` header** at the proxy. 
  Tikal rejects any WebSocket handshake that carries one (this prevents cross‑site hijacking). 
  Caddy's `reverse_proxy` and the provided `nginx.conf` already behave correctly.
- **Do not expect a browser to speak the control API.** 
  Browsers can't set the `Authorization` header on `new WebSocket()`, and tikal rejects browser origins. 
  The WebSocket API is only for native clients (the Tikal app, Python scripts, etc.). A browser can only show the status page.
- **Do not use `--insecure` as a shortcut** to expose tikal to the open internet. 
  It disables safety checks but adds no encryption or auth. Only use it when something else (like a tailnet or a trusted, firewalled LAN) is protecting the port.

---

## Appendix: nginx

Prefer nginx? See `nginx.conf` in this directory.
```