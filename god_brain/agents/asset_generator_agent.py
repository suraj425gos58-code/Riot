from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from god_brain.agents.base_agent import GodBaseAgent


# ============================================================================
# PRODUCTION LIMITS
# ============================================================================

MAX_DESCRIPTION_CHARS = 4096
MAX_STYLE_CHARS = 512
MAX_DEPENDENCIES = 32
MAX_TAGS = 48
MAX_LOD_LEVELS = 4
MAX_FORMATS = 16


# ============================================================================
# DETERMINISTIC HELPERS
# ============================================================================

def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return (text or default)[:MAX_DESCRIPTION_CHARS]


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _unique(values: Iterable[Any], limit: int) -> List[str]:
    output: List[str] = []
    seen: set[str] = set()

    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue

        seen.add(text)
        output.append(text)

        if len(output) >= limit:
            break

    return output


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


# ============================================================================
# ASSET INTENT ANALYSIS
# ============================================================================

class AssetIntentAnalyzer:
    """
    Deterministic pre-planner for the AssetGeneratorAgent.

    Important:
    This does not generate a real artifact.
    It creates a bounded production specification which the model can refine.
    """

    _TYPE_RULES = (
        (
            "terrain",
            (
                "terrain",
                "ground",
                "landscape",
                "mountain",
                "hill",
                "cliff",
                "island",
                "road",
                "floor",
            ),
        ),
        (
            "character",
            (
                "character",
                "human",
                "person",
                "soldier",
                "player",
                "npc",
                "enemy",
                "hero",
                "villain",
                "monster",
                "creature",
            ),
        ),
        (
            "vehicle",
            (
                "car",
                "vehicle",
                "truck",
                "bus",
                "bike",
                "motorcycle",
                "vehicle",
                "aircraft",
                "helicopter",
                "boat",
                "ship",
            ),
        ),
        (
            "building",
            (
                "building",
                "house",
                "apartment",
                "tower",
                "castle",
                "shop",
                "store",
                "warehouse",
                "office",
            ),
        ),
        (
            "weapon",
            (
                "weapon",
                "gun",
                "rifle",
                "pistol",
                "sword",
                "knife",
                "bow",
                "launcher",
            ),
        ),
        (
            "prop",
            (
                "chair",
                "table",
                "lamp",
                "tree",
                "rock",
                "barrel",
                "crate",
                "door",
                "sign",
                "prop",
            ),
        ),
        (
            "vfx",
            (
                "particle",
                "explosion",
                "smoke",
                "fire",
                "spark",
                "vfx",
                "effect",
                "flame",
            ),
        ),
        (
            "ui",
            (
                "button",
                "hud",
                "icon",
                "menu",
                "interface",
                "ui",
                "widget",
            ),
        ),
        (
            "animation",
            (
                "animation",
                "walk cycle",
                "run cycle",
                "idle animation",
                "attack animation",
                "movement animation",
            ),
        ),
    )

    def classify_type(self, description: str) -> str:
        text = description.lower()

        for asset_type, keywords in self._TYPE_RULES:
            if any(keyword in text for keyword in keywords):
                return asset_type

        return "model_3d"

    def classify_complexity(self, description: str) -> str:
        text = description.lower()

        extreme_terms = (
            "hero asset",
            "cinematic",
            "photorealistic",
            "ultra realistic",
            "high fidelity",
            "film quality",
        )

        heavy_terms = (
            "detailed",
            "realistic",
            "high detail",
            "game ready",
            "production quality",
        )

        lightweight_terms = (
            "low poly",
            "mobile",
            "stylized",
            "simple",
            "optimized",
        )

        if any(term in text for term in extreme_terms):
            return "extreme"

        if any(term in text for term in heavy_terms):
            return "heavy"

        if any(term in text for term in lightweight_terms):
            return "optimized"

        return "standard"

    def infer_engine_target(
        self,
        description: str,
        context: Mapping[str, Any],
    ) -> Dict[str, str]:
        text = (
            description
            + " "
            + str(context.get("engine", ""))
            + " "
            + str(context.get("target_platform", ""))
        ).lower()

        engine = str(context.get("engine") or "").strip()

        if not engine:
            if "unreal" in text:
                engine = "unreal-compatible"
            elif "three.js" in text or "threejs" in text:
                engine = "threejs-compatible"
            else:
                engine = "runtime-agnostic"

        platform = str(context.get("target_platform") or "").strip().lower()

        if not platform:
            if "mobile" in text or "android" in text:
                platform = "mobile_apk"
            elif "web" in text or "html5" in text or "browser" in text:
                platform = "web_html5"
            elif "pc" in text or "desktop" in text or "windows" in text:
                platform = "pc_exe"
            else:
                platform = "web_html5"

        return {
            "engine": engine,
            "target_platform": platform,
        }

    def build_budget(
        self,
        asset_type: str,
        complexity: str,
        target_platform: str,
    ) -> Dict[str, Any]:

        platform = target_platform.lower()

        if asset_type in {"ui", "animation", "vfx"}:
            base_polygons = 0
        elif platform == "mobile_apk":
            base_polygons = 8000
        elif platform == "web_html5":
            base_polygons = 12000
        elif platform == "pc_exe":
            base_polygons = 30000
        else:
            base_polygons = 16000

        multiplier = {
            "optimized": 0.35,
            "standard": 1.0,
            "heavy": 1.8,
            "extreme": 3.0,
        }.get(complexity, 1.0)

        polygon_budget = int(base_polygons * multiplier)

        if asset_type == "character":
            polygon_budget = max(polygon_budget, 12000)

        if asset_type == "hero asset":
            polygon_budget = max(polygon_budget, 30000)

        texture_size = 1024

        if platform == "mobile_apk":
            texture_size = 1024
        elif platform == "web_html5":
            texture_size = 1024
        elif platform == "pc_exe":
            texture_size = 2048

        if complexity == "optimized":
            texture_size = min(texture_size, 1024)
        elif complexity == "extreme":
            texture_size = max(texture_size, 2048)

        return {
            "max_polygon_count": polygon_budget,
            "target_texture_resolution": texture_size,
            "max_material_slots": (
                2 if complexity == "optimized"
                else 4 if complexity == "standard"
                else 8
            ),
            "draw_call_budget": (
                1 if complexity == "optimized"
                else 2 if complexity == "standard"
                else 4
            ),
            "streaming_priority": (
                "high"
                if asset_type in {"character", "vehicle", "terrain"}
                else "normal"
            ),
        }

    def build_lods(
        self,
        polygon_budget: int,
        complexity: str,
    ) -> List[Dict[str, Any]]:

        if polygon_budget <= 0:
            return []

        if complexity == "optimized":
            ratios = [1.0, 0.50, 0.25]
        elif complexity == "extreme":
            ratios = [1.0, 0.60, 0.35, 0.18]
        else:
            ratios = [1.0, 0.50, 0.25, 0.12]

        lods: List[Dict[str, Any]] = []

        for level, ratio in enumerate(ratios[:MAX_LOD_LEVELS]):
            lods.append(
                {
                    "level": level,
                    "polygon_ratio": ratio,
                    "max_distance": (
                        0.0
                        if level == 0
                        else float(35 * level * level)
                    ),
                    "generated_from": "same_source_asset",
                }
            )

        return lods

    def build_material_profile(
        self,
        asset_type: str,
        complexity: str,
    ) -> Dict[str, Any]:

        maps = [
            "base_color",
            "normal",
            "roughness",
        ]

        if complexity in {"heavy", "extreme"}:
            maps.extend(
                [
                    "metallic",
                    "ao",
                ]
            )

        if asset_type == "vfx":
            shader = "vfx_particle_shader"
        elif asset_type == "ui":
            shader = "ui_material"
        else:
            shader = "pbr"

        return {
            "shader": shader,
            "texture_slots": {
                item: f"{item}_map"
                for item in maps
            },
            "properties": {
                "pbr": shader == "pbr",
                "two_sided": asset_type in {"ui", "vfx"},
                "emissive": asset_type in {"vfx"},
            },
            "texture_profile": {
                "color_space": "sRGB",
                "mipmaps": True,
                "maps_required": maps,
            },
        }

    def build_collision_profile(
        self,
        asset_type: str,
    ) -> Dict[str, Any]:

        collision_required = asset_type in {
            "terrain",
            "character",
            "vehicle",
            "building",
            "weapon",
            "prop",
        }

        if not collision_required:
            return {
                "enabled": False,
                "collision_type": None,
                "layers": [],
            }

        collision_type = {
            "terrain": "heightfield_or_mesh",
            "character": "capsule",
            "vehicle": "compound",
            "building": "simplified_mesh",
            "weapon": "primitive_or_mesh",
            "prop": "box_or_convex",
        }.get(asset_type, "convex")

        return {
            "enabled": True,
            "collision_type": collision_type,
            "layers": [
                "world",
                "gameplay",
            ],
        }

    def build_formats(
        self,
        asset_type: str,
        target_platform: str,
    ) -> List[str]:

        if asset_type == "ui":
            return ["png", "svg"]

        if asset_type == "animation":
            return ["gltf", "fbx"]

        if asset_type == "vfx":
            return ["json", "shader"]

        if asset_type in {"sound", "music", "voice"}:
            return ["wav", "ogg"]

        formats = [
            "glb",
            "gltf",
        ]

        if target_platform == "pc_exe":
            formats.append("fbx")

        return formats[:MAX_FORMATS]

    def build_dependencies(
        self,
        asset_type: str,
    ) -> List[str]:

        dependency_map = {
            "character": [
                "character_material",
                "character_collision",
                "character_animation_binding",
            ],
            "vehicle": [
                "vehicle_material",
                "vehicle_collision",
                "vehicle_wheel_binding",
            ],
            "terrain": [
                "terrain_material",
                "terrain_collision",
                "terrain_streaming",
            ],
            "building": [
                "building_material",
                "building_collision",
            ],
            "weapon": [
                "weapon_material",
                "weapon_collision",
            ],
            "vfx": [
                "particle_material",
                "particle_runtime",
            ],
            "ui": [
                "ui_material",
                "ui_runtime",
            ],
        }

        return dependency_map.get(asset_type, ["asset_material"])

    def analyze(
        self,
        description: str,
        style: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:

        context = dict(context or {})

        description = _clean(description, "production game asset")
        style = str(style or "realistic").strip()[:MAX_STYLE_CHARS]

        asset_type = self.classify_type(description)
        complexity = self.classify_complexity(
            description + " " + style
        )

        target = self.infer_engine_target(
            description,
            context,
        )

        budget = self.build_budget(
            asset_type,
            complexity,
            target["target_platform"],
        )

        lods = self.build_lods(
            budget["max_polygon_count"],
            complexity,
        )

        material = self.build_material_profile(
            asset_type,
            complexity,
        )

        collision = self.build_collision_profile(
            asset_type,
        )

        formats = self.build_formats(
            asset_type,
            target["target_platform"],
        )

        dependencies = self.build_dependencies(
            asset_type,
        )

        semantic_tags = _unique(
            [
                asset_type,
                complexity,
                style,
                target["engine"],
                target["target_platform"],
                "game-ready",
                "streaming-aware",
                "production-contract",
            ],
            MAX_TAGS,
        )

        asset_id = _stable_id(
            "assetreq",
            description,
            style,
            target["engine"],
            target["target_platform"],
        )

        return {
            "request_id": asset_id,
            "asset_type": asset_type,
            "name": description[:160],
            "style": style,
            "complexity_class": complexity,
            "generation_strategy": (
                "procedural_or_ai_assisted"
                if asset_type in {"terrain", "prop", "building", "vfx"}
                else "hybrid_ai_asset_pipeline"
            ),
            "engine_target": target["engine"],
            "target_platform": target["target_platform"],
            "performance_budget": budget,
            "lod_profiles": lods,
            "material_profile": material,
            "collision_profile": collision,
            "expected_formats": formats,
            "dependencies": dependencies[:MAX_DEPENDENCIES],
            "semantic_tags": semantic_tags,
            "streaming": {
                "enabled": True,
                "priority": budget["streaming_priority"],
                "allow_runtime_unload": asset_type not in {"character"},
            },
            "animation_requirements": (
                {
                    "required": True,
                    "locomotion": asset_type == "character",
                    "physics_binding": asset_type in {"character", "vehicle"},
                }
                if asset_type in {"character", "vehicle"}
                else {
                    "required": False,
                }
            ),
            "validation_requirements": {
                "stable_identity": True,
                "format_compatibility": True,
                "material_integrity": True,
                "collision_integrity": collision["enabled"],
                "lod_integrity": bool(lods),
                "no_execution_claims": True,
            },
            "upstream_context": _safe_json(context),
        }


# ============================================================================
# SMART ASSET GENERATOR
# ============================================================================

class AssetGeneratorAgent(GodBaseAgent):
    """
    Production-grade asset planning/generation specialist.

    Backward compatibility:
        await agent.perform_role(asset_description, style)

    The method still delegates actual model execution to GodBaseAgent.
    """

    role_name = "3D Asset Generation & Technical Art Architect"
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
                "contract": "riot.asset.blueprint.v1",
                "stage": "asset_planning",
                "agent_version": "riot.asset.v2",
            },
        )

        self._intent_analyzer = AssetIntentAnalyzer()

    def build_directive(
        self,
        description: str,
        style: str,
        analysis: Mapping[str, Any],
    ) -> str:

        analysis_json = json.dumps(
            _safe_json(analysis),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return f"""
You are the production Asset Generation & Technical Art Architect inside
the Riot / God Node game-generation engine.

Your job is NOT to merely describe a 3D object.

Your job is to convert the asset request into a precise,
downstream-consumable technical asset blueprint that can safely feed:

Asset Manifest
→ Map Builder
→ Physics
→ Gameplay
→ Universal Builder
→ QA

ORCHESTRATION RULES
===================

1. Preserve the supplied request information.
2. Use the deterministic pre-analysis as a constraint baseline.
3. Do not invent execution evidence.
4. Do not claim that a model was actually generated unless the surrounding
   runtime provides real evidence.
5. Do not fabricate source files, artifact URLs, provider names, hashes,
   compilation results or QA results.
6. Keep all identifiers stable and deterministic where possible.
7. Any dependency must be explicit.
8. Any runtime-sensitive requirement must be represented as data.
9. Never silently exceed the supplied performance budget.
10. The output must be useful to downstream software, not just readable prose.

TECHNICAL ART REQUIREMENTS
==========================

The blueprint must reason about:

- asset type
- visual style
- geometry complexity
- polygon budget
- topology expectations
- LOD strategy
- material/shader strategy
- texture maps
- texture resolution
- color-space requirements
- collision strategy
- animation requirements
- engine/runtime compatibility
- expected formats
- streaming behavior
- memory/performance constraints
- dependencies
- validation requirements

DOWNSTREAM CONTRACT
===================

Return a single JSON object in this shape:

{{
  "status": "SUCCESS|FAILED",
  "data": {{
    "request_id": "...",
    "asset_type": "...",
    "name": "...",
    "generation_strategy": "...",
    "specification": {{
      "description": "...",
      "visual_style": "...",
      "geometry": {{
        "topology": "...",
        "max_polygon_count": 0,
        "optimization": []
      }},
      "technical_requirements": [],
      "semantic_tags": []
    }},
    "material_profile": {{
      "shader": "...",
      "texture_slots": {{}},
      "properties": {{}},
      "texture_profile": {{
        "color_space": "...",
        "compression": "...",
        "mipmaps": true,
        "maps_required": []
      }}
    }},
    "collision_profile": {{
      "enabled": false,
      "collision_type": null,
      "layers": []
    }},
    "lod_profiles": [],
    "expected_formats": [],
    "dependencies": [],
    "metadata": {{
      "engine_target": "...",
      "target_platform": "...",
      "streaming": {{}},
      "validation_requirements": {{}}
    }}
  }},
  "warnings": [],
  "errors": []
}}

IMPORTANT SEMANTIC RULE
=======================

Do not put actual artifact claims into the blueprint.

This stage creates a technical specification.
Actual asset generation and artifact verification belong to downstream stages.

ASSET REQUEST
=============

Description:
{description}

Visual Style:
{style}

DETERMINISTIC PRE-ANALYSIS
==========================

{analysis_json}

Now refine this into the production asset blueprint.

Return JSON only.
""".strip()

    async def perform_role(
        self,
        asset_description: str,
        style: str = "realistic",
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """
        Build a production-grade asset blueprint and delegate execution
        through the canonical GodBaseAgent/Gateway path.

        Existing callers using:
            perform_role(description, style)

        remain valid.
        """

        description = _clean(
            asset_description,
            "production game asset",
        )

        style = str(style or "realistic").strip()[:MAX_STYLE_CHARS]

        runtime_context: Dict[str, Any] = dict(context or {})

        analysis = self._intent_analyzer.analyze(
            description=description,
            style=style,
            context=runtime_context,
        )

        directive = self.build_directive(
            description=description,
            style=style,
            analysis=analysis,
        )

        return await self.think_and_execute(
            task_directive=directive,
            context={
                "asset_intent": analysis,
                "runtime_context": _safe_json(runtime_context),
            },
        )
