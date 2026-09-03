"""
config/settings.py
============================================================

Riot Runtime Configuration & Policy Engine
-------------------------------------------

This module is the single source of truth for runtime configuration.

Design goals
============

1. Strongly typed configuration.
2. Fail-closed security defaults.
3. Environment-variable driven deployment.
4. Development/staging/production profiles.
5. Provider pools for AI and external services.
6. High-performance simulation configuration.
7. AAA game-generation budgets and quality controls.
8. Multiplayer/networking controls.
9. Build/export configuration.
10. Observability and diagnostics.
11. Feature gates.
12. Backward compatibility with existing Riot modules.
13. No secret values exposed in logs or repr().
14. Explicit validation before startup.
15. Deterministic configuration snapshots.

This file deliberately contains configuration and policy only.
It must not perform network calls, create subprocesses, start servers,
or initialize the game engine.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Final, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Riot.Config")


# ============================================================================
# CONSTANTS
# ============================================================================

MIN_ADMIN_SECRET_LENGTH: Final[int] = 12
MAX_CONTEXT_KEYS: Final[int] = 512

DEFAULT_HTTP_TIMEOUT_SECONDS: Final[int] = 60
DEFAULT_HTTP_POOL_SIZE: Final[int] = 100

DEFAULT_ENGINE_TICK_HZ: Final[int] = 60
MAX_ENGINE_TICK_HZ: Final[int] = 240

DEFAULT_TASK_LIMIT: Final[int] = 10_000
DEFAULT_TASK_TTL_SECONDS: Final[int] = 3_600

DEFAULT_RATE_LIMIT_RPM: Final[int] = 120

DEFAULT_MAX_PLAYERS_PER_WORLD: Final[int] = 30_000

DEFAULT_MAX_AGENT_COUNT: Final[int] = 32
DEFAULT_MAX_PARALLEL_AGENTS: Final[int] = 16

DEFAULT_MAX_BUILD_TIMEOUT_SECONDS: Final[int] = 1_800

DEFAULT_MAX_UPLOAD_MB: Final[int] = 512

DEFAULT_LOG_LEVEL: Final[str] = "INFO"


# ============================================================================
# ENUMS
# ============================================================================


class EnvironmentMode(str, Enum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(str, Enum):
    """Application log serialization format."""

    TEXT = "text"
    JSON = "json"


class AIGenerationMode(str, Enum):
    """Global AI generation strategy."""

    SINGLE = "single"
    ROUTED = "routed"
    SWARM = "swarm"
    ADAPTIVE = "adaptive"


class QualityTier(str, Enum):
    """Target generated-game quality profile."""

    PROTOTYPE = "prototype"
    INDIE = "indie"
    AA = "aa"
    AAA = "aaa"
    CINEMATIC = "cinematic"


class SimulationMode(str, Enum):
    """Simulation execution model."""

    LOCAL = "local"
    NATIVE = "native"
    HYBRID = "hybrid"
    DISTRIBUTED = "distributed"


class BuildTarget(str, Enum):
    """Supported build targets."""

    WEB = "web"
    ANDROID = "android"
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    SERVER = "server"


class StorageBackend(str, Enum):
    """Persistence backend."""

    LOCAL = "local"
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    REDIS = "redis"
    S3 = "s3"
    HYBRID = "hybrid"


class FeatureFlag(str, Enum):
    """Known experimental/advanced capabilities."""

    AI_SWARM = "ai_swarm"
    PROCEDURAL_WORLDS = "procedural_worlds"
    NATIVE_SIMULATION = "native_simulation"
    MULTIPLAYER = "multiplayer"
    WEBRTC_STREAMING = "webrtc_streaming"
    LIVE_EDITING = "live_editing"
    SELF_EVOLUTION = "self_evolution"
    AAA_PIPELINE = "aaa_pipeline"
    DYNAMIC_LOD = "dynamic_lod"
    ADAPTIVE_DIFFICULTY = "adaptive_difficulty"
    AUDIO_REACTIVE_WORLD = "audio_reactive_world"
    AUTONOMOUS_QA = "autonomous_qa"
    DISTRIBUTED_BUILDS = "distributed_builds"


# ============================================================================
# ENVIRONMENT PARSING HELPERS
# ============================================================================


def _get_env(
    key: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Read an environment variable safely."""

    value = os.getenv(key)

    if value is None:
        return default

    return value.strip()


def _get_bool(
    key: str,
    default: bool,
) -> bool:
    """Parse a strict-ish boolean environment variable."""

    raw = _get_env(key)

    if raw is None or raw == "":
        return default

    normalized = raw.lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "y",
        "on",
        "enabled",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "n",
        "off",
        "disabled",
    }:
        return False

    raise ValueError(
        f"Invalid boolean value for {key!r}: {raw!r}"
    )


def _get_int(
    key: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Parse and bound an integer environment variable."""

    raw = _get_env(key)

    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"{key} must be an integer, got {raw!r}"
            ) from exc

    if minimum is not None and value < minimum:
        raise ValueError(
            f"{key} must be >= {minimum}, got {value}"
        )

    if maximum is not None and value > maximum:
        raise ValueError(
            f"{key} must be <= {maximum}, got {value}"
        )

    return value


def _get_float(
    key: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Parse and bound a floating-point environment variable."""

    raw = _get_env(key)

    if raw is None or raw == "":
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(
                f"{key} must be a number, got {raw!r}"
            ) from exc

    if minimum is not None and value < minimum:
        raise ValueError(
            f"{key} must be >= {minimum}, got {value}"
        )

    if maximum is not None and value > maximum:
        raise ValueError(
            f"{key} must be <= {maximum}, got {value}"
        )

    return value


def _get_csv(
    key: str,
    default: Sequence[str] = (),
) -> List[str]:
    """Parse a comma-separated environment variable."""

    raw = _get_env(key)

    if raw is None:
        return list(default)

    if not raw:
        return []

    return [
        item.strip()
        for item in raw.split(",")
        if item.strip()
    ]


def _get_enum(
    key: str,
    enum_type: type[Enum],
    default: Enum,
) -> Enum:
    """Parse an enum from an environment variable."""

    raw = _get_env(key)

    if raw is None or raw == "":
        return default

    try:
        return enum_type(raw.lower())
    except ValueError as exc:
        valid = ", ".join(
            str(item.value)
            for item in enum_type
        )

        raise ValueError(
            f"{key} must be one of [{valid}], got {raw!r}"
        ) from exc


# ============================================================================
# SECRET UTILITIES
# ============================================================================


def _fingerprint_secret(
    value: Optional[str],
) -> Optional[str]:
    """
    Produce a non-reversible short fingerprint.

    This is useful for diagnostics because we can tell whether a secret
    changed without ever logging the secret itself.
    """

    if not value:
        return None

    digest = hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()

    return digest[:16]


def _generate_dev_secret() -> str:
    """Generate a high-entropy ephemeral development credential."""

    return secrets.token_urlsafe(32)


# ============================================================================
# PROVIDER CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class ProviderConfig:
    """
    Configuration for one AI/external provider.

    Multiple models and API keys may be supplied. Riot's gateway can later
    select among them according to latency, availability, cost, or quality.
    """

    name: str
    api_keys: Tuple[str, ...]
    models: Tuple[str, ...]
    enabled: bool = True

    request_timeout_seconds: int = 60
    max_concurrency: int = 8
    max_retries: int = 3

    # Cost controls
    cost_weight: float = 1.0
    quality_weight: float = 1.0

    # Reliability
    circuit_breaker_enabled: bool = True

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.api_keys
        )


