"""
Riot / God Node — Ultra Production Director Agent
==================================================

Purpose
-------
This is the authoritative master-planning brain for Riot's generation swarm.

It is deliberately more than a prompt wrapper.  The Director acts as a small
planning compiler:

    user concept
        -> normalized intent
        -> deterministic requirement extraction
        -> capability synthesis
        -> performance/budget synthesis
        -> dependency DAG construction
        -> model planning call
        -> structural validation
        -> canonical ArchitecturePlan projection
        -> downstream work-package contract

Compatibility
-------------
The class remains compatible with the current GodBaseAgent contract:

    await DirectorAgent().perform_role(...)

Provider retries, provider health, circuit breaking, connection pooling and
model discovery remain below this layer in GatewayRouter/SDK.

Important
---------
This agent never claims that code, assets, compilation, rendering, QA, or a
build exists unless the upstream context contains evidence.  It plans work;
execution is owned by downstream runtime components.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.game_project import (
    ArchitecturePlan,
    CapabilityStatus,
    PerformanceBudget,
    RuntimeTarget,
    RuntimeType,
    TargetPlatform,
)
from god_brain.agents.base_agent import GodBaseAgent


# ============================================================================
# GLOBAL LIMITS
# ============================================================================

_MAX_ID_LENGTH = 96
_MAX_REQUIREMENTS = 96
_MAX_SYSTEMS = 96
_MAX_WORK_PACKAGES = 128
_MAX_CAPABILITIES = 96
_MAX_CONSTRAINTS = 96
_MAX_BUILD_STEPS = 96


# ============================================================================
# ENUMS / INTERNAL PLANNING TYPES
# ============================================================================

class ComplexityClass(str, Enum):
    MICRO = "micro"
    STANDARD = "standard"
    ADVANCED = "advanced"
    HEAVY = "heavy"
    EXTREME = "extreme"


class RequirementKind(str, Enum):
    GAMEPLAY = "gameplay"
    WORLD = "world"
    ASSET = "asset"
    PHYSICS = "physics"
    AUDIO = "audio"
    UI = "ui"
    AI = "ai"
    NETWORK = "network"
    SAVE = "save"
    PERFORMANCE = "performance"
    BUILD = "build"
    QA = "qa"
    SECURITY = "security"


@dataclass(frozen=True, slots=True)
class PlanningRequirement:
    requirement_id: str
    kind: RequirementKind
    importance: int
    reason: str
    evidence: str


@dataclass(frozen=True, slots=True)
class WorkPackage:
    package_id: str
    owner: str
    priority: int
    depends_on: Tuple[str, ...]
    deliverables: Tuple[str, ...]
    acceptance: Tuple[str, ...]


# ============================================================================
# MODEL CONTRACTS
# ============================================================================

class DirectorModel(BaseModel):
    """
    Small LLM-facing model.

    Deliberately excludes execution evidence and implementation claims.  The
    deterministic compiler below supplies those only from real context.
    """

    model_config = ConfigDict(extra="ignore")

    project_summary: str = ""
    game_genre: str = "unknown"
    visual_style: str = "unknown"
    complexity_class: str = "advanced"
    core_gameplay_loop: str = ""
    systems: List[Dict[str, Any]] = Field(default_factory=list)
    required_agents: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    technical_constraints: List[str] = Field(default_factory=list)
    build_steps: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    unresolved_decisions: List[str] = Field(default_factory=list)


class DirectorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str = "SUCCESS"
    data: DirectorModel = Field(default_factory=DirectorModel)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


# ============================================================================
# DETERMINISTIC HELPERS
# ============================================================================

def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(_clean(x) for x in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"[:_MAX_ID_LENGTH]


def _bounded_unique(values: Iterable[Any], limit: int) -> List[str]:
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


def _normalize_platform(value: Any) -> TargetPlatform:
    raw = _clean(value, "web").lower()
    aliases = {
        "web": TargetPlatform.WEB_HTML5,
        "html5": TargetPlatform.WEB_HTML5,
        "browser": TargetPlatform.WEB_HTML5,
        "web_html5": TargetPlatform.WEB_HTML5,
        "android": TargetPlatform.MOBILE_APK,
        "mobile": TargetPlatform.MOBILE_APK,
        "apk": TargetPlatform.MOBILE_APK,
        "mobile_apk": TargetPlatform.MOBILE_APK,
        "pc": TargetPlatform.PC_EXE,
        "desktop": TargetPlatform.PC_EXE,
        "windows": TargetPlatform.PC_EXE,
        "exe": TargetPlatform.PC_EXE,
        "pc_exe": TargetPlatform.PC_EXE,
        "cloud": TargetPlatform.CLOUD_STREAM,
        "stream": TargetPlatform.CLOUD_STREAM,
        "cloud_stream": TargetPlatform.CLOUD_STREAM,
    }
    return aliases.get(raw, TargetPlatform.WEB_HTML5)


def _runtime_for(platform: TargetPlatform) -> RuntimeType:
    if platform is TargetPlatform.WEB_HTML5:
        return RuntimeType.WEB
    if platform is TargetPlatform.MOBILE_APK:
        return RuntimeType.NATIVE_MOBILE
    if platform is TargetPlatform.PC_EXE:
        return RuntimeType.DESKTOP
    return RuntimeType.CLOUD_STREAM


def _platform_budget(platform: TargetPlatform, complexity: ComplexityClass) -> PerformanceBudget:
    """
    Hardware-aware baseline budget.

    These are planning ceilings, not promises.  They intentionally bias toward
    safe execution and leave room for generated content.
    """

    if platform is TargetPlatform.WEB_HTML5:
        defaults = {
            ComplexityClass.MICRO: (60, 256, 32, 800, 120),
            ComplexityClass.STANDARD: (60, 384, 64, 1200, 180),
            ComplexityClass.ADVANCED: (60, 512, 96, 1800, 220),
            ComplexityClass.HEAVY: (60, 768, 128, 2200, 260),
            ComplexityClass.EXTREME: (60, 1024, 160, 2600, 320),
        }[complexity]
    elif platform is TargetPlatform.MOBILE_APK:
        defaults = {
            ComplexityClass.MICRO: (60, 384, 24, 600, 90),
            ComplexityClass.STANDARD: (60, 512, 48, 900, 120),
            ComplexityClass.ADVANCED: (60, 768, 72, 1300, 160),
            ComplexityClass.HEAVY: (60, 1024, 96, 1600, 180),
            ComplexityClass.EXTREME: (60, 1536, 128, 2000, 220),
        }[complexity]
    elif platform is TargetPlatform.PC_EXE:
        defaults = {
            ComplexityClass.MICRO: (60, 512, 64, 1200, 160),
            ComplexityClass.STANDARD: (60, 1024, 128, 2200, 220),
            ComplexityClass.ADVANCED: (60, 2048, 256, 4500, 300),
            ComplexityClass.HEAVY: (60, 4096, 512, 8000, 450),
            ComplexityClass.EXTREME: (120, 8192, 1024, 14000, 600),
        }[complexity]
    else:
        defaults = {
            ComplexityClass.MICRO: (60, 512, 64, 1500, 180),
            ComplexityClass.STANDARD: (60, 1024, 128, 3000, 240),
            ComplexityClass.ADVANCED: (60, 2048, 256, 6000, 360),
            ComplexityClass.HEAVY: (60, 4096, 512, 10000, 480),
            ComplexityClass.EXTREME: (120, 8192, 1024, 16000, 720),
        }[complexity]

    fps, memory, npcs, draw_calls, visible = defaults
    return PerformanceBudget(
        target_fps=fps,
        max_memory_mb=memory,
        max_active_npcs=npcs,
        max_visible_entities=visible,
        max_draw_calls=draw_calls,
        max_texture_memory_mb=max(32, int(memory * 0.30)),
        max_world_memory_mb=max(64, int(memory * 0.35)),
        max_generation_time_seconds=max(120, memory * 2),
        quality_profile="high" if complexity in {
            ComplexityClass.ADVANCED,
            ComplexityClass.HEAVY,
            ComplexityClass.EXTREME,
        } else "balanced",
        additional_constraints={
            "frame_budget_ms": round(1000.0 / fps, 3),
            "streaming_required": complexity in {
                ComplexityClass.HEAVY,
                ComplexityClass.EXTREME,
            },
            "fixed_physics_step_hz": 60,
        },
    )


# ============================================================================
# DIRECTOR AGENT
# ============================================================================

class DirectorAgent(GodBaseAgent):
    """
    Ultra-production master planner.

    Main pipeline:
        classify -> extract -> budget -> semantic model call -> compile -> audit

    The heavy logic is intentionally local and deterministic where possible.
    That reduces provider dependence and makes downstream orchestration more
    predictable.
    """

    role_name = "Game Director / Master Planner / Architecture Compiler"
    service_type = "brain"
    required_capabilities = frozenset(
        {
            "text_generation",
            "structured_output",
            "planning",
            "long_context",
        }
    )

    def __init__(self, gateway: Any = None, **kwargs: Any) -> None:
        kwargs.pop("role_name", None)
        kwargs.pop("service_type", None)
        kwargs.pop("required_capabilities", None)

        super().__init__(
            role_name=self.role_name,
            service_type=self.service_type,
            gateway=gateway,
            required_capabilities=self.required_capabilities,
            temperature=0.10,
            max_tokens=18000,
            metadata={
                "stage": "master_planning",
                "agent_version": "riot.director.v5-ultra",
                "output_contract": "architecture_plan",
                "planning_mode": "hybrid_deterministic_llm",
            },
            **kwargs,
        )

    # ----------------------------------------------------------------------
    # Prompt
    # ----------------------------------------------------------------------

    def build_system_prompt(
        self,
        task_directive: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> str:
        base = super().build_system_prompt(task_directive, context=context)

        return (
            base
            + r"""

