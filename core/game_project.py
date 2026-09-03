"""
core/game_project.py

RIOT / GOD NODE
CANONICAL GAME PROJECT CONTRACT
================================

This module defines the canonical, strongly-typed state model shared by
the complete AI game-generation pipeline.

Design goals
------------
1. One authoritative GameProject state.
2. Strong contracts between pipeline stages.
3. Explicit lifecycle/state transitions.
4. Deterministic procedural-generation support.
5. Asset/world/source/build provenance.
6. Dependency tracking between generated artifacts.
7. Runtime/build capability declarations.
8. Protection against fake success states.
9. Serialization-safe models for persistence and APIs.
10. Lightweight enough for constrained environments.

Canonical pipeline
------------------
USER PROMPT
    -> ROUTING
    -> PLANNING
    -> ASSET PLANNING
    -> ASSET GENERATION
    -> WORLD GENERATION
    -> SCENE GENERATION
    -> PHYSICS
    -> GAMEPLAY
    -> SOURCE GENERATION
    -> QA
    -> READY FOR BUILD
    -> BUILDING
    -> COMPLETED

This module intentionally DOES NOT implement rendering, physics, AI
providers, filesystem persistence, or compilation. It defines the contracts
through which those systems communicate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# HELPERS
# ============================================================================


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    """Generate a UUID4 identifier, optionally with a readable prefix."""
    value = uuid4().hex
    return f"{prefix}{value}" if prefix else value


def content_sha256(content: str) -> str:
    """Generate a deterministic SHA-256 checksum for text content."""
    return sha256(content.encode("utf-8")).hexdigest()


# ============================================================================
# BASE MODEL
# ============================================================================


class RiotModel(BaseModel):
    """
    Base configuration for canonical pipeline contracts.

    Extra fields are forbidden so schema drift does not silently propagate
    through the engine.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        use_enum_values=False,
    )


# ============================================================================
# LIFECYCLE / STATUS
# ============================================================================


class ProjectStatus(str, Enum):
    CREATED = "CREATED"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"

    ASSET_PLANNING = "ASSET_PLANNING"
    ASSET_GENERATION = "ASSET_GENERATION"

    WORLD_GENERATION = "WORLD_GENERATION"
    SCENE_GENERATION = "SCENE_GENERATION"

    PHYSICS_CONFIG = "PHYSICS_CONFIG"
    GAMEPLAY_GENERATION = "GAMEPLAY_GENERATION"
    SOURCE_GENERATION = "SOURCE_GENERATION"

    QA_TESTING = "QA_TESTING"
    READY_FOR_BUILD = "READY_FOR_BUILD"

    BUILDING = "BUILDING"
    COMPLETED = "COMPLETED"

    BUILD_FAILED = "BUILD_FAILED"
    FAILED = "FAILED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    UNSUPPORTED = "UNSUPPORTED"


class BuildStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    BUILDING = "BUILDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"


class CapabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class TargetPlatform(str, Enum):
    WEB_HTML5 = "web_html5"
    MOBILE_APK = "mobile_apk"
    PC_EXE = "pc_exe"
    CLOUD_STREAM = "cloud_stream"


class RuntimeType(str, Enum):
    WEB = "web"
    NATIVE_MOBILE = "native_mobile"
    DESKTOP = "desktop"
    CLOUD_STREAM = "cloud_stream"
    EXTERNAL = "external"


# ============================================================================
# COMMON MATH TYPES
# ============================================================================


class Vector2(RiotModel):
    x: float = 0.0
    y: float = 0.0


