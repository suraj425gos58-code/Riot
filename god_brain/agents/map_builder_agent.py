from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from god_brain.agents.base_agent import GodBaseAgent


# ============================================================================
# PRODUCTION LIMITS
# ============================================================================

MAX_THEME_CHARS = 4096
MAX_ASSETS = 256
MAX_SYSTEMS = 64
MAX_PLACEMENTS = 512
MAX_STREAMING_ZONES = 64
MAX_TAGS = 48
MAX_DEPENDENCIES = 64


# ============================================================================
# DETERMINISTIC HELPERS
# ============================================================================

def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:MAX_THEME_CHARS]


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


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ============================================================================
# WORLD INTENT ANALYZER
# ============================================================================

class WorldIntentAnalyzer:
    """
    Deterministic planning layer for the MapBuilderAgent.

    This layer never claims that a real map artifact exists.
    It derives a bounded world specification which can later be consumed by
    orchestration, scene generation, runtime and QA.
    """

    _BIOME_RULES = (
        (
            "urban",
            (
                "city",
                "urban",
                "downtown",
                "street",
                "metropolis",
                "town",
                "neighborhood",
                "apartment",
            ),
        ),
        (
            "rural",
            (
                "village",
                "farm",
                "rural",
                "countryside",
                "field",
            ),
        ),
        (
            "forest",
            (
                "forest",
                "jungle",
                "woods",
                "tree",
                "wild",
            ),
        ),
        (
            "desert",
            (
                "desert",
                "sand",
                "dune",
                "dry",
                "wasteland",
            ),
        ),
        (
            "snow",
            (
                "snow",
                "ice",
                "arctic",
                "frozen",
                "winter",
            ),
        ),
        (
            "island",
            (
                "island",
                "ocean",
                "sea",
                "coast",
                "beach",
            ),
        ),
        (
            "scifi",
            (
                "sci-fi",
                "scifi",
                "cyberpunk",
                "futuristic",
                "space",
                "alien",
            ),
        ),
        (
            "fantasy",
            (
                "fantasy",
                "magic",
                "medieval",
                "castle",
                "kingdom",
            ),
        ),
        (
            "horror",
            (
                "horror",
                "haunted",
                "dark",
                "abandoned",
                "graveyard",
            ),
        ),
    )

    def classify_biome(self, theme: str) -> str:
        text = theme.lower()

        for biome, keywords in self._BIOME_RULES:
            if any(keyword in text for keyword in keywords):
                return biome

        return "default"

    def classify_scale(
        self,
        theme: str,
        plan: Mapping[str, Any],
    ) -> str:
        text = (
            theme
            + " "
            + str(plan.get("complexity_class", ""))
            + " "
            + str(plan.get("systems", ""))
        ).lower()

        extreme_terms = (
            "open world",
            "massive world",
            "huge city",
            "gta",
            "mmorpg",
            "persistent world",
            "massive multiplayer",
        )

        heavy_terms = (
            "large world",
            "large map",
            "open map",
            "large city",
            "multiple regions",
            "streaming",
        )

        compact_terms = (
            "small map",
            "arena",
            "single room",
            "indoor",
            "corridor",
            "compact",
        )

        if any(term in text for term in extreme_terms):
            return "extreme"

        if any(term in text for term in heavy_terms):
            return "large"

        if any(term in text for term in compact_terms):
            return "compact"

        return "standard"

    def build_dimensions(
        self,
        scale: str,
        sector_index: int,
    ) -> Dict[str, float]:
        base = {
            "compact": 256.0,
            "standard": 512.0,
            "large": 1024.0,
            "extreme": 2048.0,
        }.get(scale, 512.0)

        # A sector is intentionally smaller than the whole world.
        sector_size = base / {
            "compact": 1.0,
            "standard": 2.0,
            "large": 4.0,
            "extreme": 8.0,
        }.get(scale, 2.0)

        return {
            "world_width": base,
            "world_depth": base,
            "world_height": max(128.0, base * 0.25),
            "sector_width": max(128.0, sector_size),
            "sector_depth": max(128.0, sector_size),
            "sector_height": max(64.0, sector_size * 0.25),
            "sector_index": float(max(0, sector_index)),
        }

    def build_streaming_policy(
        self,
        scale: str,
        dimensions: Mapping[str, Any],
    ) -> Dict[str, Any]:

        if scale == "compact":
            zone_count = 1
            load_distance = 180.0
            unload_distance = 240.0
        elif scale == "standard":
            zone_count = 2
            load_distance = 220.0
            unload_distance = 320.0
        elif scale == "large":
            zone_count = 4
            load_distance = 300.0
            unload_distance = 450.0
        else:
            zone_count = 8
            load_distance = 450.0
            unload_distance = 650.0

        return {
            "enabled": True,
            "zone_count": zone_count,
            "load_distance": load_distance,
            "unload_distance": unload_distance,
            "strategy": (
                "sector_streaming"
                if scale in {"large", "extreme"}
                else "distance_streaming"
            ),
            "priority_policy": "gameplay-first",
            "preload_radius": load_distance * 0.60,
            "memory_reclaim_after_unload": True,
            "allow_parallel_sector_generation": scale != "compact",
            "dimensions": dict(dimensions),
        }

    def build_navigation_policy(
        self,
        biome: str,
        theme: str,
    ) -> Dict[str, Any]:

        outdoor = biome in {
            "urban",
            "rural",
            "forest",
            "desert",
            "snow",
            "island",
            "fantasy",
            "scifi",
        }

        return {
            "navigation_required": True,
            "navigation_type": (
                "navmesh"
                if outdoor
                else "hybrid_grid_navmesh"
            ),
            "agent_radius": 0.45,
            "agent_height": 1.8,
            "max_slope_degrees": 42.0,
            "step_height": 0.45,
            "dynamic_obstacles": True,
            "off_mesh_links": biome in {"urban", "fantasy", "scifi"},
            "path_recalculation": "event_driven",
            "theme_context": theme[:512],
        }

    def classify_asset_role(
        self,
        asset: Mapping[str, Any],
    ) -> str:

        text = (
            str(asset.get("asset_type", ""))
            + " "
            + str(asset.get("name", ""))
            + " "
            + str(asset.get("type", ""))
        ).lower()

        if any(term in text for term in (
            "terrain",
            "ground",
            "landscape",
            "road",
        )):
            return "terrain"

        if any(term in text for term in (
            "building",
            "house",
            "tower",
            "shop",
            "structure",
        )):
            return "structure"

        if any(term in text for term in (
            "vehicle",
            "car",
            "truck",
            "bike",
        )):
            return "vehicle"

        if any(term in text for term in (
            "character",
            "npc",
            "enemy",
            "player",
            "human",
        )):
            return "actor"

        if any(term in text for term in (
            "tree",
            "rock",
            "bush",
            "prop",
            "decoration",
        )):
            return "environment_prop"

        return "generic"

    def _asset_id(self, asset: Any, index: int) -> str:
        if isinstance(asset, Mapping):
            raw_id = (
                asset.get("asset_id")
                or asset.get("id")
                or asset.get("request_id")
            )
            if raw_id:
                return str(raw_id)

        return _stable_id(
            "asset_ref",
            index,
            json.dumps(_safe_json(asset), sort_keys=True),
        )

    def normalize_assets(
        self,
        generated_assets: Any,
    ) -> List[Dict[str, Any]]:

        if generated_assets is None:
            return []

        if isinstance(generated_assets, Mapping):
            candidate = (
                generated_assets.get("assets")
                or generated_assets.get("items")
                or generated_assets.get("results")
            )

            if isinstance(candidate, list):
                generated_assets = candidate
            else:
                generated_assets = [generated_assets]

        elif not isinstance(generated_assets, (list, tuple)):
            generated_assets = [generated_assets]

        normalized: List[Dict[str, Any]] = []

        for index, asset in enumerate(generated_assets[:MAX_ASSETS]):
            if isinstance(asset, Mapping):
                data = dict(_safe_json(asset))
            else:
                data = {
                    "name": str(asset),
                }

            data["asset_id"] = self._asset_id(
                data,
                index,
            )

            data["world_role"] = self.classify_asset_role(
                data,
            )

            normalized.append(data)

        return normalized

    def derive_spawn_policy(
        self,
        biome: str,
        assets: List[Dict[str, Any]],
    ) -> Dict[str, Any]:

        actor_count = sum(
            1
            for asset in assets
            if asset.get("world_role") == "actor"
        )

        vehicle_count = sum(
            1
            for asset in assets
            if asset.get("world_role") == "vehicle"
        )

        structure_count = sum(
            1
            for asset in assets
            if asset.get("world_role") == "structure"
        )

        return {
            "spawn_point": {
                "x": 0.0,
                "y": 1.0,
                "z": 0.0,
            },
            "actor_spawn_strategy": (
                "distributed_navigation_nodes"
                if actor_count > 0
                else "player_only"
            ),
            "vehicle_spawn_strategy": (
                "road_network_slots"
                if vehicle_count > 0
                else "disabled"
            ),
            "structure_distribution": (
                "road_aligned"
                if biome == "urban"
                else "biome_clustered"
            ),
            "minimum_spawn_clearance": 2.0,
            "avoid_spawn_inside_collision": True,
            "safe_start_zone": True,
            "avoid_streaming_boundary": True,
            "source_asset_counts": {
                "actors": actor_count,
                "vehicles": vehicle_count,
                "structures": structure_count,
            },
        }


