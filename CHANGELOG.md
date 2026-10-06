# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and for versions >= 1.0.0 this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
    - Web API: New documentation file in ./docs/websocket/security.md
    - Web API: Ready-to-run reverse-proxy examples under ./examples/Websocket/reverse-proxy/ 
        (Caddyfile with public-domain and LAN variants, an nginx equivalent, and a decision guide covering Caddy vs. Tailscale plus a Windows quick-start).

### Changed
    - Web API: The heartbeat watchdog now blocks every toy (and stops it) instead of just stopping it. Toys stay blocked 
        until a client unblocks them. Unlike a block a client asks for, the watchdog's block keeps a toy's pause
        (block+pause possible at the same time).
    - Web API: The `heartbeat_timeout` event payload gained `reason` ("timeout" or "disconnect"), `blocked_toy_ids` and
        `failed_toy_ids`, so a client can tell which toys were blocked and which could not be reached (and may still be running).
    - Web API: `direct_command` is refused for a blocked toy (new error: Blocked Toy), as a raw command could drive it.
    - High-Level API + Web API: A toy whose connection fails is now reconnected with repeated attempts for up to one minute before
        it is given up (previously a single attempt, which the High-Level API also cut off after 5 seconds). Each attempt stops the
        toy and pauses its pattern once connected, so a stop that failed before (e.g., the heartbeat watchdog's) still gets through.
        Web API: a toy that is given up is declared lost and removed. High-Level API: the reconnection-failure callback fires and
        the toy is disconnected, as before.
    - High-Level API: After reconnecting, a toy is now stopped and its pattern paused instead of resuming on its own (a block is
        left alone). Resume with `set_paused(False)`.
    - High-Level API: Commands for a toy that is not connected (reconnecting, given up, powered off, or disconnected) are no longer
        queued: their callbacks receive None right away, as for a failed command. Previously they were sent up to a minute late
        after a reconnect, or never, with a callback that never fired.
    - High-Level API: `ToyHub` and `ToyController` are now a synchronous wrapper over the async core the WebSocket server runs on,
        so both APIs behave the same. What changes for High-Level users:
        - Commands are sent right away in the background, in the order they are called, instead of from a queue polled every 50 ms.
          State changes (pause, block, pattern, limits, and the pause a manual intensity causes) are visible as soon as the method returns.
        - A command that fails twice starts a reconnect (`on_disconnect` fires), like a lost connection. Pattern playback retries a
          send that failed; previously a failed stop when pausing or blocking was never retried.
        - A command that could not be delivered reports None to its callback (previously False for intensity and stop commands).
          `intensity2` on a toy without a secondary capability and `change_rotation_direction` on a toy without rotation report
          False (previously True).
        - `get_information` reports the same dictionary as the WebSocket `get_info` with `full=true`, plus `battery` (e.g., keys
          `model_name`, `intensity_names`, `status`), instead of human-readable keys such as "Battery level".
        - The battery callback receives every connected toy's last known level whenever one of them changes (checked every 2
          minutes) and after toys were connected or reconnected.
        - Connecting a toy requires a first battery query to succeed, as in the WebSocket API (`AddConnectionError` otherwise).
        - Errors are the core's exception classes, now exported from `tikal.high_level` (`InvalidModelError`, `BadModelError`,
          `AddConnectionError`, `ToyAlreadyAddedError`, `ToyConnectionError`, `ToyNotConnectedError`, `UnknownToyError`,
          `DiscoveryStartError`, `DiscoveryError`). `disconnect_toys_*` reports an unknown toy as `UnknownToyError` in its place in
          the result list (previously skipped, so the results no longer lined up with the input).
        - The ToyCache is updated only when a model change succeeds (`update_model_name` used to store an invalid model name too),
          and `ToyController.set_model_name` now updates it as well.
        - `on_error` also receives exceptions raised by your callbacks. They are caught, so they cannot break the toy communication..
    - Web API: Nothing is sent to a toy that is not connected (e.g., reconnecting). A command that needs the toy fails right away
        with a Connection Error whose message says the toy is not connected, instead of being tried twice (and, between two
        reconnect attempts, possibly getting through). A command that also changes the toy's state (block, pause, stop, pattern,
        intensity limits) still records that state; the reconnect stops the toy before it is used again.
    - Web API: Disconnecting the last client now stops every toy and pauses its pattern, regardless of the `--timeout` setting.
        Previously `--timeout 0` (auto-shutdown disabled) left a running pattern driving the toy with nobody connected.
    - Web API: The CLI now handles SIGINT/SIGTERM (where the platform supports it) by shutting the server down gracefully,
        so a terminating signal stops the toys instead of killing the process with toys still running.
    - Web API: Status webpage performs an origin check. Other webpages are forbidden to access the status page.
    - Web API: binding the websocket server to a non-localhost address is now only allowed if the server is started with the --insecure flag. 
        Else an error message is logged and the server terminated.
    - Web API: set_pattern now stores the pattern exactly as sent instead of baking the current intensity limits into it.
        get_state therefore reports the pattern you sent, and withdrawing a limit restores the pattern's own values.
    - High-Level API + Web API: Clearing a pattern (set_pattern with an empty list) still stops the toy, but no longer pauses it.
    - Web API: set_intensity1_limit / set_intensity2_limit now also bring a toy that is already running above the new limit down to it, instead of only clamping later commands.
    - Low-Level API: A model change now only interrupts the capabilities it has to. Capabilities both models drive with the same command
        keep their level (Solace -> Sex Machine interrupts nothing). A command that the new model replaces or drops is switched off as part of the change.
        The new model is validated first, so a rejected model (BadModelError) doesn't stop a running toy.
    - Low-Level API: The non-strict intensity1 / intensity2 only record the new level once the toy acknowledges the command.
        current_intensities therefore reports what the toy is actually at instead of what was last requested.
    - Low-Level API: New helper `carried_over_capabilities(old, new)` in toy_data, exported from tikal.low_level. Brand Toy implementations
        use it to decide which capabilities survive a model change.

