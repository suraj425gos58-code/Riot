"""
Riot / God Node — Production Gameplay Synthesis Agent
======================================================

Purpose
-------
Turns the Director/asset/world/physics contracts into an executable gameplay
specification that downstream orchestration can compile into real source.

Design goals
------------
* Deterministic, bounded, machine-readable output.
* No fabricated execution/build/QA claims.
* Keeps gameplay systems explicit: state, entities, actions, rules, events,
  win/lose conditions, input mapping, camera, save-state requirements and
  runtime constraints.
* Accepts both the current orchestrator contract and direct callers.
* Uses GodBaseAgent for provider routing, timeouts, cancellation and output
  validation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from god_brain.agents.base_agent import GodBaseAgent


# ============================================================================
# HARD LIMITS
# ============================================================================

MAX_ENTITIES = 256
MAX_SYSTEMS = 96
MAX_ACTIONS = 256
MAX_RULES = 512
MAX_EVENTS = 512
MAX_VARIABLES = 256
MAX_SOURCE_FILES = 128
MAX_STRING = 2048


# ============================================================================
# VALIDATED OUTPUT CONTRACTS
# ============================================================================

class GameplayEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    name: str
    archetype: str = "generic"
    asset_ref: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)
    initial_state: Dict[str, Any] = Field(default_factory=dict)
    spawn_rules: List[str] = Field(default_factory=list)


class GameplayAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_id: str
    name: str
    actor: str = "player"
    trigger: str
    preconditions: List[str] = Field(default_factory=list)
    effects: List[str] = Field(default_factory=list)
    cooldown_seconds: float = Field(default=0.0, ge=0.0, le=3600.0)
    repeatable: bool = True


class GameplayEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str
    name: str
    trigger: str
    effects: List[str] = Field(default_factory=list)
    priority: int = Field(default=50, ge=0, le=100)


class GameplayRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rule_id: str
    condition: str
    consequence: str
    priority: int = Field(default=50, ge=0, le=100)
    once: bool = False


class GameplaySystem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    system_id: str
    name: str
    purpose: str
    update_frequency: str = "event"
    dependencies: List[str] = Field(default_factory=list)
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)


class GameplayVariable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: str = "number"
    default: Any = 0
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    persistent: bool = False


class InputBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_id: str
    keyboard: List[str] = Field(default_factory=list)
    mouse: List[str] = Field(default_factory=list)
    touch: List[str] = Field(default_factory=list)
    gamepad: List[str] = Field(default_factory=list)


class GameplaySpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = "riot.gameplay.v1"
    title: str = "Riot Generated Gameplay"
    core_loop: str = ""
    runtime_model: str = "realtime"
    camera_model: str = "third_person"
    entities: List[GameplayEntity] = Field(default_factory=list)
    systems: List[GameplaySystem] = Field(default_factory=list)
    actions: List[GameplayAction] = Field(default_factory=list)
    events: List[GameplayEvent] = Field(default_factory=list)
    rules: List[GameplayRule] = Field(default_factory=list)
    variables: List[GameplayVariable] = Field(default_factory=list)
    input_bindings: List[InputBinding] = Field(default_factory=list)
    win_conditions: List[str] = Field(default_factory=list)
    lose_conditions: List[str] = Field(default_factory=list)
    save_state: List[str] = Field(default_factory=list)
    runtime_constraints: List[str] = Field(default_factory=list)
    source_requirements: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GameplayEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "SUCCESS"
    data: GameplaySpec = Field(default_factory=GameplaySpec)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


# ============================================================================
# HELPERS
# ============================================================================

def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:MAX_STRING]


def _bounded_unique(values: Iterable[Any], limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = _clean(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(_clean(item) for item in parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _safe_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_json(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _safe_json(model_dump(mode="json"))
        except Exception:
            pass
    return str(value)


def _unwrap(value: Any) -> Any:
    current = value
    for _ in range(6):
        if not isinstance(current, Mapping):
            return current

        if "data" in current and len(current) <= 8:
            current = current["data"]
            continue
        if "result" in current and len(current) <= 8:
            current = current["result"]
            continue
        if "output" in current and len(current) <= 8:
            current = current["output"]
            continue
        break
    return current


def _collection(value: Any, *keys: str, limit: int) -> List[Any]:
    current = _unwrap(value)
    if isinstance(current, Mapping):
        for key in keys:
            candidate = current.get(key)
            if isinstance(candidate, list):
                return candidate[:limit]
        return [current]
    if isinstance(current, list):
        return current[:limit]
    return [current] if current not in (None, "") else []


# ============================================================================
# AGENT
# ============================================================================

class GameplayAgent(GodBaseAgent):
    """
    Specialist gameplay compiler.

    The agent returns a GameplayEnvelope whose `data` is deliberately
    declarative. It does not claim that the resulting game was executed or
    compiled; downstream source assembly owns that responsibility.
    """

    role_name = "Gameplay Systems Architect"
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
            required_capabilities={"text_generation"},
            temperature=0.15,
            metadata={"contract": "riot.gameplay.v1"},
        )

    @staticmethod
    def _normalize_direct_inputs(
        game_plan: Any,
        generated_assets: Any,
        world: Any,
        physics: Any,
        prompt: str = "",
    ) -> Dict[str, Any]:
        plan = _safe_json(game_plan)
        assets = _collection(
            generated_assets, "assets", "items", "results",
            limit=MAX_ENTITIES,
        )
        world_data = _collection(
            world, "world", "sectors", "chunks", "items", "results",
            limit=MAX_ENTITIES,
        )
        physics_data = _safe_json(physics)

        asset_ids: List[str] = []
        for index, asset in enumerate(assets):
            if isinstance(asset, Mapping):
                raw_id = asset.get("id") or asset.get("asset_id")
            else:
                raw_id = None
            asset_ids.append(_clean(raw_id, _stable_id("asset", index, asset)))

        return {
            "prompt": _clean(prompt, "Generated game"),
            "game_plan": plan,
            "generated_assets": assets,
            "world": world_data,
            "physics": physics_data,
            "asset_ids": asset_ids,
        }

    def build_directive(self, context: Mapping[str, Any]) -> str:
        context_json = json.dumps(
            _safe_json(context),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return f"""
