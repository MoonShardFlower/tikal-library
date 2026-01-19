# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
    - High Level API tested. This will be Version 1.0.0 and the first version on PyPI
    - With 1.0.0 the development status will be updated to beta and will likely remain in beta (Unless I get enough reports to confirm most lovense toys to be working)
    - Added: High-Level API: ToyController has pattern_version property and get_pattern_data method (view docs for details)

## [0.2.1] - 2026-01-20

### Fixed

    - Both APIs: If an exception was raised in the disconnect method of LovenseBLED, the toy would not be fully disconnected
    - High-Level API: If a timeout occured during a reconnection attempt of ToyHub, the toy would not be disconnected

## [0.2.0] - 2026-01-10

### Added

    - High Level API: Introduced High Level API

## [0.1.0] - 2026-01-07

## Changed

    - Repository made public. Library is in alpha and not available on PyPI yet
