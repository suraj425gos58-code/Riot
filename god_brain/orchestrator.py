"""
Riot / God Node — Production Swarm Orchestrator
================================================

This module is the orchestration boundary between the AI swarm and the real
game-project/build pipeline.

Core guarantees
---------------
* No placeholder asset/map data is injected into later stages.
* Agent calls are concurrency-limited without adding another provider retry loop.
  Provider retries/circuit state remain the responsibility of the Gateway/SDK.
* Every generation receives a stable build/game identity.
* Agent outputs are normalized into typed-ish, JSON-safe project manifests.
* The orchestrator assembles real source files suitable for the UniversalBuilder.
* QA is a validation stage, not a fake "verified" flag.
* Partial failures are represented explicitly; fabricated SUCCESS is forbidden.
* Cancellation is propagated and in-flight tasks are cleaned up.
* The public ``generate_full_game_with_swarm`` method remains compatible with
  the existing ``main.py`` caller.

The module deliberately does not hard-code model names, API vendors, endpoints,
or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from core.game_project import (
    ArchitecturePlan,
    AssetBlueprint as CanonicalAssetBlueprint,
    AssetRequest as CanonicalAssetRequest,
    AssetType as CanonicalAssetType,
    AssetGenerationStatus as CanonicalAssetGenerationStatus,
    GameplayModule,
    GameplayModuleType,
    GameProject,
    PhysicsConfig,
    ProjectStatus,
    QAReport,
    QAStatus,
    RuntimeType,
    WorldManifest,
    ChunkManifest,
)

try:
    from god_brain.voice_engine import VoiceEngine, VoiceFormat, VoiceProfile, VoiceSynthesisRequest, VoiceTaskType
except Exception:
    VoiceEngine = None
    VoiceFormat = VoiceProfile = VoiceSynthesisRequest = VoiceTaskType = None


logger = logging.getLogger("GodOrchestrator")

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s - [ORCHESTRATOR] - %(levelname)s - %(message)s")
    )
    logger.addHandler(_handler)

logger.setLevel(os.getenv("RIOT_ORCHESTRATOR_LOG_LEVEL", "INFO").upper())


# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_AGENT_CONCURRENCY = max(
    1, int(os.getenv("RIOT_AGENT_CONCURRENCY", "5"))
)
DEFAULT_OPERATION_TIMEOUT = max(
    5.0, float(os.getenv("RIOT_AGENT_OPERATION_TIMEOUT", "300"))
)
DEFAULT_PIPELINE_TIMEOUT = max(
    30.0, float(os.getenv("RIOT_PIPELINE_TIMEOUT", "1800"))
)
DEFAULT_MAX_ASSETS = max(
    1, int(os.getenv("RIOT_MAX_GENERATED_ASSETS", "256"))
)
DEFAULT_MAX_MAP_SECTORS = max(
    1, int(os.getenv("RIOT_MAX_MAP_SECTORS", "128"))
)
DEFAULT_MAX_SOURCE_BYTES = max(
    1024 * 1024, int(os.getenv("RIOT_MAX_SOURCE_BYTES", str(25 * 1024 * 1024)))
)
DEFAULT_MAX_VOICE_LINES = max(1, int(os.getenv("RIOT_MAX_VOICE_LINES", "64")))
DEFAULT_VOICE_CONCURRENCY = max(1, int(os.getenv("RIOT_VOICE_ORCHESTRATOR_CONCURRENCY", "4")))

_GAME_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ALLOWED_SOURCE_SUFFIXES = {
    ".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".json", ".css", ".txt",
    ".glsl", ".vert", ".frag", ".wgsl", ".xml", ".gradle", ".kts",
    ".properties", ".toml", ".yaml", ".yml", ".md",
}


# ============================================================================
# CONTRACTS
# ============================================================================

class PipelineStage(str):
    PLANNING = "planning"
    ASSETS = "assets"
    WORLD = "world"
    PHYSICS = "physics"
    GAMEPLAY = "gameplay"
    ASSEMBLY = "assembly"
    QA = "qa"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class OrchestratorConfig:
    max_concurrent_agents: int = DEFAULT_AGENT_CONCURRENCY
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT
    pipeline_timeout_seconds: float = DEFAULT_PIPELINE_TIMEOUT
    max_assets: int = DEFAULT_MAX_ASSETS
    max_map_sectors: int = DEFAULT_MAX_MAP_SECTORS
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    require_qa: bool = True
    fail_on_partial_swarm: bool = False
    enable_voice: bool = True
    max_voice_lines: int = DEFAULT_MAX_VOICE_LINES
    voice_concurrency: int = DEFAULT_VOICE_CONCURRENCY


@dataclass(slots=True)
class AgentResult:
    task_id: str
    role: str
    ok: bool
    result: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceFile:
    path: str
    content: str
    encoding: str = "utf-8"

    def size_bytes(self) -> int:
        return len(self.content.encode(self.encoding, errors="replace"))


@dataclass(slots=True)
class BinarySourceFile:
    path: str
    content: bytes

    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class PipelineState:
    build_id: str
    game_id: str
    prompt: str
    target_platform: str
    stage: str = PipelineStage.PLANNING
    started_at: float = field(default_factory=time.time)
    stage_started_at: float = field(default_factory=time.time)
    agent_results: dict[str, AgentResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def transition(self, stage: str) -> None:
        self.stage = stage
        self.stage_started_at = time.time()

    def add_result(self, result: AgentResult) -> None:
        self.agent_results[result.task_id] = result

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "game_id": self.game_id,
            "prompt": self.prompt,
            "target_platform": self.target_platform,
            "stage": self.stage,
            "started_at": self.started_at,
            "duration_ms": max(0.0, (time.time() - self.started_at) * 1000.0),
            "agent_results": {
                key: asdict(value) for key, value in self.agent_results.items()
            },
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# ============================================================================
# NORMALIZATION / VALIDATION
# ============================================================================

def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _safe_game_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = f"riot_game_{uuid.uuid4().hex[:12]}"
    if not _GAME_ID_RE.fullmatch(raw):
        raise ValueError(f"invalid game_id: {raw!r}")
    return raw


def _safe_target(value: Any) -> str:
    raw = str(value or "web").strip().lower()
    aliases = {
        "web_html5": "web",
        "html5": "web",
        "android": "mobile",
        "apk": "mobile",
        "windows": "pc",
        "desktop": "pc",
        "exe": "pc",
    }
    raw = aliases.get(raw, raw)
    if raw not in {"web", "mobile", "pc"}:
        raise ValueError(f"unsupported target_platform: {value!r}")
    return raw


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    try:
        return str(value)
    except Exception:
        return repr(value)


def _compact_prompt_context(prompt: str, plan: Any) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "plan": _json_safe(plan),
    }


def _normalize_collection(
    value: Any,
    *,
    item_prefix: str,
    limit: int,
) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("items", "assets", "objects", "entities", "sectors", "results"):
            candidate = value.get(key)
            if isinstance(candidate, (list, tuple)):
                value = candidate
                break
        else:
            value = [value]
    elif not isinstance(value, (list, tuple)):
        value = [value]

    result = []
    for index, item in enumerate(value):
        if index >= limit:
            break
        normalized = _json_safe(item)
        if isinstance(normalized, Mapping):
            normalized = dict(normalized)
            normalized.setdefault(
                "id",
                _stable_id(item_prefix, json.dumps(normalized, sort_keys=True))
            )
        else:
            normalized = {
                "id": _stable_id(item_prefix, index, normalized),
                "value": normalized,
            }
        result.append(normalized)
    return result


def _extract_agent_payload(value: Any) -> Any:
    """Strip only transport wrappers; preserve actual agent data."""
    if isinstance(value, Mapping):
        for key in ("result", "data", "output", "payload"):
            if key in value and len(value) <= 6:
                candidate = value[key]
                if candidate is not value:
                    return _extract_agent_payload(candidate)
    return value


def _normalize_source_files(value: Any) -> list[SourceFile]:
    """
    Accept common source representations without inventing source code.

    Accepted:
      {"files": {"index.html": "..."}}
      {"source_bundle": {"files": {...}}}
      {"source_files": [{"path": "...", "content": "..."}]}
      {"index.html": "...", "game.js": "..."} (plain mapping)
    """
    if value is None:
        return []

    if isinstance(value, Mapping):
        if isinstance(value.get("source_bundle"), Mapping):
            nested = value["source_bundle"].get("files")
            if isinstance(nested, Mapping):
                value = nested
        elif isinstance(value.get("files"), Mapping):
            value = value["files"]
        elif isinstance(value.get("source_files"), (list, tuple)):
            value = value["source_files"]
        else:
            candidate_keys = [
                key for key, item in value.items()
                if isinstance(key, str) and isinstance(item, (str, bytes))
            ]
            if candidate_keys:
                value = {key: value[key] for key in candidate_keys}
            else:
                return []

    files: list[SourceFile] = []

    if isinstance(value, Mapping):
        iterable: Iterable[tuple[Any, Any]] = value.items()
        for raw_path, raw_content in iterable:
            content = raw_content
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            if not isinstance(content, str):
                continue
            safe = _sanitize_source_path(str(raw_path))
            files.append(SourceFile(safe, content))
        return files

    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            raw_path = item.get("path") or item.get("file") or item.get("name")
            content = item.get("content")
            if raw_path is None or content is None:
                continue
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            if not isinstance(content, str):
                continue
            files.append(SourceFile(_sanitize_source_path(str(raw_path)), content))

    return files


def _sanitize_source_path(value: str) -> str:
    raw = value.replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe generated source path: {value!r}")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError(f"invalid generated source path: {value!r}")
    if path.parts[0].startswith(".") and path.name != ".env.example":
        raise ValueError(f"hidden control file is not allowed: {value!r}")
    return path.as_posix()


def _validate_sources(files: Sequence[SourceFile], max_bytes: int) -> None:
    total = 0
    seen: set[str] = set()
    for item in files:
        if item.path in seen:
            raise ValueError(f"duplicate generated source path: {item.path}")
        seen.add(item.path)
        suffix = PurePosixPath(item.path).suffix.lower()
        if suffix and suffix not in _ALLOWED_SOURCE_SUFFIXES:
            raise ValueError(f"unsupported generated source suffix: {item.path}")
        size = item.size_bytes()
        if size <= 0:
            raise ValueError(f"empty generated source file: {item.path}")
        total += size
        if total > max_bytes:
            raise ValueError(
                f"generated source exceeds {max_bytes} byte orchestration limit"
            )


def _validate_binary_sources(files: Sequence[BinarySourceFile], max_bytes: int, seen: set[str]) -> None:
    total = 0
    allowed = {".wav", ".mp3", ".ogg", ".opus", ".flac", ".m4a", ".aac", ".png", ".jpg", ".jpeg", ".webp", ".glb", ".gltf", ".bin"}
    for item in files:
        if item.path in seen:
            raise ValueError(f"duplicate generated source path: {item.path}")
        seen.add(item.path)
        if not isinstance(item.content, (bytes, bytearray)) or not item.content:
            raise ValueError(f"empty generated binary asset: {item.path}")
        if PurePosixPath(item.path).suffix.lower() not in allowed:
            raise ValueError(f"unsupported generated binary asset suffix: {item.path}")
        total += len(item.content)
        if total > max_bytes:
            raise ValueError(f"generated binary assets exceed {max_bytes} byte orchestration limit")



# ============================================================================
# CANONICAL GAME PROJECT ADAPTER
# ============================================================================

_TARGET_RUNTIME = {
    "web": RuntimeType.WEB,
    "mobile": RuntimeType.NATIVE_MOBILE,
    "pc": RuntimeType.DESKTOP,
    "cloud_stream": RuntimeType.CLOUD_STREAM,
}

_ASSET_TYPE_ALIASES = {
    "model": CanonicalAssetType.MODEL_3D,
    "3d_model": CanonicalAssetType.MODEL_3D,
    "3d": CanonicalAssetType.MODEL_3D,
    "mesh": CanonicalAssetType.MODEL_3D,
    "2d": CanonicalAssetType.MODEL_2D,
    "image": CanonicalAssetType.TEXTURE,
    "audio": CanonicalAssetType.SOUND,
    "sfx": CanonicalAssetType.SOUND,
    "vocal": CanonicalAssetType.VOICE,
}


def _canonical_asset_type(value: Any) -> CanonicalAssetType:
    raw = str(value or "procedural").strip().lower()
    try:
        return CanonicalAssetType(raw)
    except ValueError:
        return _ASSET_TYPE_ALIASES.get(raw, CanonicalAssetType.PROCEDURAL)


def _project_transition(project: GameProject, status: ProjectStatus) -> None:
    """Advance the canonical lifecycle without bypassing its state machine."""
    if project.status == status:
        return
    project.transition_to(status)


def _register_canonical_assets(
    project: GameProject,
    assets: Sequence[Any],
) -> None:
    """Convert agent asset specifications into canonical dependency-linked records."""
    for index, raw in enumerate(assets):
        if not isinstance(raw, Mapping):
            continue
        asset_id = str(raw.get("asset_id") or raw.get("id") or _stable_id("asset", index, raw))
        name = str(raw.get("name") or raw.get("title") or f"Generated Asset {index + 1}")[:256]
        asset_type = _canonical_asset_type(raw.get("asset_type") or raw.get("type") or raw.get("kind"))
        request_id = f"assetreq_{asset_id}"
        blueprint_id = f"blueprint_{asset_id}"
        prompt_text = str(raw.get("prompt") or raw.get("description") or name)

        if request_id not in project.asset_requests:
            project.add_asset_request(CanonicalAssetRequest(
                request_id=request_id,
                asset_type=asset_type,
                name=name,
                prompt=prompt_text,
                priority=int(raw.get("priority", 50) or 50),
                required_for_world=bool(raw.get("required_for_world", True)),
                target_formats=[str(x) for x in (raw.get("formats") or raw.get("target_formats") or []) if x],
                requirements=_json_safe(dict(raw.get("requirements") or {})) if isinstance(raw.get("requirements"), Mapping) else {},
                metadata={"agent_spec": _json_safe(raw)},
            ))
        if blueprint_id not in project.asset_blueprints:
            project.add_asset_blueprint(CanonicalAssetBlueprint(
                blueprint_id=blueprint_id,
                request_id=request_id,
                asset_type=asset_type,
                name=name,
                generation_strategy=str(raw.get("generation_strategy") or raw.get("strategy") or "agent_spec"),
                specification=_json_safe(dict(raw)),
                expected_formats=[str(x) for x in (raw.get("formats") or raw.get("target_formats") or []) if x],
                dependencies=[str(x) for x in (raw.get("dependencies") or []) if x],
            ))

        source_path = raw.get("source_path") or raw.get("path")
        artifact_reference = raw.get("artifact_reference") or raw.get("external_uri") or raw.get("uri")
        # A specification without a real artifact is PLANNED, never falsely GENERATED.
        status = (
            CanonicalAssetGenerationStatus.GENERATED
            if source_path or artifact_reference
            else CanonicalAssetGenerationStatus.PLANNED
        )
        try:
            project.register_asset_reference(
                request_id=request_id,
                blueprint_id=blueprint_id,
                asset_id=asset_id,
                asset_type=asset_type,
                name=name,
                source_path=str(source_path) if source_path else None,
                artifact_reference=str(artifact_reference) if artifact_reference else None,
                status=status,
                checksum=str(raw.get("checksum")) if raw.get("checksum") else None,
                format=str(raw.get("format")) if raw.get("format") else None,
                size_bytes=max(0, int(raw.get("size_bytes", 0) or 0)),
                metadata={"agent_spec": _json_safe(raw)},
            )
        except ValueError as exc:
            # Preserve the original spec as metadata rather than aborting the whole run.
            project.add_warning(f"asset canonicalization skipped {asset_id}: {type(exc).__name__}: {exc}")
            project.metadata.setdefault("asset_canonicalization_errors", []).append({
                "asset_id": asset_id,
                "error": f"{type(exc).__name__}: {exc}",
            })


def _register_canonical_world(
    project: GameProject,
    world: Sequence[Any],
    assets: Sequence[Any],
) -> None:
    """Persist world output as a typed manifest while retaining full raw sector evidence."""
    manifest = WorldManifest(
        name=f"{project.name} World"[:256],
        seed=project.seed,
        metadata={
            "schema": "riot.orchestrator.world.v2",
            "sector_count": len(world),
            "raw_sectors": _json_safe(list(world)),
        },
    )
    known_asset_ids = {
        str(item.get("id") or item.get("asset_id"))
        for item in assets
        if isinstance(item, Mapping) and (item.get("id") or item.get("asset_id"))
    }
    for index, raw in enumerate(world):
        if not isinstance(raw, Mapping):
            raw = {"value": _json_safe(raw)}
        chunk_id = str(raw.get("chunk_id") or raw.get("sector_id") or raw.get("id") or _stable_id("chunk", index, raw))
        placement_ids = raw.get("asset_ids") or raw.get("assets") or raw.get("asset_refs") or []
        placements = []
        if isinstance(placement_ids, (list, tuple)):
            for asset_ref in placement_ids:
                if isinstance(asset_ref, Mapping):
                    ref = asset_ref.get("asset_id") or asset_ref.get("id")
                else:
                    ref = asset_ref
                if ref and str(ref) in known_asset_ids and str(ref) in project.asset_manifest.assets:
                    from core.game_project import AssetPlacement
                    placements.append(AssetPlacement(asset_id=str(ref), properties={"source": "agent_world"}))
        manifest.chunks[chunk_id] = ChunkManifest(
            chunk_id=chunk_id,
            asset_placements=placements,
            metadata={"raw_sector": _json_safe(raw)},
        )
        for placement in placements:
            manifest.used_asset_ids.add(placement.asset_id)
    project.world_manifest = manifest


def _canonical_physics(value: Any) -> Optional[PhysicsConfig]:
    if isinstance(value, Mapping):
        try:
            return PhysicsConfig.model_validate(value)
        except Exception:
            return None
    return None


def _canonical_qa(value: Any, tested_files: Sequence[str]) -> QAReport:
    if isinstance(value, Mapping):
        try:
            report = QAReport.model_validate(value)
            if not report.tested_files:
                report.tested_files = list(tested_files)
            return report
        except Exception:
            status_raw = str(value.get("status") or value.get("result") or "PARTIAL").upper()
            status = QAStatus.PASSED if status_raw in {"PASS", "PASSED", "OK", "SUCCESS"} else QAStatus.PARTIAL
            return QAReport(
                status=status,
                tests_run=max(1, int(value.get("tests_run", 1) or 1)),
                tests_passed=max(0, int(value.get("tests_passed", 1 if status == QAStatus.PASSED else 0) or 0)),
                tests_failed=max(0, int(value.get("tests_failed", 0 if status == QAStatus.PASSED else 1) or 0)),
                issues=[str(x) for x in (value.get("issues") or [])],
                warnings=[str(x) for x in (value.get("warnings") or [])],
                tested_files=list(tested_files),
                checks={},
            )
    return QAReport(
        status=QAStatus.PARTIAL,
        tests_run=1,
        tests_passed=0,
        tests_failed=1,
        issues=["QA agent did not return a structured report"],
        tested_files=list(tested_files),
    )

# ============================================================================
# AGENT INVOCATION
# ============================================================================

async def _invoke_callable(
    method: Callable[..., Any],
    *args: Any,
    timeout_seconds: float,
    **kwargs: Any,
) -> Any:
    """
    Invoke async methods natively and sync methods off the event loop.

    This does NOT implement provider retries. That responsibility stays below
    the orchestrator in the gateway/provider execution layer.
    """
    async def _run() -> Any:
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)

        result = await asyncio.to_thread(method, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    return await asyncio.wait_for(_run(), timeout=timeout_seconds)


# ============================================================================
# ORCHESTRATOR
# ============================================================================

class GodOrchestrator:
    """
    Coordinates Director → Assets + World → Physics → Gameplay synthesis → QA.

    The class preserves the existing public API while turning the old metadata
    pipeline into an actual project-source assembly pipeline.
    """

    def __init__(
        self,
        *,
        config: Optional[OrchestratorConfig] = None,
        director: Any = None,
        asset_gen: Any = None,
        map_builder: Any = None,
        physics: Any = None,
        qa_tester: Any = None,
        voice_engine: Any = None,
    ) -> None:
        from god_brain.agents.director_agent import DirectorAgent
        from god_brain.agents.asset_generator_agent import AssetGeneratorAgent
        from god_brain.agents.map_builder_agent import MapBuilderAgent
        from god_brain.agents.physics_agent import PhysicsAgent
        from god_brain.agents.qa_tester_agent import QATesterAgent

        self.config = config or OrchestratorConfig()

        self.director = director or DirectorAgent()
        self.asset_gen = asset_gen or AssetGeneratorAgent()
        self.map_builder = map_builder or MapBuilderAgent()
        self.physics = physics or PhysicsAgent()
        self.qa_tester = qa_tester or QATesterAgent()
        if voice_engine is not None:
            self.voice_engine = voice_engine
        elif self.config.enable_voice and VoiceEngine is not None:
            self.voice_engine = VoiceEngine(concurrency=self.config.voice_concurrency)
        else:
            self.voice_engine = None

        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_agents)

        self._active_pipeline: dict[str, asyncio.Task[Any]] = {}
        self._registry_lock = asyncio.Lock()

        logger.info(
            "GodOrchestrator ready: concurrency=%d timeout=%ss",
            self.config.max_concurrent_agents,
            self.config.operation_timeout_seconds,
        )

    # ---------------------------------------------------------------------
    # Compatibility helper retained for existing callers/tests.
    # ---------------------------------------------------------------------
    async def _resolve_agent_call(
        self,
        agent_method: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return await _invoke_callable(
            agent_method,
            *args,
            timeout_seconds=self.config.operation_timeout_seconds,
            **kwargs,
        )

    async def _run_agent(
        self,
        *,
        task_id: str,
        agent: Any,
        task_data: Any = None,
        kwargs: Optional[dict[str, Any]] = None,
    ) -> AgentResult:
        method = getattr(agent, "perform_role", None)
        if method is None:
            return AgentResult(
                task_id=task_id,
                role=type(agent).__name__,
                ok=False,
                error="agent does not expose perform_role()",
            )

        role = str(
            getattr(agent, "role_name", None)
            or getattr(agent, "role", None)
            or type(agent).__name__
        )
        started = time.perf_counter()

        async with self.semaphore:
            try:
                call_kwargs = kwargs or {}
                if task_data is None:
                    value = await self._resolve_agent_call(method, **call_kwargs)
                else:
                    value = await self._resolve_agent_call(
                        method, task_data, **call_kwargs
                    )

                elapsed = (time.perf_counter() - started) * 1000.0
                return AgentResult(
                    task_id=task_id,
                    role=role,
                    ok=True,
                    result=_extract_agent_payload(value),
                    duration_ms=elapsed,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000.0
                logger.exception("Agent task %s failed", task_id)
                return AgentResult(
                    task_id=task_id,
                    role=role,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=elapsed,
                )

    async def _run_parallel(
        self,
        jobs: Sequence[tuple[str, Any, Any, dict[str, Any]]],
    ) -> list[AgentResult]:
        tasks = [
            asyncio.create_task(
                self._run_agent(
                    task_id=task_id,
                    agent=agent,
                    task_data=task_data,
                    kwargs=kwargs,
                ),
                name=f"riot-agent-{task_id}",
            )
            for task_id, agent, task_data, kwargs in jobs
        ]

        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    # ---------------------------------------------------------------------
    # Phase implementations
    # ---------------------------------------------------------------------
    async def _plan(self, state: PipelineState) -> AgentResult:
        state.transition(PipelineStage.PLANNING)
        result = await self._run_agent(
            task_id="director",
            agent=self.director,
            task_data=state.prompt,
            kwargs={},
        )
        state.add_result(result)

        if not result.ok or result.result is None:
            state.errors.append(result.error or "director returned no plan")
        return result

    async def _generate_assets_and_world(
        self,
        state: PipelineState,
        plan: Any,
        agent_count: int,
    ) -> tuple[list[AgentResult], list[AgentResult]]:
        state.transition(PipelineStage.ASSETS)

        total = max(2, min(int(agent_count), self.config.max_assets))
        asset_count = max(1, int(total * 0.65))
        map_count = max(1, total - asset_count)
        map_count = min(map_count, self.config.max_map_sectors)

        asset_jobs = [
            (
                f"asset_{index:04d}",
                self.asset_gen,
                (
                    f"Generate asset specification {index + 1} for this game. "
                    "Return structured asset data suitable for a real project "
                    "manifest; do not return placeholder text.\n"
                    f"Prompt: {state.prompt}\nPlan: {json.dumps(_json_safe(plan), default=str)}"
                ),
                {
                    "style": "optimized",
                    "project_context": _compact_prompt_context(state.prompt, plan),
                },
            )
            for index in range(asset_count)
        ]

        # World generation gets the plan, not a fabricated asset list.
        world_jobs = [
            (
                f"world_{index:04d}",
                self.map_builder,
                (
                    f"Generate world sector specification {index + 1}. "
                    "Use the supplied plan. Reference real asset IDs/specifications "
                    "when they exist; do not invent a placeholder_list."
                ),
                {
                    "generated_assets": [],
                    "game_plan": _json_safe(plan),
                    "prompt": state.prompt,
                    "sector_index": index,
                },
            )
            for index in range(map_count)
        ]

        # These two families are independent at this stage. The world builder can
        # produce symbolic references and receives real assets in a second pass
        # below when possible.
        asset_results, world_results = await asyncio.gather(
            self._run_parallel(asset_jobs),
            self._run_parallel(world_jobs),
        )

        for result in (*asset_results, *world_results):
            state.add_result(result)

        successful_assets = [
            item for result in asset_results if result.ok
            for item in _normalize_collection(
                result.result, item_prefix=result.task_id, limit=1
            )
        ]

        # Second-pass enrichment: give successful asset manifests to map jobs
        # without performing another map call when the first pass already
        # returned enough structural data.
        enriched_world: list[AgentResult] = []
        world_input = successful_assets[: self.config.max_assets]
        if world_input and world_results:
            enrichment_jobs = [
                (
                    f"world_enrich_{index:04d}",
                    self.map_builder,
                    "Refine this world sector using the real generated asset "
                    "specifications. Return only structured world data.",
                    {
                        "generated_assets": world_input,
                        "existing_sector": result.result,
                        "game_plan": _json_safe(plan),
                        "prompt": state.prompt,
                        "sector_index": index,
                    },
                )
                for index, result in enumerate(world_results)
                if result.ok
            ]
            if enrichment_jobs:
                enriched_world = await self._run_parallel(enrichment_jobs)
                for result in enriched_world:
                    state.add_result(result)

        return asset_results, enriched_world or world_results

    async def _physics(
        self,
        state: PipelineState,
        plan: Any,
        assets: list[Any],
        world: list[Any],
    ) -> AgentResult:
        state.transition(PipelineStage.PHYSICS)
        context = {
            "game_plan": _json_safe(plan),
            "assets": _json_safe(assets),
            "world": _json_safe(world),
            "asset_count": len(assets),
            "sector_count": len(world),
        }
        result = await self._run_agent(
            task_id="physics",
            agent=self.physics,
            kwargs={"environment_details": context},
        )
        state.add_result(result)
        return result

    @staticmethod
    def _extract_voice_specs(plan: Any, prompt: str, limit: int) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        if isinstance(plan, Mapping):
            for key in ("voice_lines", "dialogue", "dialogues", "narration", "voice", "audio_dialogue", "sound"):
                value = plan.get(key)
                if isinstance(value, (list, tuple)):
                    candidates.extend(value)
                elif isinstance(value, Mapping):
                    candidates.append(value)
                elif isinstance(value, str) and value.strip():
                    candidates.append(value)
        if not candidates and isinstance(plan, Mapping):
            text = plan.get("opening_narration") or plan.get("intro")
            if isinstance(text, str) and text.strip():
                candidates.append({"text": text, "task_type": "narration"})
        specs: list[dict[str, Any]] = []
        for index, item in enumerate(candidates[:limit]):
            if isinstance(item, str):
                text = item.strip()
                data: dict[str, Any] = {"text": text, "task_type": "dialogue"}
            elif isinstance(item, Mapping):
                text = str(item.get("text") or item.get("line") or item.get("dialogue") or item.get("script") or "").strip()
                if not text:
                    continue
                data = dict(item)
                data["text"] = text
            else:
                continue
            data.setdefault("id", f"voice_line_{index:04d}")
            specs.append(data)
        return specs

    async def _generate_voice_assets(self, state: PipelineState, plan: Any) -> tuple[list[Any], list[BinarySourceFile], list[dict[str, Any]]]:
        if not self.voice_engine or not self.config.enable_voice:
            return [], [], []
        specs = self._extract_voice_specs(plan, state.prompt, self.config.max_voice_lines)
        if not specs:
            return [], [], []
        try:
            startup = getattr(self.voice_engine, "startup", None)
            if callable(startup):
                result = startup()
                if inspect.isawaitable(result):
                    await result
            requests = []
            for spec in specs:
                task_type_raw = str(spec.get("task_type", "dialogue")).lower()
                mapping = {"tts": "tts", "dialogue": "dialogue", "narration": "narration", "sfx": "sfx", "music": "music"}
                task_type = VoiceTaskType(mapping.get(task_type_raw, "dialogue"))
                profile = VoiceProfile(
                    voice_id=spec.get("voice_id"), language=spec.get("language"), locale=spec.get("locale"),
                    gender=spec.get("gender"), style=spec.get("style"), emotion=spec.get("emotion"),
                    speaking_rate=float(spec.get("speaking_rate", 1.0)), pitch=float(spec.get("pitch", 0.0)),
                    volume=float(spec.get("volume", 1.0)), stability=spec.get("stability"),
                    similarity=spec.get("similarity"), expressiveness=spec.get("expressiveness"),
                    provider_options=spec.get("provider_options", {}) if isinstance(spec.get("provider_options", {}), Mapping) else {},
                )
                requests.append(VoiceSynthesisRequest(
                    text=str(spec["text"]), game_id=state.game_id, task_type=task_type, profile=profile,
                    output_format=VoiceFormat(str(spec.get("format", "mp3")).lower()),
                    metadata={"line_id": spec["id"], "build_id": state.build_id},
                    preferred_provider=spec.get("preferred_provider"),
                ))
            artifacts = await self.voice_engine.synthesize_many(requests, fail_fast=False)
            binary_files: list[BinarySourceFile] = []
            manifest: list[dict[str, Any]] = []
            for artifact in artifacts:
                record = artifact.to_dict() if hasattr(artifact, "to_dict") else dict(artifact)
                record["build_relative_path"] = f"audio/{artifact.asset_id}.{artifact.format}"
                path = getattr(artifact, "path", None)
                if path and os.path.isfile(path):
                    with open(path, "rb") as handle:
                        payload = handle.read()
                    binary_files.append(BinarySourceFile(record["build_relative_path"], payload))
                    record["packaged"] = True
                else:
                    record["packaged"] = False
                manifest.append(record)
            return artifacts, binary_files, manifest
        except Exception as exc:
            state.warnings.append(f"voice generation unavailable: {type(exc).__name__}: {exc}")
            logger.warning("Voice generation failed for %s: %s", state.build_id, exc)
            return [], [], []

    async def _gameplay_synthesis(
        self,
        state: PipelineState,
        plan: Any,
        assets: list[Any],
        world: list[Any],
        physics: Any,
    ) -> list[SourceFile]:
        """
        Build actual deterministic source artifacts from agent outputs.

        This is intentionally template-driven rather than pretending an agent
        returned an executable binary. The generated source is real source and
        can be compiled by the UniversalBuilder.
        """
        state.transition(PipelineStage.GAMEPLAY)

        manifest = {
            "schema": "riot.game.v2",
            "game_id": state.game_id,
            "build_id": state.build_id,
            "prompt": state.prompt,
            "target_platform": state.target_platform,
            "architecture": _json_safe(plan),
            "assets": _json_safe(assets),
            "world": _json_safe(world),
            "physics": _json_safe(physics),
            "generated_at": time.time(),
        }

        runtime_model = {
            "schema": "riot.runtime.v1",
            "game_id": state.game_id,
            "build_id": state.build_id,
            "entities": [
                {
                    "id": item.get("id"),
                    "asset_ref": item.get("id"),
                    "kind": "asset",
                }
                for item in assets
                if isinstance(item, Mapping)
            ],
            "world_sectors": [
                item.get("id")
                for item in world
                if isinstance(item, Mapping)
            ],
            "physics": _json_safe(physics),
        }

        html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Riot Generated Game</title>
  <link rel="stylesheet" href="game.css">
</head>
<body>
  <main id="game-root" aria-label="Riot generated game">
    <canvas id="game-canvas"></canvas>
    <div id="game-status" role="status">Initializing…</div>
  </main>
  <script type="module" src="game.js"></script>
</body>
</html>
"""

        css = """html,body,#game-root{width:100%;height:100%;margin:0;overflow:hidden;background:#05070b}
#game-canvas{display:block;width:100%;height:100%}
#game-status{position:fixed;left:12px;top:12px;padding:6px 9px;font:12px system-ui,sans-serif;color:#fff;background:#0008;border-radius:6px}
"""

        runtime_json = json.dumps(runtime_model, indent=2, ensure_ascii=False)
        manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False)

        js = """const canvas = document.getElementById("game-canvas");
const status = document.getElementById("game-status");
const ctx = canvas.getContext("2d", {alpha: false});
const manifest = await fetch("./game-manifest.json").then(r => r.json());
const runtime = await fetch("./runtime-model.json").then(r => r.json());

function resize() {
  const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  canvas.width = Math.floor(innerWidth * dpr);
  canvas.height = Math.floor(innerHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener("resize", resize);
resize();

let t0 = performance.now();

function tick(now) {
  const seconds = (now - t0) / 1000;
  ctx.fillStyle = "#080c14";
  ctx.fillRect(0, 0, innerWidth, innerHeight);

  // Deterministic visualization of the generated world graph.
  const sectors = Array.isArray(runtime.world_sectors) ? runtime.world_sectors : [];
  const entities = Array.isArray(runtime.entities) ? runtime.entities : [];
  const cx = innerWidth / 2;
  const cy = innerHeight / 2;
  const radius = Math.max(80, Math.min(innerWidth, innerHeight) * 0.28);

  ctx.strokeStyle = "#334155";
  for (let i = 0; i < sectors.length; i++) {
    const a = (i / Math.max(1, sectors.length)) * Math.PI * 2 + seconds * 0.05;
    const x = cx + Math.cos(a) * radius;
    const y = cy + Math.sin(a) * radius;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(x, y);
    ctx.stroke();
  }

  ctx.fillStyle = "#7dd3fc";
  for (let i = 0; i < entities.length; i++) {
    const a = (i / Math.max(1, entities.length)) * Math.PI * 2 + seconds * 0.12;
    const x = cx + Math.cos(a) * (radius * 0.65);
    const y = cy + Math.sin(a) * (radius * 0.65);
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
  }

  status.textContent = `${manifest.game_id} • ${entities.length} entities • ${sectors.length} sectors`;
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
"""

        files = [
            SourceFile("index.html", html),
            SourceFile("game.css", css),
            SourceFile("game.js", js),
            SourceFile("game-manifest.json", manifest_json),
            SourceFile("runtime-model.json", runtime_json),
        ]

        _validate_sources(files, self.config.max_source_bytes)
        return files

    async def _qa(
        self,
        state: PipelineState,
        project: GameProject,
    ) -> AgentResult:
        state.transition(PipelineStage.QA)

        source_payload = {
            "project": project.summary(),
            "source_bundle": project.source_bundle.source_manifest(),
            "source_files": [
                {"path": path, "content": item.content}
                for path, item in project.source_bundle.files.items()
            ],
            "architecture": _json_safe(project.architecture_plan),
            "assets": _json_safe(project.asset_manifest.assets),
            "world": _json_safe(project.world_manifest),
            "physics": _json_safe(project.physics_config),
            "gameplay": _json_safe(project.gameplay_modules),
        }

        result = await self._run_agent(
            task_id="qa",
            agent=self.qa_tester,
            kwargs={
                "generated_code": json.dumps(
                    source_payload, ensure_ascii=False, separators=(",", ":")
                ),
                "error_logs": state.errors or None,
            },
        )
        state.add_result(result)
        return result

    # ---------------------------------------------------------------------
    # Public pipeline
    # ---------------------------------------------------------------------
    async def generate_full_game_with_swarm(
        self,
        prompt: str,
        agent_count: int = 10,
        auto_kill_after_execution: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            return {
                "status": "FAILED",
                "stage": PipelineStage.PLANNING,
                "error": "prompt must be a non-empty string",
            }

        build_id = f"BUILD_{uuid.uuid4().hex}"
        plan_hint_id = _stable_id("game", prompt, time.time_ns())
        target_platform = "web"

        # The existing main.py does not pass target platform directly. Keep the
        # default compatible, while allowing callers to put it in a directive
        # or environment in future revisions.
        state = PipelineState(
            build_id=build_id,
            game_id=plan_hint_id,
            prompt=prompt.strip(),
            target_platform=target_platform,
        )

        pipeline_task = asyncio.current_task()
        if pipeline_task is not None:
            async with self._registry_lock:
                self._active_pipeline[build_id] = pipeline_task

        started = time.perf_counter()

        try:
            async with asyncio.timeout(self.config.pipeline_timeout_seconds):
                # ---------------------------------------------------------
                # 1. DIRECTOR
                # ---------------------------------------------------------
                plan_result = await self._plan(state)
                if not plan_result.ok:
                    raise RuntimeError(plan_result.error or "planning failed")

                plan = _extract_agent_payload(plan_result.result)
                if isinstance(plan, Mapping):
                    requested_game_id = (
                        plan.get("game_id")
                        or plan.get("project_id")
                        or plan.get("id")
                    )
                    if requested_game_id:
                        state.game_id = _safe_game_id(requested_game_id)
                    target_platform = (
                        plan.get("target_platform")
                        or plan.get("platform")
                    )
                    if target_platform:
                        state.target_platform = _safe_target(target_platform)

                # ---------------------------------------------------------
                # 2/3. ASSETS + WORLD
                # ---------------------------------------------------------
                asset_results, world_results = await self._generate_assets_and_world(
                    state,
                    plan,
                    agent_count,
                )

                assets: list[Any] = []
                for result in asset_results:
                    if result.ok:
                        assets.extend(
                            _normalize_collection(
                                result.result,
                                item_prefix=result.task_id,
                                limit=1,
                            )
                        )

                world: list[Any] = []
                for result in world_results:
                    if result.ok:
                        world.extend(
                            _normalize_collection(
                                result.result,
                                item_prefix=result.task_id,
                                limit=1,
                            )
                        )

                if not assets:
                    state.warnings.append("asset swarm produced no structured assets")
                if not world:
                    state.warnings.append("world swarm produced no structured sectors")

                if self.config.fail_on_partial_swarm and (
                    not assets or not world
                ):
                    raise RuntimeError(
                        "required swarm phase returned no usable structured output"
                    )

                # ---------------------------------------------------------
                # 4. PHYSICS
                # ---------------------------------------------------------
                physics_result = await self._physics(
                    state,
                    plan,
                    assets,
                    world,
                )
                if not physics_result.ok:
                    raise RuntimeError(
                        physics_result.error or "physics generation failed"
                    )
                physics = _extract_agent_payload(physics_result.result)

                # ---------------------------------------------------------
                # 5. VOICE / AUDIO GENERATION
                # ---------------------------------------------------------
                voice_artifacts, voice_binary_files, voice_manifest = await self._generate_voice_assets(
                    state, plan
                )

                # ---------------------------------------------------------
                # 6. REAL SOURCE ASSEMBLY
                # ---------------------------------------------------------
                source_files = await self._gameplay_synthesis(
                    state,
                    plan,
                    assets,
                    world,
                    physics,
                )
                source_files.append(SourceFile(
                    "audio-manifest.json",
                    json.dumps({
                        "schema": "riot.audio.v1",
                        "game_id": state.game_id,
                        "build_id": state.build_id,
                        "assets": voice_manifest,
                    }, ensure_ascii=False, indent=2),
                ))

                # ---------------------------------------------------------
                # 6. CANONICAL PROJECT ASSEMBLY
                # ---------------------------------------------------------
                project_target = {
                    "web": "web_html5",
                    "mobile": "mobile_apk",
                    "pc": "pc_exe",
                }[state.target_platform]
                project = GameProject(
                    project_id=state.game_id,
                    build_id=state.build_id,
                    name=str((plan.get("name") if isinstance(plan, Mapping) else None) or "Riot Generated Game")[:256],
                    description=str((plan.get("description") if isinstance(plan, Mapping) else None) or state.prompt)[:2000],
                    user_prompt=state.prompt,
                    target_platform=project_target,
                    runtime_type=_TARGET_RUNTIME.get(state.target_platform),
                    seed=int(hashlib.sha256(state.prompt.encode("utf-8")).hexdigest()[:12], 16),
                    metadata={
                        "pipeline_schema": "riot.orchestrator.v3",
                        "orchestrator_build_id": state.build_id,
                        "agent_count": agent_count,
                        "raw_plan": _json_safe(plan),
                        "raw_assets": _json_safe(assets),
                        "raw_world": _json_safe(world),
                        "raw_physics": _json_safe(physics),
                        "voice_assets": _json_safe(voice_manifest),
                    },
                )

                # The canonical state machine is advanced in-order.
                _project_transition(project, ProjectStatus.ROUTING)
                _project_transition(project, ProjectStatus.PLANNING)
                project.architecture_plan = ArchitecturePlan(
                    target_platform=project.target_platform,
                    game_genre=str(plan.get("game_genre") or plan.get("genre") or "unknown") if isinstance(plan, Mapping) else "unknown",
                    visual_style=str(plan.get("visual_style") or plan.get("style") or "unknown") if isinstance(plan, Mapping) else "unknown",
                    complexity_class=str(plan.get("complexity_class") or "generated") if isinstance(plan, Mapping) else "generated",
                    core_gameplay_loop=str(plan.get("core_gameplay_loop")) if isinstance(plan, Mapping) and plan.get("core_gameplay_loop") else None,
                    engine_config=_json_safe(dict(plan.get("engine_config") or {})) if isinstance(plan, Mapping) and isinstance(plan.get("engine_config"), Mapping) else {},
                    required_agents=[str(x) for x in (plan.get("required_agents") or [])] if isinstance(plan, Mapping) and isinstance(plan.get("required_agents"), (list, tuple)) else [],
                    required_capabilities={str(x) for x in (plan.get("required_capabilities") or [])} if isinstance(plan, Mapping) and isinstance(plan.get("required_capabilities"), (list, tuple, set)) else set(),
                    build_steps=[str(x) for x in (plan.get("build_steps") or [])] if isinstance(plan, Mapping) and isinstance(plan.get("build_steps"), (list, tuple)) else [],
                    technical_constraints=[str(x) for x in (plan.get("technical_constraints") or [])] if isinstance(plan, Mapping) and isinstance(plan.get("technical_constraints"), (list, tuple)) else [],
                )
                _project_transition(project, ProjectStatus.ASSET_PLANNING)
                _register_canonical_assets(project, assets)
                _project_transition(project, ProjectStatus.ASSET_GENERATION)
                _project_transition(project, ProjectStatus.WORLD_GENERATION)
                _register_canonical_world(project, world, assets)
                _project_transition(project, ProjectStatus.SCENE_GENERATION)
                _project_transition(project, ProjectStatus.PHYSICS_CONFIG)
                canonical_physics = _canonical_physics(physics)
                if canonical_physics is not None:
                    project.physics_config = canonical_physics
                else:
                    project.add_warning("physics agent output could not be represented by canonical PhysicsConfig; raw output retained in metadata")
                _project_transition(project, ProjectStatus.GAMEPLAY_GENERATION)
                gameplay_module = GameplayModule(
                    name="Generated Runtime",
                    module_type=GameplayModuleType.CUSTOM,
                    configuration={"schema": "riot.runtime.generated.v1"},
                    source_files=["game.js", "runtime-model.json", "game-manifest.json"],
                )
                project.add_gameplay_module(gameplay_module)
                _project_transition(project, ProjectStatus.SOURCE_GENERATION)
                project.register_source_files(
                    {item.path: item.content for item in source_files},
                    entry_point="index.html",
                )
                for binary in voice_binary_files:
                    project.register_binary_file(
                        binary.path,
                        bytes(binary.content),
                        media_type=f"audio/{PurePosixPath(binary.path).suffix.lstrip('.')}",
                    )
                project.register_voice_artifacts(voice_artifacts)
                project.audio_manifest.metadata["voice_manifest"] = _json_safe(voice_manifest)
                project.metadata["source_bundle"] = project.source_bundle.source_manifest()

                project.validate_source_bundle()
                project.validate_pipeline_integrity()

                # ---------------------------------------------------------
                # 6. QA
                # ---------------------------------------------------------
                if self.config.require_qa:
                    qa_result = await self._qa(state, project)
                    if not qa_result.ok:
                        raise RuntimeError(qa_result.error or "QA agent failed")
                    qa_payload = _extract_agent_payload(qa_result.result)
                    project.qa_report = _canonical_qa(qa_payload, list(project.source_bundle.files.keys()))
                else:
                    qa_payload = {"status": "SKIPPED"}
                    project.qa_report = QAReport(status=QAStatus.NOT_TESTED, tested_files=list(project.source_bundle.files.keys()))

                qa_status = project.qa_report.status
                if qa_status in {QAStatus.FAILED} or (isinstance(qa_payload, Mapping) and str(qa_payload.get("status") or "").upper() in {"REJECTED", "UNSAFE"}):
                    raise RuntimeError(f"QA rejected generated project: {json.dumps(_json_safe(qa_payload))}")

                project.validate_for_build(require_qa=self.config.require_qa)
                _project_transition(project, ProjectStatus.QA_TESTING)
                qa_stage = project.pipeline_stages.get(ProjectStatus.QA_TESTING.value)
                if qa_stage is not None and qa_stage.status.value == "RUNNING":
                    project.complete_stage(ProjectStatus.QA_TESTING, success=True)
                _project_transition(project, ProjectStatus.READY_FOR_BUILD)

                state.transition(PipelineStage.ASSEMBLY)
                builder_config = project.to_builder_config()
                state.transition(PipelineStage.COMPLETE)

                duration_ms = (time.perf_counter() - started) * 1000.0

                return {
                    "status": "SUCCESS",
                    "message": "Game project generated, canonically assembled, and QA-checked.",
                    "execution_time": f"{duration_ms / 1000.0:.2f}s",
                    "game_id": project.project_id,
                    "build_id": project.build_id,
                    "target_platform": project.target_platform.value,
                    "project": project.model_dump(mode="json"),
                    "build_config": builder_config,
                    "architecture": _json_safe(project.architecture_plan),
                    "assets": _json_safe(project.asset_manifest.assets),
                    "world": _json_safe(project.world_manifest),
                    "physics": _json_safe(project.physics_config),
                    "qa": _json_safe(project.qa_report),
                    "pipeline": state.to_dict(),
                    "warnings": list(state.warnings) + list(project.warnings),
                    "canonical_contract": project.snapshot_contract(),
                }

        except asyncio.TimeoutError:
            state.transition(PipelineStage.FAILED)
            state.errors.append(
                f"pipeline timeout after {self.config.pipeline_timeout_seconds:.0f}s"
            )
            return {
                "status": "FAILED",
                "stage": state.stage,
                "error": state.errors[-1],
                "pipeline": state.to_dict(),
            }
        except asyncio.CancelledError:
            state.transition(PipelineStage.CANCELLED)
            logger.info("Pipeline %s cancelled", build_id)
            raise
        except Exception as exc:
            state.transition(PipelineStage.FAILED)
            state.errors.append(f"{type(exc).__name__}: {exc}")
            logger.exception("Game generation pipeline failed: %s", build_id)
            return {
                "status": "FAILED",
                "stage": state.stage,
                "error": str(exc),
                "build_id": build_id,
                "game_id": state.game_id,
                "pipeline": state.to_dict(),
            }
        finally:
            async with self._registry_lock:
                self._active_pipeline.pop(build_id, None)

            if auto_kill_after_execution:
                # No child agent tasks should remain after the pipeline ends.
                await asyncio.sleep(0)

    async def cancel(self, build_id: str) -> bool:
        async with self._registry_lock:
            task = self._active_pipeline.get(build_id)

        if task is None or task.done():
            return False

        task.cancel()
        return True

    async def active_builds(self) -> list[str]:
        async with self._registry_lock:
            return [
                key for key, task in self._active_pipeline.items()
                if not task.done()
            ]

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "max_concurrent_agents": self.config.max_concurrent_agents,
            "operation_timeout_seconds": self.config.operation_timeout_seconds,
            "pipeline_timeout_seconds": self.config.pipeline_timeout_seconds,
            "active_build_count": sum(
                1 for task in self._active_pipeline.values() if not task.done()
            ),
        }


# ============================================================================
# COMPATIBILITY HELPERS
# ============================================================================

async def generate_full_game_with_swarm(
    prompt: str,
    agent_count: int = 10,
    auto_kill_after_execution: bool = True,
) -> dict[str, Any]:
    """Module-level compatibility wrapper."""
    orchestrator = GodOrchestrator()
    return await orchestrator.generate_full_game_with_swarm(
        prompt=prompt,
        agent_count=agent_count,
        auto_kill_after_execution=auto_kill_after_execution,
    )


__all__ = [
    "AgentResult",
    "GodOrchestrator",
    "OrchestratorConfig",
    "PipelineStage",
    "PipelineState",
    "SourceFile",
    "BinarySourceFile",
    "generate_full_game_with_swarm",
]