Compile a production-grade declarative gameplay specification for the supplied
Riot game project.

Required output:
{{
  "status": "SUCCESS",
  "data": {{
    "schema_version": "riot.gameplay.v1",
    "title": "...",
    "core_loop": "...",
    "runtime_model": "realtime|turn_based|hybrid",
    "camera_model": "...",
    "entities": [],
    "systems": [],
    "actions": [],
    "events": [],
    "rules": [],
    "variables": [],
    "input_bindings": [],
    "win_conditions": [],
    "lose_conditions": [],
    "save_state": [],
    "runtime_constraints": [],
    "source_requirements": [],
    "metadata": {{}}
  }},
  "warnings": [],
  "errors": []
}}

Hard requirements:
1. Every referenced entity, action, event, variable, and system must have an
   explicit stable id/name.
2. References must point to supplied asset/world ids whenever those ids exist.
3. Encode gameplay as deterministic state transitions and event/rule effects.
4. Include player controls for keyboard/mouse/touch/gamepad as applicable to the
   target platform.
5. Do not fabricate execution evidence, rendering evidence, compilation,
   completed QA, or real artifacts.
6. Do not return placeholder words such as "TBD", "lorem", or "placeholder".
7. Keep the specification bounded and implementable by a browser/native runtime.
8. Physics behavior must respect the supplied physics contract rather than
   inventing contradictory gravity/collision assumptions.
9. Prefer data-driven rules over embedding the whole game as prose.
10. Return JSON only.