### Added
    - Web API: `ToyServer.shutdown()`, a public, idempotent counterpart to `serve()` that stops and disconnects every toy and then closes the server.
    - High-Level API: `ToyHub` now registers `shutdown()` as an `atexit` safety net. A program that never calls it (including one ending in an
        uncaught exception or a KeyboardInterrupt) no longer leaves its toys running. The hook holds only a weak reference and is
        unregistered by an explicit `shutdown()`. It cannot help if the process is killed outright (SIGKILL, `os._exit`).
    - High-Level API: Intensity limits, as in the WebSocket API: `ToyController.set_intensity1_limit`, `set_intensity2_limit` and
        `intensity_limits`. A limit caps every command and pattern value, and brings a toy already running above it down right away.

### Removed
    - High-Level API: The module path `tikal.high_level.toy_cache` no longer exists. Import `ToyCache` from `tikal.high_level` instead
        (unchanged). ToyCache moved into the private async core that both APIs are built on.
    - High-Level API: `LovenseController` and `MockEstimController`. Every toy gets a `ToyController`, whatever its brand.
    - High-Level API: `ToyHub.is_running` (the background loop now runs from the hub's creation until `shutdown()`), and the
        internal `ToyController.toy`, `is_connected` setter, `process_communication` and `internal_*` methods.

### Fixed
    - High-Level API + Web API: Asking a Lovense toy for its full information (`get_information`, `get_info` / `get_all` with
        `full=true`) no longer fails when the toy does not know one of the requests.
    - Web API: A server that shuts down stops accepting connections right away, instead of only after it has stopped and
        disconnected every toy.
    - Web API: `--log-level` accepts lower-case level names (e.g., `debug`). Previously they crashed the CLI at startup.
    - High-Level API: A disconnected toy's `ToyController` no longer reports `is_blocked` as True.
    - Web API: `set_blocked`, `set_paused`, `toggle_block` and `toggle_pause` no longer undo themselves when the first attempt hits a
        connection error. The automatic retry used to flip the state back, so e.g. a block that hit a transient Bluetooth error left
        the toy unblocked while the command reported success.
    - Web API: A state change (block, pause, stop, pattern, limit) now takes full effect before the command it requires is sent.
        Previously, pausing a blocked toy whose stop failed left it paused and blocked at the same time.
    - High-Level API + Web API: A manual intensity command while a pattern plays is no longer undone right away. It pauses the pattern,
        and the next playback tick used to send the stop that pausing a pattern calls for, dropping the toy back to 0.
    - Web API: Stopping the server with Ctrl+C no longer leaves toys connected and running. `serve()` now tears the toy hub down in a
        `finally`, so cancellation still stops and disconnects every toy.
    - High-Level API + Web API: The toy cache file is now treated as untrusted input. Entries that are not `str -> str` are logged and
        skipped on load, so `get_model_name` always returns a str. 
        `ToyCache.update` filters the same way, and a non-str `default_model` falls back to an empty model name.
    - Low-Level API: A model rejected while connecting no longer leaks the BLE connection.
    - Web API: Intensity limits now apply to a pattern that is already playing. Previously a limit lowered mid-playback was ignored until
        the pattern was re-sent. Limits are now applied on every playback tick and to every manual command.
    - High-Level API + Web API: Pattern playback no longer skips a re-send after a model change. The switch stops the toy, so the values
        playback last sent no longer hold and must be re-issued.
    - Low-Level API: In-memory ToyCache now properly updates. Previously if the cache path was empty, the in-memory cache was not updated.
    - High-Level API + WebSocket API: Control loop now runs at interval = 50ms instead of interval + work_time ms
    - Packaging: Installation size of the built webserver executable significantly reduced (Prevented a few unneeded transitive dependencies from being included in the build)


## [1.2.0] - 2026-07-10

### Added
    - Packaging: Added a `tikal-server` console entry point. `pip install tikal` now provides the `tikal-server` command (previously only the bundled Windows executable or `python -m tikal.websocket.cli` worked).
    - Packaging: Added a py.typed marker. Downstream type checkers now see tikal's inline type hints.
    - Low-Level API: Added ToySpecification, a per-model dataclass (commands + recommended interval + rotation support). 
        Each brand now defines its models once (LOVENSE_TOY_SPECIFICATIONS / MOCK_ESTIM_TOY_SPECIFICATIONS) and derives its existing lookup tables from it. 
        The public LOVENSE_TOY_NAMES, ROTATION_TOY_NAMES and MIN_SEGMENT_LENGTH constants are unchanged in type and value.

### Changed
    - Web API:  Expanded Status webpage 
                (found at http://<host>:<port>/ where host and port are replaced with the configuration used to start the server e.g. http://localhost:8142/)
    - Internal: The High-Level ToyController and the WebSocket _ToyController now share a common base (BaseToyController) that owns the read-only toy passthroughs and the pattern-playback engine. No changes to the API.
    - High-Level API: ToyHub.shutdown() is now idempotent.

### Fixed
    - Low-Level API: set_model_name now validates the NEW model's commands (and restores the previous model name if they fail) instead of validating the already-set model. 
        Changing a connected toy to a different valid-but-wrong model is now correctly rejected.
    - Low-Level API: Connecting with a lower-case model_name no longer fails. Model names are now case-insensitive on every path, as documented.
    - High-Level API: Discovery failures no longer raise TypeError when no on_error callback was provided to ToyHub.
    - High-Level API: An unexpected disconnect or power-off for an already-removed toy no longer raises KeyError.
    - Web API: The shutdown command now sends a single response instead of two.
    - High-Level API: A failed battery query during background polling is now reported as None to the on_battery_update callback, instead of leaking the raised exception into the results dict.
    - Low-Level API: MockEstimToys discovery now mirrors real toys' advertising: a toy is hidden from scans while connected and reappears once it is disconnected or removed.
        Previously connected mock toys showed up as duplicates in scan results.
    - Low-Level API: Per-model brand data (commands, recommended interval, rotation support) is now derived from a single source of truth per brand, so the lookup tables can no longer drift out of sync.
    - Docs: Corrected the pattern-segment order in the WebSocket set_pattern docstrings to (duration_ms, intensity1, intensity2), matching the actual protocol and docs/websocket/actions.md.
    - WebSocket API: Toy power-off notifications are now handled. The async power-off handler had been passed to the connection builder as a sync callback, so the resulting coroutine was never awaited: 
        a toy powering off never produced a `powered_off` status and was only cleaned up later via the disconnect/lost path.
    - Low-Level API: MockEstimToy now captures the running event loop when notifications start (mirroring LovenseToy) instead of calling asyncio.get_event_loop().
    - Mock: MockBleakClient now uses asyncio.get_running_loop() instead of the deprecated asyncio.get_event_loop().
    - High-Level API: The discover_* methods now return defensive copies of ToyData, so filling in cached model names can no longer mutate the connection builder's shared continuous-scan snapshot.
    - Low-Level API: BleTransport.disconnect and UsbTransport.reconnect/disconnect now chain the underlying exception (raise ... from e).


## [1.1.0] - 2026-06-27

### Added
    - Web API: Limit Intenstiy functionality. See docs/websocket/actions.md for details.
    - Web API: Heartbeat functionality (opt-in protective measure agains client failures). See docs/websocket/actions.md for details.
	- Low-Level API: Toy class and subclasses feature the property recommended_min_interval (My suggested minimum segment length (in ms), meaning the minimum interval between intensity changes)
    - Low-Level API: class ConnectionBuilder replaces class BLEConnectionBuilder. BLEConnectionBuilder remains available for backwards compatibility.
    - Low-Level API: Added new Toy brand: Mocked Estim devices to explore how to handle the addition of new brands with potentially non-BLE toys. Mocked Estim devices ARE NOT PART OF THE API and might be removed without notice.
    - All APIs: Alpha support for Lovense Spinel and Lovense Lush Anal (I don't have these toys and had to guess their commands, so they may or may not work)


### Changed
	- WebAPI: get_info and get_all now return the new key recommended_min_interval (recommended minimal interval between intensity changes in ms)
    - Low-level API: Restructured the code base, to make it easier to add new brands. No changes to the API.
                        Objects that you were not meant to use may have changed e.g. LovenseHandler (especially import locations)


## [1.0.0] - 2026-06-02

### Changed
    - Development status is now beta. It will likely remain in beta (Unless I get enough reports to confirm most lovense toys to be working)
    - All future versions will follow semantic versioning.


## [0.6.0] - 2026-05-29

### Added
    - Low-Level API: Toy: new methods e.g. strict_intensity1, strict_get_battery_level that raise exceptions to the caller instead of swallowing them.
    - Both APIs: New Exceptions: InvalidModelError and BadModelError replace ValidationError. Backwards compatible as both inherit from ValidationError.
    - High-Level API: ToyController: new methods: set_paused and set_blocked
    - Mock: MockBleakScanner now supports continuous scanning.
    - WebAPI: Added WebAPI + Documentation

### Changed
    - Both APIs: Toy / ToyController: renamed rotate_change_direction to change_rotation_direction.
    - Low-Level API: on_disconnect callback provided to BLEConnectionBuilder now called with toy_id: str instead of the underlying transport layer
    - Low-Level API: Toy: new method: "reconnect". can be called to attempt reconnection to a disconnected toy.
    - Low-Level API: Toy: set_model_name now async and can raise InvalidModelError or BadModelError (Both inherit from ValidationError). This breaks the API.
    - Restructing of the directory structure of the code base, impacting the import paths. This breaks the API.

### Removed
    - Both APIs: Removed LovenseData. Fully replaced by ToyData with ToyData.brand = "Lovense". This breaks the API.


## [0.5.0] - 2026-05-06

### Added

    - Low-Level API: BLEConnectionBuilder: Handles the discovery and connection of all BLE based toys. Delegates brand-specific logic to internal handler classes.
    - Low-Level API: Added new properties to Toy: brand, change_rotation_direction_available, intensity_names, max_intensity
    - Both-Apis: Added new property to ToyData: brand 
    - High-Level API: ToyHub: Added new methods: start_discovery, stop_discovery

### Changed

    - High-Level API: Toy Controller: Extraced pattern logic to Pattern Handler class. No API changes.
    - High-Level API: Toy Controller: Restructured. Instantiation arguments changed. No API changes, as instantiation should only be done by ToyHub.
    - High-Level API: Toy Controller: property connected renamed to is_connected. This breaks the API
    - High-Level API: Toy Controller: property intensity_max_value renamed to max_intensity. This breaks the API
    - Low-Level API: ConnectionBuilder: The toy discovery methods of different BLE based instances of ConnectionBuilder 
        (just LovenseConnectionBuilder right now) fight over the same resource.
        To allow for future additions of other BLE based toys, the discovery and toy creation is now done by BLEConnectionBuilder.
    - Both APIs: Updated unittests and examples
    - Both APIs: model_name is no longer case sensitive

### Removed

    - Low-Level API: Lovense: intentional_disconnect property. Docstring marked property as for internal use only. Therefore no API change
    - Low-Level API: Removed LovenseConnectionBuilder and ToyConnectionBuilder (Replaced by BLEConnectionBuilder). This breaks the API.

### Fixed

    - High-Level API: ToyHub called its on_power_off callback twice when a toy powered off. Now only called once.


## [0.4.0] - 2026-04-19

### Changed
    
    - Low-Level API: Introduced a transport layer to allow for the addition of potentially non-BLE toys. More generic Toy class replaces former ToyBLED class.


## [0.3.0] - 2026-01-21

### Added
	
	- High-Level API: ToyController has new pattern_version property and get_pattern_data method (view docs for details)


## [0.2.1] - 2026-01-20

### Fixed

    - Both APIs: If an exception was raised in the disconnect method of LovenseBLED, the toy would not be fully disconnected
    - High-Level API: If a timeout occured during a reconnection attempt of ToyHub, the toy would not be disconnected


## [0.2.0] - 2026-01-10

### Added

    - High Level API: Introduced High Level API


## [0.1.0] - 2026-01-07

### Changed

    - Repository made public. Library is in alpha and not available on PyPI yet