ULTRA DIRECTOR MODE
===================

You are not a generic idea generator.

You are the architecture authority for a real downstream compiler pipeline.
Your output is consumed by typed project models and specialist agents.

OPERATING MODEL
---------------
Layer 1 — Intent:
  understand what the player must actually do.

Layer 2 — System:
  derive gameplay, world, physics, AI, UI, audio, save, performance and build
  systems.

Layer 3 — Dependency:
  turn those systems into an acyclic execution graph.

Layer 4 — Budget:
  ensure memory, FPS, draw calls, visible entities, texture memory and streaming
  are coherent for the requested platform.

Layer 5 — Execution contract:
  every important system receives an owner, inputs, outputs, dependencies and
  measurable acceptance criteria.

Layer 6 — Failure closure:
  identify missing-capability, runtime, content, performance and build failure
  modes.  Provide safe recovery/degradation decisions.

ADVANCED PLANNING RULES
-----------------------
1. Do not merely describe a game. Compile a buildable architecture.
2. Keep the minimum playable slice explicit.
3. Prefer dependency graphs over unordered feature lists.
4. Avoid circular dependencies.
5. Separate must-have systems from optional systems.
6. Large worlds must have streaming/chunking semantics.
7. Large NPC counts must have LOD/simulation-tier semantics.
8. Physics must specify fixed-step expectations where simulation is used.
9. Graphics planning must distinguish geometry, material, texture, animation and
   VFX workloads.
