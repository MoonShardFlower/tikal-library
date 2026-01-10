The TIKAL Library provides two different APIs for communicating with the toys

## High-Level API
The 'High Level' API uses a ToyHub to scan for toys, establish connections, and disconnecting toys.
The ToyHub also handles the synchronization of async operations (Either blocking code execution until the operation is
complete or executing the operation in a different thread and delivering the results via callbacks).
For each connected Toy the ToyHub produces and hands over an Implementation of the abstract ToyController (currently
just LovenseController) to control the toy.

## Low-Level API
The 'Low Level' API provides LovenseConnectionBuilder (Implementation of the abstract the
ToyConnectionBuilder) to scan and connect to toys. LovenseConnectionBuilder produces and hands over an Implementation of
the abstract ToyBLED (currently just LovenseBLED) to control the toy. Both classes are mostly async.
You can use ToyCache to remember toy model names in-between sessions.
