from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional

from god_brain.agents.base_agent import GodBaseAgent


# ============================================================================
# PRODUCTION LIMITS
# ============================================================================

MAX_CONTEXT_CHARS = 220_000
MAX_COLLISION_LAYERS = 32
MAX_MATERIALS = 64
MAX_RULES = 128
MAX_ENTITIES = 256
MAX_TAGS = 48


# ============================================================================
# SAFE / DETERMINISTIC HELPERS
# ============================================================================

def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:4096]


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(item) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _safe_json(model_dump(mode="json"))
        except Exception:
            pass

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _safe_json(to_dict())
        except Exception:
            pass

    return str(value)


def _bounded_json(value: Any, limit: int = MAX_CONTEXT_CHARS) -> str:
    text = json.dumps(
        _safe_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    if len(text) <= limit:
        return text

    head = int(limit * 0.78)
    tail = limit - head - 64

    return (
        text[:head]
        + "\n...[PHYSICS CONTEXT TRUNCATED]...\n"
        + text[-max(1, tail):]
    )


def _unique(values: Iterable[Any], limit: int) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()

    for value in values:
        text = str(value or "").strip()

        if not text or text in seen:
            continue

        seen.add(text)
        result.append(text)

        if len(result) >= limit:
            break

    return result


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)

        if not math.isfinite(number):
            return default

        return number
    except (TypeError, ValueError):
        return default


# ============================================================================
# PHYSICS INTENT ANALYZER
# ============================================================================