10. Audio planning must distinguish music, SFX and voice.
11. Network features must define authoritative state and degraded/offline mode
    when applicable.
12. Save systems must define what state survives a process restart.
13. Security requirements must not become gameplay logic.
14. A missing provider capability is a design constraint, not permission to invent
    fake output.
15. Never claim that the engine has already executed, compiled, rendered, tested,
    or built anything.
16. Do not emit markdown outside the single JSON object.

SYSTEM DEPENDENCY INVARIANTS
----------------------------
The following relationships are preferred:

  planning
      -> architecture
      -> asset/world/physics/gameplay/audio/ui
      -> source assembly
      -> QA
      -> build

Runtime systems may depend on generated data, but generated data must not claim
to depend on artifacts that do not yet exist.

PERFORMANCE INVARIANTS
----------------------
- The frame budget is derived from target FPS.
- Memory ceilings are hard planning ceilings.
- Heavy/Extreme worlds require streaming.
- Heavy/Extreme NPC counts require simulation tiers.
- Draw-call and visible-entity budgets must not be zero when 3D rendering is
  explicitly requested.
- Do not spend the entire memory budget on textures; reserve memory for world,
  runtime, physics and caches.

LLM OUTPUT CONTRACT
-------------------
Return this exact high-level shape:

{
  "status": "SUCCESS|FAILED",
  "data": {
    "project_summary": "...",
    "game_genre": "...",
    "visual_style": "...",
    "complexity_class": "micro|standard|advanced|heavy|extreme",
    "core_gameplay_loop": "...",
    "systems": [
      {
        "id": "...",
        "purpose": "...",
        "dependencies": [],
        "inputs": [],
        "outputs": [],
        "runtime_requirements": [],
        "acceptance": []
      }
    ],
    "required_agents": [],
    "required_capabilities": [],
    "technical_constraints": [],
    "build_steps": [],
    "assumptions": [],
    "unresolved_decisions": []
  },
  "warnings": [],
  "errors": []
}
"""
        )

    # ----------------------------------------------------------------------
    # Semantic classification
    # ----------------------------------------------------------------------

    def _classify_complexity(
        self,
        idea: str,
        context: Mapping[str, Any],
    ) -> ComplexityClass:
        text = (idea + " " + str(context)).lower()

        score = 0
        weighted_terms = {
            "open world": 6,
            "massive": 5,
            "gta": 6,
            "mmorpg": 8,
            "multiplayer": 4,
            "1000 npc": 7,
            "10000 npc": 10,
            "npc": 3,
            "procedural": 3,
            "streaming": 3,
            "realistic": 2,
            "pbr": 2,
            "ray tracing": 4,
            "destruction": 4,
            "vehicle": 2,
            "combat": 2,
            "inventory": 2,
            "quest": 2,
            "voice": 2,
            "dialogue": 2,
            "ai": 3,
            "physics": 2,
            "simulation": 4,
            "multiverse": 7,
            "sandbox": 5,
            "3d": 2,
        }

        for token, weight in weighted_terms.items():
            if token in text:
                score += weight

        target = context.get("target_platform")
        if str(target).lower() in {"web", "html5", "web_html5"}:
            score += 0
        elif str(target).lower() in {"mobile", "android", "apk"}:
            score += 1
        else:
            score += 2

        if score >= 28:
            return ComplexityClass.EXTREME
        if score >= 20:
            return ComplexityClass.HEAVY
        if score >= 12:
            return ComplexityClass.ADVANCED
        if score >= 6:
            return ComplexityClass.STANDARD
        return ComplexityClass.MICRO

    # ----------------------------------------------------------------------
    # Deterministic requirement extraction
    # ----------------------------------------------------------------------

    def _extract_requirements(
        self,
        idea: str,
        context: Mapping[str, Any],
        complexity: ComplexityClass,
    ) -> List[PlanningRequirement]:
        text = (idea + " " + str(context)).lower()
        requirements: List[PlanningRequirement] = []

        def add(
            kind: RequirementKind,
            importance: int,
            reason: str,
            evidence: str,
        ) -> None:
            rid = _stable_id("req", kind.value, reason)
            requirements.append(
                PlanningRequirement(
                    requirement_id=rid,
                    kind=kind,
                    importance=max(1, min(10, importance)),
                    reason=reason,
                    evidence=evidence,
                )
            )

        # Gameplay is always mandatory for a game generator.
        add(
            RequirementKind.GAMEPLAY,
            10,
            "core player loop and state machine",
            "all game concepts require player-facing state progression",
        )

        add(
            RequirementKind.QA,
            10,
            "static and runtime verification",
            "generated code must pass downstream QA before build",
        )

        add(
            RequirementKind.BUILD,
            9,
            "target-specific source/build contract",
            "orchestrator eventually hands a project to the builder",
        )

        if any(term in text for term in (
            "3d", "realistic", "pbr", "material", "texture", "model",
        )):
            add(
                RequirementKind.ASSET,
                10,
                "3D asset and material pipeline",
                "3D/realistic/PBR terms detected",
            )

        if any(term in text for term in (
            "map", "open world", "environment", "city", "island",
            "terrain", "forest", "world", "level",
        )):
            add(
                RequirementKind.WORLD,
                10,
                "world partition and scene layout",
                "world/environment terms detected",
            )

        if any(term in text for term in (
            "physics", "vehicle", "ragdoll", "destruction", "gravity",
            "collision", "rigid body", "simulation",
        )):
            add(
                RequirementKind.PHYSICS,
                9,
                "simulation and collision layer",
                "physics/simulation terms detected",
            )

        if any(term in text for term in (
            "npc", "enemy", "boss", "ai", "agent", "behavior",
            "companion", "stealth",
        )):
            add(
                RequirementKind.AI,
                9,
                "NPC/AI behavior and simulation tiers",
                "AI/NPC terms detected",
            )

        if any(term in text for term in (
            "music", "sound", "audio", "voice", "dialogue",
        )):
            add(
                RequirementKind.AUDIO,
                7,
                "audio/voice pipeline",
                "audio-related terms detected",
            )

        if any(term in text for term in (
            "ui", "hud", "menu", "inventory", "mobile controls",
            "button", "touch",
        )):
            add(
                RequirementKind.UI,
                8,
                "interactive UI and input layer",
                "UI/input terms detected",
            )

        if any(term in text for term in (
            "save", "savegame", "checkpoint", "progression",
        )):
            add(
                RequirementKind.SAVE,
                8,
                "persistent game-state layer",
                "save/progression terms detected",
            )

        if any(term in text for term in (
            "multiplayer", "online", "co-op", "pvp", "network",
            "server",
        )):
            add(
                RequirementKind.NETWORK,
                9,
                "authoritative networking and synchronization",
                "network terms detected",
            )

        if complexity in {ComplexityClass.HEAVY, ComplexityClass.EXTREME}:
            add(
                RequirementKind.PERFORMANCE,
                10,
                "budget enforcement and streaming",
                "heavy complexity requires explicit budget planning",
            )

        if any(term in text for term in (
            "login", "account", "permission", "secure", "security",
            "token", "secret",
        )):
            add(
                RequirementKind.SECURITY,
                8,
                "security boundary",
                "security/authentication terms detected",
            )

        requirements.sort(
            key=lambda item: (-item.importance, item.kind.value, item.requirement_id)
        )

        return requirements[:_MAX_REQUIREMENTS]

    # ----------------------------------------------------------------------
    # Required specialist matrix
    # ----------------------------------------------------------------------

    def _derive_agent_matrix(
        self,
        requirements: Sequence[PlanningRequirement],
        platform: TargetPlatform,
        complexity: ComplexityClass,
    ) -> Tuple[List[str], Set[str]]:
        kinds = {item.kind for item in requirements}

        agents: List[str] = [
            "DirectorAgent",
            "ArchitectureAgent",
            "GameplayAgent",
        ]
        capabilities: Set[str] = {
            "text_generation",
            "structured_output",
            "planning",
        }

        if RequirementKind.ASSET in kinds:
            agents.extend([
                "AssetGeneratorAgent",
                "MaterialPBRAgent",
                "AnimationAgent",
            ])
            capabilities.update({"asset_planning", "pbr_materials", "animation"})

        if RequirementKind.WORLD in kinds:
            agents.extend([
                "MapBuilderAgent",
                "SceneLayoutAgent",
            ])
            capabilities.update({"world_generation", "streaming_world"})

        if RequirementKind.PHYSICS in kinds:
            agents.append("PhysicsAgent")
            capabilities.update({"physics", "fixed_step_simulation"})

        if RequirementKind.AI in kinds:
            agents.extend([
                "NPCBrainAgent",
                "BehaviorPlannerAgent",
            ])
            capabilities.update({"npc_ai", "behavior_trees_or_state_machines"})

        if RequirementKind.AUDIO in kinds:
            agents.append("AudioVoiceAgent")
            capabilities.update({"audio", "voice"})

        if RequirementKind.UI in kinds:
            agents.append("UIUXAgent")
            capabilities.update({"ui_generation", "input_mapping"})

        if RequirementKind.NETWORK in kinds:
            agents.append("NetworkSyncAgent")
            capabilities.update({"networking", "state_replication"})

        if RequirementKind.SAVE in kinds:
            agents.append("SaveStateAgent")
            capabilities.add("persistent_state")

        if RequirementKind.PERFORMANCE in kinds:
            agents.append("OptimizationAgent")
            capabilities.update({"profiling", "streaming", "budget_enforcement"})

        if RequirementKind.SECURITY in kinds:
            agents.append("SecurityAgent")
            capabilities.add("security_validation")

        agents.extend([
            "CodeRuntimeAgent",
            "QATesterAgent",
            "BuildReleaseAgent",
        ])

        if platform is TargetPlatform.CLOUD_STREAM:
            agents.append("StreamingAgent")
            capabilities.update({"webrtc", "pixel_streaming"})

        if complexity in {ComplexityClass.HEAVY, ComplexityClass.EXTREME}:
            agents.extend([
                "WorldPartitionAgent",
                "SimulationLODWorldAgent",
                "IntegrationVerifierAgent",
            ])
            capabilities.update({
                "world_partitioning",
                "simulation_lod",
                "integration_verification",
            })

        if complexity is ComplexityClass.EXTREME:
            agents.extend([
                "StressTestAgent",
                "FailureRecoveryAgent",
            ])
            capabilities.update({
                "stress_testing",
                "failure_recovery",
            })

        return _bounded_unique(agents, 64), set(
            _bounded_unique(capabilities, _MAX_CAPABILITIES)
        )

    # ----------------------------------------------------------------------
    # Work-package DAG compiler
    # ----------------------------------------------------------------------

    def _build_work_graph(
        self,
        requirements: Sequence[PlanningRequirement],
        agents: Sequence[str],
        complexity: ComplexityClass,
    ) -> List[WorkPackage]:
        owners: Dict[str, str] = {
            "architecture": "ArchitectureAgent",
            "assets": "AssetGeneratorAgent",
            "world": "MapBuilderAgent",
            "physics": "PhysicsAgent",
            "gameplay": "GameplayAgent",
            "ai": "NPCBrainAgent",
            "audio": "AudioVoiceAgent",
            "ui": "UIUXAgent",
            "network": "NetworkSyncAgent",
            "save": "SaveStateAgent",
            "optimization": "OptimizationAgent",
            "security": "SecurityAgent",
            "source": "CodeRuntimeAgent",
            "qa": "QATesterAgent",
            "build": "BuildReleaseAgent",
        }

        active = {item.kind.value for item in requirements}

        packages: List[WorkPackage] = []

        def add(
            package_id: str,
            owner: str,
            priority: int,
            deps: Sequence[str],
            deliverables: Sequence[str],
            acceptance: Sequence[str],
        ) -> None:
            packages.append(
                WorkPackage(
                    package_id=package_id,
                    owner=owner,
                    priority=priority,
                    depends_on=tuple(dict.fromkeys(deps)),
                    deliverables=tuple(deliverables),
                    acceptance=tuple(acceptance),
                )
            )

        add(
            "architecture",
            owners["architecture"],
            100,
            [],
            ["canonical architecture plan"],
            [
                "all mandatory systems are identified",
                "dependencies are acyclic",
                "platform/runtime is defined",
            ],
        )

        if "asset" in active:
            add(
                "assets",
                owners["assets"],
                85,
                ["architecture"],
                ["asset manifest", "material/texture requirements"],
                [
                    "asset IDs are stable",
                    "PBR/LOD requirements are explicit where needed",
                ],
            )

        if "world" in active:
            world_deps = ["architecture"]
            if "asset" in active:
                world_deps.append("assets")
            add(
                "world",
                owners["world"],
                82,
                world_deps,
                ["world manifest", "scene/chunk placement plan"],
                [
                    "spawn points are defined",
                    "streaming/chunk strategy is explicit for large worlds",
                ],
            )

        if "physics" in active:
            physics_deps = ["architecture"]
            if "world" in active:
                physics_deps.append("world")
            add(
                "physics",
                owners["physics"],
                80,
                physics_deps,
                ["physics configuration", "collision/simulation rules"],
                [
                    "fixed-step behavior is defined",
                    "collision behavior is deterministic enough for downstream implementation",
                ],
            )

        gameplay_deps = ["architecture"]
        if "world" in active:
            gameplay_deps.append("world")
        if "physics" in active:
            gameplay_deps.append("physics")
        add(
            "gameplay",
            owners["gameplay"],
            78,
            gameplay_deps,
            ["gameplay modules", "state machine", "input/action mapping"],
            [
                "core loop is executable",
                "win/loss/progression are represented",
            ],
        )

        if "ai" in active:
            ai_deps = ["gameplay"]
            if "physics" in active:
                ai_deps.append("physics")
            add(
                "ai",
                owners["ai"],
                72,
                ai_deps,
                ["NPC behavior model", "simulation tiers"],
                [
                    "behavior transitions are bounded",
                    "heavy populations have scalable simulation tiers",
                ],
            )

        if "audio" in active:
            add(
                "audio",
                owners["audio"],
                60,
                ["architecture", "gameplay"],
                ["audio event manifest", "voice requirements"],
                ["every critical gameplay event has an audio policy"],
            )

        if "ui" in active:
            add(
                "ui",
                owners["ui"],
                64,
                ["architecture", "gameplay"],
                ["HUD/UI manifest", "input mapping"],
                ["all critical gameplay actions have UI/input access"],
            )

        if "network" in active:
            add(
                "network",
                owners["network"],
                58,
                ["gameplay", "architecture"],
                ["replication model", "authority rules"],
                [
                    "authoritative state is explicit",
                    "disconnected/degraded behavior is explicit",
                ],
            )

        if "save" in active:
            add(
                "save",
                owners["save"],
                56,
                ["gameplay"],
                ["persistent state schema"],
                ["restart-safe state boundaries are documented"],
            )

        if "security" in active:
            add(
                "security",
                owners["security"],
                50,
                ["architecture"],
                ["security constraints"],
                ["secrets are separated from generated gameplay data"],
            )

        add(
            "source",
            owners["source"],
            40,
            [pkg.package_id for pkg in packages if pkg.package_id not in {"qa", "build"}],
            ["source bundle"],
            [
                "all required work packages are represented in source",
                "no unsupported fake artifacts are embedded",
            ],
        )

        add(
            "qa",
            owners["qa"],
            25,
            ["source"],
            ["QA report"],
            [
                "syntax/static checks pass",
                "runtime-sensitive hazards are classified",
                "unverified execution claims are rejected",
            ],
        )

        add(
            "build",
            owners["build"],
            10,
            ["qa"],
            ["real target artifact"],
            [
                "builder reports an actual artifact",
                "artifact reference/checksum is available",
            ],
        )

        if complexity in {ComplexityClass.HEAVY, ComplexityClass.EXTREME}:
            add(
                "optimization",
                owners["optimization"],
                35,
                ["world", "gameplay", "source"],
                ["performance budget checks", "streaming/LOD policy"],
                [
                    "frame budget is respected",
                    "memory budget is respected",
                ],
            )

        return packages[:_MAX_WORK_PACKAGES]

    # ----------------------------------------------------------------------
    # Graph validator
    # ----------------------------------------------------------------------

    def _validate_acyclic(self, packages: Sequence[WorkPackage]) -> List[str]:
        graph = {pkg.package_id: set(pkg.depends_on) for pkg in packages}
        errors: List[str] = []

        for pkg_id, deps in graph.items():
            unknown = deps - graph.keys()
            if unknown:
                errors.append(
                    f"work package {pkg_id} references unknown dependencies: "
                    f"{sorted(unknown)}"
                )

        visiting: Set[str] = set()
        visited: Set[str] = set()

        def dfs(node: str) -> None:
            if node in visiting:
                errors.append(f"dependency cycle detected at {node}")
                return
            if node in visited:
                return

            visiting.add(node)
            for dep in graph.get(node, ()):
                dfs(dep)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            dfs(node)

        return errors

    # ----------------------------------------------------------------------
    # Canonical projection
    # ----------------------------------------------------------------------

    def _project_architecture(
        self,
        model: DirectorModel,
        platform: TargetPlatform,
        complexity: ComplexityClass,
        agents: Sequence[str],
        capabilities: Set[str],
        requirements: Sequence[PlanningRequirement],
        context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        budget = _platform_budget(platform, complexity)

        # Respect an upstream explicit budget where possible.
        supplied_budget = context.get("performance_budget")
        if isinstance(supplied_budget, Mapping):
            budget_values = budget.model_dump()
            for key in (
                "target_fps",
                "max_memory_mb",
                "max_active_npcs",
                "max_visible_entities",
                "max_draw_calls",
                "max_texture_memory_mb",
                "max_world_memory_mb",
            ):
                if supplied_budget.get(key) is not None:
                    try:
                        budget_values[key] = max(1, int(supplied_budget[key]))
                    except (TypeError, ValueError):
                        pass
            budget = PerformanceBudget(**budget_values)

        runtime = RuntimeTarget(
            runtime_type=_runtime_for(platform),
            name=platform.value,
            capability_status=CapabilityStatus.NOT_CONFIGURED,
            capabilities=set(capabilities),
            configuration={
                "planning_only": True,
                "complexity": complexity.value,
                "requirements": [req.kind.value for req in requirements],
            },
        )

        engine_config = {
            "planning_version": "riot.director.v5-ultra",
            "planning_mode": "hybrid_deterministic_llm",
            "fixed_physics_step_hz": 60,
            "frame_budget_ms": round(1000.0 / budget.target_fps, 3),
            "streaming_required": bool(
                budget.additional_constraints.get("streaming_required")
            ),
            "simulation_lod_required": complexity in {
                ComplexityClass.HEAVY,
                ComplexityClass.EXTREME,
            },
        }

        constraints = _bounded_unique(
            list(model.technical_constraints)
            + [
                f"target_platform={platform.value}",
                f"complexity={complexity.value}",
                f"memory_limit_mb={budget.max_memory_mb}",
                f"target_fps={budget.target_fps}",
                f"frame_budget_ms={engine_config['frame_budget_ms']}",
                "execution evidence must come from downstream runtime/build stages",
            ],
            _MAX_CONSTRAINTS,
        )

        build_steps = _bounded_unique(
            list(model.build_steps)
            + [
                "validate canonical GameProject",
                "assemble source and verified binary inputs",
                "run QA gate",
                "invoke target-specific UniversalBuilder backend",
                "require real artifact reference",
            ],
            _MAX_BUILD_STEPS,
        )

        genre = _clean(model.game_genre, "unknown")
        style = _clean(model.visual_style, "unknown")
        loop = _clean(
            model.core_gameplay_loop,
            "Acquire capability -> perform action -> receive feedback -> progress",
        )

        return {
            "plan_id": _stable_id(
                "plan",
                model.project_summary,
                genre,
                platform.value,
                complexity.value,
            ),
            "target_platform": platform,
            "runtime_target": runtime,
            "game_genre": genre,
            "visual_style": style,
            "complexity_class": complexity.value,
            "core_gameplay_loop": loop,
            "engine_config": engine_config,
            "required_agents": list(agents),
            "required_capabilities": set(capabilities),
            "build_steps": build_steps,
            "technical_constraints": constraints,
            "performance_budget": budget,
        }

    # ----------------------------------------------------------------------
    # Auditing
    # ----------------------------------------------------------------------

    def _audit_projection(
        self,
        projected: Mapping[str, Any],
        packages: Sequence[WorkPackage],
        requirements: Sequence[PlanningRequirement],
    ) -> Tuple[List[str], List[str]]:
        errors: List[str] = []
        warnings: List[str] = []

        target = projected.get("target_platform")
        if not isinstance(target, TargetPlatform):
            errors.append("canonical target_platform projection is invalid")

        budget = projected.get("performance_budget")
        if not isinstance(budget, PerformanceBudget):
            errors.append("canonical performance budget projection is invalid")
        else:
            frame_budget = 1000.0 / float(budget.target_fps)
            declared_frame_budget = float(
                projected.get("engine_config", {}).get("frame_budget_ms", frame_budget)
            )
            if abs(frame_budget - declared_frame_budget) > 0.05:
                errors.append("frame budget and target FPS disagree")

            if (
                budget.max_memory_mb is not None
                and budget.max_texture_memory_mb is not None
                and budget.max_world_memory_mb is not None
                and (
                    budget.max_texture_memory_mb
                    + budget.max_world_memory_mb
                    >= budget.max_memory_mb
                )
            ):
                errors.append(
                    "texture + world memory consume the entire runtime memory budget"
                )

        graph_errors = self._validate_acyclic(packages)
        errors.extend(graph_errors)

        package_owners = {pkg.owner for pkg in packages}
        required_owners = {
            "ArchitectureAgent",
            "GameplayAgent",
            "CodeRuntimeAgent",
            "QATesterAgent",
            "BuildReleaseAgent",
        }
        missing_owners = required_owners - package_owners
        if missing_owners:
            errors.append(
                f"mandatory planning owners missing: {sorted(missing_owners)}"
            )

        requirement_kinds = {item.kind.value for item in requirements}
        if "world" in requirement_kinds:
            streaming = bool(
                projected.get("engine_config", {}).get("streaming_required")
            )
            complexity = projected.get("complexity_class")
            if complexity in {"heavy", "extreme"} and not streaming:
                errors.append("heavy world does not enable streaming")

        if "ai" in requirement_kinds and projected.get("complexity_class") in {
            "heavy", "extreme"
        }:
            if not projected.get("engine_config", {}).get("simulation_lod_required"):
                errors.append("heavy NPC workload does not enable simulation LOD")

        if projected.get("complexity_class") == "extreme":
            warnings.append(
                "EXTREME planning class selected; real hardware/backend availability "
                "must be verified before execution."
            )

        return errors, warnings

    # ----------------------------------------------------------------------
    # Main public API
    # ----------------------------------------------------------------------

    async def perform_role(
        self,
        game_idea: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        target_platform: str = "web",
        performance_budget: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        idea = _clean(game_idea)
        if not idea:
            return {
                "status": "FAILED",
                "error": "game_idea cannot be empty",
                "agent_version": "riot.director.v5-ultra",
            }

        merged_context: Dict[str, Any] = dict(context or {})
        merged_context["target_platform"] = _normalize_platform(target_platform).value

        if performance_budget:
            merged_context["performance_budget"] = dict(performance_budget)

        platform = _normalize_platform(target_platform)
        complexity = self._classify_complexity(idea, merged_context)
        requirements = self._extract_requirements(
            idea,
            merged_context,
            complexity,
        )
        agents, capabilities = self._derive_agent_matrix(
            requirements,
            platform,
            complexity,
        )
        packages = self._build_work_graph(
            requirements,
            agents,
            complexity,
        )

        directive = f"""
