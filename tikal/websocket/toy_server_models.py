"""
Contains data models and payload structures used for the client-server communication of ToyServer.

The protocol is described in ``docs/websocket/``.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..funscript import Funscript, FunscriptMode

# -----------------------------------------------------------------------------
# Protocol envelopes
# -----------------------------------------------------------------------------


class RequestEnvelope(BaseModel):
    """
    Envelope of every client -> server message. No other keys are allowed.

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
    Envelope of every server -> client reply.

    Attributes:
        reply: The request's command name.
        id: The request's id.
        success: False if the command failed; *data* is an :class:`ErrorData` then.
        data: The command's result, or the error.
    """

    reply: str
    id: str
    success: bool
    data: dict[str, Any]


class EventEnvelope(BaseModel):
    """
    Envelope of a server-initiated event. Unlike a reply, it answers no request.

    Attributes:
        event: Event name, e.g., ``"connection_status_changed"``.
        success: False if *data* reports an error.
        data: The event's data.
    """

    event: str
    success: bool
    data: dict[str, Any]


# -----------------------------------------------------------------------------
# Error payload
# -----------------------------------------------------------------------------


class _ErrMsg:
    """Message templates of the error replies, see ``docs/websocket/error.md``."""

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
    TOY_NOT_CONNECTED_ERROR = "Unable to execute '{cmd}' on toy '{toy_id}': the toy is not connected ({status}), so nothing was sent. A state change the command asked for (block, pause, pattern, limit) is still recorded."
    INVALID_MODEL_ERROR = "The model name '{model_name}' is not a valid model name for '{toy_id}'. Use get_brands to get a list of valid model names for each brand."
    BAD_MODEL_ERROR = "The model name '{model_name}' is valid but they toy '{toy_id}' does not correctly respond to commands. Please check if the model name is correct. It it is please contact the developer."
    UNKNOWN_TOY_ERROR = (
        "Unable to execute '{cmd}' on '{toy_id}'. Please add the toy first."
    )
    BLOCKED_TOY_ERROR = "Unable to execute '{cmd}' on '{toy_id}'. The toy is blocked, so nothing that could drive it is sent. Unblock it first (set_blocked or toggle_block)."
    DEVELOPER_ERROR = "Unexpected error occurred in the TIKAL Web-API. If you see this, please contact MoonShardFlower@gmail.com and provide the following: {details}"


class ErrorData(BaseModel):
    """
    Data of a failed reply (and of an event reporting an error).

    Attributes:
        error: Name of the error, e.g., ``"Unknown Toy"``.
        message: Human-readable message.
        traceback: The traceback, if any.
        toy_id: The toy concerned, if any.
        model_name: The model name concerned, if any.
        brand: The brand concerned, if any.
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

# Request data with unknown keys is invalid.
_STRICT = ConfigDict(extra="forbid")


class _EmptyData(BaseModel):
    """Data of commands without arguments, e.g., ``get_brands``."""

    model_config = _STRICT


class ToyIdData(BaseModel):
    """
    Data of the commands that only take a toy.

    Attributes:
        toy_id: The toy.
    """

    toy_id: str
    model_config = _STRICT


class AddRequestData(BaseModel):
    """
    Data of ``add``.

    Attributes:
        toy_id: The toy, as found by the running scan.
        model_name: Its model name, case-insensitive.
    """

    toy_id: str
    model_name: str
    model_config = _STRICT


class SetModelData(BaseModel):
    """
    Data of ``set_model``.

    Attributes:
        toy_id: The added toy.
        model_name: New model name, case-insensitive.
    """

    toy_id: str
    model_name: str
    model_config = _STRICT


class IntensityData(BaseModel):
    """
    Data of ``intensity1`` and ``intensity2``.

    Attributes:
        toy_id: The toy.
        intensity: The level, 0 to ``max_intensity``. Values outside are clamped, and the toy's limit applies. The
            command pauses the pattern.
    """

    toy_id: str
    intensity: int
    model_config = _STRICT


class IntensityLimitData(BaseModel):
    """
    Data of ``set_intensity1_limit`` and ``set_intensity2_limit``.

    Attributes:
        toy_id: The toy.
        limit: This client's limit, clamped to 0 to ``max_intensity``. None withdraws it. The toy's limit is the lowest
            of all clients' limits (``max_intensity`` if none).
    """

    toy_id: str
    limit: Optional[int]
    model_config = _STRICT