class PhysicsIntentAnalyzer:
    """
    Deterministic physics pre-planner.

    This does not execute a physics engine.
    It builds a bounded physical simulation contract that can be refined by
    the model and later consumed by runtime/build/QA systems.
    """

    _COLLISION_LAYER_NAMES = (
        "world",
        "player",
        "npc",
        "vehicle",
        "projectile",
        "pickup",
        "interaction",
        "trigger",
    )

    _VEHICLE_TERMS = (
        "car",
        "vehicle",
        "truck",
        "bus",
        "bike",
        "motorcycle",
        "boat",
        "ship",
        "aircraft",
    )

    _CHARACTER_TERMS = (
        "character",
        "player",
        "npc",
        "enemy",
        "human",
        "monster",
        "creature",
    )

    _DESTRUCTIBLE_TERMS = (
        "destructible",
        "breakable",
        "destroy",
        "destruction",
        "ragdoll",
    )

    def detect_runtime(
        self,
        environment: Mapping[str, Any],
    ) -> Dict[str, str]:

        text = _bounded_json(environment).lower()

        engine = str(
            environment.get("physics_engine")
            or environment.get("engine")
            or ""
        ).strip()

        platform = str(
            environment.get("target_platform")
            or environment.get("platform")
            or ""
        ).strip().lower()

        if not engine:
            if "three.js" in text or "threejs" in text:
                engine = "threejs-compatible"
            elif "unreal" in text:
                engine = "unreal-compatible"
            else:
                engine = "runtime-agnostic"

        if not platform:
            if "mobile" in text or "android" in text:
                platform = "mobile_apk"
            elif "pc" in text or "desktop" in text:
                platform = "pc_exe"
            else:
                platform = "web_html5"

        return {
            "physics_engine": engine,
            "target_platform": platform,
        }

    def detect_simulation_scale(
        self,
        environment: Mapping[str, Any],
    ) -> str:

        text = _bounded_json(environment).lower()

        if any(
            term in text
            for term in (
                "10000 npc",
                "10000 entities",
                "massive simulation",
                "extreme simulation",
                "thousands of npc",
                "open world simulation",
            )
        ):
            return "extreme"

        if any(
            term in text
            for term in (
                "1000 npc",
                "large simulation",
                "open world",
                "large world",
                "many vehicles",
                "crowd simulation",
            )
        ):
            return "large"

        if any(
            term in text
            for term in (
                "small",
                "simple",
                "single player",
                "arena",
            )
        ):
            return "compact"

        return "standard"

    def detect_entities(
        self,
        environment: Mapping[str, Any],
    ) -> Dict[str, bool]:

        text = _bounded_json(environment).lower()

        return {
            "characters": any(
                term in text
                for term in self._CHARACTER_TERMS
            ),
            "vehicles": any(
                term in text
                for term in self._VEHICLE_TERMS
            ),
            "destruction": any(
                term in text
                for term in self._DESTRUCTIBLE_TERMS
            ),
            "projectiles": any(
                term in text
                for term in (
                    "bullet",
                    "projectile",
                    "missile",
                    "rocket",
                )
            ),
            "interactive_objects": any(
                term in text
                for term in (
                    "door",
                    "button",
                    "switch",
                    "pickup",
                    "interactive",
                )
            ),
        }

    def build_core_config(
        self,
        *,
        scale: str,
        target_platform: str,
        entity_features: Mapping[str, bool],
        environment: Mapping[str, Any],
    ) -> Dict[str, Any]:

        # Stable baseline. Upstream game-specific data can refine these values.
        gravity_y = _number(
            environment.get("gravity_y"),
            -9.81,
        )

        if target_platform == "mobile_apk":
            timestep = 1.0 / 60.0

        elif target_platform == "web_html5":
            timestep = 1.0 / 60.0

        elif scale == "extreme":
            timestep = 1.0 / 60.0

        else:
            timestep = 1.0 / 60.0

        return {
            "gravity": {
                "x": 0.0,
                "y": gravity_y,
                "z": 0.0,
            },
            "friction_default": 0.55,
            "restitution_default": 0.15,
            "time_scale": 1.0,
            "physics_engine": str(
                environment.get(
                    "physics_engine",
                    "builtin",
                )
            ),
            "fixed_timestep": timestep,
            "max_substeps": (
                4
                if scale == "extreme"
                else 3
                if scale == "large"
                else 2
            ),
            "interpolation": True,
            "continuous_collision_detection": (
                entity_features.get(
                    "projectiles",
                    False,
                )
                or entity_features.get(
                    "vehicles",
                    False,
                )
            ),
            "deterministic_simulation": scale in {
                "large",
                "extreme",
            },
        }

    def build_collision_layers(
        self,
        entity_features: Mapping[str, bool],
    ) -> Dict[str, int]:

        layers: Dict[str, int] = {}

        bit = 1

        for layer in self._COLLISION_LAYER_NAMES:
            layers[layer] = bit
            bit <<= 1

        # Preserve all base layers but allow downstream systems to inspect
        # feature-specific availability.
        if not entity_features.get("vehicles", False):
            layers.pop("vehicle", None)

        if not entity_features.get("projectiles", False):
            layers.pop("projectile", None)

        return layers

    def build_material_profiles(
        self,
        entity_features: Mapping[str, bool],
    ) -> List[Dict[str, Any]]:

        profiles: List[Dict[str, Any]] = [
            {
                "material_id": "material_default",
                "name": "Default Surface",
                "friction": 0.55,
                "restitution": 0.15,
                "static_friction": 0.60,
                "dynamic_friction": 0.50,
                "rolling_friction": 0.02,
                "density": 1000.0,
                "tags": [
                    "default",
                    "solid",
                ],
            },
            {
                "material_id": "material_ice",
                "name": "Ice",
                "friction": 0.08,
                "restitution": 0.05,
                "static_friction": 0.10,
                "dynamic_friction": 0.06,
                "rolling_friction": 0.005,
                "density": 920.0,
                "tags": [
                    "slippery",
                ],
            },
            {
                "material_id": "material_metal",
                "name": "Metal",
                "friction": 0.42,
                "restitution": 0.20,
                "static_friction": 0.48,
                "dynamic_friction": 0.38,
                "rolling_friction": 0.015,
                "density": 7850.0,
                "tags": [
                    "hard",
                    "metal",
                ],
            },
            {
                "material_id": "material_rubber",
                "name": "Rubber",
                "friction": 0.85,
                "restitution": 0.35,
                "static_friction": 0.90,
                "dynamic_friction": 0.75,
                "rolling_friction": 0.02,
                "density": 1100.0,
                "tags": [
                    "vehicle",
                    "high-grip",
                ],
            },
        ]

        if entity_features.get("vehicles"):
            profiles.append(
                {
                    "material_id": "material_road",
                    "name": "Road",
                    "friction": 0.80,
                    "restitution": 0.05,
                    "static_friction": 0.85,
                    "dynamic_friction": 0.72,
                    "rolling_friction": 0.018,
                    "density": 2400.0,
                    "tags": [
                        "road",
                        "vehicle-surface",
                    ],
                }
            )

        if entity_features.get("destruction"):
            profiles.append(
                {
                    "material_id": "material_fragile",
                    "name": "Fragile Surface",
                    "friction": 0.45,
                    "restitution": 0.10,
                    "static_friction": 0.50,
                    "dynamic_friction": 0.40,
                    "rolling_friction": 0.02,
                    "density": 600.0,
                    "tags": [
                        "destructible",
                    ],
                }
            )

        return profiles[:MAX_MATERIALS]

    def build_interaction_rules(
        self,
        features: Mapping[str, bool],
    ) -> List[Dict[str, Any]]:

        rules: List[Dict[str, Any]] = [
            {
                "rule_id": "collision_resolution",
                "condition": "colliders_overlap",
                "response": [
                    "resolve_penetration",
                    "apply_contact_impulse",
                    "emit_collision_event",
                ],
            },
            {
                "rule_id": "sleep_when_resting",
                "condition": "linear_and_angular_velocity_below_threshold",
                "response": [
                    "allow_rigid_body_sleep",
                ],
            },
            {
                "rule_id": "fixed_step_integrator",
                "condition": "simulation_tick",
                "response": [
                    "integrate_velocity",
                    "solve_constraints",
                    "integrate_position",
                ],
            },
        ]

        if features.get("projectiles"):
            rules.append(
                {
                    "rule_id": "projectile_ccd",
                    "condition": "fast_projectile_detected",
                    "response": [
                        "perform_continuous_collision_query",
                        "resolve_first_valid_hit",
                        "emit_projectile_hit_event",
                    ],
                }
            )

        if features.get("vehicles"):
            rules.append(
                {
                    "rule_id": "vehicle_surface_response",
                    "condition": "vehicle_contact_with_surface",
                    "response": [
                        "sample_surface_friction",
                        "apply_longitudinal_force",
                        "apply_lateral_force",
                        "apply_drag",
                    ],
                }
            )

        if features.get("destruction"):
            rules.append(
                {
                    "rule_id": "destruction_response",
                    "condition": "destructible_object_damage_threshold_reached",
                    "response": [
                        "disable_primary_collider",
                        "spawn_destructible_state",
                        "emit_destruction_event",
                    ],
                }
            )

        return rules[:MAX_RULES]

    def build_performance_policy(
        self,
        scale: str,
        platform: str,
    ) -> Dict[str, Any]:

        if scale == "compact":
            max_dynamic_bodies = 300
            max_active_contacts = 1200

        elif scale == "large":
            max_dynamic_bodies = 1500
            max_active_contacts = 6000

        elif scale == "extreme":
            max_dynamic_bodies = 4000
            max_active_contacts = 16000

        else:
            max_dynamic_bodies = 800
            max_active_contacts = 3000

        return {
            "max_dynamic_bodies": max_dynamic_bodies,
            "max_active_contacts": max_active_contacts,
            "broadphase": "dynamic_aabb_or_engine_default",
            "sleeping": True,
            "contact_cache": True,
            "query_batching": True,
            "event_batching": True,
            "substep_cap": 4,
            "profiling": {
                "enabled": True,
                "sample_interval_frames": 60,
            },
            "platform": platform,
        }

    def build(self, environment: Mapping[str, Any]) -> Dict[str, Any]:

        runtime = self.detect_runtime(
            environment,
        )

        scale = self.detect_simulation_scale(
            environment,
        )

        features = self.detect_entities(
            environment,
        )

        core = self.build_core_config(
            scale=scale,
            target_platform=runtime["target_platform"],
            entity_features=features,
            environment=environment,
        )

        layers = self.build_collision_layers(
            features,
        )

        materials = self.build_material_profiles(
            features,
        )

        rules = self.build_interaction_rules(
            features,
        )

        performance = self.build_performance_policy(
            scale,
            runtime["target_platform"],
        )

        return {
            "contract_version": "riot.physics.v1",
            "runtime": runtime,
            "simulation_scale": scale,
            "features": features,
            "core": core,
            "collision_layers": layers,
            "materials": materials,
            "interaction_rules": rules,
            "performance_policy": performance,
            "solver": {
                "position_iterations": (
                    8
                    if scale in {"large", "extreme"}
                    else 6
                ),
                "velocity_iterations": (
                    4
                    if scale in {"large", "extreme"}
                    else 3
                ),
                "warm_starting": True,
                "constraint_stabilization": True,
            },
        }


