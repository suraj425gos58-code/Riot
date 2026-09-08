"""
RIOT / GOD NODE — Canonical Runtime Contracts
=============================================

Phase 6: Runtime Contracts

This module defines the stable boundary between:

    Project State
        ->
    Orchestrator / Runtime Compiler
        ->
    Runtime / Simulation
        ->
    QA / Telemetry / Build

Design rules
------------
* Runtime contracts are data contracts, not execution engines.
* Contracts must be deterministic, bounded and serializable.
* Runtime code must never silently fabricate successful execution.
* Runtime state is separated from project-definition state.
* Commands, events, frames, execution requests and execution results use
  explicit schemas.
* Artifact references are references only; this module never creates files.
* Failure and unsupported states are first-class.
* Contracts are intentionally independent from Gateway/provider logic.
* No circular dependency on agents, orchestrator or scheduler internals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.game_project import (
    BuildStatus,
    CapabilityStatus,
    ProjectStatus,
    RuntimeType,
    StageStatus,
    TargetPlatform,
)


# ============================================================================
# GLOBAL LIMITS
# ============================================================================

MAX_ID_LENGTH = 128
MAX_NAME_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 8_192
MAX_COMMANDS_PER_FRAME = 4_096
MAX_EVENTS_PER_FRAME = 8_192
MAX_BATCH_SIZE = 4_096
MAX_METADATA_KEYS = 256
MAX_DEPENDENCIES = 256
MAX_OUTPUT_REFERENCES = 512
MAX_ERROR_LENGTH = 4_096
MAX_LOG_LENGTH = 32_768


# ============================================================================
# HELPERS
# ============================================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_runtime_id(prefix: str = "") -> str:
    value = uuid4().hex
    result = f"{prefix}{value}"
    return result[:MAX_ID_LENGTH]


def content_sha256(value: str | bytes) -> str:
    payload = (
        value.encode("utf-8")
        if isinstance(value, str)
        else bytes(value)
    )
    return sha256(payload).hexdigest()


def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:MAX_DESCRIPTION_LENGTH]


def _bounded_unique(
    values: Iterable[Any],
    limit: int,
) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()

    for value in values:
        text = _clean(value)

        if not text or text in seen:
            continue

        seen.add(text)
        result.append(text)

        if len(result) >= limit:
            break

    return result


# ============================================================================
# BASE CONTRACT
# ============================================================================

class RuntimeContract(BaseModel):
    """
    Base model for all Phase-6 runtime contracts.

    Extra fields are rejected deliberately so accidental contract drift is
    detected immediately rather than silently ignored.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        use_enum_values=False,
    )


# ============================================================================
# RUNTIME LIFECYCLE
# ============================================================================