class Vector3(RiotModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class Transform(RiotModel):
    position: Vector3 = Field(default_factory=Vector3)
    rotation: Vector3 = Field(default_factory=Vector3)
    scale: Vector3 = Field(
        default_factory=lambda: Vector3(x=1.0, y=1.0, z=1.0)
    )


# ============================================================================
# PERFORMANCE / TARGET PROFILE
# ============================================================================


class PerformanceBudget(RiotModel):
    """
    Generation-time and runtime constraints.

    These values are targets, not guarantees.
    """

    target_fps: int = Field(default=60, ge=15, le=480)

    max_memory_mb: Optional[int] = Field(default=None, ge=64)

    max_active_npcs: Optional[int] = Field(default=None, ge=1)

    max_visible_entities: Optional[int] = Field(default=None, ge=1)

    max_draw_calls: Optional[int] = Field(default=None, ge=1)

    max_texture_memory_mb: Optional[int] = Field(default=None, ge=1)

    max_world_memory_mb: Optional[int] = Field(default=None, ge=1)

    max_generation_time_seconds: Optional[int] = Field(default=None, ge=1)

    quality_profile: str = "balanced"

    additional_constraints: Dict[str, Any] = Field(default_factory=dict)


class RuntimeTarget(RiotModel):
    """
    Explicit runtime boundary.

    The Python control plane is not itself assumed to be a renderer.
    """

    runtime_type: RuntimeType
    name: str
    version: Optional[str] = None

    capability_status: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED

    capabilities: Set[str] = Field(default_factory=set)

    configuration: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# ARCHITECTURE PLAN
# ============================================================================


class ArchitecturePlan(RiotModel):
    """High-level technical decisions produced by planning/director stages."""

    plan_id: str = Field(default_factory=lambda: new_id("plan_"))

    target_platform: TargetPlatform

    runtime_target: Optional[RuntimeTarget] = None

    game_genre: str = "unknown"

    visual_style: str = "unknown"

    complexity_class: str = "unknown"

    core_gameplay_loop: Optional[str] = None

    engine_config: Dict[str, Any] = Field(default_factory=dict)

    required_agents: List[str] = Field(default_factory=list)

    required_capabilities: Set[str] = Field(default_factory=set)

    build_steps: List[str] = Field(default_factory=list)

    technical_constraints: List[str] = Field(default_factory=list)

    performance_budget: PerformanceBudget = Field(
        default_factory=PerformanceBudget
    )


# ============================================================================
# ASSET PIPELINE
# ============================================================================


class AssetType(str, Enum):
    TERRAIN = "terrain"
    MODEL_2D = "model_2d"
    MODEL_3D = "model_3d"
    TEXTURE = "texture"
    MATERIAL = "material"
    SKYBOX = "skybox"

    ANIMATION = "animation"

    SOUND = "sound"
    MUSIC = "music"
    VOICE = "voice"

    UI = "ui"
    VFX = "vfx"
    PARTICLE = "particle"
    SHADER = "shader"

    COLLISION = "collision"
    NAVIGATION = "navigation"
    PROCEDURAL = "procedural"


class AssetGenerationStatus(str, Enum):
    REQUESTED = "REQUESTED"
    PLANNED = "PLANNED"
    GENERATING = "GENERATING"
    GENERATED = "GENERATED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class AssetRequest(RiotModel):
    """Input requirement for an asset."""

    request_id: str = Field(default_factory=lambda: new_id("assetreq_"))

    asset_type: AssetType

    name: str

    prompt: str

    priority: int = Field(default=50, ge=0, le=100)

    required_for_world: bool = True

    target_formats: List[str] = Field(default_factory=list)

    requirements: Dict[str, Any] = Field(default_factory=dict)

    metadata: Dict[str, Any] = Field(default_factory=dict)


class TextureProfile(RiotModel):
    """
    Texture requirements.

    This is a specification, not a claim that textures were actually created.
    """

    width: Optional[int] = Field(default=None, ge=1)
    height: Optional[int] = Field(default=None, ge=1)

    channels: Optional[int] = Field(default=None, ge=1, le=4)

    color_space: str = "sRGB"

    compression: Optional[str] = None

    mipmaps: bool = True

    maps_required: Set[str] = Field(default_factory=set)


class MaterialProfile(RiotModel):
    """PBR/material requirements and resulting references."""

    shader: Optional[str] = None

    texture_slots: Dict[str, str] = Field(default_factory=dict)

    properties: Dict[str, Any] = Field(default_factory=dict)

    texture_profile: Optional[TextureProfile] = None


class CollisionProfile(RiotModel):
    enabled: bool = False

    collision_type: Optional[str] = None

    source_asset_id: Optional[str] = None

    layers: Set[str] = Field(default_factory=set)


class LODProfile(RiotModel):
    level: int = Field(default=0, ge=0)

    source_path: Optional[str] = None

    max_distance: Optional[float] = Field(default=None, ge=0.0)

    polygon_ratio: Optional[float] = Field(
        default=None,
        gt=0.0,
        le=1.0,
    )


class AssetBlueprint(RiotModel):
    """
    Asset design specification.

    Blueprint -> GeneratedAsset is an explicit stage transition.
    """

    blueprint_id: str = Field(default_factory=lambda: new_id("blueprint_"))

    request_id: str

    asset_type: AssetType

    name: str

    generation_strategy: str = "unspecified"

    specification: Dict[str, Any] = Field(default_factory=dict)

    material_profile: Optional[MaterialProfile] = None

    collision_profile: Optional[CollisionProfile] = None

    lod_profiles: List[LODProfile] = Field(default_factory=list)

    expected_formats: List[str] = Field(default_factory=list)

    dependencies: List[str] = Field(default_factory=list)


class GeneratedAsset(RiotModel):
    """
    Record of an asset that actually exists as an artifact.

    GENERATED is never allowed without a source/artifact reference.
    """

    asset_id: str = Field(default_factory=lambda: new_id("asset_"))

    request_id: str

    blueprint_id: str

    asset_type: AssetType

    name: str

    status: AssetGenerationStatus = AssetGenerationStatus.REQUESTED

    source_path: Optional[str] = None

    artifact_reference: Optional[str] = None

    format: Optional[str] = None

    size_bytes: int = Field(default=0, ge=0)

    dimensions: Optional[Vector3] = None

    material_profile: Optional[MaterialProfile] = None

    collision_profile: Optional[CollisionProfile] = None

    lods: List[LODProfile] = Field(default_factory=list)

    source_provider: Optional[str] = None

    source_model: Optional[str] = None

    checksum: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_generated_state(self) -> "GeneratedAsset":
        if self.status == AssetGenerationStatus.GENERATED:
            if not self.source_path and not self.artifact_reference:
                raise ValueError(
                    "GENERATED asset must have source_path or "
                    "artifact_reference."
                )
        return self


class AssetManifest(RiotModel):
    """Authoritative asset registry for a project."""

    manifest_id: str = Field(default_factory=lambda: new_id("assets_"))

    assets: Dict[str, GeneratedAsset] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=utc_now)

    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def total_assets(self) -> int:
        """Always derive count from actual registry contents."""
        return len(self.assets)

    @property
    def generated_count(self) -> int:
        return sum(
            1
            for asset in self.assets.values()
            if asset.status == AssetGenerationStatus.GENERATED
        )

    def add_asset(self, asset: GeneratedAsset) -> None:
        self.assets[asset.asset_id] = asset
        self.updated_at = utc_now()

    def get(self, asset_id: str) -> Optional[GeneratedAsset]:
        return self.assets.get(asset_id)

    def require(self, asset_id: str) -> GeneratedAsset:
        asset = self.get(asset_id)
        if asset is None:
            raise KeyError(
                f"Asset '{asset_id}' does not exist in AssetManifest."
            )
        return asset

    def validate_references(self, asset_ids: List[str]) -> None:
        missing = [asset_id for asset_id in asset_ids if asset_id not in self.assets]

        if missing:
            raise ValueError(
                f"AssetManifest contains missing asset references: {missing}"
            )


