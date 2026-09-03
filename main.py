"""
main.py
Riot Control Plane
=================

Application entrypoint for the Riot game-generation platform.

Step 1 goals:
- Harden the public control plane.
- Remove the insecure implicit master PIN.
- Make task execution bounded and observable.
- Keep blocking native/C++ work off the asyncio event loop.
- Make startup/shutdown deterministic.
- Preserve existing subsystem interfaces so deeper modules can be upgraded
  independently in later steps.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# LOGGING
# ============================================================================

LOG_LEVEL = os.getenv("GOD_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=(
        "%(asctime)s | %(levelname)s | %(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger("Riot.Main")

# ============================================================================
# RUNTIME CONFIGURATION
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent

ENVIRONMENT = os.getenv("GOD_ENV", "development").strip().lower()

ENGINE_TICK_HZ = max(
    1,
    min(
        int(os.getenv("GOD_ENGINE_TICK_HZ", "60")),
        240,
    ),
)

TASK_MAX_SIZE = max(
    100,
    int(os.getenv("GOD_MAX_TASKS", "10000")),
)

TASK_TTL_SECONDS = max(
    60,
    int(os.getenv("GOD_TASK_TTL", "3600")),
)

MAX_DIRECTIVE_LENGTH = max(
    1024,
    int(os.getenv("GOD_MAX_DIRECTIVE_LENGTH", "50000")),
)

MAX_CONTEXT_KEYS = max(
    16,
    int(os.getenv("GOD_MAX_CONTEXT_KEYS", "256")),
)

ALLOWED_ORIGINS_RAW = os.getenv(
    "GOD_CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5173",
)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in ALLOWED_ORIGINS_RAW.split(",")
    if origin.strip()
]

# ============================================================================
# SECURITY
# ============================================================================

# There is deliberately NO fallback password.
#
# Production and staging require GOD_MASTER_PIN.
#
# Development may opt into an ephemeral generated secret by setting:
#
#   GOD_ALLOW_EPHEMERAL_DEV_SECRET=true
#
# The generated secret is printed once to the local log because otherwise a
# local developer would have no way to authenticate. This mode must never be
# used in production.
#
MASTER_PIN = os.getenv("GOD_MASTER_PIN", "").strip()

ALLOW_EPHEMERAL_DEV_SECRET = (
    os.getenv(
        "GOD_ALLOW_EPHEMERAL_DEV_SECRET",
        "false",
    ).strip().lower()
    == "true"
)

if not MASTER_PIN:
    if ENVIRONMENT in {"development", "dev"} and ALLOW_EPHEMERAL_DEV_SECRET:
        MASTER_PIN = secrets.token_urlsafe(24)
        logger.warning(
            "Development mode is using an ephemeral administrative secret. "
            "Set GOD_MASTER_PIN for persistent authentication."
        )
        logger.warning("Ephemeral GOD_MASTER_PIN: %s", MASTER_PIN)
    else:
        raise RuntimeError(
            "GOD_MASTER_PIN is required. "
            "Refusing to start without an administrator secret. "
            "For local development only, set "
            "GOD_ALLOW_EPHEMERAL_DEV_SECRET=true."
        )

# Never permit an obviously weak administrative secret.
if len(MASTER_PIN) < 12:
    raise RuntimeError(
        "GOD_MASTER_PIN must contain at least 12 characters."
    )


def _authorize(
    provided_secret: str | None,
    *,
    header_secret: str | None = None,
) -> None:
    """
    Constant-time authorization check.

    Header authentication is preferred because it avoids repeatedly placing
    the administrative secret into request JSON bodies. The legacy body value
    remains supported for compatibility with the current UI.
    """

    candidate = (
        header_secret.strip()
        if header_secret
        else (provided_secret or "").strip()
    )

    if not candidate or not hmac.compare_digest(candidate, MASTER_PIN):
        raise HTTPException(
            status_code=403,
            detail="ACCESS DENIED",
        )


# ============================================================================
# COMPONENT REGISTRY
# ============================================================================

SYSTEM_REGISTRY: dict[str, Any] = {}
BOOT_FAILURES: dict[str, str] = {}


def _register_component(
    name: str,
    factory: Any,
    *,
    critical: bool = False,
) -> None:
    """
    Register one Riot subsystem without bringing down optional components.
    """

    try:
        SYSTEM_REGISTRY[name] = factory()
        logger.info("Subsystem online: %s", name)
    except Exception as exc:
        BOOT_FAILURES[name] = str(exc)

        if critical:
            logger.critical(
                "Critical subsystem failed: %s | %s",
                name,
                exc,
            )
        else:
            logger.warning(
                "Optional subsystem failed: %s | %s",
                name,
                exc,
            )


# ============================================================================
# SUBSYSTEM BOOTSTRAP
# ============================================================================

def bootstrap_subsystems() -> None:
    """
    Construct existing Riot services.

    This first-stage refactor keeps the current interfaces intact. A later
    step will move this into a dedicated dependency container.
    """

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------

    try:
        from security_vault.encryption import GodAuth

        SYSTEM_REGISTRY["vault"] = GodAuth()

        logger.info("Security vault online.")
    except Exception as exc:
        BOOT_FAILURES["vault"] = str(exc)
        logger.critical("Security vault failed: %s", exc)

    # ------------------------------------------------------------------
    # Economy
    # ------------------------------------------------------------------

    try:
        from economy_vault.billing_core import GodEconomyEngine

        SYSTEM_REGISTRY["economy"] = GodEconomyEngine()

        logger.info("Economy engine online.")
    except Exception as exc:
        BOOT_FAILURES["economy"] = str(exc)
        logger.warning("Economy engine unavailable: %s", exc)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    try:
        from cloud_storage.db_manager import db_vault

        SYSTEM_REGISTRY["db_cloud"] = db_vault

        logger.info("Storage layer online.")
    except Exception as exc:
        BOOT_FAILURES["db_cloud"] = str(exc)
        logger.warning("Storage layer unavailable: %s", exc)

    # ------------------------------------------------------------------
    # Shared HTTP
    # ------------------------------------------------------------------

    try:
        from god_brain.connection_pool import HTTP_CLIENT

        SYSTEM_REGISTRY["connection_pool"] = HTTP_CLIENT

        logger.info("HTTP connection pool online.")
    except Exception as exc:
        BOOT_FAILURES["connection_pool"] = str(exc)
        logger.critical(
            "HTTP connection pool failed: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # AI gateway
    # ------------------------------------------------------------------

    try:
        from core.gateway import GatewayRouter

        gateway = GatewayRouter()

        SYSTEM_REGISTRY["gateway"] = gateway

        logger.info("AI gateway online.")
    except Exception as exc:
        BOOT_FAILURES["gateway"] = str(exc)
        logger.critical("AI gateway failed: %s", exc)

    # ------------------------------------------------------------------
    # Master intent router
    # ------------------------------------------------------------------

    try:
        from the_god_router.intent_classifier import (
            master_router_instance,
        )

        SYSTEM_REGISTRY["master_router"] = master_router_instance

        logger.info("Intent router online.")
    except Exception as exc:
        BOOT_FAILURES["master_router"] = str(exc)
        logger.critical(
            "Intent router failed: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # AI orchestrator
    # ------------------------------------------------------------------

    try:
        from god_brain.orchestrator import GodOrchestrator

        SYSTEM_REGISTRY["orchestrator"] = GodOrchestrator()

        logger.info("AI orchestrator online.")
    except Exception as exc:
        BOOT_FAILURES["orchestrator"] = str(exc)
        logger.critical(
            "AI orchestrator failed: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # Simulation / multiplayer
    # ------------------------------------------------------------------

    try:
        from simulation_scheduler.config import SchedulerConfig
        from simulation_scheduler.scheduler import SimulationScheduler
        from core_engine.cpp_bridge import SimulationCPPAdapter
        from multiplayer_nexus.sync_server import init_nexus

        engine_config = SchedulerConfig()

        scheduler = SimulationScheduler(
            engine_config,
        )

        cpp_bridge = SimulationCPPAdapter(
            workspace_dir=str(
                PROJECT_ROOT / "workspace_cpp",
            ),
        )

        nexus = init_nexus(
            scheduler,
        )

        SYSTEM_REGISTRY["scheduler"] = scheduler
        SYSTEM_REGISTRY["cpp_bridge"] = cpp_bridge
        SYSTEM_REGISTRY["nexus"] = nexus

        logger.info(
            "Simulation and multiplayer runtime online."
        )

    except Exception as exc:
        BOOT_FAILURES["simulation"] = str(exc)
        logger.critical(
            "Simulation subsystem failed: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # ODRE
    # ------------------------------------------------------------------

    try:
        from core_engine.odre_core import reality_core

        SYSTEM_REGISTRY["odre_engine"] = reality_core

        logger.info("ODRE subsystem online.")
    except Exception as exc:
        BOOT_FAILURES["odre_engine"] = str(exc)
        logger.warning(
            "ODRE subsystem unavailable: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # Procedural world builder
    # ------------------------------------------------------------------

    try:
        from assets_factory.world_builder import world_forge

        SYSTEM_REGISTRY["world_forge"] = world_forge

        logger.info("World builder online.")
    except Exception as exc:
        BOOT_FAILURES["world_forge"] = str(exc)
        logger.warning(
            "World builder unavailable: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # Pixel streaming
    # ------------------------------------------------------------------

    try:
        from pixel_streaming.stream_manager import PixelStreamEngine

        SYSTEM_REGISTRY["pixel_stream"] = PixelStreamEngine()

        logger.info("Pixel streaming subsystem online.")
    except Exception as exc:
        BOOT_FAILURES["pixel_stream"] = str(exc)
        logger.warning(
            "Pixel streaming unavailable: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # Live editor
    # ------------------------------------------------------------------

    try:
        from live_editor.hot_reloader import vibe_coder_engine

        SYSTEM_REGISTRY["hot_reloader"] = vibe_coder_engine

        logger.info("Live editor subsystem online.")
    except Exception as exc:
        BOOT_FAILURES["hot_reloader"] = str(exc)
        logger.warning(
            "Live editor unavailable: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # Compiler
    # ------------------------------------------------------------------

    try:
        from game_compilers.universal_builder import game_builder

        SYSTEM_REGISTRY["builder"] = game_builder

        logger.info("Game builder online.")
    except Exception as exc:
        BOOT_FAILURES["builder"] = str(exc)
        logger.warning(
            "Game builder unavailable: %s",
            exc,
        )

    # ------------------------------------------------------------------
    # Self evolution
    # ------------------------------------------------------------------

    try:
        from god_brain.self_evolution import EvolutionEngine

        SYSTEM_REGISTRY["evolution"] = EvolutionEngine()

        logger.info("Self-evolution subsystem online.")
    except Exception as exc:
        BOOT_FAILURES["evolution"] = str(exc)
        logger.warning(
            "Self-evolution unavailable: %s",
            exc,
        )


# Bootstrap once during module import to preserve the current application's
# public interfaces. The next architecture step will replace this with a
# proper application container and dependency-injection lifecycle.
bootstrap_subsystems()

# ============================================================================
# TASK STORE
# ============================================================================


class TaskStore:
    """
    Bounded in-memory task state.

    This is intentionally still process-local for Step 1. It prevents the
    current unbounded dictionary from growing forever and gives us TTL/eviction
    semantics. A distributed job store will replace it later.
    """

    def __init__(
        self,
        *,
        max_size: int,
        ttl_seconds: int,
    ) -> None:
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

        self._tasks: dict[str, dict[str, Any]] = {}
        self._updated_at: dict[str, float] = {}
        self._lock = RLock()

    def _cleanup_locked(self) -> None:
        now = time.monotonic()

        expired = [
            task_id
            for task_id, updated_at in self._updated_at.items()
            if now - updated_at > self.ttl_seconds
        ]

        for task_id in expired:
            self._tasks.pop(task_id, None)
            self._updated_at.pop(task_id, None)

        overflow = len(self._tasks) - self.max_size

        if overflow > 0:
            oldest = sorted(
                self._updated_at.items(),
                key=lambda item: item[1],
            )[:overflow]

            for task_id, _ in oldest:
                self._tasks.pop(task_id, None)
                self._updated_at.pop(task_id, None)

    def create(self, task_id: str) -> None:
        with self._lock:
            self._cleanup_locked()

            now = time.monotonic()

            self._tasks[task_id] = {
                "task_id": task_id,
                "status": "QUEUED",
                "progress": 0,
                "result": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }

            self._updated_at[task_id] = now

    def update(
        self,
        task_id: str,
        **changes: Any,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None:
                return

            task.update(changes)

            now = time.time()

            task["updated_at"] = now
            self._updated_at[task_id] = time.monotonic()

            self._cleanup_locked()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()

            task = self._tasks.get(task_id)

            if task is None:
                return None

            return dict(task)


TASKS = TaskStore(
    max_size=TASK_MAX_SIZE,
    ttl_seconds=TASK_TTL_SECONDS,
)

RUNNING_TASKS: dict[str, asyncio.Task[Any]] = {}

# ============================================================================
# REQUEST SCHEMAS
# ============================================================================


class SecurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GodCommandPayload(SecurePayload):
    directive: str = Field(
        ...,
        min_length=1,
        max_length=MAX_DIRECTIVE_LENGTH,
    )

    # Legacy compatibility field.
    # Header authentication is preferred.
    master_pin: str | None = Field(
        default=None,
        max_length=256,
    )

    context_data: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_CONTEXT_KEYS,
    )


class BuildExportPayload(SecurePayload):
    game_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
    )

    target_platform: str = Field(
        pattern="^(web|mobile|pc)$",
    )

    master_pin: str | None = Field(
        default=None,
        max_length=256,
    )


class WebRTCOfferPayload(SecurePayload):
    player_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
    )

    sdp: str = Field(
        ...,
        min_length=1,
        max_length=500_000,
    )

    type: str = Field(
        ...,
        min_length=1,
        max_length=32,
    )


# ============================================================================
# ASYNC TASK EXECUTION
# ============================================================================


async def process_god_command_task(
    task_id: str,
    directive: str,
    context_data: dict[str, Any] | None = None,
) -> None:
    """
    Full generation pipeline.

    The current underlying orchestrator interface is preserved.
    """

    TASKS.update(
        task_id,
        status="ANALYZING",
        progress=10,
    )

    router = SYSTEM_REGISTRY.get("master_router")
    orchestrator = SYSTEM_REGISTRY.get("orchestrator")

    try:
        if router is None:
            raise RuntimeError(
                "Master intent router is unavailable."
            )

        routing_plan = await router.analyze_and_allocate(
            directive,
        )

        TASKS.update(
            task_id,
            status="ORCHESTRATING_SWARM",
            progress=30,
        )

        if orchestrator is None:
            raise RuntimeError(
                "AI orchestrator is unavailable."
            )

        architecture = routing_plan.get(
            "architecture",
            {},
        )

        target_platform = architecture.get(
            "target_platform",
            "web_html5",
        )

        agent_count = (
            10
            if target_platform != "web_html5"
            else 5
        )

        swarm_result = (
            await orchestrator.generate_full_game_with_swarm(
                prompt=directive,
                agent_count=agent_count,
            )
        )

        TASKS.update(
            task_id,
            progress=90,
        )

        if swarm_result.get("status") == "FAILED":
            raise RuntimeError(
                swarm_result.get(
                    "error",
                    "Swarm execution failed.",
                )
            )

        TASKS.update(
            task_id,
            status="SUCCESS",
            progress=100,
            result={
                "routing_plan": routing_plan,
                "final_build": swarm_result.get(
                    "final_build",
                ),
                "context_keys": sorted(
                    (context_data or {}).keys()
                ),
            },
        )

    except asyncio.CancelledError:
        TASKS.update(
            task_id,
            status="CANCELLED",
            progress=0,
        )
        raise

    except Exception:
        logger.exception(
            "Generation task failed: %s",
            task_id,
        )

        TASKS.update(
            task_id,
            status="FAILED",
            result={
                "error": (
                    "Generation pipeline failed. "
                    "Check server logs using the task ID."
                )
            },
        )


def _task_done_callback(
    task_id: str,
) -> Any:
    def _callback(
        task: asyncio.Task[Any],
    ) -> None:
        RUNNING_TASKS.pop(
            task_id,
            None,
        )

        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "Unhandled background task failure: %s",
                task_id,
            )

    return _callback


# ============================================================================
# ENGINE LOOP
# ============================================================================


async def engine_tick_loop() -> None:
    """
    Fixed-rate engine scheduler.

    IMPORTANT:
    Native/C++ execution is dispatched with asyncio.to_thread() so the
    FastAPI event loop isn't blocked by subprocess or native execution.
    """

    scheduler = SYSTEM_REGISTRY.get(
        "scheduler",
    )

    cpp_bridge = SYSTEM_REGISTRY.get(
        "cpp_bridge",
    )

    if scheduler is None or cpp_bridge is None:
        logger.error(
            "Engine loop unavailable: scheduler or C++ bridge missing."
        )
        return

    tick_interval = 1.0 / ENGINE_TICK_HZ
    next_tick = time.monotonic()

    logger.info(
        "Engine tick loop started at %d Hz.",
        ENGINE_TICK_HZ,
    )

    while True:
        try:
            batches = scheduler.build_batches()

            for batch in batches:
                # Do NOT execute native/subprocess work directly on the
                # FastAPI asyncio event loop.
                await asyncio.to_thread(
                    cpp_bridge.execute,
                    batch,
                )

        except asyncio.CancelledError:
            logger.info(
                "Engine tick loop stopping."
            )
            raise

        except Exception:
            logger.exception(
                "Engine tick failed."
            )

        next_tick += tick_interval

        sleep_for = next_tick - time.monotonic()

        if sleep_for > 0:
            await asyncio.sleep(
                sleep_for,
            )
        else:
            # The loop missed its deadline. Reset the schedule instead of
            # creating an ever-growing backlog.
            next_tick = time.monotonic()


# ============================================================================
# APPLICATION LIFECYCLE
# ============================================================================


@asynccontextmanager
async def lifespan(
    application: FastAPI,
):
    """
    Deterministic Riot runtime lifecycle.
    """

    logger.info(
        "Riot control plane starting."
    )

    connection_pool = SYSTEM_REGISTRY.get(
        "connection_pool",
    )

    if connection_pool is not None:
        await connection_pool.startup()

    engine_task = asyncio.create_task(
        engine_tick_loop(),
        name="riot-engine-tick-loop",
    )

    try:
        yield

    finally:
        logger.info(
            "Riot control plane shutting down."
        )

        engine_task.cancel()

        try:
            await engine_task
        except asyncio.CancelledError:
            pass

        active = list(
            RUNNING_TASKS.values()
        )

        for task in active:
            task.cancel()

        if active:
            await asyncio.gather(
                *active,
                return_exceptions=True,
            )

        RUNNING_TASKS.clear()

        if connection_pool is not None:
            await connection_pool.shutdown()

        logger.info(
            "Riot shutdown complete."
        )


# ============================================================================
# FASTAPI
# ============================================================================


app = FastAPI(
    title="Riot Control Plane",
    version="11.0",
    description=(
        "Secure AI game-generation and simulation control plane."
    ),
    lifespan=lifespan,
)

# A wildcard origin with credentials enabled is intentionally prohibited.
#
# If you explicitly configure "*" we disable credentials instead of creating
# the unsafe "*" + credentials combination.
USE_WILDCARD_CORS = ALLOWED_ORIGINS == ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=not USE_WILDCARD_CORS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-God-Master-Key",
    ],
)


@app.middleware("http")
async def security_headers(
    request: Any,
    call_next: Any,
) -> Any:
    response = await call_next(request)

    response.headers.setdefault(
        "X-Content-Type-Options",
        "nosniff",
    )

    response.headers.setdefault(
        "X-Frame-Options",
        "DENY",
    )

    response.headers.setdefault(
        "Referrer-Policy",
        "no-referrer",
    )

    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )

    return response


# ============================================================================
# GATEWAY ROUTER
# ============================================================================

gateway = SYSTEM_REGISTRY.get("gateway")

if gateway is not None:
    app.include_router(
        gateway.get_router(),
    )


# ============================================================================
# HEALTH
# ============================================================================


@app.get(
    "/healthz",
    tags=["System"],
)
async def healthz() -> JSONResponse:
    critical_components = {
        "connection_pool",
        "gateway",
        "master_router",
        "orchestrator",
        "scheduler",
        "cpp_bridge",
        "nexus",
    }

    missing = sorted(
        component
        for component in critical_components
        if component not in SYSTEM_REGISTRY
    )

    status = (
        "healthy"
        if not missing
        else "degraded"
    )

    return JSONResponse(
        status_code=200,
        content={
            "status": status,
            "environment": ENVIRONMENT,
            "engine_tick_hz": ENGINE_TICK_HZ,
            "components_online": sorted(
                SYSTEM_REGISTRY.keys(),
            ),
            "critical_components_missing": missing,
            "boot_failures": {
                name: error
                for name, error in BOOT_FAILURES.items()
                if name in critical_components
            },
            "active_tasks": len(
                RUNNING_TASKS,
            ),
        },
    )


# ============================================================================
# CONTROL PANEL
# ============================================================================


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def serve_control_panel() -> HTMLResponse:
    index_path = (
        PROJECT_ROOT / "index.html"
    )

    if not index_path.is_file():
        return HTMLResponse(
            content=(
                "<h1>Riot Control Plane Online</h1>"
                "<p>index.html is not installed.</p>"
            ),
            status_code=200,
        )

    return HTMLResponse(
        content=index_path.read_text(
            encoding="utf-8",
        ),
        status_code=200,
    )


# ============================================================================
# GAME GENERATION
# ============================================================================


@app.post(
    "/api/v2/execute",
)
async def execute_command(
    payload: GodCommandPayload,
    x_god_master_key: str | None = Header(
        default=None,
        alias="X-God-Master-Key",
    ),
) -> JSONResponse:
    _authorize(
        payload.master_pin,
        header_secret=x_god_master_key,
    )

    directive = payload.directive.strip()

    if not directive:
        raise HTTPException(
            status_code=422,
            detail="Directive cannot be empty.",
        )

    task_id = (
        f"TASK_{uuid.uuid4().hex}"
    )

    TASKS.create(
        task_id,
    )

    task = asyncio.create_task(
        process_god_command_task(
            task_id=task_id,
            directive=directive,
            context_data=payload.context_data,
        ),
        name=f"riot-generation-{task_id}",
    )

    RUNNING_TASKS[task_id] = task

    task.add_done_callback(
        _task_done_callback(
            task_id,
        ),
    )

    return JSONResponse(
        status_code=202,
        content={
            "status": "PROCESSING",
            "task_id": task_id,
        },
    )


# ============================================================================
# TASK STATUS
# ============================================================================


@app.get(
    "/api/v2/status/{task_id}",
)
async def check_status(
    task_id: str,
) -> JSONResponse:
    task = TASKS.get(
        task_id,
    )

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    return JSONResponse(
        status_code=200,
        content=task,
    )


# ============================================================================
# EXPORT
# ============================================================================


@app.post(
    "/api/v2/export",
)
async def trigger_universal_build(
    payload: BuildExportPayload,
    x_god_master_key: str | None = Header(
        default=None,
        alias="X-God-Master-Key",
    ),
) -> JSONResponse:
    _authorize(
        payload.master_pin,
        header_secret=x_god_master_key,
    )

    builder = SYSTEM_REGISTRY.get(
        "builder",
    )

    if builder is None:
        raise HTTPException(
            status_code=503,
            detail="Universal builder unavailable.",
        )

    # The artifact store integration is intentionally kept for the next
    # file-by-file upgrade. We preserve the current builder contract here.
    build_config = {
        "game_id": payload.game_id,
        "target_platform": payload.target_platform,
        "html_content": (
            "<!-- Riot build pipeline placeholder. -->"
            "<canvas id='game'></canvas>"
        ),
        "js_content": (
            "console.log('Riot build pipeline online');"
        ),
    }

    try:
        result = await builder.build_game(
            build_config,
        )

        return JSONResponse(
            status_code=200,
            content=result,
        )

    except Exception:
        logger.exception(
            "Build failed for game %s",
            payload.game_id,
        )

        raise HTTPException(
            status_code=500,
            detail="Build pipeline failed.",
        )


# ============================================================================
# SELF EVOLUTION
# ============================================================================


@app.post(
    "/api/v2/evolve",
)
async def trigger_self_evolution(
    pin: str | None = None,
    x_god_master_key: str | None = Header(
        default=None,
        alias="X-God-Master-Key",
    ),
) -> JSONResponse:
    _authorize(
        pin,
        header_secret=x_god_master_key,
    )

    evolution_engine = SYSTEM_REGISTRY.get(
        "evolution",
    )

    if evolution_engine is None:
        raise HTTPException(
            status_code=503,
            detail="Evolution engine unavailable.",
        )

    try:
        result = await evolution_engine.evolve()

        return JSONResponse(
            status_code=200,
            content=result,
        )

    except Exception:
        logger.exception(
            "Self-evolution request failed."
        )

        raise HTTPException(
            status_code=500,
            detail="Self-evolution pipeline failed.",
        )


# ============================================================================
# WEBRTC
# ============================================================================


@app.post(
    "/api/v2/stream/offer",
)
async def webrtc_handshake(
    payload: WebRTCOfferPayload,
) -> JSONResponse:
    stream_engine = SYSTEM_REGISTRY.get(
        "pixel_stream",
    )

    if stream_engine is None:
        raise HTTPException(
            status_code=503,
            detail="Pixel streaming unavailable.",
        )

    try:
        result = (
            await stream_engine.create_stream_connection(
                player_id=payload.player_id,
                offer_sdp=payload.sdp,
                offer_type=payload.type,
            )
        )

        return JSONResponse(
            status_code=200,
            content=result,
        )

    except Exception:
        logger.exception(
            "WebRTC handshake failed for player %s",
            payload.player_id,
        )

        raise HTTPException(
            status_code=500,
            detail="WebRTC handshake failed.",
        )


# ============================================================================
# LIVE EDITOR
# ============================================================================


@app.websocket(
    "/live-edit/{game_id}",
)
async def ws_vibe_coder(
    websocket: WebSocket,
    game_id: str,
) -> None:
    reloader = SYSTEM_REGISTRY.get(
        "hot_reloader",
    )

    if reloader is None:
        await websocket.close(
            code=1011,
            reason="Hot Reloader Offline",
        )
        return

    await reloader.connection_manager.connect(
        game_id,
        websocket,
    )

    try:
        while True:
            await websocket.receive_json()

    except WebSocketDisconnect:
        reloader.connection_manager.disconnect(
            game_id,
        )

    except Exception:
        logger.exception(
            "Live editor WebSocket failed: %s",
            game_id,
        )

        reloader.connection_manager.disconnect(
            game_id,
        )


# ============================================================================
# MULTIPLAYER
# ============================================================================


@app.websocket(
    "/ws/multiplayer/{player_id}",
)
async def ws_multiplayer_nexus(
    websocket: WebSocket,
    player_id: str,
) -> None:
    nexus = SYSTEM_REGISTRY.get(
        "nexus",
    )

    if nexus is None:
        await websocket.close(
            code=1011,
            reason="Nexus Offline",
        )
        return

    await nexus.connect_player(
        player_id,
        websocket,
    )

    try:
        while True:
            data = await websocket.receive_json()

            await nexus.process_action(
                player_id,
                data,
            )

    except WebSocketDisconnect:
        nexus.disconnect_player(
            player_id,
        )

    except Exception:
        logger.exception(
            "Multiplayer WebSocket failed: %s",
            player_id,
        )

        nexus.disconnect_player(
            player_id,
        )


# ============================================================================
# LOCAL DEVELOPMENT ENTRYPOINT
# ============================================================================


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv(
            "GOD_HOST",
            "0.0.0.0",
        ),
        port=int(
            os.getenv(
                "GOD_PORT",
                "8000",
            )
        ),
        reload=(
            ENVIRONMENT
            in {"development", "dev"}
        ),
        log_level=LOG_LEVEL.lower(),
    )