class SetPausedData(BaseModel):
    """
    Data of ``set_paused``.

    Attributes:
        toy_id: The toy.
        pause: True to pause pattern playback (the toy stops, the pattern time stands still), False to resume it.
    """

    toy_id: str
    pause: bool
    model_config = _STRICT


class SetBlockedData(BaseModel):
    """
    Data of ``set_blocked``.

    Attributes:
        toy_id: The toy.
        block: True to block the toy (it stays at 0 whatever the pattern or manual commands; blocking ends a pause),
            False to unblock it.
    """

    toy_id: str
    block: bool
    model_config = _STRICT


class SetPatternData(BaseModel):
    """
    Data of ``set_pattern``.

    Attributes:
        toy_id: The toy.
        pattern: Segments as ``[duration_ms, intensity1, intensity2]``. An empty list clears the pattern.
        wraparound: Loop the pattern. If False, it plays once, then the intensities are 0.
        reset_time: Start at the beginning. If False, keep the current pattern time.
    """

    toy_id: str
    pattern: list[tuple[int, int, int]]
    wraparound: bool
    reset_time: bool
    model_config = _STRICT

    @field_validator("pattern", mode="before")
    @classmethod
    def coerce_pattern(cls, v: Any) -> list[tuple[int, int, int]]:
        """
        Accept segments as lists or tuples.

        Args:
            v: The ``pattern`` as sent.

        Returns:
            The segments as tuples.
        """
        return [tuple(seg) for seg in v]

    @model_validator(mode="after")
    def check_segments(self) -> "SetPatternData":
        """
        Check that every segment is three integers.

        Returns:
            The model, unchanged.

        Raises:
            ValueError: A segment is not three integers.
        """
        for i, seg in enumerate(self.pattern):
            if len(seg) != 3:
                raise ValueError(f"Pattern segment {i} must have exactly 3 values")
            if not all(isinstance(x, int) for x in seg):
                raise ValueError(f"Pattern segment {i} values must all be integers")
        return self


class SetFunscriptData(BaseModel):
    """
    Data of ``set_funscript``.

    Attributes:
        toy_id: The toy.
        script: The script for the primary capability: the content of a ``.funscript`` file (actions as ``{"at": ms,
            "pos": 0-100}``; other fields such as ``metadata`` are ignored), optionally with the actions as compact
            ``[at, pos]`` pairs.
        secondary: Optional script for the secondary capability, in the same form.
        mode: ``"speed"`` (default: the intensity follows the stroke speed) or ``"position"``.
        max_speed: Stroke speed in positions per second that maps to the maximum intensity in ``"speed"`` mode.
            Positive. Defaults to 400.
        start_ms: Pattern time to start at in ms. Defaults to 0. Negative values count as 0.
    """

    toy_id: str
    script: Funscript
    secondary: Optional[Funscript] = None
    mode: FunscriptMode = "speed"
    max_speed: float = Field(default=400.0, gt=0, allow_inf_nan=False)
    start_ms: float = Field(default=0.0, allow_inf_nan=False)
    model_config = _STRICT


class SeekData(BaseModel):
    """
    Data of ``seek``.

    Attributes:
        toy_id: The toy.
        pattern_time: The new pattern time in ms (the time scale of ``elapsed`` in the toy's state). Negative values
            count as 0.
    """

    toy_id: str
    pattern_time: float = Field(allow_inf_nan=False)
    model_config = _STRICT


class HeartbeatEnableData(BaseModel):
    """
    Data of ``enable_heartbeat``.

    Attributes:
        enable: True to arm the heartbeat watchdog for this client, False to disarm it.
    """

    enable: bool
    model_config = _STRICT


class GetInfoData(BaseModel):
    """
    Data of ``get_info`` and ``get_all``.

    Attributes:
        toy_id: The toy.
        full: Also ask the toy for brand-specific information. If False, the reply needs no request to the toy.
    """

    toy_id: str
    full: bool
    model_config = _STRICT


class DirectCommandData(BaseModel):
    """
    Data of ``direct_command``.

    Attributes:
        toy_id: The toy.
        command: Raw command in the toy's protocol. It does not change the tracked state: do not use it for intensities.
    """

    toy_id: str
    command: str
    model_config = _STRICT


# -----------------------------------------------------------------------------
# Response models
# -----------------------------------------------------------------------------


