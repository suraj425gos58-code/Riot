"""
RIOT / GOD NODE — Canonical Game Project Contract
==================================================

Single authoritative project state shared by orchestration, voice/assets,
world/scene generation, QA and the UniversalBuilder.

This version preserves the existing public model names/methods while closing
the orchestration/build contract gap:

* canonical platform aliases are normalized before enum validation;
* SourceBundle can carry verified binary artifacts as well as text source;
* the canonical project can emit the exact source-first builder contract;
* orchestrator output can be recorded without fabricating typed assets;
* voice/source integration is represented in structured metadata;
* build/QA integrity gates remain fail-closed;
* successful builds still require an actual artifact reference.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# HELPERS
# ============================================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    value = uuid4().hex
    return f"{prefix}{value}" if prefix else value


def content_sha256(content: str | bytes) -> str:
    if isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        payload = bytes(content)
    return sha256(payload).hexdigest()


def _safe_relative_path(value: str) -> str:
    raw = str(value).replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe project path: {value!r}")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError(f"invalid project path: {value!r}")
    return path.as_posix()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    if isinstance(value, Enum):
        return value.value
    return str(value)


def _canonical_target(value: Any) -> "TargetPlatform":
    if isinstance(value, TargetPlatform):
        return value
    raw = str(value or "").strip().lower()
    aliases = {
        "web": "web_html5",
        "html5": "web_html5",
        "web_html5": "web_html5",
        "mobile": "mobile_apk",
        "android": "mobile_apk",
        "apk": "mobile_apk",
        "mobile_apk": "mobile_apk",
        "pc": "pc_exe",
        "desktop": "pc_exe",
        "windows": "pc_exe",
        "exe": "pc_exe",
        "pc_exe": "pc_exe",
        "cloud": "cloud_stream",
        "stream": "cloud_stream",
        "cloud_stream": "cloud_stream",
    }
    try:
        return TargetPlatform(aliases.get(raw, raw))
    except ValueError as exc:
        raise ValueError(f"unsupported target_platform: {value!r}") from exc


# ============================================================================
# BASE MODEL
# ============================================================================

class RiotModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        use_enum_values=False,
    )


# ============================================================================
# LIFECYCLE
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
# MATH / TARGET
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
    scale: Vector3 = Field(default_factory=lambda: Vector3(x=1.0, y=1.0, z=1.0))


class PerformanceBudget(RiotModel):
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
    runtime_type: RuntimeType
    name: str
    version: Optional[str] = None
    capability_status: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED
    capabilities: Set[str] = Field(default_factory=set)
    configuration: Dict[str, Any] = Field(default_factory=dict)


class ArchitecturePlan(RiotModel):
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
    performance_budget: PerformanceBudget = Field(default_factory=PerformanceBudget)


# ============================================================================
# ASSETS
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
    width: Optional[int] = Field(default=None, ge=1)
    height: Optional[int] = Field(default=None, ge=1)
    channels: Optional[int] = Field(default=None, ge=1, le=4)
    color_space: str = "sRGB"
    compression: Optional[str] = None
    mipmaps: bool = True
    maps_required: Set[str] = Field(default_factory=set)


class MaterialProfile(RiotModel):
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
    polygon_ratio: Optional[float] = Field(default=None, gt=0.0, le=1.0)


class AssetBlueprint(RiotModel):
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
                raise ValueError("GENERATED asset must have source_path or artifact_reference.")
        return self


class AssetManifest(RiotModel):
    manifest_id: str = Field(default_factory=lambda: new_id("assets_"))
    assets: Dict[str, GeneratedAsset] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def total_assets(self) -> int:
        return len(self.assets)

    @property
    def generated_count(self) -> int:
        return sum(
            1 for asset in self.assets.values()
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
            raise KeyError(f"Asset '{asset_id}' does not exist in AssetManifest.")
        return asset

    def validate_references(self, asset_ids: Iterable[str]) -> None:
        missing = [asset_id for asset_id in asset_ids if asset_id not in self.assets]
        if missing:
            raise ValueError(f"AssetManifest contains missing asset references: {missing}")


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
            raise ValueError("unload_distance must be >= load_distance.")
        return self


class ChunkManifest(RiotModel):
    chunk_id: str = Field(default_factory=lambda: new_id("chunk_"))
    coordinate: Vector3 = Field(default_factory=Vector3)
    dimensions: Vector3 = Field(default_factory=Vector3)
    asset_placements: List[AssetPlacement] = Field(default_factory=list)
    streaming_zone_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WorldManifest(RiotModel):
    world_id: str = Field(default_factory=lambda: new_id("world_"))
    name: str = "Generated World"
    seed: int = Field(default=0, ge=0)
    dimensions: Vector3 = Field(default_factory=Vector3)
    biome: str = "default"
    spawn_point: Vector3 = Field(default_factory=Vector3)
    skybox_type: Optional[str] = None
    fog_density: float = Field(default=0.0, ge=0.0, le=1.0)
    chunks: Dict[str, ChunkManifest] = Field(default_factory=dict)
    streaming_zones: Dict[str, StreamingZone] = Field(default_factory=dict)
    used_asset_ids: Set[str] = Field(default_factory=set)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def validate_assets(self, manifest: AssetManifest) -> None:
        manifest.validate_references(self.used_asset_ids)
        for chunk in self.chunks.values():
            manifest.validate_references(
                placement.asset_id for placement in chunk.asset_placements
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
    scene_id: str = Field(default_factory=lambda: new_id("scene_"))
    objects: Dict[str, SceneObject] = Field(default_factory=dict)
    root_object_ids: List[str] = Field(default_factory=list)

    def add_object(self, obj: SceneObject) -> None:
        if obj.parent_id and obj.parent_id not in self.objects:
            raise ValueError(f"Parent object '{obj.parent_id}' does not exist.")
        self.objects[obj.object_id] = obj
        if obj.parent_id is None and obj.object_id not in self.root_object_ids:
            self.root_object_ids.append(obj.object_id)

    def validate_assets(self, manifest: AssetManifest) -> None:
        manifest.validate_references(
            obj.asset_id for obj in self.objects.values() if obj.asset_id
        )


# ============================================================================
# PHYSICS / GAMEPLAY
# ============================================================================

class PhysicsConfig(RiotModel):
    gravity: Vector3 = Field(
        default_factory=lambda: Vector3(x=0.0, y=-9.81, z=0.0)
    )
    friction_default: float = Field(default=0.5, ge=0.0, le=1.0)
    restitution_default: float = Field(default=0.2, ge=0.0, le=1.0)
    time_scale: float = Field(default=1.0, gt=0.0)
    physics_engine: str = "builtin"
    fixed_timestep: float = Field(default=1.0 / 60.0, gt=0.0)
    collision_layers: Dict[str, int] = Field(default_factory=dict)
    configuration: Dict[str, Any] = Field(default_factory=dict)


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
    metadata: Dict[str, Any] = Field(default_factory=dict)


class UIManifest(RiotModel):
    hud_enabled: bool = True
    menu_style: str = "default"
    ui_assets: List[str] = Field(default_factory=list)
    screens: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# SOURCE BUNDLE
# ============================================================================

class SourceFile(RiotModel):
    path: str
    content: str
    language: str = ""
    checksum: Optional[str] = None
    generated_at: datetime = Field(default_factory=utc_now)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @model_validator(mode="after")
    def calculate_checksum(self) -> "SourceFile":
        if not self.checksum:
            self.checksum = content_sha256(self.content)
        return self


class BinarySourceFile(RiotModel):
    """Verified binary payload that belongs to the source/build bundle."""

    path: str
    content: bytes
    media_type: Optional[str] = None
    checksum: Optional[str] = None
    generated_at: datetime = Field(default_factory=utc_now)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @model_validator(mode="after")
    def validate_binary(self) -> "BinarySourceFile":
        if not self.content:
            raise ValueError("BinarySourceFile content cannot be empty.")
        calculated = content_sha256(self.content)
        if self.checksum and self.checksum != calculated:
            raise ValueError("BinarySourceFile checksum mismatch.")
        self.checksum = calculated
        return self


class SourceBundle(RiotModel):
    bundle_id: str = Field(default_factory=lambda: new_id("bundle_"))
    files: Dict[str, SourceFile] = Field(default_factory=dict)
    binary_files: Dict[str, BinarySourceFile] = Field(default_factory=dict)
    entry_point: Optional[str] = None
    build_system: Optional[str] = None
    dependencies: Dict[str, str] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def add_file(self, path: str, content: str, language: str = "") -> None:
        normalized = _safe_relative_path(path)
        self.files[normalized] = SourceFile(
            path=normalized,
            content=content,
            language=language,
        )

    def add_binary_file(
        self,
        path: str,
        content: bytes,
        *,
        media_type: Optional[str] = None,
        checksum: Optional[str] = None,
    ) -> None:
        normalized = _safe_relative_path(path)
        self.binary_files[normalized] = BinarySourceFile(
            path=normalized,
            content=content,
            media_type=media_type,
            checksum=checksum,
        )

    def require_file(self, path: str) -> SourceFile:
        normalized = _safe_relative_path(path)
        file = self.files.get(normalized)
        if file is None:
            raise KeyError(f"Required source file '{normalized}' does not exist.")
        return file

    def require_binary_file(self, path: str) -> BinarySourceFile:
        normalized = _safe_relative_path(path)
        file = self.binary_files.get(normalized)
        if file is None:
            raise KeyError(f"Required binary file '{normalized}' does not exist.")
        return file

    def validate_entry_point(self) -> None:
        if not self.entry_point:
            raise ValueError("SourceBundle has no entry point.")
        self.require_file(self.entry_point)

    @property
    def total_files(self) -> int:
        return len(self.files) + len(self.binary_files)

    @property
    def text_file_count(self) -> int:
        return len(self.files)

    @property
    def binary_file_count(self) -> int:
        return len(self.binary_files)

    @property
    def is_empty(self) -> bool:
        return self.total_files == 0

    def as_builder_files(self) -> Dict[str, Any]:
        """Return the exact source-first mapping accepted by UniversalBuilder."""
        result: Dict[str, Any] = {}
        for path, item in self.files.items():
            result[_safe_relative_path(path)] = item.content
        for path, item in self.binary_files.items():
            result[_safe_relative_path(path)] = bytes(item.content)
        return result

    def source_manifest(self) -> Dict[str, Any]:
        return {
            "schema": "riot.source_bundle.v3",
            "bundle_id": self.bundle_id,
            "entry_point": self.entry_point,
            "build_system": self.build_system,
            "text_file_count": len(self.files),
            "binary_file_count": len(self.binary_files),
            "file_count": self.total_files,
            "files": sorted(self.files.keys()),
            "binary_files": sorted(self.binary_files.keys()),
            "text_hash": sha256(
                "\n".join(
                    f"{path}:{self.files[path].checksum or content_sha256(self.files[path].content)}"
                    for path in sorted(self.files)
                ).encode("utf-8")
            ).hexdigest(),
            "binary_hash": sha256(
                "\n".join(
                    f"{path}:{self.binary_files[path].checksum or content_sha256(self.binary_files[path].content)}"
                    for path in sorted(self.binary_files)
                ).encode("utf-8")
            ).hexdigest(),
        }


# ============================================================================
# QA / BUILD
# ============================================================================

class QAStatus(str, Enum):
    NOT_TESTED = "NOT_TESTED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class QAReport(RiotModel):
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
            raise ValueError("tests_passed + tests_failed cannot exceed tests_run.")
        return self


class BuildArtifact(RiotModel):
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
                    "SUCCESS build artifact must contain file_path or artifact_reference."
                )
        return self

    def transition_to(self, status: BuildStatus) -> None:
        self.status = status
        self.updated_at = utc_now()


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
# STATE MACHINE
# ============================================================================

_ALLOWED_TRANSITIONS: Dict[ProjectStatus, Set[ProjectStatus]] = {
    ProjectStatus.CREATED: {ProjectStatus.ROUTING, ProjectStatus.FAILED},
    ProjectStatus.ROUTING: {ProjectStatus.PLANNING, ProjectStatus.FAILED},
    ProjectStatus.PLANNING: {ProjectStatus.ASSET_PLANNING, ProjectStatus.FAILED},
    ProjectStatus.ASSET_PLANNING: {ProjectStatus.ASSET_GENERATION, ProjectStatus.FAILED},
    ProjectStatus.ASSET_GENERATION: {ProjectStatus.WORLD_GENERATION, ProjectStatus.FAILED},
    ProjectStatus.WORLD_GENERATION: {ProjectStatus.SCENE_GENERATION, ProjectStatus.FAILED},
    ProjectStatus.SCENE_GENERATION: {ProjectStatus.PHYSICS_CONFIG, ProjectStatus.FAILED},
    ProjectStatus.PHYSICS_CONFIG: {ProjectStatus.GAMEPLAY_GENERATION, ProjectStatus.FAILED},
    ProjectStatus.GAMEPLAY_GENERATION: {ProjectStatus.SOURCE_GENERATION, ProjectStatus.FAILED},
    ProjectStatus.SOURCE_GENERATION: {ProjectStatus.QA_TESTING, ProjectStatus.FAILED},
    ProjectStatus.QA_TESTING: {ProjectStatus.READY_FOR_BUILD, ProjectStatus.FAILED},
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
    ProjectStatus.BUILD_FAILED: {ProjectStatus.BUILDING, ProjectStatus.FAILED},
    ProjectStatus.FAILED: set(),
    ProjectStatus.COMPLETED: set(),
}


# ============================================================================
# CANONICAL GAME PROJECT
# ============================================================================

class GameProject(RiotModel):
    project_id: str = Field(default_factory=lambda: new_id("project_"))
    build_id: Optional[str] = None
    name: str = "Untitled Project"
    description: str = ""
    user_prompt: str = ""
    seed: int = Field(default=0, ge=0)

    status: ProjectStatus = ProjectStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    target_platform: TargetPlatform = TargetPlatform.WEB_HTML5
    runtime_type: Optional[RuntimeType] = None
    runtime_target: Optional[RuntimeTarget] = None
    performance_budget: PerformanceBudget = Field(default_factory=PerformanceBudget)
    architecture_plan: Optional[ArchitecturePlan] = None

    asset_requests: Dict[str, AssetRequest] = Field(default_factory=dict)
    asset_blueprints: Dict[str, AssetBlueprint] = Field(default_factory=dict)
    asset_manifest: AssetManifest = Field(default_factory=AssetManifest)

    world_manifest: Optional[WorldManifest] = None
    scene_graph: Optional[SceneGraph] = None

    gameplay_modules: Dict[str, GameplayModule] = Field(default_factory=dict)
    physics_config: Optional[PhysicsConfig] = None
    audio_manifest: AudioManifest = Field(default_factory=AudioManifest)
    ui_manifest: UIManifest = Field(default_factory=UIManifest)

    source_bundle: SourceBundle = Field(default_factory=SourceBundle)
    qa_report: Optional[QAReport] = None
    build_artifacts: Dict[str, BuildArtifact] = Field(default_factory=dict)
    pipeline_stages: Dict[str, PipelineStageRecord] = Field(default_factory=dict)

    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_platform", mode="before")
    @classmethod
    def normalize_target_platform(cls, value: Any) -> TargetPlatform:
        return _canonical_target(value)

    # ------------------------------------------------------------------------
    # STATE
    # ------------------------------------------------------------------------

    def transition_to(self, new_status: ProjectStatus, *, force: bool = False) -> None:
        if self.status == new_status:
            return
        if not force and new_status not in _ALLOWED_TRANSITIONS.get(self.status, set()):
            raise ValueError(
                f"Invalid project transition: {self.status.value} -> {new_status.value}"
            )
        self.status = new_status
        self.updated_at = utc_now()

    def start_stage(self, stage: ProjectStatus) -> PipelineStageRecord:
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
            raise ValueError(f"Stage '{stage.value}' was never started.")
        now = utc_now()
        record.completed_at = now
        if record.started_at:
            record.duration_ms = (now - record.started_at).total_seconds() * 1000.0
        record.status = StageStatus.PASSED if success else StageStatus.FAILED
        record.error = error
        self.updated_at = now

    # ------------------------------------------------------------------------
    # DIAGNOSTICS
    # ------------------------------------------------------------------------

    def add_error(self, error: str) -> None:
        if error:
            self.errors.append(str(error))
        self.updated_at = utc_now()

    def add_warning(self, warning: str) -> None:
        if warning:
            self.warnings.append(str(warning))
        self.updated_at = utc_now()

    def fail(self, error: str) -> None:
        self.add_error(error)
        if self.status not in {ProjectStatus.COMPLETED, ProjectStatus.FAILED}:
            self.status = ProjectStatus.FAILED
        self.updated_at = utc_now()

    # ------------------------------------------------------------------------
    # ASSETS / GAMEPLAY
    # ------------------------------------------------------------------------

    def add_asset_request(self, request: AssetRequest) -> None:
        self.asset_requests[request.request_id] = request
        self.updated_at = utc_now()

    def add_asset_blueprint(self, blueprint: AssetBlueprint) -> None:
        if blueprint.request_id not in self.asset_requests:
            raise ValueError("AssetBlueprint references an unknown AssetRequest.")
        self.asset_blueprints[blueprint.blueprint_id] = blueprint
        self.updated_at = utc_now()

    def add_generated_asset(self, asset: GeneratedAsset) -> None:
        if asset.request_id not in self.asset_requests:
            raise ValueError("GeneratedAsset references an unknown AssetRequest.")
        if asset.blueprint_id not in self.asset_blueprints:
            raise ValueError("GeneratedAsset references an unknown AssetBlueprint.")
        self.asset_manifest.add_asset(asset)
        self.updated_at = utc_now()

    def add_gameplay_module(self, module: GameplayModule) -> None:
        self.gameplay_modules[module.module_id] = module
        self.updated_at = utc_now()

    # ------------------------------------------------------------------------
    # WORLD / SOURCE
    # ------------------------------------------------------------------------

    def validate_world_dependencies(self) -> None:
        if self.world_manifest:
            self.world_manifest.validate_assets(self.asset_manifest)
        if self.scene_graph:
            self.scene_graph.validate_assets(self.asset_manifest)

    def validate_source_bundle(self) -> None:
        if self.source_bundle.is_empty:
            raise ValueError("GameProject contains no generated source files.")
        if self.source_bundle.entry_point:
            self.source_bundle.validate_entry_point()

    # ------------------------------------------------------------------------
    # ORCHESTRATOR INTEGRATION
    # ------------------------------------------------------------------------

    def record_orchestrator_output(self, output: Mapping[str, Any]) -> None:
        """Record orchestration evidence while keeping typed fields authoritative."""
        if not isinstance(output, Mapping):
            raise ValueError("orchestrator output must be a mapping")

        game_id = output.get("game_id") or output.get("project_id")
        build_id = output.get("build_id")
        if game_id:
            self.project_id = str(game_id)
        if build_id:
            self.build_id = str(build_id)

        target = output.get("target_platform")
        if target is None and isinstance(output.get("build_config"), Mapping):
            target = output["build_config"].get("target_platform")
        if target is not None:
            self.target_platform = _canonical_target(target)

        project_payload = output.get("project")
        if isinstance(project_payload, Mapping):
            name = project_payload.get("name")
            description = project_payload.get("description")
            if isinstance(name, str) and name.strip():
                self.name = name.strip()
            if isinstance(description, str):
                self.description = description

        plan = output.get("plan") or output.get("architecture_plan")
        if isinstance(plan, Mapping):
            architecture = dict(plan)
            architecture["target_platform"] = _canonical_target(
                architecture.get("target_platform", self.target_platform)
            )
            try:
                self.architecture_plan = ArchitecturePlan.model_validate(architecture)
            except Exception as exc:
                self.add_warning(f"architecture plan mapping failed: {type(exc).__name__}: {exc}")

        qa = output.get("qa")
        if isinstance(qa, Mapping):
            try:
                self.qa_report = QAReport.model_validate(qa)
            except Exception as exc:
                self.add_warning(f"QA mapping failed: {type(exc).__name__}: {exc}")

        build_config = output.get("build_config")
        if isinstance(build_config, Mapping):
            source = build_config.get("source_bundle")
            if isinstance(source, Mapping):
                files = source.get("files")
                if isinstance(files, Mapping):
                    entry_point = (
                        source.get("manifest", {}).get("entry_point")
                        if isinstance(source.get("manifest"), Mapping)
                        else None
                    )
                    for path, content in files.items():
                        if isinstance(content, bytes):
                            self.source_bundle.add_binary_file(str(path), content)
                        elif isinstance(content, str) and content:
                            self.source_bundle.add_file(
                                str(path),
                                content,
                                PurePosixPath(str(path)).suffix.lstrip("."),
                            )
                    if entry_point:
                        self.source_bundle.entry_point = _safe_relative_path(str(entry_point))

        self.metadata.setdefault("integration", {})
        self.metadata["integration"]["orchestrator"] = {
            "schema": "riot.orchestrator.integration.v1",
            "game_id": game_id,
            "build_id": build_id,
            "status": _json_safe(output.get("status")),
            "target_platform": self.target_platform.value,
            "raw_hash": sha256(
                repr(_json_safe(output)).encode("utf-8")
            ).hexdigest(),
        }
        self.updated_at = utc_now()

    # Backward-compatible alias used by the previous integration layer.
    def register_orchestrator_output(self, output: Mapping[str, Any]) -> Dict[str, Any]:
        self.record_orchestrator_output(output)
        return dict(self.metadata.get("integration", {}).get("orchestrator", {}))

    def register_source_files(
        self,
        files: Mapping[str, str],
        *,
        entry_point: str = "index.html",
    ) -> Dict[str, Any]:
        if not isinstance(files, Mapping) or not files:
            raise ValueError("source files must be a non-empty mapping")
        for path, content in files.items():
            if not isinstance(content, str) or not content:
                raise ValueError(f"source file is empty or non-text: {path}")
            self.source_bundle.add_file(
                str(path),
                content,
                PurePosixPath(str(path)).suffix.lstrip("."),
            )
        self.source_bundle.entry_point = _safe_relative_path(entry_point)
        self.metadata.setdefault("integration", {})
        self.metadata["integration"]["source_bundle"] = self.source_bundle.source_manifest()
        self.updated_at = utc_now()
        return dict(self.metadata["integration"]["source_bundle"])

    def register_binary_file(
        self,
        path: str,
        content: bytes,
        *,
        media_type: Optional[str] = None,
        checksum: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.source_bundle.add_binary_file(
            path,
            content,
            media_type=media_type,
            checksum=checksum,
        )
        self.metadata.setdefault("integration", {})
        self.metadata["integration"]["source_bundle"] = self.source_bundle.source_manifest()
        self.updated_at = utc_now()
        return dict(self.metadata["integration"]["source_bundle"])

    def register_voice_artifacts(self, artifacts: Iterable[Any]) -> Dict[str, Any]:
        normalized: List[Dict[str, Any]] = []
        errors: List[str] = []
        for raw in artifacts or ():
            try:
                if isinstance(raw, Mapping):
                    getter = raw.get
                    asset_id = getter("asset_id") or getter("id")
                    path = getter("path") or getter("source_path")
                    external_uri = getter("external_uri") or getter("uri") or getter("url")
                    fmt = getter("format") or getter("extension")
                    size = int(getter("size_bytes") or getter("bytes") or 0)
                    verified = bool(getter("verified", False))
                    digest = getter("sha256") or getter("checksum")
                    provider = getter("provider") or getter("source_provider")
                    request_id = getter("request_id")
                else:
                    asset_id = getattr(raw, "asset_id", None) or getattr(raw, "id", None)
                    path = getattr(raw, "path", None) or getattr(raw, "source_path", None)
                    external_uri = getattr(raw, "external_uri", None)
                    fmt = getattr(raw, "format", None)
                    size = int(getattr(raw, "size_bytes", 0) or 0)
                    verified = bool(getattr(raw, "verified", False))
                    digest = getattr(raw, "sha256", None)
                    provider = getattr(raw, "provider", None)
                    request_id = getattr(raw, "request_id", None)

                if not asset_id:
                    raise ValueError("voice artifact missing asset_id")
                if path:
                    path = _safe_relative_path(str(path))
                if path and not verified:
                    raise ValueError(f"local voice artifact is not verified: {path}")
                if path and size <= 0:
                    raise ValueError(f"voice artifact has invalid size: {path}")

                record = {
                    "asset_id": str(asset_id),
                    "path": path,
                    "external_uri": str(external_uri) if external_uri else None,
                    "format": str(fmt).lower().lstrip(".") if fmt else None,
                    "size_bytes": max(size, 0),
                    "sha256": str(digest) if digest else None,
                    "verified": verified,
                    "provider": str(provider) if provider else None,
                    "request_id": str(request_id) if request_id else None,
                    "kind": "voice",
                }
                normalized.append(record)

                if path and isinstance(raw, Mapping) and isinstance(raw.get("content"), (bytes, bytearray)):
                    self.register_binary_file(
                        path,
                        bytes(raw["content"]),
                        media_type=f"audio/{record['format']}" if record["format"] else None,
                        checksum=record["sha256"],
                    )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        self.audio_manifest.metadata["schema"] = "riot.audio.v2"
        self.audio_manifest.metadata["artifacts"] = normalized
        self.audio_manifest.metadata["registration_errors"] = errors
        self.metadata.setdefault("integration", {})
        self.metadata["integration"]["voice"] = {
            "schema": "riot.audio.v2",
            "artifacts": normalized,
            "errors": errors,
            "local_count": sum(1 for x in normalized if x.get("path")),
            "external_count": sum(1 for x in normalized if x.get("external_uri") and not x.get("path")),
        }
        self.updated_at = utc_now()
        return dict(self.metadata["integration"]["voice"])

    # ------------------------------------------------------------------------
    # BUILD ADAPTER / VALIDATION
    # ------------------------------------------------------------------------

    def to_builder_config(self) -> Dict[str, Any]:
        """Emit the exact source-first config shape consumed by UniversalBuilder."""
        self.validate_pipeline_integrity()
        self.validate_source_bundle()

        target_map = {
            TargetPlatform.WEB_HTML5: "web",
            TargetPlatform.MOBILE_APK: "mobile",
            TargetPlatform.PC_EXE: "pc",
            TargetPlatform.CLOUD_STREAM: "web",
        }

        config: Dict[str, Any] = {
            "game_id": self.project_id,
            "build_id": self.build_id or new_id("build_"),
            "target_platform": target_map[self.target_platform],
            "source_bundle": {
                "files": self.source_bundle.as_builder_files(),
                "manifest": self.source_bundle.source_manifest(),
            },
            "metadata": {
                "riot_game_project_schema": "riot.game_project.v4",
                "project_id": self.project_id,
                "build_id": self.build_id,
                "target_platform": self.target_platform.value,
                "project_summary": self.summary(),
            },
        }
        return config

    def to_universal_builder_config(self) -> Dict[str, Any]:
        return self.to_builder_config()

    def validate_for_build(self, *, require_qa: bool = True) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []
        try:
            self.validate_pipeline_integrity()
        except Exception as exc:
            errors.append(f"canonical integrity: {type(exc).__name__}: {exc}")

        try:
            self.validate_source_bundle()
        except Exception as exc:
            errors.append(f"source bundle: {type(exc).__name__}: {exc}")

        if require_qa:
            qa_status = self.qa_report.status if self.qa_report else QAStatus.NOT_TESTED
            if qa_status not in {QAStatus.PASSED, QAStatus.PARTIAL}:
                errors.append(f"QA gate not satisfied: {qa_status.value}")

        if self.source_bundle.binary_file_count:
            warnings.append(
                f"{self.source_bundle.binary_file_count} embedded binary source artifacts present"
            )

        report = {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "project_id": self.project_id,
            "build_id": self.build_id,
            "target_platform": self.target_platform.value,
            "source_file_count": self.source_bundle.text_file_count,
            "binary_file_count": self.source_bundle.binary_file_count,
            "asset_count": self.asset_manifest.total_assets,
            "generated_asset_count": self.asset_manifest.generated_count,
            "qa_status": self.qa_report.status.value if self.qa_report else QAStatus.NOT_TESTED.value,
            "project_hash": sha256(
                repr(
                    {
                        "project_id": self.project_id,
                        "build_id": self.build_id,
                        "status": self.status.value,
                        "source": self.source_bundle.source_manifest(),
                    }
                ).encode("utf-8")
            ).hexdigest(),
        }
        if errors:
            raise ValueError(_json_safe(report))
        return report

    # ------------------------------------------------------------------------
    # BUILD ARTIFACTS
    # ------------------------------------------------------------------------

    def add_build_artifact(self, artifact: BuildArtifact) -> None:
        self.build_artifacts[artifact.platform.value] = artifact
        self.updated_at = utc_now()

    # ------------------------------------------------------------------------
    # INTEGRITY
    # ------------------------------------------------------------------------

    def validate_pipeline_integrity(self) -> None:
        if self.architecture_plan and self.architecture_plan.target_platform != self.target_platform:
            raise ValueError(
                "ArchitecturePlan target platform does not match GameProject target platform."
            )

        self.validate_world_dependencies()

        if not self.source_bundle.is_empty:
            if self.source_bundle.entry_point:
                self.source_bundle.validate_entry_point()

        if self.qa_report and self.qa_report.status == QAStatus.PASSED and self.source_bundle.is_empty:
            raise ValueError("QA cannot PASS when SourceBundle is empty.")

        for artifact in self.build_artifacts.values():
            if artifact.status == BuildStatus.SUCCESS:
                if not artifact.file_path and not artifact.artifact_reference:
                    raise ValueError("Successful build has no real artifact reference.")

        for record in self.pipeline_stages.values():
            if record.status == StageStatus.RUNNING and record.completed_at is not None:
                raise ValueError(
                    f"Running stage '{record.stage.value}' cannot have completed_at."
                )
            if record.status in {StageStatus.PASSED, StageStatus.FAILED}:
                if record.completed_at is None:
                    raise ValueError(
                        f"Finished stage '{record.stage.value}' must have completed_at."
                    )

    # ------------------------------------------------------------------------
    # SNAPSHOT / SUMMARY
    # ------------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "build_id": self.build_id,
            "name": self.name,
            "status": self.status.value,
            "target_platform": self.target_platform.value,
            "runtime_type": self.runtime_type.value if self.runtime_type else None,
            "seed": self.seed,
            "assets_requested": len(self.asset_requests),
            "asset_blueprints": len(self.asset_blueprints),
            "assets_generated": self.asset_manifest.generated_count,
            "world_ready": self.world_manifest is not None,
            "scene_ready": self.scene_graph is not None,
            "gameplay_modules": len(self.gameplay_modules),
            "source_files": self.source_bundle.total_files,
            "text_source_files": self.source_bundle.text_file_count,
            "binary_source_files": self.source_bundle.binary_file_count,
            "qa_status": self.qa_report.status.value if self.qa_report else QAStatus.NOT_TESTED.value,
            "build_artifacts": len(self.build_artifacts),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def snapshot_contract(self) -> Dict[str, Any]:
        return {
            "schema": "riot.game_project.v4",
            "project": self.summary(),
            "source": self.source_bundle.source_manifest(),
            "integration": _json_safe(self.metadata.get("integration", {})),
            "contract_hash": sha256(
                repr(
                    {
                        "summary": self.summary(),
                        "source": self.source_bundle.source_manifest(),
                    }
                ).encode("utf-8")
            ).hexdigest(),
            "timestamp": utc_now().isoformat(),
        }

    def dict_summary(self) -> Dict[str, Any]:
        return self.summary()


__all__ = [
    "utc_now",
    "new_id",
    "content_sha256",
    "RiotModel",
    "ProjectStatus",
    "StageStatus",
    "BuildStatus",
    "CapabilityStatus",
    "TargetPlatform",
    "RuntimeType",
    "Vector2",
    "Vector3",
    "Transform",
    "PerformanceBudget",
    "RuntimeTarget",
    "ArchitecturePlan",
    "AssetType",
    "AssetGenerationStatus",
    "AssetRequest",
    "TextureProfile",
    "MaterialProfile",
    "CollisionProfile",
    "LODProfile",
    "AssetBlueprint",
    "GeneratedAsset",
    "AssetManifest",
    "AssetPlacement",
    "StreamingZone",
    "ChunkManifest",
    "WorldManifest",
    "SceneObject",
    "SceneGraph",
    "PhysicsConfig",
    "GameplayModuleType",
    "GameplayModule",
    "AudioManifest",
    "UIManifest",
    "SourceFile",
    "BinarySourceFile",
    "SourceBundle",
    "QAStatus",
    "QAReport",
    "BuildArtifact",
    "PipelineStageRecord",
    "GameProject",
]