# ============================================================================
# PHYSICS AGENT
# ============================================================================

class PhysicsAgent(GodBaseAgent):
    """
    Production physics architecture specialist.

    Existing-compatible call:
        await agent.perform_role(environment_details)

    Extended call:
        await agent.perform_role(
            environment_details,
            game_plan=...,
            assets=...,
            world=...,
            build_id=...,
            target_platform=...,
            context=...,
        )

    The agent plans physics. It does not falsely claim that the actual physics
    engine has executed the generated configuration.
    """

    role_name = "Physics, Collision & Simulation Architecture Specialist"
    service_type = "brain"

    def __init__(
        self,
        *,
        gateway: Any = None,
        config: Any = None,
    ) -> None:

        super().__init__(
            role_name=self.role_name,
            service_type=self.service_type,
            gateway=gateway,
            config=config,
            required_capabilities={
                "text_generation",
                "structured_output",
            },
            temperature=0.12,
            metadata={
                "contract": "riot.physics.v1",
                "stage": "physics_config",
                "agent_version": "riot.physics.v2",
            },
        )

        self._intent_analyzer = PhysicsIntentAnalyzer()

    # ------------------------------------------------------------------
    # Directive builder
    # ------------------------------------------------------------------

    def build_directive(
        self,
        environment_details: Mapping[str, Any],
        physics_baseline: Mapping[str, Any],
    ) -> str:

        baseline_json = _bounded_json(
            physics_baseline,
        )

        environment_json = _bounded_json(
            environment_details,
        )

        return f"""
You are the Physics, Collision & Simulation Architecture Specialist inside
the Riot / God Node game-generation engine.

You are part of a production game-generation pipeline.

UPSTREAM
========

Director
  -> Asset planning
  -> World/Map planning
  -> Physics
  -> Gameplay
  -> Runtime
  -> Builder
  -> QA

Your responsibility is to produce a precise physics/simulation contract.

You are NOT merely a prompt-to-description model.

You must reason about:

- gravity
- fixed timestep
- time scale
- rigid bodies
- collision detection
- collision layers
- material interactions
- friction
- restitution
- continuous collision detection
- character movement
- vehicle movement where required
- projectile behavior where required
- triggers
- constraints
- sleeping
- solver iteration
- deterministic simulation
- performance limits
- event emission
- runtime integration
- platform restrictions

CRITICAL RULES
==============

1. Preserve upstream world/asset/game-plan information.
2. Do not invent assets that were not supplied.
3. Do not claim the physics simulation was executed.
4. Do not claim collision tests passed unless real evidence is supplied.
5. Do not claim a runtime, build or compiled physics backend exists.
6. Physics values must be internally coherent.
7. Collision layers must have stable names and numeric identifiers.
8. Use a fixed simulation timestep for real-time physics.
9. Fast-moving projectiles should use CCD semantics when applicable.
10. Do not allocate a separate physics world per asset.
11. Do not embed provider/API logic here.
12. Do not create a second gateway connection.
13. Downstream runtime must be able to convert this into its actual physics backend.
14. Report uncertainties in warnings/errors instead of fabricating data.
15. Return structured JSON only.

PHYSICS CONTRACT
================

Return exactly this high-level shape:

{{
  "status": "SUCCESS|FAILED",
  "data": {{
    "contract_version": "riot.physics.v1",
    "simulation": {{
      "gravity": {{
        "x": 0,
        "y": -9.81,
        "z": 0
      }},
      "friction_default": 0.55,
      "restitution_default": 0.15,
      "time_scale": 1.0,
      "physics_engine": "...",
      "fixed_timestep": 0.0166667,
      "max_substeps": 4,
      "deterministic": true
    }},
    "collision_layers": {{}},
    "materials": [],
    "rigid_body_policy": {{}},
    "character_controller": {{}},
    "vehicle_physics": {{}},
    "projectile_physics": {{}},
    "interaction_rules": [],
    "solver": {{}},
    "performance_policy": {{}},
    "events": [],
    "dependencies": [],
    "validation_requirements": {{}},
    "metadata": {{}}
  }},
  "warnings": [],
  "errors": []
}}

RIGID BODY POLICY
=================

For every category that uses dynamic physics specify:

- static/dynamic/kinematic semantics
- mass policy
- center of mass policy
- linear damping
- angular damping
- sleep policy
- interpolation
- CCD
- allowed collision layers
- runtime update requirements

CHARACTER CONTROLLER
====================

When characters are present specify:

- capsule/controller representation
- height
- radius
- step height
- slope limit
- ground detection
- movement acceleration
- braking
- jump/gravity interaction
- collision filtering
- moving-platform handling

Do not use a full rigid-body controller unless the supplied game design explicitly
requires it.

VEHICLE PHYSICS
===============

When vehicles are present specify:

- wheel/contact model
- suspension
- steering
- traction
- longitudinal force
- lateral force
- drag
- braking
- rollover protection
- surface friction interaction

PROJECTILES
===========

When projectiles are present specify:

- initial velocity
- gravity influence
- drag
- CCD/query mode
- hit filtering
- impact event
- lifetime/despawn

PERFORMANCE
===========

The physics system must remain bounded.

Avoid:

- unbounded dynamic bodies
- unbounded contact events
- per-frame full-world collision scans
- duplicate collision queries
- unlimited substeps

Prefer:

- broadphase filtering
- sleeping
- query batching
- event batching
- fixed-step simulation
- collision layers
- bounded active-body budgets

UPSTREAM PHYSICS BASELINE
=========================

{baseline_json}

UPSTREAM ENVIRONMENT
====================

{environment_json}

Return JSON only.
""".strip()

    # ------------------------------------------------------------------
    # Public execution contract
    # ------------------------------------------------------------------

    async def perform_role(
        self,
        environment_details: Optional[dict] = None,
        *,
        game_plan: Any = None,
        assets: Any = None,
        world: Any = None,
        build_id: str = "",
        target_platform: str = "",
        context: Optional[Mapping[str, Any]] = None,
    ) -> dict:

        environment: Dict[str, Any] = dict(
            environment_details or {}
        )

        # Keep all upstream information attached to one bounded context.
        if game_plan is not None:
            environment["game_plan"] = _safe_json(
                game_plan
            )

        if assets is not None:
            environment["assets"] = _safe_json(
                assets
            )

        if world is not None:
            environment["world"] = _safe_json(
                world
            )

        if context:
            environment["context"] = _safe_json(
                context
            )

        if build_id:
            environment["build_id"] = str(
                build_id
            ).strip()

        if target_platform:
            environment["target_platform"] = str(
                target_platform
            ).strip()

        analysis = self._intent_analyzer.build(
            environment,
        )

        features = analysis["features"]

        rigid_body_policy = {
            "default_body_type": "static_or_kinematic",
            "dynamic_body_enabled_for": _unique(
                [
                    "characters"
                    if features.get("characters")
                    else None,
                    "vehicles"
                    if features.get("vehicles")
                    else None,
                    "projectiles"
                    if features.get("projectiles")
                    else None,
                    "destructible_objects"
                    if features.get("destruction")
                    else None,
                    "interactive_objects"
                    if features.get("interactive_objects")
                    else None,
                ],
                16,
            ),
            "mass_policy": {
                "static": "infinite",
                "dynamic": "asset_or_category_defined",
                "kinematic": "explicit_controller_defined",
            },
            "linear_damping_default": 0.05,
            "angular_damping_default": 0.05,
            "sleeping": True,
            "interpolation": True,
            "continuous_collision": analysis["core"][
                "continuous_collision_detection"
            ],
        }

        character_controller = {
            "enabled": features.get(
                "characters",
                False,
            ),
            "representation": "capsule",
            "height": 1.8,
            "radius": 0.45,
            "step_height": 0.45,
            "slope_limit_degrees": 42.0,
            "ground_detection": "shape_cast_or_engine_ground_query",
            "movement_model": "acceleration_based",
            "braking": True,
            "moving_platform_support": True,
            "dynamic_rigid_body_controller": False,
        }

        vehicle_physics = {
            "enabled": features.get(
                "vehicles",
                False,
            ),
            "model": (
                "wheel_suspension_and_contact"
                if features.get("vehicles")
                else "disabled"
            ),
            "traction_model": "surface_friction_aware",
            "steering_model": "speed_sensitive",
            "braking_model": "axle_weighted",
            "suspension": {
                "enabled": True,
                "travel": 0.30,
                "spring": 28000.0,
                "damper": 4200.0,
            },
            "rollover_protection": True,
        }

        projectile_physics = {
            "enabled": features.get(
                "projectiles",
                False,
            ),
            "integration": "fixed_step",
            "continuous_collision_detection": features.get(
                "projectiles",
                False,
            ),
            "gravity_enabled": True,
            "drag_enabled": True,
            "lifetime_seconds": 8.0,
            "hit_event_required": True,
        }

        events = [
            {
                "event_id": "physics_collision",
                "trigger": "contact_resolved",
                "payload": [
                    "body_a",
                    "body_b",
                    "contact_point",
                    "normal",
                    "relative_velocity",
                    "impulse",
                ],
            },
            {
                "event_id": "physics_trigger_enter",
                "trigger": "trigger_enter",
                "payload": [
                    "trigger_id",
                    "entity_id",
                ],
            },
            {
                "event_id": "physics_trigger_exit",
                "trigger": "trigger_exit",
                "payload": [
                    "trigger_id",
                    "entity_id",
                ],
            },
        ]

        if features.get("projectiles"):
            events.append(
                {
                    "event_id": "physics_projectile_hit",
                    "trigger": "projectile_collision",
                    "payload": [
                        "projectile_id",
                        "target_id",
                        "impact_point",
                        "impact_normal",
                        "impact_speed",
                    ],
                }
            )

        if features.get("destruction"):
            events.append(
                {
                    "event_id": "physics_destruction",
                    "trigger": "destruction_threshold",
                    "payload": [
                        "entity_id",
                        "damage",
                        "impulse",
                    ],
                }
            )

        dependencies = [
            "director.architecture_plan",
            "world.world_manifest",
            "asset.asset_manifest",
            "runtime.simulation_scheduler",
            "runtime.collision_system",
            "gameplay.entity_state",
        ]

        validation_requirements = {
            "gravity_vector_valid": True,
            "fixed_timestep_positive": True,
            "collision_layers_unique": True,
            "material_ranges_valid": True,
            "no_nan_infinity_values": True,
            "bounded_dynamic_body_count": True,
            "bounded_substeps": True,
            "no_execution_claims": True,
            "world_asset_references_preserved": True,
            "collision_events_structured": True,
        }

        physics_contract = {
            "contract_version": "riot.physics.v1",
            "simulation": analysis["core"],
            "collision_layers": analysis[
                "collision_layers"
            ],
            "materials": analysis[
                "materials"
            ],
            "rigid_body_policy": rigid_body_policy,
            "character_controller": character_controller,
            "vehicle_physics": vehicle_physics,
            "projectile_physics": projectile_physics,
            "interaction_rules": analysis[
                "interaction_rules"
            ],
            "solver": analysis[
                "solver"
            ],
            "performance_policy": analysis[
                "performance_policy"
            ],
            "events": events,
            "dependencies": dependencies,
            "validation_requirements": validation_requirements,
            "metadata": {
                "agent_contract": "riot.physics.v1",
                "agent_version": "riot.physics.v2",
                "build_id": str(
                    build_id or ""
                ).strip(),
                "target_platform": str(
                    target_platform
                    or analysis["runtime"][
                        "target_platform"
                    ]
                ),
                "simulation_scale": analysis[
                    "simulation_scale"
                ],
                "feature_flags": features,
                "execution_status": "PLANNED_NOT_EXECUTED",
            },
        }

        directive = self.build_directive(
            environment_details=environment,
            physics_baseline=physics_contract,
        )

        model_result = await self.think_and_execute(
            task_directive=directive,
            context={
                "physics_baseline": physics_contract,
                "upstream": _safe_json(environment),
            },
        )

        return {
            "status": "SUCCESS",
            "data": physics_contract,
            "model_refinement": _safe_json(
                model_result
            ),
            "warnings": [],
            "errors": [],
            "metadata": {
                "agent_contract": "riot.physics.v1",
                "agent_version": "riot.physics.v2",
                "execution_status": "PLANNED_NOT_EXECUTED",
            },
        }