# ============================================================================
# ENGINE CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class EngineSettings:
    """High-performance simulation/runtime configuration."""

    tick_hz: int = DEFAULT_ENGINE_TICK_HZ

    simulation_mode: SimulationMode = SimulationMode.HYBRID

    max_entities: int = 1_000_000
    max_active_entities_per_tick: int = 250_000

    physics_substeps: int = 4
    deterministic_simulation: bool = True

    worker_threads: int = 0
    native_workers: int = 8

    enable_parallel_batches: bool = True
    max_batches_per_tick: int = 256

    fixed_timestep: bool = True

    target_frame_budget_ms: float = 16.666
    max_tick_overrun_ms: float = 100.0

    enable_dynamic_lod: bool = True
    enable_streaming_world: bool = True
    world_chunk_size: int = 256

    enable_entity_sleeping: bool = True
    enable_spatial_partitioning: bool = True

    spatial_cell_size: int = 128


# ============================================================================
# MULTIPLAYER CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class MultiplayerSettings:
    """Authoritative multiplayer runtime configuration."""

    enabled: bool = True

    max_players_per_world: int = (
        DEFAULT_MAX_PLAYERS_PER_WORLD
    )

    max_connections_per_process: int = 10_000

    tick_hz: int = 60

    snapshot_hz: int = 20

    interpolation_buffer_ms: int = 100

    max_action_rate_per_second: int = 30

    max_message_size_kb: int = 256

    enable_interest_management: bool = True
    enable_spatial_replication: bool = True
    enable_delta_snapshots: bool = True
    enable_client_prediction: bool = True
    enable_server_reconciliation: bool = True

    interest_radius: float = 2_000.0

    max_replication_entities: int = 2_048

    heartbeat_seconds: int = 15

    disconnect_timeout_seconds: int = 45


# ============================================================================
# AI GENERATION CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class GenerationSettings:
    """AAA game-generation policy and resource limits."""

    mode: AIGenerationMode = (
        AIGenerationMode.ADAPTIVE
    )

    quality_tier: QualityTier = (
        QualityTier.AAA
    )

    max_agents: int = DEFAULT_MAX_AGENT_COUNT

    max_parallel_agents: int = (
        DEFAULT_MAX_PARALLEL_AGENTS
    )

    max_generation_minutes: int = 30

    max_prompt_length: int = 100_000

    max_context_tokens: int = 2_000_000

    enable_planning_agent: bool = True
    enable_world_agent: bool = True
    enable_asset_agent: bool = True
    enable_gameplay_agent: bool = True
    enable_physics_agent: bool = True
    enable_audio_agent: bool = True
    enable_ui_agent: bool = True
    enable_network_agent: bool = True
    enable_optimization_agent: bool = True
    enable_qa_agent: bool = True
    enable_build_agent: bool = True

    enable_agent_memory: bool = True
    enable_agent_reflection: bool = True
    enable_agent_cross_review: bool = True

    # Number of iterative refinement passes.
    refinement_passes: int = 3

    # Quality gate threshold.
    minimum_quality_score: float = 0.80

    # AI call cost protection.
    max_generation_cost_usd: float = 50.0


# ============================================================================
# BUILD CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class BuildSettings:
    """Universal build/export system configuration."""

    enabled_targets: Tuple[str, ...] = (
        "web",
        "android",
        "windows",
        "linux",
        "macos",
        "server",
    )

    build_timeout_seconds: int = (
        DEFAULT_MAX_BUILD_TIMEOUT_SECONDS
    )

    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB

    artifact_retention_days: int = 30

    parallel_builds: int = 4

    deterministic_builds: bool = True

    enable_build_cache: bool = True

    enable_incremental_builds: bool = True

    enable_source_maps: bool = True

    enable_asset_compression: bool = True

    enable_code_minification: bool = True

    enable_binary_signing: bool = False

    build_directory: Path = Path("./builds")

    artifact_directory: Path = Path("./artifacts")

    workspace_directory: Path = Path("./workspace")


# ============================================================================
# STORAGE CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class StorageSettings:
    """Game/world/artifact persistence configuration."""

    backend: StorageBackend = (
        StorageBackend.HYBRID
    )

    local_data_directory: Path = Path(
        "./local_cloud_data"
    )

    cache_directory: Path = Path(
        "./runtime_cache"
    )

    object_directory: Path = Path(
        "./objects"
    )

    database_url: Optional[str] = None

    redis_url: Optional[str] = None

    object_storage_url: Optional[str] = None

    enable_cache: bool = True

    cache_ttl_seconds: int = 3_600

    enable_compression: bool = True

    enable_integrity_hashes: bool = True


# ============================================================================
# SECURITY CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class SecuritySettings:
    """Application security and generated-code safety policy."""

    require_authentication: bool = True

    admin_secret: Optional[str] = None

    secret_fingerprint: Optional[str] = None

    min_secret_length: int = MIN_ADMIN_SECRET_LENGTH

    allow_ephemeral_dev_secret: bool = False

    enable_rate_limiting: bool = True

    requests_per_minute: int = (
        DEFAULT_RATE_LIMIT_RPM
    )

    websocket_messages_per_second: int = 30

    max_request_body_mb: int = 16

    allow_debug_endpoints: bool = False

    enable_audit_log: bool = True

    enable_security_headers: bool = True

    enable_request_signatures: bool = True

    allow_self_evolution: bool = False

    require_human_approval_for_evolution: bool = True

    enable_generated_code_sandbox: bool = True

    enable_path_sandbox: bool = True

    enable_subprocess_limits: bool = True