# ============================================================================
# WORLD / SCENE
# ============================================================================


class AssetPlacement(RiotModel):
    placement_id: str = Field(default_factory=lambda: new_id("placement_"))

    asset_id: str

    transform: Transform = Field(default_factory=Transform)

    properties: Dict[str, Any] = Field(default_factory=dict)


class StreamingZone(RiotModel):
    zone_id: str = Field(default_factory=lambda: new_id("zone_"))

    name: str

    center: Vector3 = Field(default_factory=Vector3)

    dimensions: Vector3 = Field(default_factory=Vector3)

    load_distance: float = Field(default=100.0, ge=0.0)

    unload_distance: float = Field(default=150.0, ge=0.0)

    priority: int = Field(default=50, ge=0, le=100)

    @model_validator(mode="after")
    def validate_distances(self) -> "StreamingZone":
        if self.unload_distance < self.load_distance:
            raise ValueError(
                "unload_distance must be >= load_distance."
            )
        return self


class ChunkManifest(RiotModel):
    chunk_id: str = Field(default_factory=lambda: new_id("chunk_"))

    coordinate: Vector3 = Field(default_factory=Vector3)

    dimensions: Vector3 = Field(default_factory=Vector3)

    asset_placements: List[AssetPlacement] = Field(
        default_factory=list
    )

    streaming_zone_id: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorldManifest(RiotModel):
    """Authoritative world representation."""

    world_id: str = Field(default_factory=lambda: new_id("world_"))

    name: str = "Generated World"

    seed: int = Field(default=0, ge=0)

    dimensions: Vector3 = Field(default_factory=Vector3)

    biome: str = "default"

    spawn_point: Vector3 = Field(default_factory=Vector3)

    skybox_type: Optional[str] = None

    fog_density: float = Field(default=0.0, ge=0.0, le=1.0)

    chunks: Dict[str, ChunkManifest] = Field(default_factory=dict)

    streaming_zones: Dict[str, StreamingZone] = Field(
        default_factory=dict
    )

    used_asset_ids: Set[str] = Field(default_factory=set)

    metadata: Dict[str, Any] = Field(default_factory=dict)

    def validate_assets(self, manifest: AssetManifest) -> None:
        manifest.validate_references(list(self.used_asset_ids))

        for chunk in self.chunks.values():
            manifest.validate_references(
                [placement.asset_id for placement in chunk.asset_placements]
            )