class RuntimeStatus(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class RuntimeExecutionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"


class RuntimeCommandType(str, Enum):
    INITIALIZE = "INITIALIZE"
    START = "START"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STEP = "STEP"
    STOP = "STOP"
    RESET = "RESET"
    LOAD_STATE = "LOAD_STATE"
    SAVE_STATE = "SAVE_STATE"
    APPLY_PATCH = "APPLY_PATCH"


class RuntimeEventType(str, Enum):
    RUNTIME_STARTED = "RUNTIME_STARTED"
    RUNTIME_PAUSED = "RUNTIME_PAUSED"
    RUNTIME_RESUMED = "RUNTIME_RESUMED"
    RUNTIME_STOPPED = "RUNTIME_STOPPED"
    FRAME_STARTED = "FRAME_STARTED"
    FRAME_COMPLETED = "FRAME_COMPLETED"
    ENTITY_SPAWNED = "ENTITY_SPAWNED"
    ENTITY_DESTROYED = "ENTITY_DESTROYED"
    STATE_CHANGED = "STATE_CHANGED"
    PATCH_APPLIED = "PATCH_APPLIED"
    ERROR = "ERROR"
    WARNING = "WARNING"
    CUSTOM = "CUSTOM"


class RuntimeExecutionMode(str, Enum):
    REALTIME = "realtime"
    FIXED_STEP = "fixed_step"
    BATCH = "batch"
    REPLAY = "replay"
    HEADLESS = "headless"


# ============================================================================
# RUNTIME CAPABILITY CONTRACT
# ============================================================================

class RuntimeCapability(RuntimeContract):
    name: str
    status: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED
    version: Optional[str] = None
    provider: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = str(value).strip()

        if not value:
            raise ValueError("Runtime capability name cannot be empty.")

        return value[:MAX_NAME_LENGTH]


class RuntimeCapabilitySet(RuntimeContract):
    capabilities: Dict[str, RuntimeCapability] = Field(
        default_factory=dict
    )

    def add(self, capability: RuntimeCapability) -> None:
        self.capabilities[capability.name] = capability

    def get(
        self,
        name: str,
    ) -> Optional[RuntimeCapability]:
        return self.capabilities.get(name)

    def require_available(
        self,
        names: Iterable[str],
    ) -> None:
        missing = [
            name
            for name in names
            if (
                name not in self.capabilities
                or self.capabilities[name].status
                not in {
                    CapabilityStatus.AVAILABLE,
                    CapabilityStatus.DEGRADED,
                }
            )
        ]

        if missing:
            raise ValueError(
                "Required runtime capabilities are unavailable: "
                f"{missing}"
            )


# ============================================================================
# ARTIFACT / REFERENCE CONTRACTS
# ============================================================================

class RuntimeArtifactReference(RuntimeContract):
    """
    Immutable reference to an artifact.

    This does not mean the artifact exists. The producing subsystem must
    provide verified evidence separately.
    """

    reference_id: str = Field(
        default_factory=lambda: new_runtime_id("ref_")
    )
    kind: str
    path: Optional[str] = None
    uri: Optional[str] = None
    checksum: Optional[str] = None
    media_type: Optional[str] = None
    size_bytes: Optional[int] = Field(
        default=None,
        ge=0,
    )
    verified: bool = False
    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_reference(self) -> "RuntimeArtifactReference":
        if not self.path and not self.uri:
            raise ValueError(
                "RuntimeArtifactReference requires path or uri."
            )

        return self


# ============================================================================
# RUNTIME TARGET
# ============================================================================

class RuntimeTargetContract(RuntimeContract):
    runtime_type: RuntimeType
    target_platform: TargetPlatform
    name: str
    version: Optional[str] = None

    capabilities: Set[str] = Field(
        default_factory=set
    )

    configuration: Dict[str, Any] = Field(
        default_factory=dict
    )

    capability_status: CapabilityStatus = (
        CapabilityStatus.NOT_CONFIGURED
    )


# ============================================================================
# ENTITY / STATE CONTRACTS
# ============================================================================

class RuntimeEntityState(RuntimeContract):
    entity_id: str
    active: bool = True
    position: Tuple[float, float, float] = (
        0.0,
        0.0,
        0.0,
    )
    rotation: Tuple[float, float, float] = (
        0.0,
        0.0,
        0.0,
    )
    velocity: Tuple[float, float, float] = (
        0.0,
        0.0,
        0.0,
    )
    properties: Dict[str, Any] = Field(
        default_factory=dict
    )
    components: Set[str] = Field(
        default_factory=set
    )

    @field_validator(
        "position",
        "rotation",
        "velocity",
    )
    @classmethod
    def validate_vector(
        cls,
        value: Tuple[float, float, float],
    ) -> Tuple[float, float, float]:
        if len(value) != 3:
            raise ValueError(
                "Runtime vectors must contain exactly three values."
            )

        return tuple(float(item) for item in value)


class RuntimeWorldState(RuntimeContract):
    world_id: str

    frame_id: int = Field(
        default=0,
        ge=0,
    )

    simulation_time: float = Field(
        default=0.0,
        ge=0.0,
    )

    time_scale: float = Field(
        default=1.0,
        gt=0.0,
    )

    entities: Dict[str, RuntimeEntityState] = Field(
        default_factory=dict
    )

    global_state: Dict[str, Any] = Field(
        default_factory=dict
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# COMMAND CONTRACT
# ============================================================================

class RuntimeCommand(RuntimeContract):
    command_id: str = Field(
        default_factory=lambda: new_runtime_id("cmd_")
    )

    command_type: RuntimeCommandType

    runtime_id: str
    project_id: str

    issued_at: datetime = Field(
        default_factory=utc_now
    )

    source: str = "system"

    payload: Dict[str, Any] = Field(
        default_factory=dict
    )

    expected_state_version: Optional[int] = Field(
        default=None,
        ge=0,
    )

    timeout_seconds: Optional[float] = Field(
        default=None,
        gt=0.0,
    )

    idempotency_key: Optional[str] = None

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @field_validator("command_type")
    @classmethod
    def normalize_command_type(
        cls,
        value: RuntimeCommandType,
    ) -> RuntimeCommandType:
        return value


# ============================================================================
# EVENT CONTRACT
# ============================================================================

class RuntimeEvent(RuntimeContract):
    event_id: str = Field(
        default_factory=lambda: new_runtime_id("event_")
    )

    event_type: RuntimeEventType

    runtime_id: str
    project_id: str

    frame_id: Optional[int] = Field(
        default=None,
        ge=0,
    )

    simulation_time: Optional[float] = Field(
        default=None,
        ge=0.0,
    )

    emitted_at: datetime = Field(
        default_factory=utc_now
    )

    source: str = "runtime"

    payload: Dict[str, Any] = Field(
        default_factory=dict
    )

    severity: str = "INFO"

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# FRAME CONTRACT
# ============================================================================

class RuntimeFrame(RuntimeContract):
    """
    Canonical simulation-frame envelope.

    The runtime engine can attach state deltas, events and metrics without
    changing the outer contract.
    """

    frame_id: int = Field(
        ge=0
    )

    runtime_id: str
    project_id: str

    simulation_time: float = Field(
        default=0.0,
        ge=0.0,
    )

    delta_time: float = Field(
        default=1.0 / 60.0,
        ge=0.0,
        le=10.0,
    )

    execution_mode: RuntimeExecutionMode = (
        RuntimeExecutionMode.REALTIME
    )

    commands: List[RuntimeCommand] = Field(
        default_factory=list
    )

    events: List[RuntimeEvent] = Field(
        default_factory=list
    )

    entity_updates: Dict[
        str,
        RuntimeEntityState,
    ] = Field(
        default_factory=dict
    )

    metrics: Dict[str, float] = Field(
        default_factory=dict
    )

    state_version: int = Field(
        default=0,
        ge=0,
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_frame_size(self) -> "RuntimeFrame":
        if len(self.commands) > MAX_COMMANDS_PER_FRAME:
            raise ValueError(
                "RuntimeFrame exceeds MAX_COMMANDS_PER_FRAME."
            )

        if len(self.events) > MAX_EVENTS_PER_FRAME:
            raise ValueError(
                "RuntimeFrame exceeds MAX_EVENTS_PER_FRAME."
            )

        return self


# ============================================================================
# RUNTIME EXECUTION REQUEST
# ============================================================================

class RuntimeExecutionRequest(RuntimeContract):
    """
    Canonical request sent from orchestration/compiler layers to runtime.

    It is intentionally declarative. It describes what should execute; it
    does not itself execute anything.
    """

    request_id: str = Field(
        default_factory=lambda: new_runtime_id("runtime_req_")
    )

    project_id: str
    runtime_id: str

    target: RuntimeTargetContract

    mode: RuntimeExecutionMode = (
        RuntimeExecutionMode.REALTIME
    )

    commands: List[RuntimeCommand] = Field(
        default_factory=list
    )

    initial_state: Optional[RuntimeWorldState] = None

    requested_frame_count: Optional[int] = Field(
        default=None,
        ge=1,
    )

    requested_delta_time: Optional[float] = Field(
        default=None,
        gt=0.0,
        le=10.0,
    )

    required_capabilities: Set[str] = Field(
        default_factory=set
    )

    dependencies: List[str] = Field(
        default_factory=list
    )

    input_artifacts: List[
        RuntimeArtifactReference
    ] = Field(
        default_factory=list
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_request(self) -> "RuntimeExecutionRequest":
        if len(self.commands) > MAX_BATCH_SIZE:
            raise ValueError(
                "RuntimeExecutionRequest contains too many commands."
            )

        if len(self.dependencies) > MAX_DEPENDENCIES:
            raise ValueError(
                "RuntimeExecutionRequest contains too many dependencies."
            )

        if len(self.input_artifacts) > MAX_OUTPUT_REFERENCES:
            raise ValueError(
                "RuntimeExecutionRequest contains too many artifacts."
            )

        return self


# ============================================================================
# EXECUTION METRICS
# ============================================================================

class RuntimeMetrics(RuntimeContract):
    frames_processed: int = Field(
        default=0,
        ge=0,
    )

    commands_processed: int = Field(
        default=0,
        ge=0,
    )

    events_emitted: int = Field(
        default=0,
        ge=0,
    )

    active_entities: int = Field(
        default=0,
        ge=0,
    )

    active_npcs: int = Field(
        default=0,
        ge=0,
    )

    frame_time_ms: float = Field(
        default=0.0,
        ge=0.0,
    )

    simulation_time_seconds: float = Field(
        default=0.0,
        ge=0.0,
    )

    memory_mb: Optional[float] = Field(
        default=None,
        ge=0.0,
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# EXECUTION ERROR
# ============================================================================

class RuntimeErrorRecord(RuntimeContract):
    code: str

    message: str

    fatal: bool = False

    recoverable: bool = False

    stage: Optional[str] = None

    command_id: Optional[str] = None

    frame_id: Optional[int] = Field(
        default=None,
        ge=0,
    )

    timestamp: datetime = Field(
        default_factory=utc_now
    )

    details: Dict[str, Any] = Field(
        default_factory=dict
    )

    @field_validator("message")
    @classmethod
    def bound_message(
        cls,
        value: str,
    ) -> str:
        return str(value)[:MAX_ERROR_LENGTH]


# ============================================================================
# RUNTIME EXECUTION RESULT
# ============================================================================

class RuntimeExecutionResult(RuntimeContract):
    """
    Canonical result returned by a runtime backend.

    `COMPLETED` means the backend completed the requested execution contract;
    it does not automatically mean the produced game is QA-passed or build-
    ready.
    """

    request_id: str
    runtime_id: str
    project_id: str

    status: RuntimeExecutionStatus

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    frames_processed: int = Field(
        default=0,
        ge=0,
    )

    final_state: Optional[RuntimeWorldState] = None

    final_frame: Optional[RuntimeFrame] = None

    metrics: RuntimeMetrics = Field(
        default_factory=RuntimeMetrics
    )

    output_artifacts: List[
        RuntimeArtifactReference
    ] = Field(
        default_factory=list
    )

    errors: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    warnings: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    backend: Optional[str] = None
    backend_version: Optional[str] = None

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_result(self) -> "RuntimeExecutionResult":
        if (
            self.status
            is RuntimeExecutionStatus.COMPLETED
            and self.completed_at is None
        ):
            raise ValueError(
                "COMPLETED RuntimeExecutionResult requires completed_at."
            )

        if len(self.output_artifacts) > MAX_OUTPUT_REFERENCES:
            raise ValueError(
                "RuntimeExecutionResult contains too many artifacts."
            )

        return self


# ============================================================================
# STAGE CONTRACTS
# ============================================================================

class RuntimeStageContract(RuntimeContract):
    """
    A single deterministic runtime pipeline stage.

    This is the contract that later compilers/schedulers will consume.
    """

    stage_id: str = Field(
        default_factory=lambda: new_runtime_id("stage_")
    )

    name: str

    project_id: str

    project_status: ProjectStatus

    status: StageStatus = StageStatus.PENDING

    owner: str

    depends_on: List[str] = Field(
        default_factory=list
    )

    required_capabilities: Set[str] = Field(
        default_factory=set
    )

    inputs: List[
        RuntimeArtifactReference
    ] = Field(
        default_factory=list
    )

    expected_outputs: List[str] = Field(
        default_factory=list
    )

    timeout_seconds: Optional[float] = Field(
        default=None,
        gt=0.0,
    )

    max_retries: int = Field(
        default=0,
        ge=0,
        le=20,
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_stage(self) -> "RuntimeStageContract":
        self.depends_on = _bounded_unique(
            self.depends_on,
            MAX_DEPENDENCIES,
        )

        self.expected_outputs = _bounded_unique(
            self.expected_outputs,
            MAX_OUTPUT_REFERENCES,
        )

        return self


class RuntimeStageResult(RuntimeContract):
    stage_id: str

    project_id: str

    status: StageStatus

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    duration_ms: Optional[float] = Field(
        default=None,
        ge=0.0,
    )

    output_artifacts: List[
        RuntimeArtifactReference
    ] = Field(
        default_factory=list
    )

    errors: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    warnings: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# RUNTIME SESSION
# ============================================================================

class RuntimeSessionContract(RuntimeContract):
    """
    Persistent logical runtime session.

    This is not the same thing as a websocket/session transport.
    """

    runtime_id: str = Field(
        default_factory=lambda: new_runtime_id("runtime_")
    )

    project_id: str

    status: RuntimeStatus = RuntimeStatus.CREATED

    execution_mode: RuntimeExecutionMode = (
        RuntimeExecutionMode.REALTIME
    )

    target: RuntimeTargetContract

    state_version: int = Field(
        default=0,
        ge=0,
    )

    current_frame_id: int = Field(
        default=0,
        ge=0,
    )

    simulation_time: float = Field(
        default=0.0,
        ge=0.0,
    )

    runtime_capabilities: RuntimeCapabilitySet = Field(
        default_factory=RuntimeCapabilitySet
    )

    metrics: RuntimeMetrics = Field(
        default_factory=RuntimeMetrics
    )

    last_error: Optional[
        RuntimeErrorRecord
    ] = None

    created_at: datetime = Field(
        default_factory=utc_now
    )

    updated_at: datetime = Field(
        default_factory=utc_now
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    def touch(self) -> None:
        self.updated_at = utc_now()


# ============================================================================
# RUNTIME HEALTH
# ============================================================================

class RuntimeHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class RuntimeHealthContract(RuntimeContract):
    runtime_id: str

    status: RuntimeHealthStatus = (
        RuntimeHealthStatus.UNKNOWN
    )

    capability_status: CapabilityStatus = (
        CapabilityStatus.NOT_CONFIGURED
    )

    checked_at: datetime = Field(
        default_factory=utc_now
    )

    latency_ms: Optional[float] = Field(
        default=None,
        ge=0.0,
    )

    memory_mb: Optional[float] = Field(
        default=None,
        ge=0.0,
    )

    active_entities: int = Field(
        default=0,
        ge=0,
    )

    active_npcs: int = Field(
        default=0,
        ge=0,
    )

    frame_rate: Optional[float] = Field(
        default=None,
        ge=0.0,
    )

    errors: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# RUNTIME SNAPSHOT / SAVE CONTRACT
# ============================================================================

class RuntimeSnapshotContract(RuntimeContract):
    snapshot_id: str = Field(
        default_factory=lambda: new_runtime_id("snapshot_")
    )

    runtime_id: str
    project_id: str

    frame_id: int = Field(
        ge=0
    )

    state_version: int = Field(
        ge=0
    )

    created_at: datetime = Field(
        default_factory=utc_now
    )

    world_state: RuntimeWorldState

    checksum: Optional[str] = None

    artifact_reference: Optional[
        RuntimeArtifactReference
    ] = None

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def ensure_checksum(self) -> "RuntimeSnapshotContract":
        if not self.checksum:
            canonical = self.world_state.model_dump_json(
                sort_keys=True
            )
            self.checksum = content_sha256(
                canonical
            )

        return self


# ============================================================================
# RUNTIME PATCH CONTRACT
# ============================================================================

class RuntimePatchContract(RuntimeContract):
    patch_id: str = Field(
        default_factory=lambda: new_runtime_id("patch_")
    )

    runtime_id: str
    project_id: str

    base_state_version: int = Field(
        ge=0
    )

    target_state_version: int = Field(
        ge=0
    )

    operations: List[Dict[str, Any]] = Field(
        default_factory=list
    )

    source: str = "runtime_compiler"

    created_at: datetime = Field(
        default_factory=utc_now
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_versions(self) -> "RuntimePatchContract":
        if (
            self.target_state_version
            < self.base_state_version
        ):
            raise ValueError(
                "target_state_version cannot be lower than "
                "base_state_version."
            )

        return self


# ============================================================================
# PIPELINE EXECUTION ENVELOPE
# ============================================================================

class RuntimePipelineRequest(RuntimeContract):
    """
    High-level contract for submitting a complete runtime-oriented pipeline.

    Later phases can map these stages onto agents, compilers and schedulers.
    """

    request_id: str = Field(
        default_factory=lambda: new_runtime_id("pipeline_req_")
    )

    project_id: str

    target: RuntimeTargetContract

    stages: List[
        RuntimeStageContract
    ] = Field(
        default_factory=list
    )

    runtime_request: Optional[
        RuntimeExecutionRequest
    ] = None

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_pipeline(self) -> "RuntimePipelineRequest":
        if len(self.stages) > MAX_BATCH_SIZE:
            raise ValueError(
                "RuntimePipelineRequest contains too many stages."
            )

        stage_ids = {
            stage.stage_id
            for stage in self.stages
        }

        for stage in self.stages:
            missing = [
                dependency
                for dependency in stage.depends_on
                if dependency not in stage_ids
            ]

            if missing:
                raise ValueError(
                    f"Stage '{stage.stage_id}' has missing dependencies: "
                    f"{missing}"
                )

        return self


class RuntimePipelineResult(RuntimeContract):
    request_id: str

    project_id: str

    status: RuntimeExecutionStatus

    stage_results: List[
        RuntimeStageResult
    ] = Field(
        default_factory=list
    )

    runtime_result: Optional[
        RuntimeExecutionResult
    ] = None

    errors: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    warnings: List[
        RuntimeErrorRecord
    ] = Field(
        default_factory=list
    )

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# SERIALIZATION HELPERS
# ============================================================================

def runtime_json(
    value: RuntimeContract,
) -> str:
    """
    Canonical JSON representation for hashing, transport and persistence.
    """
    return value.model_dump_json(
        by_alias=True,
        exclude_none=False,
        round_trip=True,
    )


def runtime_dict(
    value: RuntimeContract,
) -> Dict[str, Any]:
    """
    Safe dictionary representation for internal adapters.
    """
    return value.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=False,
    )


def runtime_checksum(
    value: RuntimeContract,
) -> str:
    return content_sha256(
        runtime_json(value)
    )


# ============================================================================
# PUBLIC EXPORTS
# ============================================================================

__all__ = [
    "RuntimeArtifactReference",
    "RuntimeCapability",
    "RuntimeCapabilitySet",
    "RuntimeCommand",
    "RuntimeCommandType",
    "RuntimeContract",
    "RuntimeEntityState",
    "RuntimeErrorRecord",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeExecutionMode",
    "RuntimeExecutionRequest",
    "RuntimeExecutionResult",
    "RuntimeFrame",
    "RuntimeHealthContract",
    "RuntimeHealthStatus",
    "RuntimeMetrics",
    "RuntimePatchContract",
    "RuntimePipelineRequest",
    "RuntimePipelineResult",
    "RuntimeSessionContract",
    "RuntimeSnapshotContract",
    "RuntimeStageContract",
    "RuntimeStageResult",
    "RuntimeStatus",
    "RuntimeTargetContract",
    "content_sha256",
    "new_runtime_id",
    "runtime_checksum",
    "runtime_dict",
    "runtime_json",
    "utc_now",
]