# ============================================================================
# NETWORK CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class NetworkSettings:
    """HTTP/WebSocket/WebRTC network configuration."""

    host: str = "0.0.0.0"

    port: int = 8000

    public_base_url: Optional[str] = None

    allowed_origins: Tuple[str, ...] = (
        "http://localhost:3000",
        "http://localhost:5173",
    )

    http_timeout_seconds: int = (
        DEFAULT_HTTP_TIMEOUT_SECONDS
    )

    http_pool_size: int = (
        DEFAULT_HTTP_POOL_SIZE
    )

    websocket_ping_interval: int = 20

    websocket_ping_timeout: int = 20

    max_connections: int = 20_000

    enable_http2: bool = True

    enable_compression: bool = True


# ============================================================================
# OBSERVABILITY CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class ObservabilitySettings:
    """Logging, metrics, tracing, and diagnostics."""

    log_level: str = DEFAULT_LOG_LEVEL

    log_format: LogFormat = LogFormat.TEXT

    enable_metrics: bool = True

    enable_tracing: bool = True

    enable_request_logging: bool = True

    enable_performance_logging: bool = True

    enable_engine_telemetry: bool = True

    enable_ai_telemetry: bool = True

    enable_build_telemetry: bool = True

    metrics_port: int = 9090

    trace_sample_rate: float = 0.10


# ============================================================================
# FEATURE FLAGS
# ============================================================================


@dataclass(frozen=True)
class FeatureSettings:
    """Runtime feature gates."""

    ai_swarm: bool = True
    procedural_worlds: bool = True
    native_simulation: bool = True
    multiplayer: bool = True
    webrtc_streaming: bool = True
    live_editing: bool = True
    self_evolution: bool = False
    aaa_pipeline: bool = True
    dynamic_lod: bool = True
    adaptive_difficulty: bool = True
    audio_reactive_world: bool = True
    autonomous_qa: bool = True
    distributed_builds: bool = False


# ============================================================================
# TASK CONFIGURATION
# ============================================================================


@dataclass(frozen=True)
class TaskSettings:
    """Generation/build/background task control."""

    max_registry_size: int = DEFAULT_TASK_LIMIT

    ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS

    max_concurrent_jobs: int = 8

    max_concurrent_builds: int = 4

    max_concurrent_evolutions: int = 1

    job_timeout_seconds: int = 3_600

    enable_priority_queue: bool = True

    enable_task_persistence: bool = False


# ============================================================================
# MAIN CONFIGURATION
# ============================================================================


