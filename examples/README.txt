The TIKAL Library provides two different APIs for communicating with the toys

## High-Level API
The 'High Level' API uses a ToyHub to scan for toys, establish connections, and disconnecting toys.
The ToyHub also handles the synchronization of async operations (Either blocking code execution until the operation is
complete or executing the operation in a different thread and delivering the results via callbacks).
For each connected Toy the ToyHub produces and hands over an Implementation of the abstract ToyController (currently
just LovenseController) to control the toy.

## Low-Level API
The 'Low Level' API provides BLEConnectionBuilder to scan and connect to toys. BLEConnectionBuilder produces and hands over an Implementation of
the abstract Toy (currently just Lovense) to control the toy. Both classes are mostly async.
You can use ToyCache to remember toy model names in-between sessions.

## WebSocket API
The 'WebSocket' API provides ToyServer, which you should access via a WebSocket connection. It's the best tested API, as
the Tikal APP relies on it. Besides the Python files, I provide Windows executables for it (GitHub Releases).
As the docstring-based documentation is not very useful for this API, I instead provide Markdown files in docs/websocket.