class AckData(BaseModel):
    """
    Reply of commands without a result of their own.

    Attributes:
        ack: True. For ``intensity1``, ``intensity2`` and ``change_rotation_direction``: whether the command took effect
        toy_id: The toy, if the command names one.
    """

    ack: bool = True
    toy_id: Optional[str] = None


class BrandsData(BaseModel):
    """
    Reply of ``get_brands``.

    Attributes:
        brands: Brand -> its model names, e.g., ``{"Lovense": ["Solace", "Sex Machine", ...]}``.
    """

    brands: dict[str, list[str]]


class ToyIdsData(BaseModel):
    """
    Reply of ``get_toy_ids``.

    Attributes:
        toy_ids: The added toys.
    """

    toy_ids: list[str]


class ToyStateData(BaseModel):
    """
    Reply of ``get_state``, and data of the ``toy_state_changed`` event.

    Attributes:
        toy_id: The toy.
        current_intensities: ``[intensity1, intensity2]``: the levels the toy last acknowledged. intensity2 is 0 without
            a secondary capability.
        intensity_limits: ``[limit1, limit2]``: manual levels and pattern values are capped at these.
        is_blocked: Whether the toy is blocked (it stays at 0).
        pattern_version: Increases whenever the pattern, the pause, or the position changes.
        pattern: Segments as ``[duration_ms, intensity1, intensity2]``. Empty without a pattern.
        wraparound: Whether the pattern loops.
        is_paused: Whether pattern playback is paused.
        elapsed: The pattern time in ms, without paused time. A wraparound does not reset it: the position in the
            pattern is ``elapsed`` modulo the pattern's duration.
    """

    toy_id: str
    current_intensities: list[int]
    intensity_limits: list[int]
    is_blocked: bool
    pattern_version: int
    pattern: list[tuple[int, int, int]]
    wraparound: bool
    is_paused: bool
    elapsed: float


class BatteryResponseData(BaseModel):
    """
    Reply of ``get_battery``.

    Attributes:
        battery: The last known level in percent. None if the toy has no battery.
        toy_id: The toy.
    """

    battery: Optional[int]
    toy_id: str


class ConnectionStatusResponseData(BaseModel):
    """
    Reply of ``get_connection_status``.

    Attributes:
        connection_status: ``"connected"``, ``"reconnecting"``, ``"lost"``, or ``"powered_off"``.
        toy_id: The toy.
    """

    connection_status: str
    toy_id: str


class InfoResponseData(BaseModel):
    """
    Reply of ``get_info``, and data of the ``model_changed`` event. With ``full``, brand-specific keys are added
    (Lovense: ``status``, ``batch_number``, ``device_type``).

    Attributes:
        toy_id: The toy.
        name: Human-readable name, e.g., the Bluetooth name.
        model_name: Model name, e.g., ``"Gush"``.
        brand: Brand, e.g., ``"Lovense"``.
        intensity_names: Display names of the two capabilities. The second is empty without a secondary capability.
        supports_rotation: Whether the toy can change its rotation direction.
        max_intensity: Highest intensity level of both capabilities.
        recommended_min_interval: Recommended minimum interval between intensity changes, in ms.
    """

    toy_id: str
    name: str
    model_name: str
    brand: str
    intensity_names: list[str]
    supports_rotation: bool
    max_intensity: int
    recommended_min_interval: int
    model_config = {"extra": "allow"}


class DirectCommandResponseData(BaseModel):
    """
    Reply of ``direct_command``.

    Attributes:
        response: The toy's raw reply.
        toy_id: The toy.
    """

    response: str
    toy_id: str


class GetAllResponseData(BaseModel):
    """
    Reply of ``get_all``. The keys of :class:`ToyStateData` and :class:`InfoResponseData` (brand-specific ones
    included), plus these.

    Attributes:
        battery: The last known battery level in percent. None if the toy has no battery.
        connection_status: ``"connected"``, ``"reconnecting"``, ``"lost"``, or ``"powered_off"``.
    """

    toy_id: str
    name: str
    model_name: str
    brand: str
    intensity_names: list[str]
    supports_rotation: bool
    max_intensity: int
    recommended_min_interval: int
    battery: Optional[int] = None
    current_intensities: list[int]
    intensity_limits: list[int]
    is_blocked: bool
    pattern_version: int
    pattern: list[tuple[int, int, int]]
    wraparound: bool
    is_paused: bool
    elapsed: float
    connection_status: str
    model_config = {"extra": "allow"}
