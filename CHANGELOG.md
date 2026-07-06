# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and for versions >= 1.0.0 this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
    - Packaging: Added a py.typed marker. Downstream type checkers now see tikal's inline type hints.

### Changed
    - Web API:  Expanded Status webpage 
                (found at http://<host>:<port>/ where host and port are replaced with the configuration used to start the server e.g. http://localhost:8142/)

### Fixed
    - Low-Level API: set_model_name now validates the NEW model's commands (and restores the previous model name if they fail) instead of validating the already-set model. Changing a connected toy to a different valid-but-wrong model is now correctly rejected.
    - Low-Level API: Connecting with a lower-case model_name no longer fails. Model names are now case-insensitive on every path, as documented.
    - High-Level API: Discovery failures no longer raise TypeError when no on_error callback was provided to ToyHub.
    - High-Level API: An unexpected disconnect or power-off for an already-removed toy no longer raises KeyError.
    - Web API: The shutdown command now sends a single response instead of two.
    - High-Level API: A failed battery query during background polling is now reported as None to the on_battery_update callback, instead of leaking the raised exception into the results dict.
    - Low-Level API: MockEstimToys discovery now mirrors real toys' advertising: a toy is hidden from scans while connected and reappears once it is disconnected or removed. Previously connected mock toys showed up as duplicates in scan results.


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