class SceneObject(RiotModel):
    object_id: str = Field(default_factory=lambda: new_id("object_"))

    name: str = ""

    asset_id: Optional[str] = None

    parent_id: Optional[str] = None

    transform: Transform = Field(default_factory=Transform)

    components: Dict[str, Any] = Field(default_factory=dict)

    properties: Dict[str, Any] = Field(default_factory=dict)


class SceneGraph(RiotModel):
    """Hierarchical representation of a runtime scene."""

    scene_id: str = Field(default_factory=lambda: new_id("scene_"))

    objects: Dict[str, SceneObject] = Field(default_factory=dict)

    root_object_ids: List[str] = Field(default_factory=list)

    def add_object(self, obj: SceneObject) -> None:
        if obj.parent_id and obj.parent_id not in self.objects:
            raise ValueError(
                f"Parent object '{obj.parent_id}' does not exist."
            )

        self.objects[obj.object_id] = obj

        if obj.parent_id is None and obj.object_id not in self.root_object_ids:
            self.root_object_ids.append(obj.object_id)

    def validate_assets(self, manifest: AssetManifest) -> None:
        referenced = [
            obj.asset_id
            for obj in self.objects.values()
            if obj.asset_id
        ]

        manifest.validate_references(referenced)


# ============================================================================
# PHYSICS
# ============================================================================


class PhysicsConfig(RiotModel):
    gravity: Vector3 = Field(
        default_factory=lambda: Vector3(
            x=0.0,
            y=-9.81,
            z=0.0,
        )
    )

    friction_default: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
    )

    restitution_default: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
    )

    time_scale: float = Field(
        default=1.0,
        gt=0.0,
    )

    physics_engine: str = "builtin"

    fixed_timestep: float = Field(
        default=1.0 / 60.0,
        gt=0.0,
    )

    collision_layers: Dict[str, int] = Field(
        default_factory=dict
    )

    configuration: Dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================================
# GAMEPLAY MODULES
# ============================================================================


class GameplayModuleType(str, Enum):
    PLAYER = "player"
    CAMERA = "camera"
    COMBAT = "combat"
    AI = "ai"
    NPC = "npc"
    INVENTORY = "inventory"
    QUEST = "quest"
    ECONOMY = "economy"
    VEHICLE = "vehicle"
    WEAPON = "weapon"
    MULTIPLAYER = "multiplayer"
    SAVE_SYSTEM = "save_system"
    CUSTOM = "custom"


class GameplayModule(RiotModel):
    module_id: str = Field(default_factory=lambda: new_id("gameplay_"))

    name: str

    module_type: GameplayModuleType

    enabled: bool = True

    configuration: Dict[str, Any] = Field(default_factory=dict)

    source_files: List[str] = Field(default_factory=list)

    dependencies: List[str] = Field(default_factory=list)

    runtime_capabilities: Set[str] = Field(default_factory=set)


# ============================================================================
# AUDIO / UI
# ============================================================================


class AudioManifest(RiotModel):
    music_track: Optional[str] = None

    ambient_sound: Optional[str] = None

    sound_effects: Dict[str, str] = Field(default_factory=dict)

    voice_lines: Dict[str, str] = Field(default_factory=dict)


class UIManifest(RiotModel):
    hud_enabled: bool = True

    menu_style: str = "default"

    ui_assets: List[str] = Field(default_factory=list)

    screens: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# SOURCE GENERATION
# ============================================================================


