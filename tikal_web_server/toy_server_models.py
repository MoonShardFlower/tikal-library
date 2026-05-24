"""
Contains data models and payload structures used for the client-server communication of ToyServer

This module defines the primary data models for handling request and response envelopes, event envelopes,
error payloads, and specific request payload structures for commands related to toys management.
Each model is built with Pydantic for data validation and serialization purposes.
"""

from typing import Optional, Any

from pydantic import BaseModel, field_validator, model_validator, Field


# -----------------------------------------------------------------------------
# Protocol envelopes
# -----------------------------------------------------------------------------


class RequestEnvelope(BaseModel):
    """
    Outer envelope for every client -> server message.

    Attributes:
        request: Command name (e.g. "add", "get_toy_state").
        id: Caller-supplied token echoed back in the response ``id`` field.
        data: Command-specific payload. Empty dict for commands that take no arguments.
    """

    request: str
    id: str
    data: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class ResponseEnvelope(BaseModel):
    """
    Outer envelope for every server -> client response.

    Attributes:
        reply: Echoes the request field of the originating request.
        id: Echoes the id field of the originating request.
        success: True if the command succeeded; False if it produced an error. Lets clients branch on a single key without inspecting data.
        data: Command-specific result payload on success, or an ErrorData-shaped dict on failure.
    """

    reply: str
    id: str
    success: bool
    data: dict[str, Any]


class EventEnvelope(BaseModel):
    """
    Envelope for server-initiated events broadcast to clients.

    Unlike ResponseEnvelope, events are not correlated to a specific request and carry no id.

    Attributes:
        event: Event name (e.g. "on_status_change", "on_scan_update").
        success: True for normal data events, False when the event carries error information.
        data: Event payload.
    """

    event: str
    success: bool
    data: dict


# -----------------------------------------------------------------------------
# Error payload
# -----------------------------------------------------------------------------


class _ErrMsg:
    MALFORMED_REQUEST = "Unable to parse request. Please verify the envelope is correctly formed and contains all required fields (request, id, data)."
    UNKNOWN_COMMAND = (
        "Unknown command '{cmd}'. Please verify the command name is correct."
    )
    INVALID_DATA = "Validation of data field failed for command '{cmd}' failed. Please check field types and required keys: {detail}"
    DISCOVERY_START_ERROR = "Unable start discovery of toys. Please verify that Bluetooth is enabled. Contact the developer if the problem persists."
    DISCOVER_ERROR = "Discovery of toys failed. Please verify that Bluetooth is enabled. Contact the developer if the problem persists."
    UNDISCOVERED_TOY_ERROR = (
        "Unable to add toy '{toy_id}'. This toy was never discovered."
    )
    UNAVAILABLE_TOY_ERROR = "Unable to add toy '{toy_id}'. This toy was discovered at some point, but has since then become unavailable."
    TOY_ALREADY_ADDED_ERROR = "Unable to add toy '{toy_id}'. This toy was already added or is currently being added."
    ADD_CONNECTION_ERROR = "Unable to add toy '{toy_id}'. Please verify that the toy is still turned on and not connected anywhere else. Contact the developer if the problem persists."
    TOY_CONNECTION_ERROR = "Unable to execute '{cmd}' on toy '{toy_id}'. Please verify that the toy is still turned on and not connected anywhere else. Will attempt to reconnect."
    INVALID_MODEL_ERROR = "The model name '{model_name}' is not a valid model name for '{toy_id}'. Use get_brands to get a list of valid model names for each brand."
    BAD_MODEL_ERROR = "The model name '{model_name}' is valid but they toy '{toy_id}' does not correctly respond to commands. Please check if the model name is correct. It it is please contact the developer."
    UNKNOWN_TOY_ERROR = (
        "Unable to execute '{cmd}' on '{toy_id}'. Please add the toy first."
    )
    DEVELOPER_ERROR = "Unexpected error occurred in the TIKAL Web-API. If you see this, please contact MoonShardFlower@gmail.com and provide the following: {details}"


class ErrorData(BaseModel):
    """
    The error payload returned by the server when an error occurs.

    Attributes:
        error: Name of the error.
        message: Human-readable error message.
        traceback: Traceback of the error if available.
        toy_id: Toy ID that caused the error, if applicable.
        model_name: Model name that caused the error, if applicable.
        brand: Brand of the toy that caused the error, if applicable.
    """

    error: str
    message: str
    traceback: Optional[str] = None
    toy_id: Optional[str] = None
    model_name: Optional[str] = None
    brand: Optional[str] = None

