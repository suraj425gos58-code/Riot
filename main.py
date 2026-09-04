"""
Riot / God Node — Production Master Runtime
============================================

Single-process control plane for the personal Riot game-generation engine.

Pipeline:
    HTTP/WebSocket
        -> authentication
        -> master intent router
        -> GodOrchestrator
        -> real source bundle
        -> QA
        -> UniversalBuilder
        -> verified artifact
        -> bounded task/project registries

Design guarantees
-----------------
* No fake build source or dummy artifact path is generated here.
* The advanced orchestrator is the canonical source of generated projects.
* The UniversalBuilder receives the orchestrator's real ``source_bundle``.
* Background work is explicitly tracked and cancelled on shutdown.
* Task/project registries are bounded by TTL and entry count.
* WebSocket endpoints require the same configured master credential.
* CORS is configuration-driven and cannot silently enable wildcard credentials.
* Sync subsystem calls are moved off the asyncio event loop correctly.
* Existing public endpoints remain compatible with the previous main.py surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import hmac
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

try:
    from core.game_project import (
        BuildArtifact as CanonicalBuildArtifact,
        BuildStatus as CanonicalBuildStatus,
        GameProject,
        ProjectStatus,
        TargetPlatform,
    )
except Exception:
    CanonicalBuildArtifact = CanonicalBuildStatus = GameProject = ProjectStatus = TargetPlatform = None


# ============================================================================
# RUNTIME CONFIGURATION
# ============================================================================

logger = logging.getLogger("GodNode.Main")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - [GOD NODE CORE] - %(levelname)s - %(message)s"
        )
    )
    logger.addHandler(handler)
logger.setLevel(os.getenv("RIOT_LOG_LEVEL", "INFO").upper())


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    task_ttl_seconds: float = max(60.0, float(os.getenv("RIOT_TASK_TTL_SECONDS", "3600")))
    max_task_entries: int = max(64, int(os.getenv("RIOT_MAX_TASK_ENTRIES", "2048")))
    project_ttl_seconds: float = max(300.0, float(os.getenv("RIOT_PROJECT_TTL_SECONDS", "21600")))
    max_project_entries: int = max(16, int(os.getenv("RIOT_MAX_PROJECT_ENTRIES", "128")))
    max_concurrent_pipelines: int = max(1, int(os.getenv("RIOT_MAX_CONCURRENT_PIPELINES", "2")))
    max_concurrent_builds: int = max(1, int(os.getenv("RIOT_MAX_CONCURRENT_BUILDS", "1")))
    heartbeat_seconds: float = max(5.0, float(os.getenv("RIOT_WS_HEARTBEAT_SECONDS", "20")))
    cors_origins: tuple[str, ...] = tuple(_csv_env("RIOT_CORS_ORIGINS"))


CONFIG = RuntimeConfig()
MASTER_PIN = os.getenv("GOD_MASTER_PIN", "").strip()
if not MASTER_PIN:
    logger.warning(
        "GOD_MASTER_PIN is not configured; protected API/WebSocket operations will be unavailable."
    )


# ============================================================================
# GLOBAL STATE / REGISTRIES
# ============================================================================

SYSTEM_REGISTRY: dict[str, Any] = {}

# In-memory project registry is intentionally bounded. The generated source bundle
# is retained so /api/v2/export can build the exact project returned by orchestration.
_generated_projects: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_project_aliases: dict[str, str] = {}
_project_lock = asyncio.Lock()

# Task registry is a bounded control-plane cache, not an unbounded history database.
_active_tasks: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_task_lock = asyncio.Lock()

# Every long-running background task gets explicitly tracked for graceful shutdown.
_runtime_tasks: set[asyncio.Task[Any]] = set()
_runtime_task_lock = asyncio.Lock()

_pipeline_semaphore = asyncio.Semaphore(CONFIG.max_concurrent_pipelines)
_build_semaphore = asyncio.Semaphore(CONFIG.max_concurrent_builds)


# ============================================================================
# SAFE ASYNC DISPATCH
# ============================================================================

async def call_maybe_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke async callables natively and sync callables in a worker thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)

    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


# ============================================================================
# TASK / PROJECT REGISTRY
# ============================================================================

async def _purge_expired_locked(
    registry: "OrderedDict[str, dict[str, Any]]",
    *,
    ttl_seconds: float,
) -> None:
    now = time.time()
    expired = [
        key
        for key, value in registry.items()
        if now - float(value.get("updated_at", value.get("created_at", now))) > ttl_seconds
    ]
    for key in expired:
        registry.pop(key, None)


async def _ensure_task_capacity_locked() -> None:
    await _purge_expired_locked(_active_tasks, ttl_seconds=CONFIG.task_ttl_seconds)
    while len(_active_tasks) >= CONFIG.max_task_entries:
        _active_tasks.popitem(last=False)


async def _set_task(
    task_id: str,
    *,
    status: str,
    progress: int,
    result: Any = None,
    error: Optional[str] = None,
    kind: str = "pipeline",
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    async with _task_lock:
        current = _active_tasks.get(task_id)
        if current is None:
            await _ensure_task_capacity_locked()
            current = {
                "task_id": task_id,
                "kind": kind,
                "created_at": time.time(),
            }
        current.update(
            {
                "status": status,
                "progress": max(0, min(100, int(progress))),
                "updated_at": time.time(),
                "result": result,
                "error": error,
            }
        )
        if metadata:
            current["metadata"] = dict(metadata)
        _active_tasks[task_id] = current
        _active_tasks.move_to_end(task_id)


async def _get_task(task_id: str) -> Optional[dict[str, Any]]:
    async with _task_lock:
        await _purge_expired_locked(_active_tasks, ttl_seconds=CONFIG.task_ttl_seconds)
        value = _active_tasks.get(task_id)
        return dict(value) if value else None


async def _store_project(project: Mapping[str, Any]) -> None:
    game_id = str(project.get("game_id") or "").strip()
    build_id = str(project.get("build_id") or "").strip()
    if not _ID_RE.fullmatch(game_id) or not _ID_RE.fullmatch(build_id):
        raise ValueError("invalid generated project identity")

    record = dict(project)
    record["game_id"] = game_id
    record["build_id"] = build_id
    record["stored_at"] = time.time()
    record["updated_at"] = time.time()

    async with _project_lock:
        await _purge_expired_locked(_generated_projects, ttl_seconds=CONFIG.project_ttl_seconds)
        old = _generated_projects.pop(game_id, None)
        if old is not None:
            old_build_id = str(old.get("build_id") or "")
            if old_build_id:
                _project_aliases.pop(old_build_id, None)
        while len(_generated_projects) >= CONFIG.max_project_entries:
            evicted_game_id, evicted = _generated_projects.popitem(last=False)
            evicted_build_id = str(evicted.get("build_id") or "")
            if evicted_build_id:
                _project_aliases.pop(evicted_build_id, None)

        _generated_projects[game_id] = record
        _project_aliases[build_id] = game_id



def _validate_identity(value: str, name: str) -> str:
    value = str(value or "").strip()
    if not _ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"Invalid {name}")
    return value



async def _get_project(identity: str) -> Optional[dict[str, Any]]:
    async with _project_lock:
        await _purge_expired_locked(_generated_projects, ttl_seconds=CONFIG.project_ttl_seconds)
        game_id = identity if identity in _generated_projects else _project_aliases.get(identity)
        record = _generated_projects.get(game_id) if game_id else None
        if record is None:
            if identity in _project_aliases:
                _project_aliases.pop(identity, None)
            return None
        record = dict(record)
        record["updated_at"] = time.time()
        _generated_projects.move_to_end(game_id)
        return record


async def _track_runtime_task(task: asyncio.Task[Any]) -> None:
    async with _runtime_task_lock:
        _runtime_tasks.add(task)

    def _done(completed: asyncio.Task[Any]) -> None:
        try:
            _runtime_tasks.discard(completed)
        except Exception:
            pass

    task.add_done_callback(_done)


async def _spawn(coro: Awaitable[Any], *, name: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name)
    await _track_runtime_task(task)
    return task


# ============================================================================
# MODULE BOOTSTRAP
# ============================================================================

# Security & economy
try:
    from security_vault.encryption import GodAuth
    SYSTEM_REGISTRY["vault"] = GodAuth()
    logger.info("Security Vault ONLINE")
except Exception as exc:
    logger.critical("Security Vault failed: %s", exc)

try:
    from economy_vault.billing_core import GodEconomyEngine
    SYSTEM_REGISTRY["economy"] = GodEconomyEngine()
    logger.info("Economy Engine ONLINE")
except Exception as exc:
    logger.warning("Economy Engine unavailable: %s", exc)

# Cloud / database
try:
    from cloud_storage.db_manager import db_vault
    SYSTEM_REGISTRY["db_cloud"] = db_vault
    logger.info("Cloud Database ONLINE")
except Exception as exc:
    logger.warning("Cloud DB unavailable: %s", exc)

# Connection pool
try:
    from god_brain.connection_pool import HTTP_CLIENT
    SYSTEM_REGISTRY["connection_pool"] = HTTP_CLIENT
    logger.info("HTTP Connection Pool ONLINE")
except Exception as exc:
    logger.critical("HTTP Connection Pool failed: %s", exc)

# Dynamic gateway
try:
    from core.gateway import GatewayRouter
    gateway_instance = GatewayRouter()
    SYSTEM_REGISTRY["gateway"] = gateway_instance
    logger.info("Dynamic API Gateway ONLINE")
except Exception as exc:
    logger.critical("API Gateway failed: %s", exc)

# Master intent router
try:
    from the_god_router.intent_classifier import master_router_instance
    SYSTEM_REGISTRY["master_router"] = master_router_instance
    logger.info("Master Intent Router ONLINE")
except Exception as exc:
    logger.critical("Master Router failed: %s", exc)

# Advanced orchestrator — this is the canonical generation boundary.
try:
    from god_brain.orchestrator import GodOrchestrator
    SYSTEM_REGISTRY["orchestrator"] = GodOrchestrator()
    logger.info("Advanced God Orchestrator ONLINE")
except Exception as exc:
    logger.critical("God Orchestrator failed: %s", exc)

# Runtime engine
try:
    from simulation_scheduler.config import SchedulerConfig
    from simulation_scheduler.scheduler import SimulationScheduler
    from core_engine.cpp_bridge import SimulationCPPAdapter
    from multiplayer_nexus.sync_server import init_nexus

    scheduler = SimulationScheduler(SchedulerConfig())
    SYSTEM_REGISTRY["scheduler"] = scheduler

    cpp_adapter = SimulationCPPAdapter(workspace_dir="workspace_cpp")
    SYSTEM_REGISTRY["cpp_bridge"] = cpp_adapter

    SYSTEM_REGISTRY["nexus"] = init_nexus(scheduler)
    logger.info("Simulation Scheduler / C++ Bridge / Multiplayer Nexus ONLINE")
except Exception as exc:
    logger.critical("Core Game Engine bootstrap failed: %s", exc)

try:
    from core_engine.odre_core import reality_core
    SYSTEM_REGISTRY["odre_engine"] = reality_core
    logger.info("ODRE Engine ONLINE")
except Exception as exc:
    logger.warning("ODRE Engine unavailable: %s", exc)

try:
    from assets_factory.world_builder import world_forge
    SYSTEM_REGISTRY["world_forge"] = world_forge
    logger.info("Procedural World Builder ONLINE")
except Exception as exc:
    logger.warning("World Builder unavailable: %s", exc)

# Streaming / live edit / builder / evolution
try:
    from pixel_streaming.stream_manager import PixelStreamEngine
    SYSTEM_REGISTRY["pixel_stream"] = PixelStreamEngine()
    logger.info("Pixel Streaming ONLINE")
except Exception as exc:
    logger.warning("Pixel Streaming unavailable: %s", exc)

try:
    from live_editor.hot_reloader import vibe_coder_engine
    SYSTEM_REGISTRY["hot_reloader"] = vibe_coder_engine
    logger.info("Live Editor ONLINE")
except Exception as exc:
    logger.warning("Live Editor unavailable: %s", exc)

try:
    from game_compilers.universal_builder import game_builder
    SYSTEM_REGISTRY["builder"] = game_builder
    logger.info("Universal Builder ONLINE")
except Exception as exc:
    logger.critical("Universal Builder failed: %s", exc)

try:
    from god_brain.self_evolution import EvolutionEngine
    SYSTEM_REGISTRY["evolution"] = EvolutionEngine()
    logger.info("Self-Evolution Engine ONLINE")
except Exception as exc:
    logger.warning("Self-Evolution unavailable: %s", exc)


# ============================================================================
# AUTHENTICATION
# ============================================================================

def _credential_is_valid(candidate: Optional[str]) -> bool:
    if not MASTER_PIN or not candidate:
        return False
    return bool(hmac.compare_digest(str(candidate), MASTER_PIN))


def _require_pin(candidate: Optional[str]) -> None:
    if not _credential_is_valid(candidate):
        if not MASTER_PIN:
            raise HTTPException(status_code=503, detail="Master credential is not configured")
        raise HTTPException(status_code=403, detail="ACCESS DENIED")


async def _authorize_websocket(websocket: WebSocket) -> bool:
    candidate = websocket.query_params.get("token") or websocket.headers.get("x-god-pin")
    if not _credential_is_valid(candidate):
        await websocket.close(code=1008, reason="Unauthorized")
        return False
    return True


# ============================================================================
# ENGINE TICK LOOP
# ============================================================================

async def engine_tick_loop() -> None:
    """Run the scheduler at ~60Hz without blocking on native execution."""
    logger.info("Master Engine Tick Loop Activated")
    scheduler = SYSTEM_REGISTRY.get("scheduler")
    cpp_bridge = SYSTEM_REGISTRY.get("cpp_bridge")
    if scheduler is None or cpp_bridge is None:
        logger.error("Tick Loop disabled: Scheduler or C++ Bridge missing")
        return

    interval = 1.0 / 60.0
    next_tick = time.perf_counter()

    while True:
        tick_started = time.perf_counter()
        try:
            batches = await asyncio.to_thread(scheduler.build_batches)
            for batch in batches:
                # execute() is explicitly non-blocking in the advanced bridge.
                result = cpp_bridge.execute(batch)
                if isinstance(result, Mapping) and str(result.get("status")) in {
                    "rejected",
                    "unavailable",
                    "failed",
                }:
                    logger.debug("Simulation submission returned %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Engine Tick Error")

        elapsed = time.perf_counter() - tick_started
        next_tick += interval
        sleep_for = max(0.0, next_tick - time.perf_counter())
        if elapsed > interval:
            # Drop accumulated lag instead of creating an infinite catch-up loop.
            next_tick = time.perf_counter()
            sleep_for = 0.0
        await asyncio.sleep(sleep_for)


async def registry_maintenance_loop() -> None:
    """Periodically age out task/project state without blocking request handlers."""
    while True:
        try:
            await asyncio.sleep(30.0)
            async with _task_lock:
                await _purge_expired_locked(_active_tasks, ttl_seconds=CONFIG.task_ttl_seconds)
            async with _project_lock:
                await _purge_expired_locked(
                    _generated_projects,
                    ttl_seconds=CONFIG.project_ttl_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Registry maintenance failure")


# ============================================================================
# FASTAPI LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GOD NODE V2 BOOT SEQUENCE INITIATED")
    connection_pool = SYSTEM_REGISTRY.get("connection_pool")

    if connection_pool is not None:
        await call_maybe_async(connection_pool.startup)

    tracked: list[asyncio.Task[Any]] = []
    tracked.append(await _spawn(engine_tick_loop(), name="riot-engine-tick"))
    tracked.append(await _spawn(registry_maintenance_loop(), name="riot-registry-maintenance"))

    try:
        yield
    finally:
        logger.info("GOD NODE V2 SHUTDOWN SEQUENCE INITIATED")
        async with _runtime_task_lock:
            pending = list(_runtime_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for resource_name in ("cpp_bridge", "nexus", "pixel_stream", "hot_reloader"):
            resource = SYSTEM_REGISTRY.get(resource_name)
            shutdown = getattr(resource, "shutdown", None)
            if shutdown is not None:
                with contextlib.suppress(Exception):
                    await call_maybe_async(shutdown)

        for resource_name in ("orchestrator", "builder", "vault", "economy", "db_cloud", "odre_engine", "world_forge", "evolution"):
            resource = SYSTEM_REGISTRY.get(resource_name)
            shutdown = getattr(resource, "shutdown", None)
            if shutdown is not None:
                with contextlib.suppress(Exception):
                    await call_maybe_async(shutdown)

        if connection_pool is not None:
            with contextlib.suppress(Exception):
                await call_maybe_async(connection_pool.shutdown)


# ============================================================================
# APP
# ============================================================================

app = FastAPI(
    title="Riot / God Node",
    version="11.0-production",
    lifespan=lifespan,
)

# Wildcard + credentials is intentionally forbidden. Empty origins means no CORS
# expansion beyond same-origin requests unless explicitly configured.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CONFIG.cors_origins),
    allow_credentials=bool(CONFIG.cors_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-God-Pin"],
)

if SYSTEM_REGISTRY.get("gateway") is not None:
    app.include_router(SYSTEM_REGISTRY["gateway"].get_router())


# ============================================================================
# SCHEMAS
# ============================================================================

class GodCommandPayload(BaseModel):
    directive: str = Field(..., min_length=1, max_length=2_000_000)
    master_pin: str = Field(..., min_length=1, max_length=256)
    context_data: Dict[str, Any] = Field(default_factory=dict)


class BuildExportPayload(BaseModel):
    game_id: str = Field(..., min_length=1, max_length=128)
    target_platform: str = Field(...)
    master_pin: str = Field(..., min_length=1, max_length=256)

    @field_validator("game_id")
    @classmethod
    def validate_game_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value.strip()):
            raise ValueError("invalid game_id")
        return value.strip()

    @field_validator("target_platform")
    @classmethod
    def validate_target(cls, value: str) -> str:
        value = value.strip().lower()
        aliases = {
            "web_html5": "web",
            "html5": "web",
            "android": "mobile",
            "apk": "mobile",
            "windows": "pc",
            "desktop": "pc",
            "exe": "pc",
        }
        value = aliases.get(value, value)
        if value not in {"web", "mobile", "pc"}:
            raise ValueError("unsupported target_platform")
        return value


class WebRTCOfferPayload(BaseModel):
    player_id: str = Field(..., min_length=1, max_length=128)
    sdp: str = Field(..., min_length=1, max_length=2_000_000)
    type: str = Field(..., min_length=1, max_length=64)


# ============================================================================
# CANONICAL PROJECT ADAPTERS
# ============================================================================

def _canonical_project_from_payload(payload: Mapping[str, Any]) -> Any:
    if GameProject is None:
        return None
    project_data = payload.get("project")
    if isinstance(project_data, Mapping):
        try:
            return GameProject.model_validate(project_data)
        except Exception:
            return None
    return None


def _canonicalize_build_result(project_payload: Mapping[str, Any], build_result: Mapping[str, Any]) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    updated = dict(project_payload)
    project_obj = _canonical_project_from_payload(updated)
    artifact = build_result.get("artifact")
    canonical_artifact = None
    if project_obj is not None and isinstance(artifact, Mapping):
        try:
            platform = {
                "web": TargetPlatform.WEB_HTML5,
                "mobile": TargetPlatform.MOBILE_APK,
                "pc": TargetPlatform.PC_EXE,
            }.get(str(artifact.get("target") or build_result.get("target_platform") or "web").lower(), project_obj.target_platform)
            status = CanonicalBuildStatus.SUCCESS if str(build_result.get("status") or "").upper() == "SUCCESS" else CanonicalBuildStatus.FAILED
            canonical_artifact = CanonicalBuildArtifact(
                platform=platform,
                status=status,
                file_path=str(artifact.get("path")) if artifact.get("path") else None,
                artifact_reference=str(artifact.get("path")) if artifact.get("path") else None,
                file_size_bytes=int(artifact.get("size_bytes") or 0),
                checksum=str(artifact.get("sha256")) if artifact.get("sha256") else None,
                build_logs="\n".join(
                    [str(x.get("stderr") or "") for x in (build_result.get("commands") or []) if isinstance(x, Mapping)]
                ),
            )
            project_obj.add_build_artifact(canonical_artifact)
            try:
                project_obj.transition_to(ProjectStatus.COMPLETED)
            except Exception:
                try:
                    project_obj.transition_to(ProjectStatus.BUILDING)
                    project_obj.transition_to(ProjectStatus.COMPLETED)
                except Exception:
                    pass
            updated["project"] = project_obj.model_dump(mode="json")
            updated["canonical_contract"] = project_obj.snapshot_contract()
        except Exception as exc:
            updated.setdefault("warnings", []).append(f"canonical build artifact attachment failed: {type(exc).__name__}: {exc}")
    return updated, canonical_artifact.model_dump(mode="json") if canonical_artifact is not None else None


def _extract_task_error(result: Any) -> Optional[str]:
    if not isinstance(result, Mapping):
        return "Pipeline returned a non-mapping result"
    for key in ("error", "message"):
        value = result.get(key)
        if value and not isinstance(value, (dict, list)):
            return str(value)
    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        return str(errors[0])
    return None


# ============================================================================
# GENERATION / BUILD PIPELINE
# ============================================================================

async def _execute_pipeline_task(
    task_id: str,
    directive: str,
    context_data: Optional[Mapping[str, Any]] = None,
) -> None:
    async with _pipeline_semaphore:
        await _set_task(task_id, status="ANALYZING", progress=5)

        router = SYSTEM_REGISTRY.get("master_router")
        orchestrator = SYSTEM_REGISTRY.get("orchestrator")
        builder = SYSTEM_REGISTRY.get("builder")

        try:
            if router is None:
                raise RuntimeError("Master Router offline")
            if orchestrator is None:
                raise RuntimeError("God Orchestrator offline")
            if builder is None:
                raise RuntimeError("Universal Builder offline")

            # 1. Intent/resource planning.
            routing_method = router.analyze_and_allocate
            routing_kwargs = {"context_data": dict(context_data or {})}
            try:
                signature = inspect.signature(routing_method)
                if "context_data" in signature.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                    routing_plan = await call_maybe_async(routing_method, directive, **routing_kwargs)
                else:
                    routing_plan = await call_maybe_async(routing_method, directive)
            except (TypeError, ValueError):
                routing_plan = await call_maybe_async(routing_method, directive)
            if not isinstance(routing_plan, Mapping):
                routing_plan = {"raw": routing_plan}

            await _set_task(
                task_id,
                status="ORCHESTRATING_SWARM",
                progress=20,
                result={"routing_plan": dict(routing_plan)},
            )

            # 2. Real source generation + QA via the advanced orchestrator.
            architecture = routing_plan.get("architecture")
            architecture = architecture if isinstance(architecture, Mapping) else {}
            requested_platform = architecture.get("target_platform") or "web"
            requested_platform = str(requested_platform).strip().lower()
            if requested_platform == "web_html5":
                requested_platform = "web"
            elif requested_platform in {"android", "apk"}:
                requested_platform = "mobile"
            elif requested_platform in {"windows", "desktop", "exe"}:
                requested_platform = "pc"

            agent_count = int(architecture.get("agent_count") or (10 if requested_platform != "web" else 5))
            agent_count = max(1, min(agent_count, 32))

            await _set_task(task_id, status="GENERATING_PROJECT", progress=35)
            swarm_result = await call_maybe_async(
                orchestrator.generate_full_game_with_swarm,
                prompt=directive,
                agent_count=agent_count,
            )

            if not isinstance(swarm_result, Mapping):
                raise RuntimeError("Orchestrator returned an invalid result envelope")
            if str(swarm_result.get("status")).upper() != "SUCCESS":
                raise RuntimeError(
                    str(swarm_result.get("error") or swarm_result.get("message") or "Generation failed")
                )

            build_config = swarm_result.get("build_config")
            if not isinstance(build_config, Mapping):
                raise RuntimeError("Orchestrator did not return a real build_config")

            game_id = str(swarm_result.get("game_id") or build_config.get("game_id") or "").strip()
            build_id = str(swarm_result.get("build_id") or build_config.get("build_id") or "").strip()
            if not _ID_RE.fullmatch(game_id) or not _ID_RE.fullmatch(build_id):
                raise RuntimeError("Orchestrator returned invalid project identity")

            # The orchestrator's validated target is the source of truth. Do not silently
            # overwrite it from untrusted request data.
            real_target = str(build_config.get("target_platform") or "web").strip().lower()
            if real_target not in {"web", "mobile", "pc"}:
                raise RuntimeError(f"unsupported generated target: {real_target}")

            project_record = {
                "game_id": game_id,
                "build_id": build_id,
                "target_platform": real_target,
                "build_config": dict(build_config),
                "pipeline": swarm_result.get("pipeline"),
                "qa": swarm_result.get("qa"),
                "project": swarm_result.get("project"),
                "architecture": swarm_result.get("architecture"),
                "assets": swarm_result.get("assets"),
                "world": swarm_result.get("world"),
                "physics": swarm_result.get("physics"),
                "warnings": swarm_result.get("warnings") or [],
                "canonical_contract": swarm_result.get("canonical_contract"),
            }
            await _store_project(project_record)

            await _set_task(
                task_id,
                status="BUILDING",
                progress=70,
                result={
                    "game_id": game_id,
                    "build_id": build_id,
                    "target_platform": real_target,
                },
                metadata={"source": "advanced_orchestrator"},
            )

            # 3. Actual builder invocation. No mock config, no synthetic bytes.
            async with _build_semaphore:
                build_result = await call_maybe_async(
                    builder.build_game,
                    dict(build_config),
                )

            if not isinstance(build_result, Mapping):
                raise RuntimeError("Universal Builder returned an invalid result envelope")

            status = str(build_result.get("status") or "FAILED").upper()
            success = status == "SUCCESS" and bool(build_result.get("artifact"))

            enriched_project_record = dict(project_record)
            enriched_project_record["updated_at"] = time.time()
            enriched_project_record, canonical_artifact = _canonicalize_build_result(enriched_project_record, build_result)
            await _store_project(enriched_project_record)

            final_payload = {
                "routing_plan": dict(routing_plan),
                "generation": {
                    "game_id": game_id,
                    "build_id": build_id,
                    "target_platform": real_target,
                    "pipeline": swarm_result.get("pipeline"),
                    "qa": swarm_result.get("qa"),
                },
                "build": dict(build_result),
                "canonical_build_artifact": canonical_artifact,
            }

            await _set_task(
                task_id,
                status="SUCCESS" if success else status,
                progress=100 if success else 100,
                result=final_payload,
                error=None if success else (dict(build_result).get("errors") or ["Build did not complete successfully"])[0],
            )

        except asyncio.CancelledError:
            await _set_task(task_id, status="CANCELLED", progress=0)
            raise
        except Exception as exc:
            logger.exception("Generation task %s failed", task_id)
            await _set_task(
                task_id,
                status="FAILED",
                progress=100,
                error=f"{type(exc).__name__}: {exc}",
            )


# ============================================================================
# REST ENDPOINTS
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def serve_control_panel() -> HTMLResponse:
    index_path = Path(__file__).resolve().parent / "index.html"
    try:
        content = await asyncio.to_thread(index_path.read_text, encoding="utf-8")
        return HTMLResponse(content=content, status_code=200)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>God Node Active. index.html missing.</h1>",
            status_code=200,
        )


def _service_health_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    critical = {"gateway", "orchestrator", "builder"}
    for name in sorted(set(SYSTEM_REGISTRY) | critical):
        snapshot[name] = "ONLINE" if SYSTEM_REGISTRY.get(name) is not None else "OFFLINE"
    if not MASTER_PIN:
        snapshot["auth"] = "NOT_CONFIGURED"
    else:
        snapshot["auth"] = "READY"
    return snapshot


@app.get("/api/v2/health")
async def health() -> JSONResponse:
    registry_keys = sorted(SYSTEM_REGISTRY.keys())
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": app.version,
            "configured_master_credential": bool(MASTER_PIN),
            "services": registry_keys,
            "service_health": _service_health_snapshot(),
            "limits": {
                "max_concurrent_pipelines": CONFIG.max_concurrent_pipelines,
                "max_concurrent_builds": CONFIG.max_concurrent_builds,
                "max_task_entries": CONFIG.max_task_entries,
                "max_project_entries": CONFIG.max_project_entries,
            },
        },
    )


@app.post("/api/v2/execute")
async def execute_command(payload: GodCommandPayload) -> JSONResponse:
    _require_pin(payload.master_pin)
    task_id = f"TASK_{uuid.uuid4().hex}"
    await _set_task(
        task_id,
        status="QUEUED",
        progress=0,
        kind="generation",
        metadata={"directive_length": len(payload.directive)},
    )
    await _spawn(
        _execute_pipeline_task(task_id, payload.directive, payload.context_data),
        name=f"riot-pipeline-{task_id}",
    )
    return JSONResponse(
        status_code=202,
        content={"status": "PROCESSING", "task_id": task_id},
    )


@app.get("/api/v2/status/{task_id}")
async def check_status(task_id: str, x_god_pin: Optional[str] = Header(default=None)) -> JSONResponse:
    _require_pin(x_god_pin)
    task_id = _validate_identity(task_id, "task_id")
    task = await _get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(status_code=200, content=task)


@app.post("/api/v2/export")
async def trigger_universal_build(payload: BuildExportPayload) -> JSONResponse:
    """Build the exact real source bundle previously generated for a game."""
    _require_pin(payload.master_pin)

    builder = SYSTEM_REGISTRY.get("builder")
    if builder is None:
        raise HTTPException(status_code=503, detail="Universal Builder offline")

    project = await _get_project(payload.game_id)
    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Generated project not found or expired; run /api/v2/execute first",
        )

    build_config = dict(project.get("build_config") or {})
    if not build_config.get("source_bundle"):
        raise HTTPException(status_code=409, detail="Stored project has no real source bundle")

    generated_target = str(build_config.get("target_platform") or "web").lower()
    if generated_target != payload.target_platform:
        # Explicit platform conversion is not safe unless the generated project itself
        # declares a compatible target. Fail closed instead of fabricating an adaptation.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Generated project targets {generated_target!r}; requested "
                f"{payload.target_platform!r}. Regenerate for the desired target."
            ),
        )

    build_task_id = f"BUILD_{uuid.uuid4().hex}"
    await _set_task(
        build_task_id,
        status="QUEUED",
        progress=0,
        kind="build",
        metadata={"game_id": payload.game_id, "target_platform": generated_target},
    )

    async def _run_export() -> None:
        async with _build_semaphore:
            try:
                await _set_task(build_task_id, status="BUILDING", progress=25)
                result = await call_maybe_async(builder.build_game, build_config)
                status = str(result.get("status") if isinstance(result, Mapping) else "FAILED").upper()
                success = status == "SUCCESS" and isinstance(result, Mapping) and bool(result.get("artifact"))
                canonical_artifact = None
                if isinstance(result, Mapping):
                    updated_project, canonical_artifact = _canonicalize_build_result(project, result)
                    await _store_project(updated_project)
                    result = dict(result)
                    result["canonical_build_artifact"] = canonical_artifact
                await _set_task(
                    build_task_id,
                    status="SUCCESS" if success else status,
                    progress=100,
                    result=result,
                    error=None if success else "Build failed or produced no verified artifact",
                )
            except asyncio.CancelledError:
                await _set_task(build_task_id, status="CANCELLED", progress=0)
                raise
            except Exception as exc:
                logger.exception("Build %s failed", build_task_id)
                await _set_task(
                    build_task_id,
                    status="FAILED",
                    progress=100,
                    error=f"{type(exc).__name__}: {exc}",
                )

    await _spawn(_run_export(), name=f"riot-export-{build_task_id}")
    return JSONResponse(
        status_code=202,
        content={"status": "BUILD_QUEUED", "build_task_id": build_task_id},
    )


@app.post("/api/v2/evolve")
async def trigger_self_evolution(pin: str) -> JSONResponse:
    _require_pin(pin)
    evolution_engine = SYSTEM_REGISTRY.get("evolution")
    if evolution_engine is None:
        raise HTTPException(status_code=503, detail="Evolution Engine offline")

    task_id = f"EVOLVE_{uuid.uuid4().hex}"
    await _set_task(task_id, status="QUEUED", progress=0, kind="evolution")

    async def _run_evolve() -> None:
        try:
            await _set_task(task_id, status="RUNNING", progress=10)
            result = await call_maybe_async(evolution_engine.evolve)
            await _set_task(task_id, status="SUCCESS", progress=100, result=result)
        except asyncio.CancelledError:
            await _set_task(task_id, status="CANCELLED", progress=0)
            raise
        except Exception as exc:
            logger.exception("Evolution %s failed", task_id)
            await _set_task(
                task_id,
                status="FAILED",
                progress=100,
                error=f"{type(exc).__name__}: {exc}",
            )

    await _spawn(_run_evolve(), name=f"riot-evolution-{task_id}")
    return JSONResponse(status_code=202, content={"status": "EVOLVE_QUEUED", "task_id": task_id})


@app.post("/api/v2/stream/offer")
async def webrtc_handshake(
    payload: WebRTCOfferPayload,
    x_god_pin: Optional[str] = Header(default=None),
) -> JSONResponse:
    _require_pin(x_god_pin)
    stream_engine = SYSTEM_REGISTRY.get("pixel_stream")
    if stream_engine is None:
        raise HTTPException(status_code=503, detail="Pixel Stream Engine offline")
    try:
        result = await call_maybe_async(
            stream_engine.create_stream_connection,
            player_id=payload.player_id,
            offer_sdp=payload.sdp,
            offer_type=payload.type,
        )
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        logger.exception("WebRTC Handshake failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ============================================================================
# WEBSOCKETS
# ============================================================================

async def _start_heartbeat(websocket: WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(CONFIG.heartbeat_seconds)
            await websocket.send_json({"type": "heartbeat", "ts": time.time()})
    except asyncio.CancelledError:
        return
    except Exception:
        return


@app.websocket("/live-edit/{game_id}")
async def ws_vibe_coder(websocket: WebSocket, game_id: str) -> None:
    game_id = _validate_identity(game_id, "game_id")
    if not await _authorize_websocket(websocket):
        return

    reloader = SYSTEM_REGISTRY.get("hot_reloader")
    if reloader is None:
        await websocket.close(code=1011, reason="Hot Reloader Offline")
        return

    await websocket.accept()
    heartbeat_task = asyncio.create_task(_start_heartbeat(websocket))
    try:
        await call_maybe_async(reloader.connection_manager.connect, game_id, websocket)
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break
            await call_maybe_async(reloader.handle_update, game_id, data)
    except Exception:
        logger.exception("Hot-reload WebSocket failure for %s", game_id)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(Exception):
            await call_maybe_async(reloader.connection_manager.disconnect, game_id)


@app.websocket("/ws/multiplayer/{player_id}")
async def ws_multiplayer_nexus(websocket: WebSocket, player_id: str) -> None:
    player_id = _validate_identity(player_id, "player_id")
    if not await _authorize_websocket(websocket):
        return

    nexus = SYSTEM_REGISTRY.get("nexus")
    if nexus is None:
        await websocket.close(code=1011, reason="Nexus Offline")
        return

    await websocket.accept()
    heartbeat_task = asyncio.create_task(_start_heartbeat(websocket))
    try:
        await call_maybe_async(nexus.connect_player, player_id, websocket)
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break
            await call_maybe_async(nexus.process_action, player_id, data)
    except Exception:
        logger.exception("Multiplayer WebSocket failure for %s", player_id)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(Exception):
            await call_maybe_async(nexus.disconnect_player, player_id)


# ============================================================================
# SERVER ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("RIOT_BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("RIOT_BIND_PORT", "8000")),
        reload=False,
        log_level=os.getenv("RIOT_UVICORN_LOG_LEVEL", "info"),
    )