class SourceFile(RiotModel):
    """Actual generated source artifact."""

    path: str

    content: str

    language: str = ""

    checksum: Optional[str] = None

    generated_at: datetime = Field(default_factory=utc_now)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()

        if not normalized:
            raise ValueError("Source file path cannot be empty.")

        if normalized.startswith("/"):
            raise ValueError("Source file path must be relative.")

        parts = normalized.split("/")

        if ".." in parts:
            raise ValueError(
                f"Source file path may not escape project root: {value}"
            )

        return normalized

    @model_validator(mode="after")
    def calculate_checksum(self) -> "SourceFile":
        if not self.checksum:
            self.checksum = content_sha256(self.content)
        return self


class SourceBundle(RiotModel):
    """Collection of actual generated project source files."""

    bundle_id: str = Field(default_factory=lambda: new_id("bundle_"))

    files: Dict[str, SourceFile] = Field(default_factory=dict)

    entry_point: Optional[str] = None

    build_system: Optional[str] = None

    dependencies: Dict[str, str] = Field(default_factory=dict)

    metadata: Dict[str, Any] = Field(default_factory=dict)

    def add_file(
        self,
        path: str,
        content: str,
        language: str = "",
    ) -> None:

        normalized = path.replace("\\", "/")

        self.files[normalized] = SourceFile(
            path=normalized,
            content=content,
            language=language,
        )

    def require_file(self, path: str) -> SourceFile:
        normalized = path.replace("\\", "/")

        file = self.files.get(normalized)

        if file is None:
            raise KeyError(
                f"Required source file '{normalized}' does not exist."
            )

        return file

    def validate_entry_point(self) -> None:
        if not self.entry_point:
            raise ValueError(
                "SourceBundle has no entry point."
            )

        self.require_file(self.entry_point)

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def is_empty(self) -> bool:
        return not bool(self.files)


# ============================================================================
# QA
# ============================================================================


class QAStatus(str, Enum):
    NOT_TESTED = "NOT_TESTED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class QAReport(RiotModel):
    """Validation evidence for the actual generated project."""

    report_id: str = Field(default_factory=lambda: new_id("qa_"))

    status: QAStatus = QAStatus.NOT_TESTED

    tests_run: int = Field(default=0, ge=0)

    tests_passed: int = Field(default=0, ge=0)

    tests_failed: int = Field(default=0, ge=0)

    issues: List[str] = Field(default_factory=list)

    warnings: List[str] = Field(default_factory=list)

    tested_files: List[str] = Field(default_factory=list)

    checks: Dict[str, StageStatus] = Field(default_factory=dict)

    timestamp: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_counts(self) -> "QAReport":

        if self.tests_passed + self.tests_failed > self.tests_run:
            raise ValueError(
                "tests_passed + tests_failed cannot exceed tests_run."
            )

        return self


# ============================================================================
# BUILD ARTIFACTS
# ============================================================================


class BuildArtifact(RiotModel):
    """
    Real build result.

    SUCCESS requires an actual artifact path/reference.
    """

    artifact_id: str = Field(default_factory=lambda: new_id("artifact_"))

    platform: TargetPlatform

    runtime_type: Optional[RuntimeType] = None

    status: BuildStatus = BuildStatus.NOT_STARTED

    file_path: Optional[str] = None

    artifact_reference: Optional[str] = None

    file_size_bytes: int = Field(default=0, ge=0)

    checksum: Optional[str] = None

    download_url: Optional[str] = None

    build_logs: str = ""

    error_message: Optional[str] = None

    toolchain: Optional[str] = None

    toolchain_version: Optional[str] = None

    created_at: datetime = Field(default_factory=utc_now)

    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_success(self) -> "BuildArtifact":

        if self.status == BuildStatus.SUCCESS:
            if not self.file_path and not self.artifact_reference:
                raise ValueError(
                    "SUCCESS build artifact must contain "
                    "file_path or artifact_reference."
                )

        return self

    def transition_to(self, status: BuildStatus) -> None:
        self.status = status
        self.updated_at = utc_now()


# ============================================================================
# PIPELINE STAGE RECORD
# ============================================================================


class PipelineStageRecord(RiotModel):
    stage: ProjectStatus

    status: StageStatus = StageStatus.PENDING

    started_at: Optional[datetime] = None

    completed_at: Optional[datetime] = None

    duration_ms: Optional[float] = Field(default=None, ge=0.0)

    attempts: int = Field(default=0, ge=0)

    error: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# PROJECT STATE MACHINE
# ============================================================================


