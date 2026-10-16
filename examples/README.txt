Examples of the three TIKAL APIs. They use mock toys, so they run without hardware.

## High-Level API (synchronous; start here)
A ToyHub discovers and connects toys, and hands out a ToyController for each. Its methods either block until done or
return at once and pass their result to a callback. It runs the same engine as the WebSocket server.
- HighLevelAPI/part1_toy_hub.py: discovering, connecting and disconnecting toys; the toy cache; callbacks
- HighLevelAPI/part2_lovense_controller.py: controlling a toy: intensities, patterns, pause, block, limits
- HighLevelAPI/part3_funscript.py: playing funscripts, and following a video

## Low-Level API (async)
A ConnectionBuilder discovers and connects toys. Each connected toy is a Toy (e.g., a LovenseToy) that you command
directly.
- LowLevelAPI/part1_connection_builder.py: discovering and connecting toys
- LowLevelAPI/part2_lovense_toy.py: controlling a toy
- ToyCache/part3_toy_cache.py: remembering each toy's model name between sessions

## WebSocket API
The ToyServer (tikal-server) lets any language control the toys over a WebSocket. The Tikal App uses it, which makes it
the best-tested API. Windows executables are available on GitHub Releases. The protocol is documented in docs/websocket.
- Websocket/toy_server_example.py: a walkthrough of the protocol against a server with mock toys
- Websocket/reverse-proxy/: reaching the server beyond localhost, safely