# ============================================================================
# MAP BUILDER AGENT
# ============================================================================

class MapBuilderAgent(GodBaseAgent):
    """
    Production world/scene planning specialist.

    Output is a structured world-sector contract rather than a fake claim
    that a real scene has already been rendered.

    Existing orchestrator compatibility:
        perform_role(environment_theme, generated_assets)

    Extended contract:
        perform_role(
            environment_theme,
            generated_assets,
            game_plan=...,
            prompt=...,
            sector_index=...,
            build_id=...,
        )
    """

    role_name = "Environment, World & Scene Architecture Specialist"
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
            temperature=0.15,
            metadata={
                "contract": "riot.world.sector.v1",
                "stage": "world_generation",
                "agent_version": "riot.map.v2",
            },
        )

        self._intent_analyzer = WorldIntentAnalyzer()

    # ------------------------------------------------------------------
    # Directive
    # ------------------------------------------------------------------

    def build_directive(
        self,
        environment_theme: str,
        analysis: Mapping[str, Any],
    ) -> str:

        analysis_json = json.dumps(
            _safe_json(analysis),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return f"""
You are the Environment, World & Scene Architecture Specialist inside the
Riot / God Node game-generation engine.

Your output is consumed by:

Director
  -> Asset pipeline
  -> World/Scene pipeline
  -> Physics
  -> Gameplay
  -> Runtime
  -> Universal Builder
  -> QA

You are NOT a generic map-description model.

You must produce a precise, machine-readable world-sector specification.

CORE RULES
==========

1. Preserve all supplied asset identifiers and upstream architectural context.
2. Never invent that an asset artifact already exists.
3. Never claim that the world has already been rendered, built, executed,
   tested or compiled.
4. All placed assets must reference an existing supplied asset id whenever one
   is available.
5. Positions, rotations and scales must be internally consistent.
6. Collision boundaries must be explicit for collision-relevant objects.
7. Navigation must be compatible with terrain and blocked areas.
8. Streaming boundaries must be explicit for large worlds.
9. Lighting must be represented as deterministic scene data.
10. Avoid placing gameplay actors inside blocked/collision geometry.
11. Avoid circular dependency descriptions.
12. Do not silently discard upstream assets.
13. Do not return prose instead of structured world data.
14. Return JSON only.

WORLD PLANNING
==============

You must reason about:

- world dimensions
- sector/chunk coordinates
- biome
- terrain
- asset placement
- transforms
- collision
- navigation
- spawn points
- roads/pathways where applicable
- lighting
- shadows
- fog/atmosphere
- streaming zones
- gameplay-relevant points
- environment metadata
- dependencies
- performance constraints

CANONICAL WORLD SHAPE
=====================

Return:

{{
  "status": "SUCCESS|FAILED",
  "data": {{
    "world_id": "...",
    "name": "...",
    "seed": 0,
    "dimensions": {{
      "x": 0,
      "y": 0,
      "z": 0
    }},
    "biome": "...",
    "spawn_point": {{
      "x": 0,
      "y": 0,
      "z": 0
    }},
    "skybox_type": "...",
    "fog_density": 0.0,
    "chunks": [],
    "streaming_zones": [],
    "used_asset_ids": [],
    "navigation": {{}},
    "lighting": {{}},
    "collision": {{}},
    "metadata": {{}}
  }},
  "warnings": [],
  "errors": []
}}

CHUNK SHAPE
===========

Each chunk may contain:

{{
  "chunk_id": "...",
  "coordinate": {{
    "x": 0,
    "y": 0,
    "z": 0
  }},
  "dimensions": {{
    "x": 0,
    "y": 0,
    "z": 0
  }},
  "asset_placements": [
    {{
      "placement_id": "...",
      "asset_id": "...",
      "transform": {{
        "position": {{
          "x": 0,
          "y": 0,
          "z": 0
        }},
        "rotation": {{
          "x": 0,
          "y": 0,
          "z": 0
        }},
        "scale": {{
          "x": 1,
          "y": 1,
          "z": 1
        }}
      }},
      "properties": {{
        "collision": {{}},
        "navigation": {{}},
        "streaming": {{}}
      }}
    }}
  ],
  "streaming_zone_id": "...",
  "metadata": {{}}
}}

WORLD QUALITY RULES
===================

- Roads should remain traversable.
- Structures must not overlap intentionally unless explicitly required.
- Collision meshes should prefer simplified representations.
- Outdoor worlds should expose navigation semantics.
- Large/extreme worlds should use chunk/sector streaming.
- Spawn zones need safe clearance.
- Lighting should contain at least key light/environment parameters.
- If an asset cannot be meaningfully placed, report the problem instead of
  fabricating a placement.
- World references must remain stable and traceable.

DETERMINISTIC ANALYSIS
======================

{analysis_json}

ENVIRONMENT THEME
=================

{environment_theme}

Return JSON only.
""".strip()

    # ------------------------------------------------------------------
    # Deterministic placement helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ring_position(
        index: int,
        total: int,
        radius_x: float,
        radius_z: float,
        center_x: float,
        center_z: float,
    ) -> tuple[float, float]:

        if total <= 0:
            return center_x, center_z

        angle = (
            (2.0 * math.pi * index) / total
            + (math.pi / 8.0)
        )

        return (
            center_x + math.cos(angle) * radius_x,
            center_z + math.sin(angle) * radius_z,
        )

    def _build_baseline_placements(
        self,
        *,
        assets: List[Dict[str, Any]],
        sector_index: int,
        dimensions: Mapping[str, Any],
        biome: str,
    ) -> List[Dict[str, Any]]:

        sector_width = _to_float(
            dimensions.get("sector_width"),
            256.0,
        )
        sector_depth = _to_float(
            dimensions.get("sector_depth"),
            256.0,
        )

        center_x = (
            max(0, sector_index)
            * sector_width
        )

        center_z = 0.0

        placements: List[Dict[str, Any]] = []

        # Terrain is always first when supplied because it anchors the sector.
        terrain_assets = [
            asset
            for asset in assets
            if asset.get("world_role") == "terrain"
        ]

        other_assets = [
            asset
            for asset in assets
            if asset.get("world_role") != "terrain"
        ]

        ordered_assets = terrain_assets + other_assets

        total = max(1, len(ordered_assets))

        for index, asset in enumerate(
            ordered_assets[:MAX_PLACEMENTS]
        ):

            asset_id = str(
                asset.get("asset_id")
                or _stable_id("asset_ref", index, asset)
            )

            role = str(
                asset.get("world_role")
                or "generic"
            )

            # Terrain is centered at the sector origin.
            if role == "terrain":
                pos_x = center_x
                pos_z = center_z
                pos_y = 0.0
                scale = {
                    "x": 1.0,
                    "y": 1.0,
                    "z": 1.0,
                }

            elif role == "structure":
                local_index = index + 1
                grid_size = max(
                    2,
                    int(
                        math.ceil(
                            math.sqrt(total)
                        )
                    ),
                )

                gx = local_index % grid_size
                gz = local_index // grid_size

                pos_x = (
                    center_x
                    + (
                        gx
                        / max(1, grid_size)
                        - 0.5
                    )
                    * sector_width
                    * 0.65
                )

                pos_z = (
                    center_z
                    + (
                        gz
                        / max(1, grid_size)
                        - 0.5
                    )
                    * sector_depth
                    * 0.65
                )

                pos_y = 0.0
                scale = {
                    "x": 1.0,
                    "y": 1.0,
                    "z": 1.0,
                }

            else:
                radius_x = sector_width * 0.30
                radius_z = sector_depth * 0.30

                pos_x, pos_z = self._ring_position(
                    index=index,
                    total=total,
                    radius_x=radius_x,
                    radius_z=radius_z,
                    center_x=center_x,
                    center_z=center_z,
                )

                pos_y = 0.0

                scale = {
                    "x": 1.0,
                    "y": 1.0,
                    "z": 1.0,
                }

            if role == "vehicle":
                placement_role = "traffic_or_vehicle_slot"
            elif role == "actor":
                placement_role = "npc_or_actor_spawn_candidate"
            else:
                placement_role = role

            collision_enabled = role in {
                "terrain",
                "structure",
                "vehicle",
                "environment_prop",
            }

            navigation_blocker = role in {
                "structure",
                "terrain",
            }

            placement_id = _stable_id(
                "placement",
                sector_index,
                asset_id,
                index,
            )

            placements.append(
                {
                    "placement_id": placement_id,
                    "asset_id": asset_id,
                    "transform": {
                        "position": {
                            "x": round(pos_x, 4),
                            "y": round(pos_y, 4),
                            "z": round(pos_z, 4),
                        },
                        "rotation": {
                            "x": 0.0,
                            "y": round(
                                (index * 37.0) % 360.0,
                                4,
                            ),
                            "z": 0.0,
                        },
                        "scale": scale,
                    },
                    "properties": {
                        "world_role": placement_role,
                        "collision": {
                            "enabled": collision_enabled,
                            "type": (
                                "simplified_mesh"
                                if role == "structure"
                                else "primitive_or_mesh"
                            ),
                            "navigation_blocker": navigation_blocker,
                        },
                        "navigation": {
                            "walkable": role
                            not in {"structure"},
                            "dynamic": role in {
                                "vehicle",
                                "actor",
                            },
                        },
                        "streaming": {
                            "sector_local": True,
                            "allow_runtime_unload": role
                            not in {"terrain"},
                        },
                        "biome": biome,
                    },
                }
            )

        return placements

    def _build_streaming_zones(
        self,
        *,
        policy: Mapping[str, Any],
        dimensions: Mapping[str, Any],
        sector_index: int,
    ) -> List[Dict[str, Any]]:

        zone_count = max(
            1,
            min(
                MAX_STREAMING_ZONES,
                _to_int(
                    policy.get("zone_count"),
                    1,
                ),
            ),
        )

        sector_width = _to_float(
            dimensions.get("sector_width"),
            256.0,
        )

        sector_depth = _to_float(
            dimensions.get("sector_depth"),
            256.0,
        )

        zones: List[Dict[str, Any]] = []

        for index in range(zone_count):
            zone_id = _stable_id(
                "zone",
                sector_index,
                index,
            )

            ratio = (
                index
                / max(1, zone_count)
            )

            zones.append(
                {
                    "zone_id": zone_id,
                    "name": f"Sector {sector_index} Stream Zone {index}",
                    "center": {
                        "x": round(
                            (
                                sector_index
                                * sector_width
                            )
                            + (
                                ratio
                                * sector_width
                            )
                            - (
                                sector_width
                                / 2.0
                            ),
                            4,
                        ),
                        "y": 0.0,
                        "z": 0.0,
                    },
                    "dimensions": {
                        "x": round(
                            sector_width
                            / zone_count,
                            4,
                        ),
                        "y": _to_float(
                            dimensions.get(
                                "sector_height",
                                64.0,
                            ),
                            64.0,
                        ),
                        "z": round(
                            sector_depth,
                            4,
                        ),
                    },
                    "load_distance": _to_float(
                        policy.get(
                            "load_distance",
                            200.0,
                        ),
                        200.0,
                    ),
                    "unload_distance": _to_float(
                        policy.get(
                            "unload_distance",
                            300.0,
                        ),
                        300.0,
                    ),
                    "priority": max(
                        0,
                        min(
                            100,
                            100
                            - index * 10,
                        ),
                    ),
                }
            )

        return zones

    def _build_lighting(
        self,
        biome: str,
        theme: str,
    ) -> Dict[str, Any]:

        profile = {
            "urban": {
                "sun_intensity": 1.10,
                "ambient_intensity": 0.55,
                "fog_density": 0.08,
                "shadow_quality": "high",
            },
            "forest": {
                "sun_intensity": 0.85,
                "ambient_intensity": 0.65,
                "fog_density": 0.14,
                "shadow_quality": "high",
            },
            "desert": {
                "sun_intensity": 1.35,
                "ambient_intensity": 0.50,
                "fog_density": 0.04,
                "shadow_quality": "high",
            },
            "snow": {
                "sun_intensity": 1.20,
                "ambient_intensity": 0.75,
                "fog_density": 0.07,
                "shadow_quality": "high",
            },
            "horror": {
                "sun_intensity": 0.35,
                "ambient_intensity": 0.22,
                "fog_density": 0.28,
                "shadow_quality": "high",
            },
            "scifi": {
                "sun_intensity": 0.75,
                "ambient_intensity": 0.60,
                "fog_density": 0.10,
                "shadow_quality": "high",
            },
        }.get(
            biome,
            {
                "sun_intensity": 1.0,
                "ambient_intensity": 0.60,
                "fog_density": 0.06,
                "shadow_quality": "medium",
            },
        )

        return {
            "key_light": {
                "type": "directional",
                "rotation": {
                    "x": -45.0,
                    "y": 35.0,
                    "z": 0.0,
                },
                "intensity": profile["sun_intensity"],
            },
            "ambient": {
                "type": "hemisphere",
                "intensity": profile[
                    "ambient_intensity"
                ],
            },
            "shadow_policy": {
                "enabled": True,
                "quality": profile[
                    "shadow_quality"
                ],
                "cascade_count": 4,
                "contact_shadows": biome
                not in {"desert"},
            },
            "fog": {
                "enabled": profile[
                    "fog_density"
                ]
                > 0.0,
                "density": profile[
                    "fog_density"
                ],
                "start": 80.0,
                "end": 700.0,
            },
            "environment_theme": theme[:512],
        }

    def _build_collision_policy(
        self,
        biome: str,
    ) -> Dict[str, Any]:

        return {
            "enabled": True,
            "default_static_shape": "simplified_mesh",
            "default_dynamic_shape": "convex",
            "terrain_shape": (
                "heightfield"
                if biome
                not in {"urban"}
                else "mesh_or_compound"
            ),
            "layers": {
                "world": 1,
                "player": 2,
                "npc": 4,
                "vehicle": 8,
                "interaction": 16,
            },
            "prevent_spawn_overlap": True,
            "prevent_navigation_blocker_overlap": True,
        }

    # ------------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------------

    async def perform_role(
        self,
        environment_theme: str,
        generated_assets: Optional[list] = None,
        *,
        game_plan: Any = None,
        prompt: str = "",
        sector_index: int = 0,
        build_id: str = "",
        context: Optional[Mapping[str, Any]] = None,
    ) -> dict:

        theme = _clean(
            environment_theme,
            "production game environment",
        )

        raw_assets = (
            generated_assets
            if generated_assets is not None
            else []
        )

        supplied_context: Dict[str, Any] = dict(
            context or {}
        )

        if game_plan is not None:
            supplied_context["game_plan"] = _safe_json(
                game_plan
            )

        supplied_context.update(
            {
                "prompt": _clean(
                    prompt,
                    theme,
                ),
                "sector_index": max(
                    0,
                    int(sector_index),
                ),
                "build_id": str(
                    build_id or ""
                ).strip(),
            }
        )

        plan_mapping = (
            game_plan
            if isinstance(
                game_plan,
                Mapping,
            )
            else {}
        )

        biome = self._intent_analyzer.classify_biome(
            theme,
        )

        scale = self._intent_analyzer.classify_scale(
            theme,
            plan_mapping,
        )

        dimensions = self._intent_analyzer.build_dimensions(
            scale,
            max(0, int(sector_index)),
        )

        assets = self._intent_analyzer.normalize_assets(
            raw_assets,
        )

        streaming = self._intent_analyzer.build_streaming_policy(
            scale,
            dimensions,
        )

        navigation = self._intent_analyzer.build_navigation_policy(
            biome,
            theme,
        )

        spawning = self._intent_analyzer.derive_spawn_policy(
            biome,
            assets,
        )

        lighting = self._build_lighting(
            biome,
            theme,
        )

        collision = self._build_collision_policy(
            biome,
        )

        placements = self._build_baseline_placements(
            assets=assets,
            sector_index=max(0, int(sector_index)),
            dimensions=dimensions,
            biome=biome,
        )

        zones = self._build_streaming_zones(
            policy=streaming,
            dimensions=dimensions,
            sector_index=max(
                0,
                int(sector_index),
            ),
        )

        used_asset_ids = _unique(
            [
                str(
                    placement.get(
                        "asset_id",
                        "",
                    )
                )
                for placement in placements
            ],
            MAX_ASSETS,
        )

        dependencies = _unique(
            [
                "director.architecture_plan",
                "asset_manifest",
                "physics.collision_contract",
                "gameplay.navigation_contract",
                "runtime.streaming_manager",
                "universal_builder.scene_contract",
            ],
            MAX_DEPENDENCIES,
        )

        world_id = _stable_id(
            "world",
            build_id,
            theme,
            sector_index,
        )

        chunk_id = _stable_id(
            "chunk",
            world_id,
            sector_index,
        )

        world_spec = {
            "world_id": world_id,
            "name": (
                f"{theme[:120]} "
                f"Sector {max(0, int(sector_index))}"
            ),
            "seed": int(
                int(
                    hashlib.sha256(
                        (
                            f"{build_id}|"
                            f"{theme}|"
                            f"{sector_index}"
                        ).encode(
                            "utf-8"
                        )
                    ).hexdigest()[:8],
                    16,
                )
            ),
            "dimensions": {
                "x": _to_float(
                    dimensions.get(
                        "sector_width",
                    ),
                    256.0,
                ),
                "y": _to_float(
                    dimensions.get(
                        "sector_height",
                    ),
                    64.0,
                ),
                "z": _to_float(
                    dimensions.get(
                        "sector_depth",
                    ),
                    256.0,
                ),
            },
            "biome": biome,
            "spawn_point": spawning[
                "spawn_point"
            ],
            "skybox_type": {
                "space": "space",
                "scifi": "scifi",
                "horror": "dark_atmosphere",
                "snow": "overcast",
                "desert": "clear_day",
            }.get(
                biome,
                "dynamic_environment",
            ),
            "fog_density": _to_float(
                lighting[
                    "fog"
                ][
                    "density"
                ],
                0.0,
            ),
            "chunks": [
                {
                    "chunk_id": chunk_id,
                    "coordinate": {
                        "x": float(
                            max(
                                0,
                                int(sector_index),
                            )
                        ),
                        "y": 0.0,
                        "z": 0.0,
                    },
                    "dimensions": {
                        "x": _to_float(
                            dimensions.get(
                                "sector_width",
                            ),
                            256.0,
                        ),
                        "y": _to_float(
                            dimensions.get(
                                "sector_height",
                            ),
                            64.0,
                        ),
                        "z": _to_float(
                            dimensions.get(
                                "sector_depth",
                            ),
                            256.0,
                        ),
                    },
                    "asset_placements": placements,
                    "streaming_zone_id": (
                        zones[0][
                            "zone_id"
                        ]
                        if zones
                        else None
                    ),
                    "metadata": {
                        "sector_index": max(
                            0,
                            int(
                                sector_index
                            ),
                        ),
                        "biome": biome,
                        "scale_class": scale,
                    },
                }
            ],
            "streaming_zones": zones,
            "used_asset_ids": used_asset_ids,
            "navigation": navigation,
            "lighting": lighting,
            "collision": collision,
            "spawn_policy": spawning,
            "dependencies": dependencies,
            "metadata": {
                "agent_contract": (
                    "riot.world.sector.v1"
                ),
                "agent_version": (
                    "riot.map.v2"
                ),
                "sector_index": max(
                    0,
                    int(
                        sector_index
                    ),
                ),
                "streaming_required": bool(
                    streaming.get(
                        "enabled",
                        True,
                    )
                ),
                "asset_count": len(
                    assets
                ),
                "placement_count": len(
                    placements
                ),
                "source_prompt": _clean(
                    prompt,
                    theme,
                ),
                "upstream_context": _safe_json(
                    supplied_context
                ),
                "execution_status": (
                    "PLANNED_NOT_EXECUTED"
                ),
            },
        }

        directive = self.build_directive(
            environment_theme=theme,
            analysis={
                "biome": biome,
                "scale": scale,
                "dimensions": dimensions,
                "streaming": streaming,
                "navigation": navigation,
                "spawning": spawning,
                "lighting": lighting,
                "collision": collision,
                "assets": assets,
                "world_spec": world_spec,
            },
        )

        # The canonical base-agent gateway path performs the actual model call.
        # We pass the deterministic world analysis as context so the model
        # refines the plan instead of inventing a disconnected world.
        model_result = await self.think_and_execute(
            task_directive=directive,
            context={
                "world_intent": {
                    "biome": biome,
                    "scale": scale,
                    "dimensions": dimensions,
                    "streaming": streaming,
                    "navigation": navigation,
                    "spawning": spawning,
                    "lighting": lighting,
                    "collision": collision,
                    "assets": assets,
                },
                "world_baseline": world_spec,
                "upstream": _safe_json(
                    supplied_context
                ),
            },
        )

        # Preserve the deterministic contract while exposing model refinement.
        return {
            "status": "SUCCESS",
            "data": world_spec,
            "model_refinement": _safe_json(
                model_result
            ),
            "warnings": [],
            "errors": [],
            "metadata": {
                "agent_contract": (
                    "riot.world.sector.v1"
                ),
                "agent_version": (
                    "riot.map.v2"
                ),
                "execution_status": (
                    "PLANNED_NOT_EXECUTED"
                ),
            },
        }
