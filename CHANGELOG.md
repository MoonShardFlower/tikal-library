# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and for versions >= 1.0.0 this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
    - Development status is now beta. It will likely remain in beta (Unless I get enough reports to confirm most lovense toys to be working)

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