_ALLOWED_TRANSITIONS: Dict[ProjectStatus, Set[ProjectStatus]] = {
    ProjectStatus.CREATED: {
        ProjectStatus.ROUTING,
        ProjectStatus.FAILED,
    },

    ProjectStatus.ROUTING: {
        ProjectStatus.PLANNING,
        ProjectStatus.FAILED,
    },

    ProjectStatus.PLANNING: {
        ProjectStatus.ASSET_PLANNING,
        ProjectStatus.FAILED,
    },

    ProjectStatus.ASSET_PLANNING: {
        ProjectStatus.ASSET_GENERATION,
        ProjectStatus.FAILED,
    },

    ProjectStatus.ASSET_GENERATION: {
        ProjectStatus.WORLD_GENERATION,
        ProjectStatus.FAILED,
    },

    ProjectStatus.WORLD_GENERATION: {
        ProjectStatus.SCENE_GENERATION,
        ProjectStatus.FAILED,
    },

    ProjectStatus.SCENE_GENERATION: {
        ProjectStatus.PHYSICS_CONFIG,
        ProjectStatus.FAILED,
    },

    ProjectStatus.PHYSICS_CONFIG: {
        ProjectStatus.GAMEPLAY_GENERATION,
        ProjectStatus.FAILED,
    },

    ProjectStatus.GAMEPLAY_GENERATION: {
        ProjectStatus.SOURCE_GENERATION,
        ProjectStatus.FAILED,
    },

    ProjectStatus.SOURCE_GENERATION: {
        ProjectStatus.QA_TESTING,
        ProjectStatus.FAILED,
    },

    ProjectStatus.QA_TESTING: {
        ProjectStatus.READY_FOR_BUILD,
        ProjectStatus.FAILED,
    },

    ProjectStatus.READY_FOR_BUILD: {
        ProjectStatus.BUILDING,
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
    },

    ProjectStatus.BUILDING: {
        ProjectStatus.COMPLETED,
        ProjectStatus.BUILD_FAILED,
        ProjectStatus.FAILED,
    },

    ProjectStatus.BUILD_FAILED: {
        ProjectStatus.BUILDING,
        ProjectStatus.FAILED,
    },

    ProjectStatus.FAILED: set(),

    ProjectStatus.COMPLETED: set(),
}


# ============================================================================
# CANONICAL GAME PROJECT
# ============================================================================