# -----------------------------------------------------------------------------
# Request models
# -----------------------------------------------------------------------------

_STRICT = {"extra": "forbid"}


class _EmptyData(BaseModel):
    """No arguments required (e.g., get_brands, start_scan)."""

    model_config = _STRICT


class ToyIdData(BaseModel):
    """
    Single toy_id argument: Shared by the many commands that need nothing else.

    Attributes:
        toy_id: Unique identifier of the toy to perform the command on.
    """

    toy_id: str
    model_config = _STRICT


class AddRequestData(BaseModel):
    """
    Payload for the add command.

    Attributes:
        toy_id: Unique identifier of the toy to connect (e.g., its Bluetooth address). The toy must have been discovered by an active scan first.
        model_name: Model name to assign (case-insensitive). Must be a valid model for the toy's brand. Use the "get_brands" command for the full list
    """

    toy_id: str
    model_name: str
    model_config = _STRICT


class SetModelData(BaseModel):
    """
    Payload for the set_model command.

    Attributes:
        toy_id: Unique identifier of the already-added toy whose model assignment should change.
        model_name: New model name to assign (case-insensitive). Must be valid for the toy's brand.

    """

    toy_id: str
    model_name: str
    model_config = _STRICT


class IntensityData(BaseModel):
    """
    Payload for the intensity1 and intensity2 commands.

    Attributes:
        toy_id: Unique identifier of the toy to command.
        intensity: Target intensity level (0 – ``max_intensity`` inclusive). Setting an intensity manually will automatically pause any active pattern;
    """

    toy_id: str
    intensity: int
    model_config = _STRICT


class SetPausedData(BaseModel):
    """
    Payload for the set_paused command.

    Attributes:
        toy_id: Unique identifier of the toy to pause or resume.
        pause: True to pause pattern playback (toy stops, pattern timer freezes). False to resume from where it left off.
    """

    toy_id: str
    pause: bool
    model_config = _STRICT


class SetBlockedData(BaseModel):
    """
    Payload for the set_blocked command.

    Attributes:
        toy_id: Unique identifier of the toy to block or unblock.
        block:  True to force both intensities to zero regardless of any pattern or manual commands; ``False`` to restore normal operation.
                Blocking a paused toy clears the pause state (a toy cannot be both paused and blocked).
    """

    toy_id: str
    block: bool
    model_config = _STRICT


class SetPatternData(BaseModel):
    """
    Payload for the set_pattern command.

    Attributes:
        toy_id (str): Identifier for the toy for which the pattern is being set.
        pattern (list[tuple[int, int, int]]): The sequence of segments in the pattern, where each segment is (intensity1, intensity2, duration in ms)
        wraparound (bool): Determines if the pattern should wrap around when the end is reached.
        reset_time (bool): Indicates whether the pattern should reset the time counter after being set.
    """

    toy_id: str
    pattern: list[tuple[int, int, int]]
    wraparound: bool
    reset_time: bool
    model_config = _STRICT

    @field_validator("pattern", mode="before")
    @classmethod
    def coerce_pattern(cls, v: Any) -> list[tuple[int, int, int]]:
        """Accept either list-of-tuples or list-of-lists and normalize to list-of-tuples."""
        return [tuple(seg) for seg in v]  # type: ignore[return-value]

    @model_validator(mode="after")
    def check_segments(self) -> "SetPatternData":
        """Validate each segment of the pattern."""
        for i, seg in enumerate(self.pattern):
            if len(seg) != 3:
                raise ValueError(f"Pattern segment {i} must have exactly 3 values")
            if not all(isinstance(x, int) for x in seg):
                raise ValueError(f"Pattern segment {i} values must all be integers")
        return self


class GetInfoData(BaseModel):
    """
    Payload for the get_info command.

    Attributes:
        toy_id: Unique identifier of the toy to query.
        full:   If False, return only the inexpensive in-memory fields (toy_id, name, model_name, brand, intensity_names, supports_rotation, max_intensity, battery) with no requests to the toy.
                If True, also request additional brand-specific information from the toy. Returns an empty dict if the command could not be delivered.
    """

    toy_id: str
    full: bool
    model_config = _STRICT


