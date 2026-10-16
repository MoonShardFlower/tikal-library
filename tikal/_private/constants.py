"""Private module: timing constants of the async core."""

#: Interval of the pattern playback loop, in seconds (20 ticks per second).
COMMUNICATION_INTERVAL = 0.05

#: Interval between two battery polls of the connected toys, in seconds.
BATTERY_UPDATE_INTERVAL = 120.0

#: How long a toy whose connection failed is reconnected before it is given up, in seconds.
#: A single Bluetooth connect can take up to 30 s (bleak's default timeout).
RECONNECT_WINDOW = 60.0

#: Pause between two reconnect attempts, in seconds.
RECONNECT_PAUSE = 5.0