class GameProject(RiotModel):
    """
    Canonical source of truth for one generated game project.

    Every major subsystem should eventually read from or update this
    validated project state.
    """

    # ------------------------------------------------------------------------
    # IDENTITY
    # ------------------------------------------------------------------------

    project_id: str = Field(default_factory=lambda: new_id("project_"))

    name: str = "Untitled Project"

    description: str = ""

    user_prompt: str = ""

    # ------------------------------------------------------------------------
    # DETERMINISTIC GENERATION
    # ------------------------------------------------------------------------

    seed: int = Field(default=0, ge=0)

    # ------------------------------------------------------------------------
    # LIFECYCLE
    # ------------------------------------------------------------------------

    status: ProjectStatus = ProjectStatus.CREATED

    created_at: datetime = Field(default_factory=utc_now)

    updated_at: datetime = Field(default_factory=utc_now)

    # ------------------------------------------------------------------------
    # TARGET
    # ------------------------------------------------------------------------

    target_platform: TargetPlatform = TargetPlatform.WEB_HTML5

    runtime_type: Optional[RuntimeType] = None

    runtime_target: Optional[RuntimeTarget] = None

    performance_budget: PerformanceBudget = Field(
        default_factory=PerformanceBudget
    )

    # ------------------------------------------------------------------------
    # ARCHITECTURE
    # ------------------------------------------------------------------------

    architecture_plan: Optional[ArchitecturePlan] = None

    # ------------------------------------------------------------------------
    # ASSET PIPELINE
    # ------------------------------------------------------------------------

    asset_requests: Dict[str, AssetRequest] = Field(
        default_factory=dict
    )

    asset_blueprints: Dict[str, AssetBlueprint] = Field(
        default_factory=dict
    )

    asset_manifest: AssetManifest = Field(
        default_factory=AssetManifest
    )

    # ------------------------------------------------------------------------
    # WORLD / SCENE
    # ------------------------------------------------------------------------

    world_manifest: Optional[WorldManifest] = None

    scene_graph: Optional[SceneGraph] = None

    # ------------------------------------------------------------------------
    # GAMEPLAY
    # ------------------------------------------------------------------------

    gameplay_modules: Dict[str, GameplayModule] = Field(
        default_factory=dict
    )

    physics_config: Optional[PhysicsConfig] = None

    audio_manifest: AudioManifest = Field(
        default_factory=AudioManifest
    )

    ui_manifest: UIManifest = Field(
        default_factory=UIManifest
    )

    # ------------------------------------------------------------------------
    # SOURCE
    # ------------------------------------------------------------------------

    source_bundle: SourceBundle = Field(
        default_factory=SourceBundle
    )

    # ------------------------------------------------------------------------
    # QA
    # ------------------------------------------------------------------------

    qa_report: Optional[QAReport] = None

    # ------------------------------------------------------------------------
    # BUILD
    # ------------------------------------------------------------------------

    build_artifacts: Dict[str, BuildArtifact] = Field(
        default_factory=dict
    )

    # ------------------------------------------------------------------------
    # PIPELINE HISTORY
    # ------------------------------------------------------------------------

    pipeline_stages: Dict[str, PipelineStageRecord] = Field(
        default_factory=dict
    )

    # ------------------------------------------------------------------------
    # DIAGNOSTICS
    # ------------------------------------------------------------------------

    errors: List[str] = Field(default_factory=list)

    warnings: List[str] = Field(default_factory=list)

    metadata: Dict[str, Any] = Field(default_factory=dict)

    # ========================================================================
    # PIPELINE STATE
    # ========================================================================

    def transition_to(
        self,
        new_status: ProjectStatus,
        *,
        force: bool = False,
    ) -> None:

        if self.status == new_status:
            return

        if not force:

            allowed = _ALLOWED_TRANSITIONS.get(
                self.status,
                set(),
            )

            if new_status not in allowed:

                raise ValueError(
                    f"Invalid project transition: "
                    f"{self.status.value} -> {new_status.value}"
                )

        self.status = new_status
        self.updated_at = utc_now()

    def start_stage(
        self,
        stage: ProjectStatus,
    ) -> PipelineStageRecord:

        now = utc_now()

        record = self.pipeline_stages.get(stage.value)

        if record is None:

            record = PipelineStageRecord(
                stage=stage,
                status=StageStatus.RUNNING,
                started_at=now,
                attempts=1,
            )

            self.pipeline_stages[stage.value] = record

        else:

            record.status = StageStatus.RUNNING
            record.started_at = now
            record.completed_at = None
            record.error = None
            record.attempts += 1

        self.updated_at = now

        return record

    def complete_stage(
        self,
        stage: ProjectStatus,
        *,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:

        record = self.pipeline_stages.get(stage.value)

        if record is None:

            raise ValueError(
                f"Stage '{stage.value}' was never started."
            )

        now = utc_now()

        record.completed_at = now

        if record.started_at:

            record.duration_ms = (
                now - record.started_at
            ).total_seconds() * 1000.0

        record.status = (
            StageStatus.PASSED
            if success
            else StageStatus.FAILED
        )

        record.error = error

        self.updated_at = now

    # ========================================================================
    # ERRORS / WARNINGS
    # ========================================================================

    def add_error(
        self,
        error: str,
    ) -> None:

        if error:
            self.errors.append(str(error))

        self.updated_at = utc_now()

    def add_warning(
        self,
        warning: str,
    ) -> None:

        if warning:
            self.warnings.append(str(warning))

        self.updated_at = utc_now()

    def fail(
        self,
        error: str,
    ) -> None:

        self.add_error(error)

        if self.status not in {
            ProjectStatus.COMPLETED,
            ProjectStatus.FAILED,
        }:

            self.status = ProjectStatus.FAILED

        self.updated_at = utc_now()

    # ========================================================================
    # ASSET MANAGEMENT
    # ========================================================================

    def add_asset_request(
        self,
        request: AssetRequest,
    ) -> None:

        self.asset_requests[
            request.request_id
        ] = request

        self.updated_at = utc_now()

    def add_asset_blueprint(
        self,
        blueprint: AssetBlueprint,
    ) -> None:

        if blueprint.request_id not in self.asset_requests:

            raise ValueError(
                "AssetBlueprint references an unknown AssetRequest."
            )

        self.asset_blueprints[
            blueprint.blueprint_id
        ] = blueprint

        self.updated_at = utc_now()

    def add_generated_asset(
        self,
        asset: GeneratedAsset,
    ) -> None:

        if asset.request_id not in self.asset_requests:

            raise ValueError(
                "GeneratedAsset references an unknown AssetRequest."
            )

        if asset.blueprint_id not in self.asset_blueprints:

            raise ValueError(
                "GeneratedAsset references an unknown AssetBlueprint."
            )

        self.asset_manifest.add_asset(asset)

        self.updated_at = utc_now()

    # ========================================================================
    # GAMEPLAY
    # ========================================================================

    def add_gameplay_module(
        self,
        module: GameplayModule,
    ) -> None:

        self.gameplay_modules[
            module.module_id
        ] = module

        self.updated_at = utc_now()

    # ========================================================================
    # WORLD VALIDATION
    # ========================================================================

    def validate_world_dependencies(self) -> None:

        if self.world_manifest:

            self.world_manifest.validate_assets(
                self.asset_manifest
            )

        if self.scene_graph:

            self.scene_graph.validate_assets(
                self.asset_manifest
            )

    # ========================================================================
    # SOURCE VALIDATION
    # ========================================================================

    def validate_source_bundle(self) -> None:

        if self.source_bundle.is_empty:

            raise ValueError(
                "GameProject contains no generated source files."
            )

        self.source_bundle.validate_entry_point()

    # ========================================================================
    # BUILD
    # ========================================================================

    def add_build_artifact(
        self,
        artifact: BuildArtifact,
    ) -> None:

        self.build_artifacts[
            artifact.platform.value
        ] = artifact

        self.updated_at = utc_now()

    # ========================================================================
    # FULL INTEGRITY VALIDATION
    # ========================================================================

    def validate_pipeline_integrity(self) -> None:

        # Architecture must target the same platform.
        if self.architecture_plan:

            if (
                self.architecture_plan.target_platform
                != self.target_platform
            ):

                raise ValueError(
                    "ArchitecturePlan target platform does not "
                    "match GameProject target platform."
                )

        # World -> Asset dependency.
        self.validate_world_dependencies()

        # Source bundle.
        if not self.source_bundle.is_empty:

            self.validate_source_bundle()

        # QA cannot report PASS with no generated source.
        if self.qa_report:

            if (
                self.qa_report.status == QAStatus.PASSED
                and self.source_bundle.is_empty
            ):

                raise ValueError(
                    "QA cannot PASS when SourceBundle is empty."
                )

        # Successful builds require real artifacts.
        for artifact in self.build_artifacts.values():

            if artifact.status == BuildStatus.SUCCESS:

                if (
                    not artifact.file_path
                    and not artifact.artifact_reference
                ):

                    raise ValueError(
                        "Successful build has no real artifact reference."
                    )

        # Every recorded stage must be internally consistent.
        for record in self.pipeline_stages.values():

            if record.status == StageStatus.RUNNING:
                if record.completed_at is not None:
                    raise ValueError(
                        f"Running stage '{record.stage.value}' "
                        "cannot have completed_at."
                    )

            if record.status in {
                StageStatus.PASSED,
                StageStatus.FAILED,
            }:

                if record.completed_at is None:
                    raise ValueError(
                        f"Finished stage '{record.stage.value}' "
                        "must have completed_at."
                    )

    # ========================================================================
    # SUMMARY
    # ========================================================================

    def summary(self) -> Dict[str, Any]:

        return {

            "project_id": self.project_id,

            "name": self.name,

            "status": self.status.value,

            "target_platform": (
                self.target_platform.value
            ),

            "runtime_type": (
                self.runtime_type.value
                if self.runtime_type
                else None
            ),

            "seed": self.seed,

            "assets_requested": len(
                self.asset_requests
            ),

            "asset_blueprints": len(
                self.asset_blueprints
            ),

            "assets_generated": (
                self.asset_manifest.generated_count
            ),

            "world_ready": (
                self.world_manifest is not None
            ),

            "scene_ready": (
                self.scene_graph is not None
            ),

            "gameplay_modules": len(
                self.gameplay_modules
            ),

            "source_files": (
                self.source_bundle.total_files
            ),

            "qa_status": (
                self.qa_report.status.value
                if self.qa_report
                else QAStatus.NOT_TESTED.value
            ),

            "build_artifacts": len(
                self.build_artifacts
            ),

            "errors": len(self.errors),

            "warnings": len(self.warnings),

            "created_at": (
                self.created_at.isoformat()
            ),

            "updated_at": (
                self.updated_at.isoformat()
            ),
        }

    # Backward-compatible alias for older callers.
    def dict_summary(self) -> Dict[str, Any]:
        return self.summary()