class DirectCommandData(BaseModel):
    """
    Payload for the direct_command command. Useful to access functionality not exposed by the server.

    Attributes:
        toy_id: Unique identifier of the target toy.
        command: Raw command string to send directly to the toy over BLE.

    Note:
        Do not use this to change the tracked state (e.g., intensities) as it bypasses the server's state tracking.
    """

    toy_id: str
    command: str
    model_config = _STRICT


# -----------------------------------------------------------------------------
# Response models
# -----------------------------------------------------------------------------


class AckData(BaseModel):
    """
    Generic acknowledgement payload returned by commands that have no meaningful data to return.

    Attributes:
        ack: Always True
        toy_id: Identifier of the affected toy, if applicable.
    """

    ack: bool = True
    toy_id: Optional[str] = None


class BrandsData(BaseModel):
    """
    Response payload for get_brands.

    Attributes:
        brands: Mapping of brand name -> list of supported model names, e.g. {"Lovense": ["Gush", "Solace", ...]}.
    """

    brands: dict[str, list[str]]


class ToyIdsData(BaseModel):
    """
    Response payload for get_toy_ids.

    Attributes:
        toy_ids: Snapshot list of all toy identifiers currently managed by ToyHub.
    """

    toy_ids: list[str]


class ToyStateData(BaseModel):
    """
    Response payload for get_toy_state and the on_toy_state_change event.

    Attributes:
        toy_id: Unique identifier of the toy.
        current_intensities: Current intensity values as [intensity1, intensity2]. intensity2 is always 0 for single-intensity toys.
        is_blocked: True when the toy is blocked (both intensities forced to zero).
        pattern_version: Increments each time the pattern state changes.
        pattern: Active pattern as a list of (duration_ms, intensity1, intensity2) tuples.
        wraparound: True if the pattern loops after the final segment; False if it stops.
        is_paused: True when pattern playback is paused.
        elapsed: Milliseconds elapsed since the start of the pattern or the last wraparound (Does not advance during pauses).
    """

    toy_id: str
    current_intensities: list[int]
    is_blocked: bool
    pattern_version: int
    pattern: list[tuple[int, int, int]]
    wraparound: bool
    is_paused: bool
    elapsed: float


class BatteryResponseData(BaseModel):
    """
    Response payload for get_battery.

    Attributes:
        battery: Battery level in the range 0–100, or None if the toy has no battery.
        toy_id: Unique identifier of the queried toy.
    """

    battery: Optional[int]
    toy_id: str


class ConnectionStatusResponseData(BaseModel):
    """
    Response payload for get_connection_status.

    Attributes:
        connection_status: Current connection status: "connected", "reconnecting", "lost", or "powered_off".
        toy_id: Unique identifier of the queried toy.
    """

    connection_status: str
    toy_id: str


class InfoResponseData(BaseModel):
    """
    Response payload for get_info. If full=True may contain additional brand-specific fields.

    Attributes:
        toy_id: Unique identifier of the queried toy.
        name: Human-readable name of the toy (e.g., Bluetooth advertisement name).
        model_name: Model name of the toy (e.g., "Gush").
        brand: Brand name of the toy (e.g., "Lovense").
        intensity_names: List of two human-readable strings. The second string is "None" if the toy only has one intensity.
        supports_rotation: True if the toy supports changing the rotation direction.
        max_intensity: Maximum intensity value supported by the toy.
    """

    toy_id: str
    name: str
    model_name: str
    brand: str
    intensity_names: list[str]
    supports_rotation: bool
    max_intensity: int
    model_config = {"extra": "allow"}


class DirectCommandResponseData(BaseModel):
    """
    Response payload for direct_command.

    Attributes:
        response: Raw response string returned by the toy, or an empty string if the command could not be delivered.
        toy_id: Unique identifier of the toy that was commanded.
    """

    response: str
    toy_id: str


class GetAllResponseData(BaseModel):
    """Response payload for get_all. Combines state, info, and connection status."""

    toy_id: str
    name: str
    model_name: str
    brand: str
    intensity_names: list[str]
    supports_rotation: bool
    max_intensity: int
    battery: Optional[int] = None
    current_intensities: list[int]
    is_blocked: bool
    pattern_version: int
    pattern: list[tuple[int, int, int]]
    wraparound: bool
    is_paused: bool
    elapsed: float
    connection_status: str
    model_config = {"extra": "allow"}