Compile the authoritative architecture for this game concept.

GAME CONCEPT:
{idea}

TARGET PLATFORM:
{platform.value}

DETERMINISTIC COMPLEXITY CLASS:
{complexity.value}

DETECTED REQUIREMENTS:
{[
    {
        "id": req.requirement_id,
        "kind": req.kind.value,
        "importance": req.importance,
        "reason": req.reason,
        "evidence": req.evidence,
    }
    for req in requirements
]}

SPECIALIST AGENTS AVAILABLE TO THE PLAN:
{agents}

CAPABILITIES:
{sorted(capabilities)}

PRECOMPILED WORK PACKAGES:
{[
    {
        "id": pkg.package_id,
        "owner": pkg.owner,
        "priority": pkg.priority,
        "depends_on": list(pkg.depends_on),
        "deliverables": list(pkg.deliverables),
        "acceptance": list(pkg.acceptance),
    }
    for pkg in packages
]}

Return one structured JSON object.
Do not claim anything was executed or verified.
Use the deterministic complexity class unless there is a compelling technical
reason to change it; explain the change as an assumption.
"""

        # Use the existing advanced BaseAgent gateway/runtime. Provider retry
        # remains below us; this call does not add another provider retry loop.
        raw = await self.think_and_execute_result(
            directive,
            context=merged_context,
            metadata={
                "target_platform": platform.value,
                "complexity_class": complexity.value,
                "requirement_count": len(requirements),
                "planned_agent_count": len(agents),
                "work_package_count": len(packages),
                "director_contract": "riot.director.v5-ultra",
            },
            response_schema=DirectorEnvelope,
            **kwargs,
        )

        if not raw.ok:
            return raw.to_dict()

        try:
            envelope = DirectorEnvelope.model_validate(raw.data)
        except ValidationError as exc:
            return {
                "status": "FAILED",
                "error": f"director response schema validation failed: {exc}",
                "agent_version": "riot.director.v5-ultra",
            }

        if envelope.status.upper() == "FAILED":
            return {
                "status": "FAILED",
                "data": envelope.data.model_dump(mode="json"),
                "warnings": envelope.warnings,
                "errors": envelope.errors,
                "agent_version": "riot.director.v5-ultra",
            }

        projected = self._project_architecture(
            envelope.data,
            platform,
            complexity,
            agents,
            capabilities,
            requirements,
            merged_context,
        )

        audit_errors, audit_warnings = self._audit_projection(
            projected,
            packages,
            requirements,
        )

        if audit_errors:
            return {
                "status": "FAILED",
                "data": {
                    "architecture": {
                        key: (
                            value.value
                            if isinstance(value, Enum)
                            else value
                        )
                        for key, value in projected.items()
                    }
                },
                "warnings": _bounded_unique(
                    envelope.warnings + audit_warnings,
                    48,
                ),
                "errors": _bounded_unique(audit_errors, 48),
                "agent_version": "riot.director.v5-ultra",
            }

        return {
            "status": "SUCCESS",
            "data": {
                "architecture": {
                    key: (
                        value.value
                        if isinstance(value, Enum)
                        else value
                    )
                    for key, value in projected.items()
                },
                "planner_summary": {
                    "project_summary": _clean(envelope.data.project_summary),
                    "complexity_class": complexity.value,
                    "requirement_count": len(requirements),
                    "specialist_agent_count": len(agents),
                    "work_package_count": len(packages),
                    "requirements": [
                        {
                            "id": item.requirement_id,
                            "kind": item.kind.value,
                            "importance": item.importance,
                            "reason": item.reason,
                            "evidence": item.evidence,
                        }
                        for item in requirements
                    ],
                    "work_packages": [
                        {
                            "id": pkg.package_id,
                            "owner": pkg.owner,
                            "priority": pkg.priority,
                            "depends_on": list(pkg.depends_on),
                            "deliverables": list(pkg.deliverables),
                            "acceptance": list(pkg.acceptance),
                        }
                        for pkg in packages
                    ],
                },
                "assumptions": _bounded_unique(
                    envelope.data.assumptions,
                    48,
                ),
                "unresolved_decisions": _bounded_unique(
                    envelope.data.unresolved_decisions,
                    48,
                ),
            },
            "warnings": _bounded_unique(
                envelope.warnings + audit_warnings,
                48,
            ),
            "errors": [],
            "agent_version": "riot.director.v5-ultra",
        }


__all__ = [
    "DirectorAgent",
    "ComplexityClass",
    "RequirementKind",
    "PlanningRequirement",
    "WorkPackage",
]
