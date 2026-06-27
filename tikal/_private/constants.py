"""Private module: timing constants shared by the High-Level and WebSocket layers."""

#: How often each control loop processes queued commands / pattern playback, in seconds.
#: At 0.05 s the loop ticks 20 times per second.
COMMUNICATION_INTERVAL = 0.05  # seconds

#: How often connected toys are polled for their battery level, in seconds.
BATTERY_UPDATE_INTERVAL = 120.0  # seconds
