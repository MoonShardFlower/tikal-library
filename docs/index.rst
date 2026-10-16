TIKAL documentation
===================
'Toys I Know And Love' is a Python library to control Lovense toys (https://www.lovense.com) over Bluetooth, without
the Lovense apps.

* Fully supported: Solace Pro and Gush 2, the toys I own and test with. Other models likely work at least partially,
  but may not work at all. Please report how yours do.
* Only Lovense toys are supported. If I get a toy of another brand that I like, I'll add it.


Installation and Usage
======================
``pip install tikal`` (Python 3.13 or newer).

* Usage examples, heavily commented: https://github.com/MoonShardFlower/tikal-library/tree/main/examples
* WebSocket protocol: the Markdown files in ``docs/websocket`` (this API reference does not cover it)

There are three APIs:

* **High-Level** (:mod:`tikal.high_level`): a :class:`~tikal.high_level.ToyHub` discovers and connects toys, and hands
  out a :class:`~tikal.high_level.ToyController` for each. Methods either block until done or return at once and pass
  their result to a callback. Features: pattern playback, funscripts, pause and block, intensity limits, battery
  monitoring, automatic reconnection. The ToyHub runs the same engine as the WebSocket server, so both APIs behave the
  same.
* **Low-Level** (:mod:`tikal.low_level`): a :class:`~tikal.low_level.ConnectionBuilder` discovers toys and connects
  them. Each connected toy is a :class:`~tikal.low_level.Toy` (e.g., a :class:`~tikal.low_level.LovenseToy`), whose
  async methods send commands directly. No pattern playback or automatic reconnection.
* **WebSocket** (:mod:`tikal.websocket`): ``tikal-server`` lets any language control the toys over a WebSocket, in
  JSON. The Tikal App uses it, which makes it the best-tested API. Windows executables are available on GitHub
  Releases.

:class:`~tikal.high_level.ToyCache` remembers each toy's model name between sessions. The High-Level and WebSocket
APIs use it.


What You Should Know
====================
TIKAL talks to the toys directly, using the protocol that stpihkal
(https://docs.buttplug.io/docs/stpihkal/protocols/lovense/) reverse-engineered (plus a little of my own work).

* It needs neither Lovense Connect, Lovense Remote, nor the Lovense USB dongle.
* The protocol knowledge is incomplete, so some toys and capabilities are not supported.
* Patterns are played by sending intensity commands, not with the toys' own pattern feature. To avoid overloading a
  toy, keep changes at least its recommended minimum interval apart (``recommended_min_interval``, per model). Two
  commands (e.g., thrust and depth) every 100 ms worked on my toys.
* A toy's model name must be set by hand: let the user pick one from ``LOVENSE_TOY_NAMES``. ToyCache remembers it.

| **Why toys cannot be identified automatically**

The commands depend on the model, but the Bluetooth name rarely tells it:

* Some toys advertise as ``LVS-<model>``.
* Most advertise as ``LVS-<identifier><firmware version>``, where the identifier is a single letter. The
  letter-to-model mapping is not public, and not unique: Nora appears as A or C, while Max and Solace Pro both appear
  as B.


Help Me
=======
* Test toys, and report whether they work fully, partially, or not at all.
* Implement missing capabilities, e.g., switching lights or playing the toys' own patterns. stpihkal
  (https://docs.buttplug.io/docs/stpihkal/protocols/lovense/) documents a few; there may be more.
* Improve the code or the documentation.
* Report bugs.

When reporting, please be polite, and name the toy model you used (e.g., Nora, Solace Pro). The software is provided
as is: I'll do my best, but I have no obligation to do anything.


Affiliation
===========
I am NOT affiliated with Lovense in any way. Bugs and issues of this software are NOT their problem.


License
=======
MIT, see LICENSE.


.. toctree::
   :maxdepth: 2
   :caption: Contents:

   modules