class GodNodeConfig:
    """
    Central Riot configuration object.

    Compatibility
    -------------
    Existing Riot modules can continue using:

        god_config.get_api_providers()
        god_config.has_provider(...)
        god_config.get_master_pin()
        god_config.is_production()
        god_config.is_development()

    while new modules can consume typed configuration sections.
    """

    def __init__(self) -> None:
        # --------------------------------------------------------------
        # Environment
        # --------------------------------------------------------------

        env_str = (
            _get_env(
                "GOD_ENV",
                EnvironmentMode.DEVELOPMENT.value,
            )
            or EnvironmentMode.DEVELOPMENT.value
        ).lower()

        try:
            self.environment = EnvironmentMode(
                env_str
            )
        except ValueError as exc:
            raise ValueError(
                "Invalid GOD_ENV. "
                "Expected one of: "
                + ", ".join(
                    item.value
                    for item in EnvironmentMode
                )
            ) from exc

        # --------------------------------------------------------------
        # Security
        # --------------------------------------------------------------

        self.master_pin = _get_env(
            "GOD_MASTER_PIN"
        )

        allow_ephemeral = _get_bool(
            "GOD_ALLOW_EPHEMERAL_DEV_SECRET",
            False,
        )

        if (
            not self.master_pin
            and self.environment == EnvironmentMode.DEVELOPMENT
            and allow_ephemeral
        ):
            self.master_pin = _generate_dev_secret()

            logger.warning(
                "Riot is using an ephemeral development administrator "
                "secret. Configure GOD_MASTER_PIN for deterministic "
                "local sessions."
            )

            logger.warning(
                "Generated development secret fingerprint: %s",
                _fingerprint_secret(
                    self.master_pin
                ),
            )

        self.security = SecuritySettings(
            require_authentication=_get_bool(
                "GOD_REQUIRE_AUTH",
                True,
            ),
            admin_secret=self.master_pin,
            secret_fingerprint=_fingerprint_secret(
                self.master_pin
            ),
            min_secret_length=_get_int(
                "GOD_MIN_SECRET_LENGTH",
                MIN_ADMIN_SECRET_LENGTH,
                minimum=12,
                maximum=1024,
            ),
            allow_ephemeral_dev_secret=allow_ephemeral,
            enable_rate_limiting=_get_bool(
                "GOD_RATE_LIMIT",
                True,
            ),
            requests_per_minute=_get_int(
                "GOD_RATE_LIMIT_RPM",
                DEFAULT_RATE_LIMIT_RPM,
                minimum=1,
                maximum=1_000_000,
            ),
            websocket_messages_per_second=_get_int(
                "GOD_WS_RATE",
                30,
                minimum=1,
                maximum=100_000,
            ),
            max_request_body_mb=_get_int(
                "GOD_MAX_REQUEST_BODY_MB",
                16,
                minimum=1,
                maximum=4_096,
            ),
            allow_debug_endpoints=_get_bool(
                "GOD_DEBUG_ENDPOINTS",
                self.environment
                in {
                    EnvironmentMode.DEVELOPMENT,
                    EnvironmentMode.TESTING,
                },
            ),
            enable_audit_log=_get_bool(
                "GOD_AUDIT_LOG",
                True,
            ),
            enable_security_headers=_get_bool(
                "GOD_SECURITY_HEADERS",
                True,
            ),
            enable_request_signatures=_get_bool(
                "GOD_REQUEST_SIGNATURES",
                True,
            ),
            allow_self_evolution=_get_bool(
                "GOD_ALLOW_SELF_EVOLUTION",
                False,
            ),
            require_human_approval_for_evolution=_get_bool(
                "GOD_HUMAN_APPROVAL_EVOLUTION",
                True,
            ),
            enable_generated_code_sandbox=_get_bool(
                "GOD_CODE_SANDBOX",
                True,
            ),
            enable_path_sandbox=_get_bool(
                "GOD_PATH_SANDBOX",
                True,
            ),
            enable_subprocess_limits=_get_bool(
                "GOD_SUBPROCESS_LIMITS",
                True,
            ),
        )

        # --------------------------------------------------------------
        # Networking
        # --------------------------------------------------------------

        origins = _get_csv(
            "GOD_CORS_ORIGINS",
            self._default_origins(),
        )

        self.network = NetworkSettings(
            host=_get_env(
                "GOD_HOST",
                "0.0.0.0",
            )
            or "0.0.0.0",
            port=_get_int(
                "GOD_PORT",
                8000,
                minimum=1,
                maximum=65_535,
            ),
            public_base_url=_get_env(
                "GOD_PUBLIC_BASE_URL"
            ),
            allowed_origins=tuple(
                origins
            ),
            http_timeout_seconds=_get_int(
                "GOD_HTTP_TIMEOUT",
                DEFAULT_HTTP_TIMEOUT_SECONDS,
                minimum=1,
                maximum=3_600,
            ),
            http_pool_size=_get_int(
                "GOD_HTTP_POOL_SIZE",
                DEFAULT_HTTP_POOL_SIZE,
                minimum=1,
                maximum=100_000,
            ),
            websocket_ping_interval=_get_int(
                "GOD_WS_PING_INTERVAL",
                20,
                minimum=1,
                maximum=300,
            ),
            websocket_ping_timeout=_get_int(
                "GOD_WS_PING_TIMEOUT",
                20,
                minimum=1,
                maximum=300,
            ),
            max_connections=_get_int(
                "GOD_MAX_CONNECTIONS",
                20_000,
                minimum=1,
                maximum=1_000_000,
            ),
            enable_http2=_get_bool(
                "GOD_HTTP2",
                True,
            ),
            enable_compression=_get_bool(
                "GOD_HTTP_COMPRESSION",
                True,
            ),
        )

        # --------------------------------------------------------------
        # AI providers
        # --------------------------------------------------------------

        self.api_providers = self._load_api_providers()

        # --------------------------------------------------------------
        # Generation
        # --------------------------------------------------------------

        self.generation = GenerationSettings(
            mode=_get_enum(
                "GOD_AI_MODE",
                AIGenerationMode,
                AIGenerationMode.ADAPTIVE,
            ),
            quality_tier=_get_enum(
                "GOD_QUALITY_TIER",
                QualityTier,
                QualityTier.AAA,
            ),
            max_agents=_get_int(
                "GOD_MAX_AGENTS",
                DEFAULT_MAX_AGENT_COUNT,
                minimum=1,
                maximum=256,
            ),
            max_parallel_agents=_get_int(
                "GOD_MAX_PARALLEL_AGENTS",
                DEFAULT_MAX_PARALLEL_AGENTS,
                minimum=1,
                maximum=128,
            ),
            max_generation_minutes=_get_int(
                "GOD_MAX_GENERATION_MINUTES",
                30,
                minimum=1,
                maximum=1_440,
            ),
            max_prompt_length=_get_int(
                "GOD_MAX_PROMPT_LENGTH",
                100_000,
                minimum=1_000,
                maximum=10_000_000,
            ),
            max_context_tokens=_get_int(
                "GOD_MAX_CONTEXT_TOKENS",
                2_000_000,
                minimum=1_000,
                maximum=20_000_000,
            ),
            enable_planning_agent=_get_bool(
                "GOD_AGENT_PLANNING",
                True,
            ),
            enable_world_agent=_get_bool(
                "GOD_AGENT_WORLD",
                True,
            ),
            enable_asset_agent=_get_bool(
                "GOD_AGENT_ASSETS",
                True,
            ),
            enable_gameplay_agent=_get_bool(
                "GOD_AGENT_GAMEPLAY",
                True,
            ),
            enable_physics_agent=_get_bool(
                "GOD_AGENT_PHYSICS",
                True,
            ),
            enable_audio_agent=_get_bool(
                "GOD_AGENT_AUDIO",
                True,
            ),
            enable_ui_agent=_get_bool(
                "GOD_AGENT_UI",
                True,
            ),
            enable_network_agent=_get_bool(
                "GOD_AGENT_NETWORK",
                True,
            ),
            enable_optimization_agent=_get_bool(
                "GOD_AGENT_OPTIMIZATION",
                True,
            ),
            enable_qa_agent=_get_bool(
                "GOD_AGENT_QA",
                True,
            ),
            enable_build_agent=_get_bool(
                "GOD_AGENT_BUILD",
                True,
            ),
            enable_agent_memory=_get_bool(
                "GOD_AGENT_MEMORY",
                True,
            ),
            enable_agent_reflection=_get_bool(
                "GOD_AGENT_REFLECTION",
                True,
            ),
            enable_agent_cross_review=_get_bool(
                "GOD_AGENT_CROSS_REVIEW",
                True,
            ),
            refinement_passes=_get_int(
                "GOD_REFINEMENT_PASSES",
                3,
                minimum=0,
                maximum=20,
            ),
            minimum_quality_score=_get_float(
                "GOD_MIN_QUALITY_SCORE",
                0.80,
                minimum=0.0,
                maximum=1.0,
            ),
            max_generation_cost_usd=_get_float(
                "GOD_MAX_GENERATION_COST_USD",
                50.0,
                minimum=0.0,
                maximum=1_000_000.0,
            ),
        )

        # --------------------------------------------------------------
        # Engine
        # --------------------------------------------------------------

        self.engine = EngineSettings(
            tick_hz=_get_int(
                "GOD_ENGINE_TICK_HZ",
                DEFAULT_ENGINE_TICK_HZ,
                minimum=1,
                maximum=MAX_ENGINE_TICK_HZ,
            ),
            simulation_mode=_get_enum(
                "GOD_SIMULATION_MODE",
                SimulationMode,
                SimulationMode.HYBRID,
            ),
            max_entities=_get_int(
                "GOD_MAX_ENTITIES",
                1_000_000,
                minimum=1,
                maximum=100_000_000,
            ),
            max_active_entities_per_tick=_get_int(
                "GOD_MAX_ACTIVE_ENTITIES_PER_TICK",
                250_000,
                minimum=1,
                maximum=100_000_000,
            ),
            physics_substeps=_get_int(
                "GOD_PHYSICS_SUBSTEPS",
                4,
                minimum=1,
                maximum=64,
            ),
            deterministic_simulation=_get_bool(
                "GOD_DETERMINISTIC_SIM",
                True,
            ),
            worker_threads=_get_int(
                "GOD_ENGINE_WORKERS",
                0,
                minimum=0,
                maximum=512,
            ),
            native_workers=_get_int(
                "GOD_NATIVE_WORKERS",
                8,
                minimum=1,
                maximum=512,
            ),
            enable_parallel_batches=_get_bool(
                "GOD_PARALLEL_BATCHES",
                True,
            ),
            max_batches_per_tick=_get_int(
                "GOD_MAX_BATCHES_PER_TICK",
                256,
                minimum=1,
                maximum=100_000,
            ),
            fixed_timestep=_get_bool(
                "GOD_FIXED_TIMESTEP",
                True,
            ),
            target_frame_budget_ms=_get_float(
                "GOD_FRAME_BUDGET_MS",
                16.666,
                minimum=1.0,
                maximum=1_000.0,
            ),
            max_tick_overrun_ms=_get_float(
                "GOD_MAX_TICK_OVERRUN_MS",
                100.0,
                minimum=1.0,
                maximum=10_000.0,
            ),
            enable_dynamic_lod=_get_bool(
                "GOD_DYNAMIC_LOD",
                True,
            ),
            enable_streaming_world=_get_bool(
                "GOD_STREAMING_WORLD",
                True,
            ),
            world_chunk_size=_get_int(
                "GOD_WORLD_CHUNK_SIZE",
                256,
                minimum=16,
                maximum=16_384,
            ),
            enable_entity_sleeping=_get_bool(
                "GOD_ENTITY_SLEEPING",
                True,
            ),
            enable_spatial_partitioning=_get_bool(
                "GOD_SPATIAL_PARTITIONING",
                True,
            ),
            spatial_cell_size=_get_int(
                "GOD_SPATIAL_CELL_SIZE",
                128,
                minimum=1,
                maximum=16_384,
            ),
        )

        # --------------------------------------------------------------
        # Multiplayer
        # --------------------------------------------------------------

        self.multiplayer = MultiplayerSettings(
            enabled=_get_bool(
                "GOD_MULTIPLAYER",
                True,
            ),
            max_players_per_world=_get_int(
                "GOD_MAX_PLAYERS_PER_WORLD",
                DEFAULT_MAX_PLAYERS_PER_WORLD,
                minimum=1,
                maximum=1_000_000,
            ),
            max_connections_per_process=_get_int(
                "GOD_MAX_CONNECTIONS_PER_PROCESS",
                10_000,
                minimum=1,
                maximum=1_000_000,
            ),
            tick_hz=_get_int(
                "GOD_MULTIPLAYER_TICK_HZ",
                60,
                minimum=1,
                maximum=240,
            ),
            snapshot_hz=_get_int(
                "GOD_SNAPSHOT_HZ",
                20,
                minimum=1,
                maximum=120,
            ),
            interpolation_buffer_ms=_get_int(
                "GOD_INTERPOLATION_BUFFER_MS",
                100,
                minimum=0,
                maximum=2_000,
            ),
            max_action_rate_per_second=_get_int(
                "GOD_MAX_ACTION_RATE",
                30,
                minimum=1,
                maximum=10_000,
            ),
            max_message_size_kb=_get_int(
                "GOD_MAX_WS_MESSAGE_KB",
                256,
                minimum=1,
                maximum=16_384,
            ),
            enable_interest_management=_get_bool(
                "GOD_INTEREST_MANAGEMENT",
                True,
            ),
            enable_spatial_replication=_get_bool(
                "GOD_SPATIAL_REPLICATION",
                True,
            ),
            enable_delta_snapshots=_get_bool(
                "GOD_DELTA_SNAPSHOTS",
                True,
            ),
            enable_client_prediction=_get_bool(
                "GOD_CLIENT_PREDICTION",
                True,
            ),
            enable_server_reconciliation=_get_bool(
                "GOD_SERVER_RECONCILIATION",
                True,
            ),
            interest_radius=_get_float(
                "GOD_INTEREST_RADIUS",
                2_000.0,
                minimum=1.0,
                maximum=1_000_000.0,
            ),
            max_replication_entities=_get_int(
                "GOD_MAX_REPLICATION_ENTITIES",
                2_048,
                minimum=1,
                maximum=100_000,
            ),
            heartbeat_seconds=_get_int(
                "GOD_HEARTBEAT_SECONDS",
                15,
                minimum=1,
                maximum=300,
            ),
            disconnect_timeout_seconds=_get_int(
                "GOD_DISCONNECT_TIMEOUT_SECONDS",
                45,
                minimum=5,
                maximum=3_600,
            ),
        )

        # --------------------------------------------------------------
        # Tasks
        # --------------------------------------------------------------

        self.tasks = TaskSettings(
            max_registry_size=_get_int(
                "GOD_MAX_TASKS",
                DEFAULT_TASK_LIMIT,
                minimum=100,
                maximum=10_000_000,
            ),
            ttl_seconds=_get_int(
                "GOD_TASK_TTL",
                DEFAULT_TASK_TTL_SECONDS,
                minimum=60,
                maximum=7_776_000,
            ),
            max_concurrent_jobs=_get_int(
                "GOD_MAX_CONCURRENT_JOBS",
                8,
                minimum=1,
                maximum=1_024,
            ),
            max_concurrent_builds=_get_int(
                "GOD_MAX_CONCURRENT_BUILDS",
                4,
                minimum=1,
                maximum=256,
            ),
            max_concurrent_evolutions=_get_int(
                "GOD_MAX_CONCURRENT_EVOLUTIONS",
                1,
                minimum=1,
                maximum=8,
            ),
            job_timeout_seconds=_get_int(
                "GOD_JOB_TIMEOUT_SECONDS",
                3_600,
                minimum=10,
                maximum=86_400,
            ),
            enable_priority_queue=_get_bool(
                "GOD_PRIORITY_QUEUE",
                True,
            ),
            enable_task_persistence=_get_bool(
                "GOD_TASK_PERSISTENCE",
                False,
            ),
        )

        # --------------------------------------------------------------
        # Builds
        # --------------------------------------------------------------

        build_targets = _get_csv(
            "GOD_BUILD_TARGETS",
            [
                BuildTarget.WEB.value,
                BuildTarget.ANDROID.value,
                BuildTarget.WINDOWS.value,
                BuildTarget.LINUX.value,
                BuildTarget.MACOS.value,
                BuildTarget.SERVER.value,
            ],
        )

        self.build = BuildSettings(
            enabled_targets=tuple(
                build_targets
            ),
            build_timeout_seconds=_get_int(
                "GOD_BUILD_TIMEOUT",
                DEFAULT_MAX_BUILD_TIMEOUT_SECONDS,
                minimum=30,
                maximum=86_400,
            ),
            max_upload_mb=_get_int(
                "GOD_MAX_UPLOAD_MB",
                DEFAULT_MAX_UPLOAD_MB,
                minimum=1,
                maximum=100_000,
            ),
            artifact_retention_days=_get_int(
                "GOD_ARTIFACT_RETENTION_DAYS",
                30,
                minimum=1,
                maximum=3_650,
            ),
            parallel_builds=_get_int(
                "GOD_PARALLEL_BUILDS",
                4,
                minimum=1,
                maximum=256,
            ),
            deterministic_builds=_get_bool(
                "GOD_DETERMINISTIC_BUILDS",
                True,
            ),
            enable_build_cache=_get_bool(
                "GOD_BUILD_CACHE",
                True,
            ),
            enable_incremental_builds=_get_bool(
                "GOD_INCREMENTAL_BUILDS",
                True,
            ),
            enable_source_maps=_get_bool(
                "GOD_SOURCE_MAPS",
                True,
            ),
            enable_asset_compression=_get_bool(
                "GOD_ASSET_COMPRESSION",
                True,
            ),
            enable_code_minification=_get_bool(
                "GOD_CODE_MINIFICATION",
                True,
            ),
            enable_binary_signing=_get_bool(
                "GOD_BINARY_SIGNING",
                False,
            ),
            build_directory=Path(
                _get_env(
                    "GOD_BUILD_DIR",
                    "./builds",
                )
                or "./builds"
            ),
            artifact_directory=Path(
                _get_env(
                    "GOD_ARTIFACT_DIR",
                    "./artifacts",
                )
                or "./artifacts"
            ),
            workspace_directory=Path(
                _get_env(
                    "GOD_WORKSPACE_DIR",
                    "./workspace",
                )
                or "./workspace"
            ),
        )

        # --------------------------------------------------------------
        # Storage
        # --------------------------------------------------------------

        self.storage = StorageSettings(
            backend=_get_enum(
                "GOD_STORAGE_BACKEND",
                StorageBackend,
                StorageBackend.HYBRID,
            ),
            local_data_directory=Path(
                _get_env(
                    "GOD_LOCAL_DATA_DIR",
                    "./local_cloud_data",
                )
                or "./local_cloud_data"
            ),
            cache_directory=Path(
                _get_env(
                    "GOD_CACHE_DIR",
                    "./runtime_cache",
                )
                or "./runtime_cache"
            ),
            object_directory=Path(
                _get_env(
                    "GOD_OBJECT_DIR",
                    "./objects",
                )
                or "./objects"
            ),
            database_url=_get_env(
                "DATABASE_URL"
            ),
            redis_url=_get_env(
                "REDIS_URL"
            ),
            object_storage_url=_get_env(
                "OBJECT_STORAGE_URL"
            ),
            enable_cache=_get_bool(
                "GOD_CACHE_ENABLED",
                True,
            ),
            cache_ttl_seconds=_get_int(
                "GOD_CACHE_TTL",
                3_600,
                minimum=1,
                maximum=31_536_000,
            ),
            enable_compression=_get_bool(
                "GOD_STORAGE_COMPRESSION",
                True,
            ),
            enable_integrity_hashes=_get_bool(
                "GOD_STORAGE_INTEGRITY_HASHES",
                True,
            ),
        )

        # --------------------------------------------------------------
        # Observability
        # --------------------------------------------------------------

        self.observability = ObservabilitySettings(
            log_level=(
                _get_env(
                    "GOD_LOG_LEVEL",
                    DEFAULT_LOG_LEVEL,
                )
                or DEFAULT_LOG_LEVEL
            ).upper(),
            log_format=_get_enum(
                "GOD_LOG_FORMAT",
                LogFormat,
                LogFormat.TEXT,
            ),
            enable_metrics=_get_bool(
                "GOD_METRICS",
                True,
            ),
            enable_tracing=_get_bool(
                "GOD_TRACING",
                True,
            ),
            enable_request_logging=_get_bool(
                "GOD_REQUEST_LOGGING",
                True,
            ),
            enable_performance_logging=_get_bool(
                "GOD_PERFORMANCE_LOGGING",
                True,
            ),
            enable_engine_telemetry=_get_bool(
                "GOD_ENGINE_TELEMETRY",
                True,
            ),
            enable_ai_telemetry=_get_bool(
                "GOD_AI_TELEMETRY",
                True,
            ),
            enable_build_telemetry=_get_bool(
                "GOD_BUILD_TELEMETRY",
                True,
            ),
            metrics_port=_get_int(
                "GOD_METRICS_PORT",
                9090,
                minimum=1,
                maximum=65_535,
            ),
            trace_sample_rate=_get_float(
                "GOD_TRACE_SAMPLE_RATE",
                0.10,
                minimum=0.0,
                maximum=1.0,
            ),
        )

        # --------------------------------------------------------------
        # Feature flags
        # --------------------------------------------------------------

        self.features = FeatureSettings(
            ai_swarm=_get_bool(
                "GOD_FEATURE_AI_SWARM",
                True,
            ),
            procedural_worlds=_get_bool(
                "GOD_FEATURE_PROCEDURAL_WORLDS",
                True,
            ),
            native_simulation=_get_bool(
                "GOD_FEATURE_NATIVE_SIMULATION",
                True,
            ),
            multiplayer=_get_bool(
                "GOD_FEATURE_MULTIPLAYER",
                True,
            ),
            webrtc_streaming=_get_bool(
                "GOD_FEATURE_WEBRTC",
                True,
            ),
            live_editing=_get_bool(
                "GOD_FEATURE_LIVE_EDITING",
                True,
            ),
            self_evolution=_get_bool(
                "GOD_FEATURE_SELF_EVOLUTION",
                False,
            ),
            aaa_pipeline=_get_bool(
                "GOD_FEATURE_AAA_PIPELINE",
                True,
            ),
            dynamic_lod=_get_bool(
                "GOD_FEATURE_DYNAMIC_LOD",
                True,
            ),
            adaptive_difficulty=_get_bool(
                "GOD_FEATURE_ADAPTIVE_DIFFICULTY",
                True,
            ),
            audio_reactive_world=_get_bool(
                "GOD_FEATURE_AUDIO_REACTIVE",
                True,
            ),
            autonomous_qa=_get_bool(
                "GOD_FEATURE_AUTONOMOUS_QA",
                True,
            ),
            distributed_builds=_get_bool(
                "GOD_FEATURE_DISTRIBUTED_BUILDS",
                False,
            ),
        )

        # --------------------------------------------------------------
        # Legacy compatibility fields
        # --------------------------------------------------------------

        self.require_secure_pin = (
            self.environment
            in {
                EnvironmentMode.STAGING,
                EnvironmentMode.PRODUCTION,
            }
        )

        self.http_timeout_seconds = (
            self.network.http_timeout_seconds
        )

        self.http_pool_size = (
            self.network.http_pool_size
        )

        self.max_tasks_registry_size = (
            self.tasks.max_registry_size
        )

        self.task_ttl_seconds = (
            self.tasks.ttl_seconds
        )

        self.rate_limit_enabled = (
            self.security.enable_rate_limiting
        )

        self.rate_limit_requests_per_minute = (
            self.security.requests_per_minute
        )

        self.log_level = (
            self.observability.log_level
        )

        # --------------------------------------------------------------
        # Final validation
        # --------------------------------------------------------------

        self._validate_configuration()

        logger.info(
            "Riot configuration validated: env=%s, "
            "quality=%s, simulation=%s, "
            "players/world=%d, providers=%d",
            self.environment.value,
            self.generation.quality_tier.value,
            self.engine.simulation_mode.value,
            self.multiplayer.max_players_per_world,
            len(self.api_providers),
        )

    # ========================================================================
    # PROVIDERS
    # ========================================================================

    def _load_api_providers(
        self,
    ) -> Dict[str, List[str]]:
        """
        Load API keys for all supported provider slots.

        Multiple keys can be supplied using numbered variables:

            OPENAI_API_KEY
            OPENAI_API_KEY_2
            OPENAI_API_KEY_3

        The same pattern is supported for other providers.
        """

        provider_specs = {
            "openai": (
                "OPENAI_API_KEY",
                _get_csv(
                    "OPENAI_MODELS",
                    [
                        "gpt-4o",
                        "gpt-4o-mini",
                    ],
                ),
            ),
            "gemini": (
                "GOOGLE_API_KEY",
                _get_csv(
                    "GOOGLE_MODELS",
                    [
                        "gemini-1.5-pro",
                        "gemini-1.5-flash",
                    ],
                ),
            ),
            "anthropic": (
                "ANTHROPIC_API_KEY",
                _get_csv(
                    "ANTHROPIC_MODELS",
                    [
                        "claude-sonnet",
                    ],
                ),
            ),
            "openrouter": (
                "OPENROUTER_API_KEY",
                _get_csv(
                    "OPENROUTER_MODELS",
                    [
                        "auto",
                    ],
                ),
            ),
            "xai": (
                "XAI_API_KEY",
                _get_csv(
                    "XAI_MODELS",
                    [
                        "grok",
                    ],
                ),
            ),
            "mistral": (
                "MISTRAL_API_KEY",
                _get_csv(
                    "MISTRAL_MODELS",
                    [
                        "mistral-large",
                    ],
                ),
            ),
            "deepseek": (
                "DEEPSEEK_API_KEY",
                _get_csv(
                    "DEEPSEEK_MODELS",
                    [
                        "deepseek-chat",
                    ],
                ),
            ),
        }

        providers: Dict[str, List[str]] = {}

        for provider_name, (
            base_key,
            _models,
        ) in provider_specs.items():

            keys: List[str] = []

            for index in range(1, 17):
                key_name = (
                    base_key
                    if index == 1
                    else f"{base_key}_{index}"
                )

                value = _get_env(
                    key_name
                )

                if value:
                    keys.append(value)

            if keys:
                providers[provider_name] = keys

                logger.info(
                    "Provider configured: %s (%d key(s))",
                    provider_name,
                    len(keys),
                )

        return providers

    # ========================================================================
    # VALIDATION
    # ========================================================================

    def _validate_configuration(self) -> None:
        """Perform cross-section configuration validation."""

        # ------------------------------------------------------------------
        # Administrator authentication
        # ------------------------------------------------------------------

        if self.security.require_authentication:
            if not self.master_pin:
                raise ValueError(
                    "Riot requires GOD_MASTER_PIN when authentication "
                    "is enabled."
                )

            if len(self.master_pin) < (
                self.security.min_secret_length
            ):
                raise ValueError(
                    "GOD_MASTER_PIN is too weak. "
                    f"Minimum length is "
                    f"{self.security.min_secret_length}."
                )

        # ------------------------------------------------------------------
        # Production safety
        # ------------------------------------------------------------------

        if self.is_production():
            if not self.master_pin:
                raise ValueError(
                    "Production requires GOD_MASTER_PIN."
                )

            if self.security.allow_debug_endpoints:
                raise ValueError(
                    "Debug endpoints cannot be enabled in production."
                )

            if self.security.allow_ephemeral_dev_secret:
                raise ValueError(
                    "Ephemeral development secrets cannot be enabled "
                    "in production."
                )

            if self.security.allow_self_evolution:
                if not self.security.require_human_approval_for_evolution:
                    raise ValueError(
                        "Self-evolution in production requires "
                        "human approval."
                    )

                if not self.security.enable_generated_code_sandbox:
                    raise ValueError(
                        "Self-evolution requires generated-code sandboxing."
                    )

        # ------------------------------------------------------------------
        # Engine validation
        # ------------------------------------------------------------------

        if (
            self.engine.max_active_entities_per_tick
            > self.engine.max_entities
        ):
            raise ValueError(
                "GOD_MAX_ACTIVE_ENTITIES_PER_TICK cannot exceed "
                "GOD_MAX_ENTITIES."
            )

        if (
            self.engine.tick_hz
            < self.multiplayer.tick_hz
            and self.multiplayer.enabled
        ):
            logger.warning(
                "Simulation tick rate (%d Hz) is lower than multiplayer "
                "tick rate (%d Hz).",
                self.engine.tick_hz,
                self.multiplayer.tick_hz,
            )

        # ------------------------------------------------------------------
        # Multiplayer validation
        # ------------------------------------------------------------------

        if (
            self.multiplayer.max_connections_per_process
            > self.network.max_connections
        ):
            raise ValueError(
                "Per-process multiplayer connections exceed configured "
                "network maximum."
            )

        if (
            self.multiplayer.snapshot_hz
            > self.multiplayer.tick_hz
        ):
            raise ValueError(
                "Snapshot Hz cannot exceed multiplayer tick Hz."
            )

        # ------------------------------------------------------------------
        # Generation validation
        # ------------------------------------------------------------------

        if (
            self.generation.max_parallel_agents
            > self.generation.max_agents
        ):
            raise ValueError(
                "Parallel AI agents cannot exceed total AI agents."
            )

        if (
            self.generation.minimum_quality_score < 0.0
            or self.generation.minimum_quality_score > 1.0
        ):
            raise ValueError(
                "Minimum quality score must be between 0 and 1."
            )

        # ------------------------------------------------------------------
        # Feature dependency checks
        # ------------------------------------------------------------------

        if (
            self.features.native_simulation
            and self.engine.simulation_mode
            == SimulationMode.LOCAL
        ):
            logger.warning(
                "Native simulation feature is enabled but simulation "
                "mode is LOCAL."
            )

        if (
            self.features.self_evolution
            and not self.security.allow_self_evolution
        ):
            raise ValueError(
                "Self-evolution feature is enabled but security policy "
                "does not allow self-evolution."
            )

        if (
            self.features.multiplayer
            and not self.multiplayer.enabled
        ):
            raise ValueError(
                "Multiplayer feature is enabled while multiplayer runtime "
                "is disabled."
            )

        if (
            self.features.distributed_builds
            and not self.build.parallel_builds
        ):
            raise ValueError(
                "Distributed builds require at least one build worker."
            )

        # ------------------------------------------------------------------
        # Build target validation
        # ------------------------------------------------------------------

        valid_targets = {
            item.value
            for item in BuildTarget
        }

        unknown_targets = set(
            self.build.enabled_targets
        ) - valid_targets

        if unknown_targets:
            raise ValueError(
                "Unknown build targets: "
                + ", ".join(
                    sorted(unknown_targets)
                )
            )

        # ------------------------------------------------------------------
        # Origin validation
        # ------------------------------------------------------------------

        if "*" in self.network.allowed_origins:
            if self.environment != EnvironmentMode.DEVELOPMENT:
                raise ValueError(
                    "Wildcard CORS is not permitted outside development."
                )

            logger.warning(
                "Wildcard CORS is enabled in development."
            )

        # ------------------------------------------------------------------
        # Provider validation
        # ------------------------------------------------------------------

        if self.generation.mode in {
            AIGenerationMode.ROUTED,
            AIGenerationMode.SWARM,
            AIGenerationMode.ADAPTIVE,
        } and not self.api_providers:

            logger.warning(
                "No AI providers are configured. Riot will start, "
                "but AI generation will be unavailable until a provider "
                "credential is configured."
            )

    # ========================================================================
    # DEFAULTS
    # ========================================================================

    def _default_origins(self) -> List[str]:
        """Return safe default origins by environment."""

        if self.environment in {
            EnvironmentMode.DEVELOPMENT,
            EnvironmentMode.TESTING,
        }:
            return [
                "http://localhost:3000",
                "http://localhost:5173",
                "http://127.0.0.1:3000",
                "http://127.0.0.1:5173",
            ]

        return []

    # ========================================================================
    # COMPATIBILITY API
    # ========================================================================

    def get_api_providers(
        self,
    ) -> Dict[str, List[str]]:
        """Return a copy of configured provider credentials."""

        return {
            provider: list(keys)
            for provider, keys
            in self.api_providers.items()
        }

    def has_provider(
        self,
        provider_name: str,
    ) -> bool:
        """Check whether a provider is configured and usable."""

        return bool(
            provider_name
            and provider_name.lower()
            in self.api_providers
        )

    def get_master_pin(self) -> str:
        """
        Return the administrator secret.

        There is intentionally no legacy '7777' fallback.
        """

        if self.master_pin:
            return self.master_pin

        raise ValueError(
            "Master PIN is not configured."
        )

    def is_production(self) -> bool:
        """Return True for production deployments."""

        return (
            self.environment
            == EnvironmentMode.PRODUCTION
        )

    def is_development(self) -> bool:
        """Return True for development deployments."""

        return (
            self.environment
            == EnvironmentMode.DEVELOPMENT
        )

    def is_testing(self) -> bool:
        """Return True for test deployments."""

        return (
            self.environment
            == EnvironmentMode.TESTING
        )

    def is_staging(self) -> bool:
        """Return True for staging deployments."""

        return (
            self.environment
            == EnvironmentMode.STAGING
        )

    # ========================================================================
    # FEATURE API
    # ========================================================================

    def is_feature_enabled(
        self,
        feature: FeatureFlag | str,
    ) -> bool:
        """
        Check a feature flag.

        Supports both:

            FeatureFlag.AI_SWARM
            "ai_swarm"
        """

        if isinstance(feature, FeatureFlag):
            feature_name = feature.value
        else:
            feature_name = str(feature).lower()

        return bool(
            getattr(
                self.features,
                feature_name,
                False,
            )
        )

    # ========================================================================
    # PROVIDER / MODEL HELPERS
    # ========================================================================

    def provider_names(self) -> Tuple[str, ...]:
        """Return configured provider names."""

        return tuple(
            sorted(
                self.api_providers.keys()
            )
        )

    def provider_count(self) -> int:
        """Return number of configured providers."""

        return len(
            self.api_providers
        )

    # ========================================================================
    # PATH MANAGEMENT
    # ========================================================================

    def ensure_runtime_directories(
        self,
    ) -> None:
        """
        Create local runtime directories.

        This operation is deliberately explicit rather than happening during
        module import.
        """

        directories: Iterable[Path] = (
            self.storage.local_data_directory,
            self.storage.cache_directory,
            self.storage.object_directory,
            self.build.build_directory,
            self.build.artifact_directory,
            self.build.workspace_directory,
        )

        for directory in directories:
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

    # ========================================================================
    # SAFE DIAGNOSTICS
    # ========================================================================

    def safe_summary(
        self,
    ) -> Mapping[str, object]:
        """
        Return a diagnostic snapshot with secrets removed.

        This object is safe for /healthz, telemetry, startup diagnostics,
        and debugging.
        """

        return {
            "environment": self.environment.value,
            "quality_tier": (
                self.generation.quality_tier.value
            ),
            "ai_mode": (
                self.generation.mode.value
            ),
            "simulation_mode": (
                self.engine.simulation_mode.value
            ),
            "engine_tick_hz": (
                self.engine.tick_hz
            ),
            "max_entities": (
                self.engine.max_entities
            ),
            "max_players_per_world": (
                self.multiplayer.max_players_per_world
            ),
            "provider_names": (
                self.provider_names()
            ),
            "provider_count": (
                self.provider_count()
            ),
            "build_targets": (
                self.build.enabled_targets
            ),
            "features": {
                name: self.is_feature_enabled(
                    name
                )
                for name in FeatureFlag
            },
            "security": {
                "authentication_required": (
                    self.security.require_authentication
                ),
                "rate_limiting": (
                    self.security.enable_rate_limiting
                ),
                "generated_code_sandbox": (
                    self.security.enable_generated_code_sandbox
                ),
                "path_sandbox": (
                    self.security.enable_path_sandbox
                ),
                "self_evolution": (
                    self.security.allow_self_evolution
                ),
            },
            "secret_fingerprint": (
                self.security.secret_fingerprint
            ),
        }

    # ========================================================================
    # CONFIGURATION FINGERPRINT
    # ========================================================================

    def configuration_fingerprint(
        self,
    ) -> str:
        """
        Return a deterministic fingerprint of non-secret runtime settings.

        Useful for telemetry and reproducing a particular runtime setup.
        """

        summary = repr(
            self.safe_summary()
        )

        return hashlib.sha256(
            summary.encode("utf-8")
        ).hexdigest()[:24]


# ============================================================================
# GLOBAL CONFIGURATION
# ============================================================================

god_config = GodNodeConfig()


__all__ = [
    "EnvironmentMode",
    "LogFormat",
    "AIGenerationMode",
    "QualityTier",
    "SimulationMode",
    "BuildTarget",
    "StorageBackend",
    "FeatureFlag",
    "ProviderConfig",
    "EngineSettings",
    "MultiplayerSettings",
    "GenerationSettings",
    "BuildSettings",
    "StorageSettings",
    "SecuritySettings",
    "NetworkSettings",
    "ObservabilitySettings",
    "FeatureSettings",
    "TaskSettings",
    "GodNodeConfig",
    "god_config",
]
