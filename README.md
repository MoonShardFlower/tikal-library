# TIKAL

[![CI/CD](https://github.com/MoonShardFlower/tikal-library/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/MoonShardFlower/tikal-library/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/MoonShardFlower/tikal-library/branch/main/graph/badge.svg)](https://codecov.io/gh/MoonShardFlower/tikal-library)
[![PyPI version](https://img.shields.io/pypi/v/tikal.svg)](https://pypi.org/project/tikal/)
[![Python versions](https://img.shields.io/pypi/pyversions/tikal.svg)](https://pypi.org/project/tikal/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

'Toys I Know And Love' is a Python library to control Lovense toys (https://www.lovense.com) over Bluetooth, without
the Lovense apps.

- Fully supported: Solace Pro and Gush 2, the toys I own and test with. Other models likely work at least partially,
  but may not work at all. Please report how yours do.
- Only Lovense toys are supported. If I get a toy of another brand that I like, I'll add it.

## Installation and Usage

`pip install tikal` (Python 3.13 or newer).

- Usage examples, heavily commented: [./examples](./examples)
- API reference: [Read the Docs](https://tikal-library.readthedocs.io/en/latest/index.html)
- WebSocket protocol: [./docs/websocket](./docs/websocket/documentation.md)

There are three APIs:

| API        | For                                                  | Entry point                         |
|------------|------------------------------------------------------|-------------------------------------|
| High-Level | synchronous Python, with callbacks for async results | `tikal.high_level.ToyHub`           |
| Low-Level  | async Python, sending commands to the toys directly  | `tikal.low_level.ConnectionBuilder` |
| WebSocket  | any language, in a separate process                  | `tikal-server`                      |

### Low-Level API

A `ConnectionBuilder` discovers toys and connects them. Each connected toy is a `Toy` (e.g., a `LovenseToy`), whose
async methods send commands directly. No pattern playback or automatic reconnection.

### High-Level API

A `ToyHub` discovers and connects toys, and hands out a `ToyController` for each. Methods either block until done or
return at once and pass their result to a callback. Features: pattern playback, funscripts, pause and block, intensity
limits, battery monitoring, automatic reconnection. The High-Level API and Websocket server run the same engine,
meaning both APIs behave the same.

### WebSocket API

`tikal-server` runs a server that lets any language control the toys over a WebSocket, in JSON. It's the best-tested 
API (because the Tikal App uses it). Windows executables are available on GitHub Releases. The protocol is documented
in Markdown in [./docs/websocket](./docs/websocket/documentation.md).

## What You Should Know

TIKAL talks to the toys directly, thanks to reverse-engineering efforts by
[stpihkal](https://docs.buttplug.io/docs/stpihkal/protocols/lovense/) (plus a little of my own work).

- It does not need Lovense Connect, Lovense Remote, nor the Lovense USB dongle.
- The protocol knowledge is incomplete, so some toys and capabilities are not supported.
- Patterns are played by sending intensity commands, not with the toys' own pattern feature. To avoid overloading a
  toy, keep changes at least its recommended minimum interval apart (`recommended_min_interval`, per model). Two
  commands (e.g., thrust and depth) every 100 ms worked on my toys.
- The model isn't known for all toys. If it is known discovery fills it in for you. Else the User must pick the model
  from `LOVENSE_TOY_NAMES`. `ToyCache` can be used to remember the choice.

### How Toys Are Identified

- By the service UUID most toys advertise: its first four hex digits tell the model (`42410001-...` -> Solace Pro).
  `LOVENSE_UUID_PREFIXES` maps them to models. I copied from the source of [Buttplug](https://github.com/buttplugio/buttplug) 
  (BSD 3-Clause, Copyright (c) 2016-2023, Nonpolynomial Labs, LLC) and would like to thank them a lot.
  The Solace Pro (`4241`) and the Gush 2 (`455a`) are confirmed on my own toys, the others are just assumed to work.
- The High-Level API and the Web API prefer the model saved in `ToyCache`, so user picks win against the defaults.

## Help Me

- Test toys, and report whether they work fully, partially, or not at all.
- Implement missing capabilities, e.g., switching lights or playing the toys' own patterns.
  [stpihkal](https://docs.buttplug.io/docs/stpihkal/protocols/lovense/) documents a few; there may be more.
- Improve the code or the documentation.
- Report bugs.

When reporting, please be polite, and name the toy model you used (e.g., Nora, Solace Pro). The software is provided
as is: I'll do my best, but I won't promise anything.

## Affiliation

I am NOT affiliated with Lovense in any way. Bugs and issues of this software are NOT their problem.

## License

MIT, see [LICENSE](LICENSE). The Lovense model table is derived from Buttplug (BSD 3-Clause), see [NOTICE](NOTICE).