PROJECT CONTEXT:
{context_json}
""".strip()

    @staticmethod
    def _fallback_spec(context: Mapping[str, Any]) -> GameplayEnvelope:
        """
        Deterministic fallback for callers that want a structurally valid
        contract even when an external model is unavailable.

        It is not presented as AI-generated gameplay; metadata explicitly marks
        the result as a deterministic baseline.
        """
        prompt = _clean(context.get("prompt"), "Riot Generated Game")
        assets = context.get("generated_assets") or []
        asset_ids = [
            _clean(item.get("id") or item.get("asset_id"))
            for item in assets
            if isinstance(item, Mapping) and (item.get("id") or item.get("asset_id"))
        ]

        player_id = _stable_id("entity", prompt, "player")
        world_id = _stable_id("entity", prompt, "world_controller")

        entities = [
            GameplayEntity(
                entity_id=player_id,
                name="Player",
                archetype="player",
                components=["transform", "input", "health", "motion"],
                initial_state={"alive": True},
            ),
            GameplayEntity(
                entity_id=world_id,
                name="World Controller",
                archetype="world_controller",
                components=["world", "rules", "event_dispatch"],
                initial_state={"state": "active"},
            ),
        ]

        if asset_ids:
            for index, asset_id in enumerate(asset_ids[:MAX_ENTITIES - 2]):
                entities.append(
                    GameplayEntity(
                        entity_id=_stable_id("entity_asset", asset_id, index),
                        name=f"Asset Entity {index + 1}",
                        archetype="asset",
                        asset_ref=asset_id,
                        components=["transform"],
                    )
                )

        systems = [
            GameplaySystem(
                system_id="system_input",
                name="Input System",
                purpose="Convert platform input into gameplay actions.",
                update_frequency="event",
                outputs=["gameplay_actions"],
            ),
            GameplaySystem(
                system_id="system_rules",
                name="Gameplay Rule System",
                purpose="Evaluate conditions and apply deterministic state effects.",
                update_frequency="frame",
                dependencies=["system_input"],
                inputs=["gameplay_actions", "world_state"],
                outputs=["events", "state_changes"],
            ),
            GameplaySystem(
                system_id="system_physics",
                name="Physics Integration",
                purpose="Apply movement and collision using the supplied physics model.",
                update_frequency="fixed_step",
                dependencies=["system_input"],
                inputs=["motion_state", "collision_state"],
                outputs=["transform", "collision_events"],
            ),
        ]

        actions = [
            GameplayAction(
                action_id="action_move",
                name="Move",
                actor=player_id,
                trigger="direction_input",
                effects=["update player velocity from directional input"],
            ),
            GameplayAction(
                action_id="action_interact",
                name="Interact",
                actor=player_id,
                trigger="interact_input",
                preconditions=["player is alive"],
                effects=["dispatch nearest valid interaction event"],
            ),
        ]

        events = [
            GameplayEvent(
                event_id="event_game_start",
                name="Game Started",
                trigger="runtime_initialized",
                effects=["set game state active"],
                priority=100,
            ),
            GameplayEvent(
                event_id="event_player_dead",
                name="Player Defeated",
                trigger="player.health <= 0",
                effects=["set player alive false", "evaluate lose conditions"],
                priority=90,
            ),
        ]

        rules = [
            GameplayRule(
                rule_id="rule_player_survival",
                condition="player.alive == false",
                consequence="enter defeat state",
                priority=100,
            ),
            GameplayRule(
                rule_id="rule_interaction",
                condition="action.interact triggered and target is valid",
                consequence="dispatch interaction event",
                priority=60,
            ),
        ]

        variables = [
            GameplayVariable(
                name="game_state",
                type="string",
                default="active",
                persistent=False,
            ),
            GameplayVariable(
                name="player_health",
                type="number",
                default=100,
                min_value=0,
                persistent=True,
            ),
        ]

        bindings = [
            InputBinding(
                action_id="action_move",
                keyboard=["W", "A", "S", "D", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"],
                gamepad=["left_stick"],
                touch=["virtual_joystick"],
            ),
            InputBinding(
                action_id="action_interact",
                keyboard=["E"],
                gamepad=["button_south"],
                touch=["interact_button"],
            ),
        ]

        return GameplayEnvelope(
            status="SUCCESS",
            data=GameplaySpec(
                title=prompt,
                core_loop="Explore -> interact -> resolve hazards/objectives -> progress -> win or lose",
                runtime_model="realtime",
                camera_model="third_person",
                entities=entities[:MAX_ENTITIES],
                systems=systems,
                actions=actions,
                events=events,
                rules=rules,
                variables=variables,
                input_bindings=bindings,
                win_conditions=["primary objective is completed"],
                lose_conditions=["player_health <= 0"],
                save_state=["game_state", "player_health", "player_transform", "objective_progress"],
                runtime_constraints=[
                    "fixed-step physics integration",
                    "deterministic state transitions",
                    "bounded entity updates",
                ],
                source_requirements=[
                    "input subsystem",
                    "state machine",
                    "event dispatcher",
                    "physics adapter",
                    "save-state serializer",
                ],
                metadata={
                    "generation_mode": "deterministic_baseline",
                    "asset_count": len(asset_ids),
                    "context_sha256": hashlib.sha256(
                        json.dumps(
                            _safe_json(context),
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest(),
                },
            ),
        )

    @staticmethod
    def _validate_cross_references(spec: GameplaySpec) -> List[str]:
        issues: List[str] = []
        entity_ids = {item.entity_id for item in spec.entities}
        action_ids = {item.action_id for item in spec.actions}
        system_ids = {item.system_id for item in spec.systems}
        variable_names = {item.name for item in spec.variables}
        event_ids = {item.event_id for item in spec.events}

        for action in spec.actions:
            if action.actor not in entity_ids and action.actor != "player":
                issues.append(
                    f"action {action.action_id} references unknown actor {action.actor}"
                )

        for system in spec.systems:
            for dependency in system.dependencies:
                if dependency not in system_ids:
                    issues.append(
                        f"system {system.system_id} references unknown dependency {dependency}"
                    )

        for binding in spec.input_bindings:
            if binding.action_id not in action_ids:
                issues.append(
                    f"input binding references unknown action {binding.action_id}"
                )

        # Detect obvious raw object-id placeholders in metadata.
        metadata_text = json.dumps(_safe_json(spec.metadata), ensure_ascii=False)
        if re.search(r"\b(TBD|TODO|placeholder|lorem ipsum)\b", metadata_text, re.I):
            issues.append("metadata contains placeholder markers")

        # Variables/events are intentionally string-referenced in declarative
        # conditions; verify at least that named symbols are not empty.
        if not variable_names:
            issues.append("gameplay specification contains no state variables")
        if not event_ids:
            issues.append("gameplay specification contains no events")

        return issues

    def perform_role(
        self,
        game_plan: Any = None,
        generated_assets: Any = None,
        world: Any = None,
        physics: Any = None,
        prompt: str = "",
        *,
        allow_deterministic_fallback: bool = False,
    ) -> dict:
        context = self._normalize_direct_inputs(
            game_plan=game_plan,
            generated_assets=generated_assets,
            world=world,
            physics=physics,
            prompt=prompt,
        )

        directive = self.build_directive(context)

        try:
            raw = self.think_and_execute_result(
                task_directive=directive,
                context=context,
                response_schema=GameplayEnvelope,
                required_capabilities={"text_generation"},
                metadata={
                    "gameplay_compile": True,
                    "asset_count": len(context["generated_assets"]),
                    "world_count": len(context["world"]),
                },
            )
        except Exception as exc:
            if not allow_deterministic_fallback:
                return {
                    "status": "FAILED",
                    "data": None,
                    "warnings": [],
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
            return self._fallback_spec(context).model_dump(mode="json")

        if hasattr(raw, "to_dict"):
            payload = raw.to_dict()
        elif isinstance(raw, Mapping):
            payload = dict(raw)
        else:
            payload = {"status": "FAILED", "errors": ["unexpected agent result type"]}

        # BaseAgent returns an AgentResult envelope. Extract its data without
        # confusing a transport envelope with the GameplayEnvelope contract.
        candidate = payload.get("data")
        if isinstance(candidate, Mapping):
            candidate_status = str(candidate.get("status", "")).upper()
            if candidate_status in {"SUCCESS", "FAILED"} and "data" in candidate:
                payload = candidate

        try:
            envelope = GameplayEnvelope.model_validate(payload)
        except ValidationError as exc:
            return {
                "status": "INVALID_OUTPUT",
                "data": None,
                "warnings": [],
                "errors": [f"Gameplay schema validation failed: {exc}"],
            }

        if envelope.status.upper() != "SUCCESS":
            return envelope.model_dump(mode="json")

        issues = self._validate_cross_references(envelope.data)
        if issues:
            return {
                "status": "INVALID_OUTPUT",
                "data": envelope.data.model_dump(mode="json"),
                "warnings": envelope.warnings,
                "errors": issues,
            }

        # Explicit evidence marker: this is a gameplay specification, not a
        # compiled/executed game.
        envelope.data.metadata["execution_evidence"] = "none"
        envelope.data.metadata["build_evidence"] = "none"
        envelope.data.metadata["qa_evidence"] = "none"

        return envelope.model_dump(mode="json")


__all__ = [
    "GameplayAction",
    "GameplayEntity",
    "GameplayEvent",
    "GameplayRule",
    "GameplaySystem",
    "GameplayVariable",
    "InputBinding",
    "GameplaySpec",
    "GameplayEnvelope",
    "GameplayAgent",
]
